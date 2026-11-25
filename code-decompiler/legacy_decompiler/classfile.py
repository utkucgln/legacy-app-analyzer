"""Minimal Java class-file reader: constant pool, members, attributes and a bytecode walker.

Extracts what an analyst would otherwise get from `javap -c -v`: string/number literals with
bytecode offsets, field ConstantValues, static initialiser values, invocations, string
special-case comparisons, numeric threshold comparisons and debug-info presence.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

ACC_PUBLIC, ACC_STATIC, ACC_FINAL, ACC_INTERFACE, ACC_ABSTRACT, ACC_ENUM = 0x1, 0x8, 0x10, 0x200, 0x400, 0x4000

_FIXED_LEN: dict[int, int] = {}
for _op in range(0x00, 0x10):
    _FIXED_LEN[_op] = 1
_FIXED_LEN.update({0x10: 2, 0x11: 3, 0x12: 2, 0x13: 3, 0x14: 3})
for _op in range(0x15, 0x1A):
    _FIXED_LEN[_op] = 2
for _op in range(0x1A, 0x36):
    _FIXED_LEN[_op] = 1
for _op in range(0x36, 0x3B):
    _FIXED_LEN[_op] = 2
for _op in range(0x3B, 0x84):
    _FIXED_LEN[_op] = 1
_FIXED_LEN[0x84] = 3
for _op in range(0x85, 0x99):
    _FIXED_LEN[_op] = 1
for _op in range(0x99, 0xA9):
    _FIXED_LEN[_op] = 3
_FIXED_LEN[0xA9] = 2
for _op in range(0xAC, 0xB2):
    _FIXED_LEN[_op] = 1
_FIXED_LEN.update({0xB2: 3, 0xB3: 3, 0xB4: 3, 0xB5: 3, 0xB6: 3, 0xB7: 3, 0xB8: 3, 0xB9: 5, 0xBA: 5,
                   0xBB: 3, 0xBC: 2, 0xBD: 3, 0xBE: 1, 0xBF: 1, 0xC0: 3, 0xC1: 3, 0xC2: 1, 0xC3: 1,
                   0xC5: 4, 0xC6: 3, 0xC7: 3, 0xC8: 5, 0xC9: 5})

IF_ICMP = set(range(0x9F, 0xA5))  # if_icmpeq..if_icmple
IF_ZERO = set(range(0x99, 0x9F))  # ifeq..ifle
JAVA_VERSIONS = {45: "1.1", 46: "1.2", 47: "1.3", 48: "1.4", 49: "5", 50: "6", 51: "7", 52: "8", 53: "9",
                 54: "10", 55: "11", 56: "12", 57: "13", 58: "14", 59: "15", 60: "16", 61: "17", 62: "18",
                 63: "19", 64: "20", 65: "21"}


class ClassFormatError(ValueError):
    pass


@dataclass
class Member:
    name: str
    descriptor: str
    access: int
    constant_value: Any = None
    code: bytes | None = None
    line_range: tuple[int, int] | None = None
    has_local_vars: bool = False
    exceptions: list[str] = field(default_factory=list)


@dataclass
class MethodFacts:
    strings: list[tuple[int, str]] = field(default_factory=list)
    numbers: list[tuple[int, Any]] = field(default_factory=list)
    invokes: list[tuple[int, str, str, str]] = field(default_factory=list)
    field_refs: list[tuple[int, str, str, str, str]] = field(default_factory=list)  # pc, op, owner, name, desc
    news: list[tuple[int, str]] = field(default_factory=list)
    string_compares: list[tuple[int, str, str]] = field(default_factory=list)  # pc, literal, method
    number_compares: list[tuple[int, Any]] = field(default_factory=list)
    constant_compares: list[tuple[int, str, str]] = field(default_factory=list)  # pc, owner.field, via
    static_assignments: list[tuple[str, Any]] = field(default_factory=list)
    string_args: list[tuple[int, str, str, str]] = field(default_factory=list)  # pc, literal, owner, method
    opcodes: list[int] = field(default_factory=list)


@dataclass
class ClassInfo:
    name: str
    super_name: str | None
    interfaces: list[str]
    access: int
    major: int
    minor: int
    source_file: str | None
    fields: list[Member]
    methods: list[Member]
    class_refs: set[str]
    cp: list[Any]

    @property
    def java_version(self) -> str:
        return JAVA_VERSIONS.get(self.major, str(self.major))

    @property
    def is_enum(self) -> bool:
        return bool(self.access & ACC_ENUM) or self.super_name == "java/lang/Enum"

    @property
    def simple_name(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def dotted(self) -> str:
        return self.name.replace("/", ".")

    def has_line_numbers(self) -> bool:
        return any(m.line_range for m in self.methods if m.code)

    def has_local_vars(self) -> bool:
        return any(m.has_local_vars for m in self.methods if m.code)

    # ------------------------------------------------------------ bytecode analysis
    def analyse(self, method: Member) -> MethodFacts:
        facts = MethodFacts()
        code = method.code or b""
        recent: list[tuple[int, str, Any]] = []  # (pc, kind, value)
        pending: list[Any] = []
        pc = 0
        while pc < len(code):
            op = code[pc]
            facts.opcodes.append(op)
            length = _FIXED_LEN.get(op)
            value: Any = None
            kind = ""
            if op in (0x12, 0x13, 0x14):
                idx = code[pc + 1] if op == 0x12 else struct.unpack_from(">H", code, pc + 1)[0]
                entry = self.cp[idx]
                if isinstance(entry, tuple) and entry[0] == "String":
                    value, kind = entry[1], "str"
                    facts.strings.append((pc, value))
                elif isinstance(entry, tuple) and entry[0] == "Number":
                    value, kind = entry[1], "num"
                    facts.numbers.append((pc, value))
            elif op == 0x10:
                value, kind = struct.unpack_from(">b", code, pc + 1)[0], "num"
                facts.numbers.append((pc, value))
            elif op == 0x11:
                value, kind = struct.unpack_from(">h", code, pc + 1)[0], "num"
                facts.numbers.append((pc, value))
            elif 0x02 <= op <= 0x08:
                value, kind = op - 0x03, "small"
            elif op in (0xB2, 0xB3, 0xB4, 0xB5):
                owner, name, desc = self._ref(struct.unpack_from(">H", code, pc + 1)[0])
                opname = {0xB2: "getstatic", 0xB3: "putstatic", 0xB4: "getfield", 0xB5: "putfield"}[op]
                facts.field_refs.append((pc, opname, owner, name, desc))
                kind, value = opname, f"{owner}.{name}"
                if op == 0xB3 and owner == self.name:
                    if pending:
                        facts.static_assignments.append((name, list(pending)))
                    pending = []
            elif op in (0xB6, 0xB7, 0xB8, 0xB9):
                owner, name, desc = self._ref(struct.unpack_from(">H", code, pc + 1)[0])
                facts.invokes.append((pc, owner, name, desc))
                kind, value = "invoke", (owner, name)
                for rpc, rkind, rval in recent[-2:]:
                    if rkind == "str":
                        facts.string_args.append((rpc, rval, owner, name))
                if owner == "java/lang/String" and name in ("equals", "equalsIgnoreCase", "startsWith", "endsWith", "contains", "indexOf"):
                    for rpc, rkind, rval in recent[-3:]:
                        if rkind == "str":
                            facts.string_compares.append((rpc, rval, name))
                if name in ("compareTo", "equals", "before", "after", "isBefore", "isAfter") or owner == "java/lang/Math":
                    for rpc, rkind, rval in recent[-4:]:
                        if rkind == "getstatic" and not rval.startswith("java/"):
                            facts.constant_compares.append((rpc, rval, name))
            elif op == 0xBB:
                idx = struct.unpack_from(">H", code, pc + 1)[0]
                facts.news.append((pc, self._class_name(idx)))
            elif op in IF_ICMP or op in IF_ZERO or op in (0x94, 0x95, 0x96, 0x97, 0x98):
                for rpc, rkind, rval in recent[-3:]:
                    if rkind == "num":
                        facts.number_compares.append((rpc, rval))
                    elif rkind == "getstatic" and not rval.startswith("java/"):
                        facts.constant_compares.append((rpc, rval, "if"))
            elif op in (0xAA, 0xAB):
                base = pc + 1 + ((4 - (pc + 1) % 4) % 4)
                if op == 0xAA:
                    low, high = struct.unpack_from(">ii", code, base + 4)
                    length = base + 12 + (high - low + 1) * 4 - pc
                else:
                    npairs = struct.unpack_from(">i", code, base + 4)[0]
                    length = base + 8 + npairs * 8 - pc
            elif op == 0xC4:
                length = 6 if code[pc + 1] == 0x84 else 4
            if length is None:
                raise ClassFormatError(f"unknown opcode 0x{op:02x} in {self.name}.{method.name} at {pc}")
            if kind:
                recent.append((pc, kind, value))
                recent = recent[-6:]
                if kind in ("str", "num", "small"):
                    pending.append(value)
            pc += length
        return facts

    def _ref(self, idx: int) -> tuple[str, str, str]:
        _, class_idx, nat_idx = self.cp[idx]
        name_idx, desc_idx = self.cp[nat_idx][1], self.cp[nat_idx][2]
        return self._class_name(class_idx), self.cp[name_idx][1], self.cp[desc_idx][1]

    def _class_name(self, idx: int) -> str:
        return self.cp[self.cp[idx][1]][1]


def _read_utf8(raw: bytes) -> str:
    return raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", errors="replace")


def parse_class(data: bytes) -> ClassInfo:
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ClassFormatError("bad magic")
    minor, major, count = struct.unpack_from(">HHH", data, 4)
    pos = 10
    cp: list[Any] = [None] * count
    i = 1
    while i < count:
        tag = data[pos]
        pos += 1
        if tag == 1:
            ln = struct.unpack_from(">H", data, pos)[0]
            cp[i] = ("Utf8", _read_utf8(data[pos + 2:pos + 2 + ln]))
            pos += 2 + ln
        elif tag == 3:
            cp[i] = ("Number", struct.unpack_from(">i", data, pos)[0]); pos += 4
        elif tag == 4:
            cp[i] = ("Number", round(struct.unpack_from(">f", data, pos)[0], 6)); pos += 4
        elif tag == 5:
            cp[i] = ("Number", struct.unpack_from(">q", data, pos)[0]); pos += 8; i += 1
        elif tag == 6:
            cp[i] = ("Number", struct.unpack_from(">d", data, pos)[0]); pos += 8; i += 1
        elif tag in (7, 8, 16, 19, 20):
            cp[i] = ({7: "Class", 8: "StringRef", 16: "MethodType", 19: "Module", 20: "Package"}[tag],
                     struct.unpack_from(">H", data, pos)[0]); pos += 2
        elif tag in (9, 10, 11, 12, 17, 18):
            a, b = struct.unpack_from(">HH", data, pos)
            cp[i] = ({9: "Field", 10: "Method", 11: "IMethod", 12: "NameAndType", 17: "Dynamic", 18: "InvokeDynamic"}[tag], a, b)
            pos += 4
        elif tag == 15:
            cp[i] = ("MethodHandle", data[pos], struct.unpack_from(">H", data, pos + 1)[0]); pos += 3
        else:
            raise ClassFormatError(f"bad constant tag {tag}")
        i += 1
    # resolve string refs into literal entries
    for idx, entry in enumerate(cp):
        if entry and entry[0] == "StringRef":
            cp[idx] = ("String", cp[entry[1]][1])

    def utf(idx: int) -> str:
        return cp[idx][1]

    access, this_idx, super_idx, icount = struct.unpack_from(">HHHH", data, pos)
    pos += 8
    interfaces = [utf(cp[struct.unpack_from(">H", data, pos + 2 * k)[0]][1]) for k in range(icount)]
    pos += 2 * icount

    def read_members() -> list[Member]:
        nonlocal pos
        (n,) = struct.unpack_from(">H", data, pos)
        pos += 2
        members = []
        for _ in range(n):
            acc, name_idx, desc_idx, acount = struct.unpack_from(">HHHH", data, pos)
            pos += 8
            m = Member(utf(name_idx), utf(desc_idx), acc)
            for _ in range(acount):
                aname_idx, alen = struct.unpack_from(">HI", data, pos)
                body = data[pos + 6:pos + 6 + alen]
                pos += 6 + alen
                aname = utf(aname_idx)
                if aname == "ConstantValue":
                    entry = cp[struct.unpack_from(">H", body, 0)[0]]
                    m.constant_value = entry[1] if entry else None
                elif aname == "Code":
                    code_len = struct.unpack_from(">I", body, 4)[0]
                    m.code = body[8:8 + code_len]
                    p = 8 + code_len
                    (exc_len,) = struct.unpack_from(">H", body, p)
                    p += 2 + exc_len * 8
                    (sub_count,) = struct.unpack_from(">H", body, p)
                    p += 2
                    lines: list[int] = []
                    for _ in range(sub_count):
                        sname_idx, slen = struct.unpack_from(">HI", body, p)
                        sname = utf(sname_idx)
                        if sname == "LineNumberTable":
                            (lcount,) = struct.unpack_from(">H", body, p + 6)
                            lines += [struct.unpack_from(">HH", body, p + 8 + 4 * k)[1] for k in range(lcount)]
                        elif sname in ("LocalVariableTable",):
                            m.has_local_vars = True
                        p += 6 + slen
                    if lines:
                        m.line_range = (min(lines), max(lines))
                elif aname == "Exceptions":
                    (ecount,) = struct.unpack_from(">H", body, 0)
                    m.exceptions = [utf(cp[struct.unpack_from(">H", body, 2 + 2 * k)[0]][1]) for k in range(ecount)]
            members.append(m)
        return members

    fields = read_members()
    methods = read_members()
    (acount,) = struct.unpack_from(">H", data, pos)
    pos += 2
    source_file = None
    for _ in range(acount):
        aname_idx, alen = struct.unpack_from(">HI", data, pos)
        if utf(aname_idx) == "SourceFile":
            source_file = utf(struct.unpack_from(">H", data, pos + 6)[0])
        pos += 6 + alen

    class_refs: set[str] = set()
    for entry in cp:
        if entry and entry[0] == "Class":
            ref = utf(entry[1]).lstrip("[")
            if ref.startswith("L") and ref.endswith(";"):
                ref = ref[1:-1]
            if len(ref) > 1:
                class_refs.add(ref)
        elif entry and entry[0] == "Utf8" and entry[1].startswith(("L", "(", "[")) and ";" in entry[1]:
            for ref in _descriptor_classes(entry[1]):
                class_refs.add(ref)
    this_name = utf(cp[this_idx][1])
    class_refs.discard(this_name)
    return ClassInfo(this_name, utf(cp[super_idx][1]) if super_idx else None, interfaces, access, major, minor,
                     source_file, fields, methods, class_refs, cp)


def _descriptor_classes(desc: str) -> list[str]:
    out, i = [], 0
    while i < len(desc):
        if desc[i] == "L":
            end = desc.find(";", i)
            if end < 0:
                break
            name = desc[i + 1:end]
            if "<" not in name and " " not in name:
                out.append(name)
            i = end + 1
        else:
            i += 1
    return out
