#!/usr/bin/env python3
"""Real backlight control over DDC/CI, for displays that actually answer.

On Apple Silicon there is no /dev/i2c and no IOFramebuffer. The DisplayPort
link is owned by the DCP -- a coprocessor running its own firmware -- and every
I2C transaction is an RPC to it through three private IOKit symbols:
IOAVServiceCreateWithService / IOAVServiceReadI2C / IOAVServiceWriteI2C.
The Intel-era IOI2CSendRequest path does not exist here; AppleCLCD2 and
IOFramebuffer are absent from the registry entirely.

Where this works it is strictly better than gamma dimming: it drives the real
backlight, the monitor stores the level in its own NVRAM, and NO resident
process is needed. Where it does not, dimmer.py falls back to gamma.

The failure mode that matters, verified on real hardware: a display can ACK at
0x37 and return a checksum-correct DDC/CI Null Message (6e 80 be) to every
request -- capabilities, identification, every VCP code. That is a stub
responder, not a control channel, and it is indistinguishable from success if
you only check the IOKit return code, which is 0 throughout. So capability is
decided by parsed, checksum-valid, non-empty DATA. Never by rc == 0.
"""
import ctypes, ctypes.util, json, os, time

CFG   = os.path.expanduser("~/.config/dimmer")
CACHE = os.path.join(CFG, "ddc.json")
CACHE_VERSION = 1

DDC_ADDR   = 0x37      # DDC/CI command address
DDC_OFFSET = 0x51      # host source address, doubles as the I2C data offset
EDID_ADDR  = 0x50
VCP_LUMINANCE = 0x10

# Checksum seeds. Host->display XORs in the display's write address (0x6E) and
# the host's source address (0x51); the reply XORs in the host's read address
# (0x50) and the display's source (0x6E). Both confirmed against real traffic.
_SEED_OUT = 0x6E ^ 0x51
_SEED_IN  = 0x50

_iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_cf    = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cg    = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))

_VP, _U32, _I32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
_UTF8 = 0x08000100

_cf.CFStringCreateWithCString.argtypes = [_VP, ctypes.c_char_p, _U32]
_cf.CFStringCreateWithCString.restype  = _VP
_cf.CFStringGetCString.argtypes = [_VP, ctypes.c_char_p, ctypes.c_long, _U32]
_cf.CFStringGetCString.restype  = ctypes.c_bool
_cf.CFRelease.argtypes = [_VP]

_iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
_iokit.IOServiceMatching.restype  = _VP
_iokit.IOServiceGetMatchingServices.argtypes = [_U32, _VP, ctypes.POINTER(_U32)]
_iokit.IOServiceGetMatchingServices.restype  = _I32
_iokit.IOIteratorNext.argtypes = [_U32]; _iokit.IOIteratorNext.restype = _U32
_iokit.IOObjectRelease.argtypes = [_U32]
_iokit.IORegistryEntryCreateCFProperty.argtypes = [_U32, _VP, _VP, _U32]
_iokit.IORegistryEntryCreateCFProperty.restype  = _VP

for _n in ("CGDisplayVendorNumber", "CGDisplayModelNumber", "CGDisplaySerialNumber"):
    getattr(_cg, _n).argtypes = [_U32]
    getattr(_cg, _n).restype  = _U32

# The private trio. Absent => this macOS build cannot do DDC/CI from userland
# at all, and the whole module degrades to "no capable displays".
AVAILABLE = all(hasattr(_iokit, n) for n in
                ("IOAVServiceCreateWithService", "IOAVServiceReadI2C", "IOAVServiceWriteI2C"))
if AVAILABLE:
    _av_create = _iokit.IOAVServiceCreateWithService
    _av_create.argtypes = [_VP, _U32]; _av_create.restype = _VP
    _av_read = _iokit.IOAVServiceReadI2C
    _av_read.argtypes = [_VP, _U32, _U32, _VP, _U32]; _av_read.restype = _I32
    _av_write = _iokit.IOAVServiceWriteI2C
    _av_write.argtypes = [_VP, _U32, _U32, _VP, _U32]; _av_write.restype = _I32


def _cfstr(s):
    return _cf.CFStringCreateWithCString(None, s.encode(), _UTF8)


def _pystr(ref):
    if not ref:
        return None
    buf = ctypes.create_string_buffer(256)
    ok = _cf.CFStringGetCString(ref, buf, 256, _UTF8)
    _cf.CFRelease(ref)
    return buf.value.decode() if ok else None


# --- EDID --------------------------------------------------------------------
def parse_edid(b):
    """Manufacturer / product / serial / model name from a 128-byte EDID block.

    The header is a fixed 00 FF FF FF FF FF FF 00 -- if that is missing we did
    not read an EDID, we read whatever the bus happened to be holding."""
    if not b or len(b) < 128 or bytes(b[:8]) != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    mfg_raw = (b[8] << 8) | b[9]                      # 3 packed 5-bit letters
    mfg = "".join(chr(64 + ((mfg_raw >> s) & 0x1F)) for s in (10, 5, 0))
    product = b[11] << 8 | b[10]                      # little-endian
    serial  = int.from_bytes(bytes(b[12:16]), "little")
    name = None
    for off in (54, 72, 90, 108):                     # the four 18-byte descriptors
        d = b[off:off + 18]
        if len(d) == 18 and d[0] == 0 and d[1] == 0 and d[3] == 0xFC:
            # Descriptor text ends at 0x0A and is space-padded to 13 bytes;
            # some panels pad with NULs instead, so strip both.
            name = (bytes(d[5:18]).split(b"\x0a")[0]
                    .decode("ascii", "replace").strip("\x00 \t").strip())
            break
    return {"mfg": mfg, "mfg_raw": mfg_raw, "product": product,
            "serial": serial, "name": name}


def fingerprint(info):
    """Stable per-monitor key for the capability cache."""
    return f"{info['mfg']}:{info['product']:04x}:{info['serial']:08x}"


# --- DDC/CI framing (pure; unit-tested without hardware) ---------------------
def frame_request(payload):
    """[len|0x80][payload...][checksum] -- what gets written to 0x37 @ 0x51."""
    pkt = bytearray([0x80 | len(payload)]) + bytearray(payload) + bytearray([0])
    ck = _SEED_OUT
    for x in pkt[:-1]:
        ck ^= x
    pkt[-1] = ck
    return bytes(pkt)


def parse_reply(buf):
    """Payload bytes from a display reply, or None if it carries no data.

    Rejects, in order: a short read, a wrong source address, a malformed length
    byte, a ZERO-LENGTH payload, and a bad checksum.

    The zero-length check is the one that matters. `6e 80 be` is a Null Message:
    correctly framed, correct checksum, completely empty. Real hardware answers
    every single request with it -- capabilities, identification, every VCP
    code -- and treating a well-formed empty reply as success is exactly how a
    tool ends up confidently reporting brightness control over a link that has
    none."""
    if not buf or len(buf) < 3:
        return None
    if buf[0] != 0x6E or not (buf[1] & 0x80):
        return None
    length = buf[1] & 0x7F
    if length == 0 or 2 + length >= len(buf):
        return None                           # Null Message, or truncated read
    ck = _SEED_IN
    for x in buf[:2 + length]:
        ck ^= x
    if ck != buf[2 + length]:
        return None                           # noise that happened to look framed
    return bytes(buf[2:2 + length])


# --- the display handle ------------------------------------------------------
class AVDisplay:
    """One external display reachable over the DCP's I2C bus."""

    def __init__(self, svc, io_object):
        self._svc = svc
        self._io  = io_object
        self.edid = None
        self.info = None

    # -- raw transport --
    def read_i2c(self, chip, offset, n):
        buf = (ctypes.c_ubyte * n)()
        rc = _av_read(self._svc, chip, offset, buf, n)
        return rc, bytes(buf)

    def write_i2c(self, chip, offset, payload):
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(bytes(payload))
        return _av_write(self._svc, chip, offset, buf, len(payload))

    def load_edid(self):
        rc, b = self.read_i2c(EDID_ADDR, 0x00, 128)
        if rc == 0:
            self.edid = b
            self.info = parse_edid(b)
        return self.info

    # -- DDC/CI framing: see frame_request / parse_reply above --
    def _request(self, payload):
        return self.write_i2c(DDC_ADDR, DDC_OFFSET, frame_request(payload)) == 0

    def _reply(self, n=16):
        rc, buf = self.read_i2c(DDC_ADDR, DDC_OFFSET, n)
        return None if rc != 0 else parse_reply(buf)

    # -- MCCS operations --
    def get_vcp(self, code, tries=3, settle=0.08):
        """(current, maximum) or None. DDC/CI is genuinely flaky; retry."""
        for attempt in range(tries):
            if self._request([0x01, code]):
                time.sleep(settle * (attempt + 1))
                p = self._reply(16)
                if p and len(p) >= 8 and p[0] == 0x02 and p[1] == 0x00 and p[2] == code:
                    return (p[6] << 8 | p[7], p[4] << 8 | p[5])
            time.sleep(0.05)
        return None

    def set_vcp(self, code, value):
        ok = self._request([0x03, code, (value >> 8) & 0xFF, value & 0xFF])
        time.sleep(0.05)                      # MCCS: >=50ms before the next command
        return ok

    # -- brightness in percent --
    def get_brightness(self):
        r = self.get_vcp(VCP_LUMINANCE)
        if not r:
            return None
        cur, mx = r
        return round(cur * 100.0 / mx, 1) if mx else None

    def set_brightness(self, pct, maximum=None):
        mx = maximum or (self.get_vcp(VCP_LUMINANCE) or (0, 0))[1]
        if not mx:
            return False
        return self.set_vcp(VCP_LUMINANCE, max(0, min(mx, round(pct * mx / 100.0))))

    def close(self):
        if self._svc:
            _cf.CFRelease(self._svc); self._svc = None
        if self._io:
            _iokit.IOObjectRelease(self._io); self._io = None


# --- enumeration -------------------------------------------------------------
def av_displays():
    """Every DCPAVServiceProxy with Location == 'External', EDID loaded.

    'Embedded' is the built-in panel; macOS drives its backlight properly and
    we must never touch it."""
    if not AVAILABLE:
        return []
    it = _U32()
    if _iokit.IOServiceGetMatchingServices(
            0, _iokit.IOServiceMatching(b"DCPAVServiceProxy"), ctypes.byref(it)) != 0:
        return []
    out, key = [], _cfstr("Location")
    try:
        while True:
            e = _iokit.IOIteratorNext(it)
            if not e:
                break
            loc = _pystr(_iokit.IORegistryEntryCreateCFProperty(e, key, None, 0))
            if loc != "External":
                _iokit.IOObjectRelease(e)
                continue
            svc = _av_create(None, e)
            if not svc:
                _iokit.IOObjectRelease(e)
                continue
            d = AVDisplay(svc, e)
            d.load_edid()
            out.append(d)
    finally:
        _cf.CFRelease(key)
        _iokit.IOObjectRelease(it)
    return out


def match_cg_display(display_id, displays):
    """Pair a CGDirectDisplayID with its AV service via EDID identity.

    m1ddc pairs by enumeration order, which is why it mixes up displays on
    multi-monitor setups. CoreGraphics exposes the same vendor/product/serial
    triple that lives in EDID bytes 8-15, so an exact identity match is
    available and is what we use. Falls back to the unambiguous single-display
    case, and otherwise refuses to guess."""
    cands = [d for d in displays if d.info]
    if not cands:
        return None
    vendor = _cg.CGDisplayVendorNumber(display_id)
    model  = _cg.CGDisplayModelNumber(display_id)
    serial = _cg.CGDisplaySerialNumber(display_id)
    exact = [d for d in cands
             if d.info["mfg_raw"] == vendor and d.info["product"] == model
             and (serial == 0 or d.info["serial"] == serial)]
    if len(exact) == 1:
        return exact[0]
    loose = [d for d in cands if d.info["product"] == model]
    if len(loose) == 1:
        return loose[0]
    return cands[0] if len(cands) == 1 else None


# --- capability probing, with a cache ----------------------------------------
def _load_cache():
    try:
        c = json.load(open(CACHE))
        return c if c.get("version") == CACHE_VERSION else {"version": CACHE_VERSION}
    except Exception:
        return {"version": CACHE_VERSION}


def _save_cache(c):
    try:
        os.makedirs(CFG, exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(c, fh, indent=1)
        os.replace(tmp, CACHE)
    except OSError:
        pass


def probe(display, refresh=False):
    """Can this display's backlight actually be driven? Cached per monitor.

    Probing costs a few hundred ms of I2C round trips, so the answer is cached
    against the EDID fingerprint: a different monitor is a different key and
    re-probes automatically."""
    if not display.info:
        return {"capable": False, "reason": "no EDID; I2C transport not reaching the display"}
    key, cache = fingerprint(display.info), _load_cache()
    if not refresh and key in cache:
        return cache[key]

    r = display.get_vcp(VCP_LUMINANCE, tries=4, settle=0.10)
    if r:
        res = {"capable": True, "max": r[1], "current": r[0],
               "reason": f"luminance reports {r[0]}/{r[1]}"}
    else:
        # Separate "answered with nothing" from "did not answer at all" -- the
        # first is a stub responder, the second a broken bus. Both are fatal,
        # but they point at different hardware.
        display._request([0x01, VCP_LUMINANCE])
        time.sleep(0.1)
        rc, raw = display.read_i2c(DDC_ADDR, DDC_OFFSET, 12)
        if rc == 0 and len(raw) >= 3 and raw[0] == 0x6E and raw[1] == 0x80:
            reason = "stub responder: valid DDC/CI Null Message to every request"
        elif rc != 0:
            reason = f"no response at 0x37 (rc={rc})"
        else:
            reason = f"unparseable reply at 0x37: {' '.join(f'{x:02x}' for x in raw[:6])}"
        res = {"capable": False, "reason": reason}
    cache[key] = res
    _save_cache(cache)
    return res


def capable():
    """[(AVDisplay, info-dict)] for displays whose backlight we can really drive."""
    out = []
    for d in av_displays():
        p = probe(d)
        if p.get("capable"):
            out.append((d, p))
        else:
            d.close()
    return out


# --- macOS's own verdict, as a cross-check -----------------------------------
# DisplayServicesCanChangeBrightness is what the Displays pane consults before
# it draws a brightness slider. It is an independent second opinion on our I2C
# probe: if it says True while probe() says no, probe() is wrong.
try:
    _ds = ctypes.CDLL("/System/Library/PrivateFrameworks/"
                      "DisplayServices.framework/DisplayServices")
    _ds.DisplayServicesCanChangeBrightness.argtypes = [_U32]
    _ds.DisplayServicesCanChangeBrightness.restype  = ctypes.c_bool
except (OSError, AttributeError):
    _ds = None


def os_can_change_brightness(display_id):
    """True / False / None when the private framework is unavailable."""
    if _ds is None:
        return None
    try:
        return bool(_ds.DisplayServicesCanChangeBrightness(display_id))
    except Exception:
        return None
