# Palworld Host Save Fixer - created by vanloads
# Licensed under GPL-3.0 (see LICENSE)
"""Reading and writing Palworld's compressed ``.sav`` container.

Layout: ``u32 uncompressed_len, u32 compressed_len, 3-byte magic, 1-byte type``
followed by the payload.

* ``PlZ`` - zlib.  Type ``0x31`` = one zlib layer, ``0x32`` = two layers
  (the header's compressed length then refers to the inner layer).
* ``PlM`` - Oodle (Mermaid/Kraken), used by Palworld since mid-2025 / v1.0.
  This is what breaks most older community tools.

Oodle is only *decompressed* here (via ``pyooz``).  Output is written as
``PlZ``/``0x31`` zlib, which the game and dedicated server still load and
re-save in their own format on the next autosave.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


class SaveFormatError(Exception):
    pass


@dataclass
class SavInfo:
    magic: bytes
    save_type: int
    header_len: int


def _describe_bad(data: bytes) -> str:
    if not data:
        return "the file is EMPTY (0 bytes)"
    if data.count(0) == len(data):
        return f"the file is {len(data):,} bytes of nothing but zeros"
    if data.count(0) > len(data) * 0.9:
        return f"the file is {len(data):,} bytes and almost entirely zeros"
    return f"the file is {len(data):,} bytes and starts with {data[:16].hex(' ')}"


def decompress_sav(data: bytes) -> tuple[bytes, SavInfo]:
    if len(data) < 12 or data[:12].count(0) == 12:
        raise SaveFormatError(f"not a valid save: {_describe_bad(data)}.")
    hdr = 12
    ulen, clen = struct.unpack_from("<II", data, 0)
    magic = data[8:11]
    if magic == b"CNK":  # some platform builds wrap the header once more
        hdr = 24
        ulen, clen = struct.unpack_from("<II", data, 12)
        magic = data[20:23]
    stype = data[hdr - 1]
    payload = data[hdr:]

    try:
        if magic == b"PlZ":
            if stype == 0x31:      # one zlib layer
                raw = zlib.decompress(payload)
            elif stype == 0x32:    # two zlib layers
                inner = zlib.decompress(payload)
                try:
                    raw = zlib.decompress(inner)
                except zlib.error:
                    # Written by version 1.0 of this tool, which mislabelled
                    # single-layer files as 0x32.
                    raw = inner
            else:
                raise SaveFormatError(f"unknown PlZ compression type {stype:#04x}")
        elif magic == b"PlM":
            try:
                import ooz
            except ImportError as e:
                raise SaveFormatError(
                    "This save uses Oodle (PlM) compression; the 'pyooz' package is "
                    "required to read it (pip install pyooz).") from e
            if clen != len(payload):
                raise SaveFormatError(
                    f"the file is cut short ({len(payload):,} of {clen:,} compressed bytes present)")
            raw = ooz.decompress(payload, ulen)
        else:
            raise SaveFormatError(f"not a valid save: {_describe_bad(data)}.")
    except zlib.error as e:
        raise SaveFormatError(f"the data inside is damaged ({e})") from e
    except SaveFormatError:
        raise
    except Exception as e:  # ooz raises plain exceptions on bad data
        raise SaveFormatError(f"the data inside is damaged ({e})") from e

    if len(raw) != ulen:
        raise SaveFormatError(f"the file is damaged (got {len(raw):,} bytes, header says {ulen:,})")
    if raw[:4] != b"GVAS":
        raise SaveFormatError("the file decompressed to something that isn't a save")
    return raw, SavInfo(magic, stype, hdr)


def compress_sav(raw: bytes) -> bytes:
    """Encode GVAS bytes as a PlZ save with a single zlib layer (type 0x31)."""
    comp = zlib.compress(raw, 6)
    out = struct.pack("<II", len(raw), len(comp)) + b"PlZ" + bytes([0x31]) + comp
    # Never write something we can't read back identically.
    check, _ = decompress_sav(out)
    if check != raw:
        raise SaveFormatError("round-trip verification failed")
    return out


def read_sav(path) -> tuple[bytes, SavInfo]:
    with open(path, "rb") as f:
        data = f.read()
    try:
        return decompress_sav(data)
    except SaveFormatError as e:
        raise SaveFormatError(f"{Path(path).name}: {e}") from e


def write_sav(path, raw: bytes) -> None:
    data = compress_sav(raw)
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    import os
    os.replace(tmp, path)
