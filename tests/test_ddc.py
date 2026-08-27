#!/usr/bin/env python3
"""Unit tests for the DDC/CI protocol layer. No hardware required.

The golden packets and the EDID block below are real captures from an
HKC E2711F behind a USB-C -> HDMI converter, so these tests pin the wire format
against hardware rather than against the implementation that produced it.

The test that earns its keep is the Null Message one. `6e 80 be` is a correctly
framed, correctly checksummed, entirely empty reply, and IOAVServiceReadI2C
returns 0 for it. Accept it and the tool reports working brightness control
over a link that has none.

Run:  python3 tests/test_ddc.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ddc

FAILED = []

def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(name)

def reply(payload, src=0x6E, seed=0x50, pad=b"", bad_ck=False):
    """Build a display->host frame independently of ddc.parse_reply."""
    buf = bytearray([src, 0x80 | len(payload)]) + bytearray(payload)
    ck = seed
    for x in buf:
        ck ^= x
    buf.append((ck + 1) & 0xFF if bad_ck else ck)
    return bytes(buf) + pad


print("\n=== DDC/CI protocol tests ===\n")

# ------------------------------------------------------------ request framing
print("-- request framing (golden packets from a real capture) --")
check("getvcp 0x10 == 82 01 10 ac",
      ddc.frame_request([0x01, 0x10]) == bytes.fromhex("820110ac"),
      ddc.frame_request([0x01, 0x10]).hex(" "))
check("setvcp 0x10=30 == 84 03 10 00 1e b6",
      ddc.frame_request([0x03, 0x10, 0x00, 0x1E]) == bytes.fromhex("84031000 1eb6".replace(" ", "")),
      ddc.frame_request([0x03, 0x10, 0x00, 0x1E]).hex(" "))
check("length byte carries the 0x80 flag",
      all(ddc.frame_request([0x01] * n)[0] == (0x80 | n) for n in (1, 2, 4, 8)))

# -------------------------------------------------------------- reply parsing
print("\n-- reply parsing --")
NULL = bytes.fromhex("6e80be" * 4)
check("REAL Null Message is rejected", ddc.parse_reply(NULL) is None, NULL[:3].hex(" "))
check("Null Message checksum really is valid (0x50^0x6e^0x80)",
      (0x50 ^ 0x6E ^ 0x80) == 0xBE, "so it cannot be rejected on checksum alone")

good = reply([0x02, 0x00, 0x10, 0x01, 0x00, 0x64, 0x00, 0x32], pad=b"\x00\x00\x00")
p = ddc.parse_reply(good)
check("valid VCP feature reply parses", p is not None and len(p) == 8, good.hex(" "))
if p:
    check("decodes current=50 max=100",
          (p[6] << 8 | p[7], p[4] << 8 | p[5]) == (50, 100),
          f"cur={p[6] << 8 | p[7]} max={p[4] << 8 | p[5]}")

check("bad checksum rejected",
      ddc.parse_reply(reply([0x02, 0x00, 0x10, 0x01, 0x00, 0x64, 0x00, 0x32],
                            pad=b"\x00\x00", bad_ck=True)) is None)
check("wrong source address rejected",
      ddc.parse_reply(reply([0x02, 0x00, 0x10, 0x01, 0x00, 0x64, 0x00, 0x32],
                            src=0x6F, pad=b"\x00\x00")) is None)
check("length byte without 0x80 rejected",
      ddc.parse_reply(bytes([0x6E, 0x08, 0x02, 0x00])) is None)
check("truncated read rejected", ddc.parse_reply(bytes([0x6E, 0x88, 0x02])) is None)
check("empty / short buffers rejected",
      all(ddc.parse_reply(b) is None for b in (b"", b"\x6e", b"\x6e\x88", None)))
check("all-0xFF bus idle rejected", ddc.parse_reply(b"\xff" * 12) is None)
check("all-zero bus rejected", ddc.parse_reply(b"\x00" * 12) is None)

# ----------------------------------------------------------------- EDID parse
print("\n-- EDID parsing (real 128-byte block) --")
EDID = bytes.fromhex(
    "00ffffffffffff00216392270100000024220103803c21782e8cb5af4f43ab26"
    "0e5054a54b0081809500b300d1c00101010101010101023a801871382d40582c"
    "450055502100001e000000fc004532373131460a202020202020000000fd0030"
    "641e713c000a202020202020000000ff003030303030303030303030303101da")
check("EDID block is 128 bytes", len(EDID) == 128, f"{len(EDID)}")
i = ddc.parse_edid(EDID)
check("parses", i is not None)
if i:
    check("manufacturer HKC", i["mfg"] == "HKC", i["mfg"])
    check("product 0x2792", i["product"] == 0x2792, hex(i["product"]))
    check("serial 1", i["serial"] == 1, str(i["serial"]))
    check("model name E2711F", i["name"] == "E2711F", repr(i["name"]))
    check("fingerprint stable", ddc.fingerprint(i) == "HKC:2792:00000001", ddc.fingerprint(i))

check("bad EDID header rejected", ddc.parse_edid(b"\x01" * 128) is None)
check("short EDID rejected", ddc.parse_edid(EDID[:64]) is None)
check("empty EDID rejected", ddc.parse_edid(b"") is None)

# ------------------------------------------------------- module-level sanity
print("\n-- module sanity --")
check("AVAILABLE is a bool", isinstance(ddc.AVAILABLE, bool), str(ddc.AVAILABLE))
check("av_displays() returns a list", isinstance(ddc.av_displays(), list))

print(f"\n=== {'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED: ' + ', '.join(FAILED)} ===\n")
sys.exit(1 if FAILED else 0)
