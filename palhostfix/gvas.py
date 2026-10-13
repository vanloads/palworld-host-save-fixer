"""Offset-tracking reader for Unreal Engine GVAS save data (as used by Palworld).

Rather than deserialising into Python objects and re-serialising (where any
unknown or newly added field becomes a source of corruption), this reader
records *where* every property lives in the original byte buffer.  Edits are
then performed as byte patches on the untouched original data:

* same-length patches (e.g. swapping one 16 byte GUID for another) need no
  further bookkeeping;
* length-changing splices (e.g. removing a map entry) are applied through
  :class:`Patcher`, which also fixes every enclosing size / count field.

Anything the reader does not understand is skipped using the size recorded in
the property tag, so new game versions that add fields do not break parsing.
"""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Iterator

# Struct types serialised as raw bytes rather than as a property list.
RAW_STRUCT_SIZES = {
    "Guid": 16,
    "Vector": 24, "Rotator": 24, "Quat": 32,
    "Vector2D": 16, "Vector4": 32,
    "LinearColor": 16, "Color": 4,
    "IntPoint": 8, "IntVector": 12,
    "DateTime": 8, "Timespan": 8,
}

FIXED_SIZES = {
    "IntProperty": 4, "UInt32Property": 4, "FloatProperty": 4,
    "Int64Property": 8, "UInt64Property": 8, "DoubleProperty": 8,
    "Int16Property": 2, "UInt16Property": 2, "Int8Property": 1,
}


class ParseError(Exception):
    pass


def guid_to_bytes(g: str | uuid.UUID) -> bytes:
    """Palworld GUID text (32 hex chars, as in Players/<GUID>.sav) -> 16 file bytes.

    Unreal stores an FGuid as four little-endian uint32s (A, B, C, D) and
    prints it as their hex concatenation.
    """
    h = str(g).replace("-", "").strip()
    if len(h) != 32:
        raise ValueError(f"not a GUID: {g!r}")
    parts = [int(h[i:i + 8], 16) for i in range(0, 32, 8)]
    return struct.pack("<4I", *parts)


def bytes_to_guid(b: bytes) -> str:
    return "".join(f"{x:08X}" for x in struct.unpack("<4I", b))


def fmt_guid(g: str) -> str:
    return f"{g[0:8]}-{g[8:12]}-{g[12:16]}-{g[16:20]}-{g[20:32]}"


@dataclass
class Node:
    """One parsed value inside the buffer.

    ``size_fields``: (offset, width) of every integer in *this* node that holds
    a byte length or element count which must grow/shrink by the number of
    bytes spliced inside it.  Map/array element counts are not included
    (they count elements, not bytes) and are adjusted explicitly when entries
    are removed.
    """
    name: str
    type: str
    start: int            # first byte of the node (property name / element)
    end: int              # one past the last byte
    data_start: int = 0   # first byte of the value payload
    value: object = None
    children: list = field(default_factory=list)
    size_fields: list = field(default_factory=list)
    struct_type: str = ""
    count_offset: int = -1  # array/map element count field, if any
    parent: "Node | None" = field(default=None, repr=False)

    # -- convenience -----------------------------------------------------
    def get(self, name: str) -> "Node | None":
        for c in self.children:
            if c.name == name:
                return c
        return None

    def path(self, *names: str) -> "Node | None":
        n: Node | None = self
        for nm in names:
            if n is None:
                return None
            n = n.get(nm)
        return n

    def walk(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def ancestors(self) -> Iterator["Node"]:
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def full_path(self) -> str:
        parts = [self.name]
        for a in self.ancestors():
            if a.name:
                parts.append(a.name)
        return ".".join(reversed(parts))


class Reader:
    def __init__(self, data: bytes | bytearray):
        self.data = data
        self.pos = 0

    def need(self, n: int):
        if n < 0 or self.pos + n > len(self.data):
            raise ParseError(f"read past end at {self.pos} (+{n})")

    def u8(self) -> int:
        self.need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def i32(self) -> int:
        self.need(4)
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def u32(self) -> int:
        self.need(4)
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def u16(self) -> int:
        self.need(2)
        v = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def i64(self) -> int:
        self.need(8)
        v = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def raw(self, n: int) -> bytes:
        self.need(n)
        v = bytes(self.data[self.pos:self.pos + n])
        self.pos += n
        return v

    def guid(self) -> str:
        return bytes_to_guid(self.raw(16))

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            if n > 1 << 20:
                raise ParseError(f"implausible string length {n} at {self.pos - 4}")
            b = self.raw(n)
            if b[-1] != 0:
                raise ParseError(f"unterminated string at {self.pos - n}")
            return b[:-1].decode("latin-1")
        n = -n
        if n > 1 << 20:
            raise ParseError(f"implausible string length {-n} at {self.pos - 4}")
        b = self.raw(n * 2)
        if b[-2:] != b"\0\0":
            raise ParseError(f"unterminated utf16 string at {self.pos - n * 2}")
        return b[:-2].decode("utf-16-le", errors="replace")

    def optional_guid(self):
        if self.u8():
            self.raw(16)


@dataclass
class Header:
    end: int
    save_game_class: str
    engine: str


def parse_header(r: Reader) -> Header:
    if r.raw(4) != b"GVAS":
        raise ParseError("not a GVAS file")
    save_ver = r.i32()
    r.i32()  # UE4 package version
    if save_ver >= 3:
        r.i32()  # UE5 package version
    r.u16(); r.u16(); r.u16(); r.u32()
    engine = r.fstring()
    r.i32()  # custom version format
    for _ in range(r.i32()):
        r.raw(16); r.i32()
    cls = r.fstring()
    return Header(r.pos, cls, engine)


class Parser:
    """Parses property lists.  ``skip`` names properties whose contents are
    not descended into (still recorded with their extent) to keep large worlds
    fast."""

    def __init__(self, data, skip: set[str] | None = None):
        self.r = Reader(data)
        self.data = data
        self.skip = skip or set()
        self.fallbacks: list[str] = []  # properties skipped by size

    # property lists ------------------------------------------------------
    def properties(self, parent: Node, end: int | None) -> None:
        r = self.r
        while True:
            if end is not None and r.pos >= end:
                return
            start = r.pos
            name = r.fstring()
            if name == "None":
                return
            ptype = r.fstring()
            size_off = r.pos
            size = r.i64()
            node = Node(name, ptype, start, 0, parent=parent)
            node.size_fields.append((size_off, 8))
            self.property_value(node, ptype, size)
            parent.children.append(node)

    def property_value(self, node: Node, ptype: str, size: int) -> None:
        r = self.r
        try:
            if ptype == "StructProperty":
                node.struct_type = r.fstring()
                r.raw(16)
                r.optional_guid()
                node.data_start = r.pos
                end = r.pos + size
                if node.name in self.skip:
                    r.pos = end
                else:
                    self.struct_body(node, node.struct_type, end)
            elif ptype == "ArrayProperty":
                inner = r.fstring()
                node.struct_type = inner
                r.optional_guid()
                node.data_start = r.pos
                end = r.pos + size
                if node.name in self.skip:
                    r.pos = end
                else:
                    self.array_body(node, inner, end)
            elif ptype == "MapProperty":
                kt, vt = r.fstring(), r.fstring()
                node.struct_type = f"{kt},{vt}"
                r.optional_guid()
                node.data_start = r.pos
                end = r.pos + size
                if node.name in self.skip:
                    r.pos = end
                else:
                    self.map_body(node, kt, vt, end)
            elif ptype == "SetProperty":
                r.fstring()
                r.optional_guid()
                node.data_start = r.pos
                r.pos += size
            elif ptype == "BoolProperty":
                node.value = bool(r.u8())
                r.optional_guid()
                node.data_start = r.pos
            elif ptype in ("ByteProperty", "EnumProperty"):
                enum = r.fstring()
                r.optional_guid()
                node.data_start = r.pos
                if ptype == "ByteProperty" and enum == "None" and size == 1:
                    node.value = r.u8()
                else:
                    node.value = r.fstring()
            elif ptype in ("StrProperty", "NameProperty"):
                r.optional_guid()
                node.data_start = r.pos
                node.value = r.fstring()
            else:
                r.optional_guid()
                node.data_start = r.pos
                if ptype in FIXED_SIZES and size == FIXED_SIZES[ptype]:
                    fmt = {"IntProperty": "<i", "UInt32Property": "<I", "FloatProperty": "<f",
                           "Int64Property": "<q", "UInt64Property": "<Q", "DoubleProperty": "<d",
                           "Int16Property": "<h", "UInt16Property": "<H", "Int8Property": "<b"}[ptype]
                    node.value = struct.unpack_from(fmt, self.data, r.pos)[0]
                r.pos += size
            if r.pos != node.data_start + size:
                raise ParseError(f"{node.name}: size mismatch")
        except ParseError:
            # Unknown layout: fall back to the recorded size.
            self.fallbacks.append(f"{node.name}@{node.start}")
            node.children.clear()
            r.pos = node.data_start + size
            node.value = None
        node.end = r.pos

    def struct_body(self, node: Node, stype: str, end: int) -> None:
        r = self.r
        if stype == "Guid":
            node.value = r.guid()
        elif stype in RAW_STRUCT_SIZES:
            r.pos += RAW_STRUCT_SIZES[stype]
        else:
            self.properties(node, end)

    def element(self, parent: Node, name: str, etype: str, stype: str, end: int) -> Node:
        """Parse one array/map element of property type ``etype``."""
        r = self.r
        n = Node(name, etype, r.pos, 0, parent=parent, struct_type=stype)
        n.data_start = r.pos
        if etype == "StructProperty":
            if stype == "Guid":
                n.value = r.guid()
            elif stype in RAW_STRUCT_SIZES:
                r.pos += RAW_STRUCT_SIZES[stype]
            elif stype == "?":
                # Struct inside a map: type isn't recorded, so sniff it.
                if self._looks_like_property_list():
                    self.properties(n, end)
                else:
                    n.struct_type = "Guid"
                    n.value = r.guid()
            else:
                self.properties(n, end)
        elif etype in ("StrProperty", "NameProperty", "EnumProperty"):
            n.value = r.fstring()
        elif etype in FIXED_SIZES:
            r.pos += FIXED_SIZES[etype]
        elif etype in ("ByteProperty", "BoolProperty"):
            n.value = r.u8()
        else:
            raise ParseError(f"unsupported element type {etype}")
        n.end = r.pos
        parent.children.append(n)
        return n

    def _looks_like_property_list(self) -> bool:
        save = self.r.pos
        try:
            name = self.r.fstring()
            if not name or not name.isprintable():
                return False
            if name == "None":
                return True
            t = self.r.fstring()
            return t.endswith("Property")
        except ParseError:
            return False
        finally:
            self.r.pos = save

    def array_body(self, node: Node, inner: str, end: int) -> None:
        r = self.r
        node.count_offset = r.pos
        count = r.i32()
        if inner == "ByteProperty":
            # Raw byte blob: the count is also a byte length.
            node.size_fields.append((node.count_offset, 4))
            node.value = (r.pos, count)
            r.pos += count
            return
        if inner == "StructProperty":
            r.fstring()
            r.fstring()
            inner_size_off = r.pos
            r.i64()
            stype = r.fstring()
            r.raw(16)
            r.optional_guid()
            node.size_fields.append((inner_size_off, 8))
            node.struct_type = stype
            for i in range(count):
                self.element(node, str(i), "StructProperty", stype, end)
            return
        for i in range(count):
            self.element(node, str(i), inner, "", end)

    def map_body(self, node: Node, kt: str, vt: str, end: int) -> None:
        r = self.r
        r.i32()  # removed-keys count, always 0 in saves
        node.count_offset = r.pos
        count = r.i32()
        for i in range(count):
            entry = Node(str(i), "MapEntry", r.pos, 0, parent=node)
            entry.data_start = r.pos
            self.element(entry, "Key", kt, "?", end)
            self.element(entry, "Value", vt, "?", end)
            entry.end = r.pos
            node.children.append(entry)


def parse_gvas(data, skip: set[str] | None = None) -> tuple[Header, Node]:
    p = Parser(data, skip)
    hdr = parse_header(p.r)
    root = Node("", "Root", 0, len(data))
    root.data_start = hdr.end
    p.properties(root, None)
    root.value = p.fallbacks
    if len(data) - p.r.pos not in (0, 4):
        raise ParseError(f"{len(data) - p.r.pos} unexpected bytes after the last property")
    return hdr, root


def structure_check(data) -> list[str]:
    """Parse everything (no skipping) and return the properties that could
    only be skipped by size.  Comparing this before and after an edit proves
    no size/count field was left inconsistent."""
    _, root = parse_gvas(data)
    return root.value


def parse_blob(data, start: int, length: int, parent: Node) -> int:
    """Parse a property list embedded in a ByteProperty array (Palworld's
    ``RawData``).  Children are attached to ``parent``; offsets are absolute.
    Returns the offset where the property list ended (trailing bytes follow)."""
    p = Parser(data)
    p.r.pos = start
    p.properties(parent, start + length)
    return p.r.pos


class Patcher:
    """Collects byte edits against an original buffer and applies them in one
    pass, fixing every enclosing size field for length-changing edits."""

    def __init__(self, data: bytes):
        self.data = bytes(data)
        self.edits: list[tuple[int, int, bytes, list]] = []  # (start, end, new, owners)
        self.int_deltas: dict[int, list] = {}  # offset -> [width, delta]

    def adjust_int(self, offset: int, delta: int, width: int = 4):
        """Add ``delta`` to the little-endian integer stored at ``offset``
        (used for element counts)."""
        self.int_deltas.setdefault(offset, [width, 0])[1] += delta

    def set_guid(self, offset: int, uid: str):
        self.replace(offset, offset + 16, guid_to_bytes(uid))

    def replace(self, start: int, end: int, new: bytes, owner: Node | None = None):
        """Replace data[start:end] with ``new``.  ``owner`` is the innermost
        node containing the range; all size fields of it and its ancestors are
        adjusted when the length changes."""
        chain = []
        if len(new) != end - start:
            if owner is None:
                raise ValueError("length-changing edit needs an owner node")
            n = owner
            while n is not None:
                chain.extend(n.size_fields)
                n = n.parent
        self.edits.append((start, end, bytes(new), chain))

    def remove_element(self, elem: Node):
        """Remove one array element / map entry, decrementing the container's
        element count."""
        container = elem.parent
        if container is None or container.count_offset < 0:
            raise ValueError("element has no countable container")
        self.replace(elem.start, elem.end, b"", container)
        if container.struct_type != "ByteProperty":
            self.adjust_int(container.count_offset, -1)

    def apply(self) -> bytes:
        edits = sorted(self.edits, key=lambda e: e[0])
        for a, b in zip(edits, edits[1:]):
            if a[1] > b[0]:
                raise ValueError(f"overlapping edits at {a[0]:#x} and {b[0]:#x}")
        # integer field adjustments: offset -> (width, delta)
        adjust: dict[int, list] = {}
        for start, end, new, chain in edits:
            delta = len(new) - (end - start)
            if delta:
                for off, width in chain:
                    adjust.setdefault(off, [width, 0])[1] += delta
        for off, (width, d) in self.int_deltas.items():
            adjust.setdefault(off, [width, 0])[1] += d
        buf = bytearray(self.data)
        removed = [(s, e) for s, e, new, _ in edits if e - s != len(new)]
        for off, (width, delta) in adjust.items():
            if any(s <= off < e for s, e in removed):
                continue  # field lives inside a removed range
            fmt = "<q" if width == 8 else "<i"
            v = struct.unpack_from(fmt, buf, off)[0] + delta
            if v < 0:
                raise ValueError(f"size field at {off:#x} went negative")
            struct.pack_into(fmt, buf, off, v)
        out = bytearray()
        pos = 0
        for start, end, new, _ in edits:
            out += buf[pos:start]
            out += new
            pos = end
        out += buf[pos:]
        return bytes(out)
