from __future__ import annotations

import json
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

_DESCRIPTOR_CANDIDATES = [
    Path(__file__).resolve().parent / "data" / "descriptors.json",
    Path(__file__).resolve().parent.parent / "proto" / "descriptors.json",
]

WIRE_VARINT = 0
WIRE_64 = 1
WIRE_LEN = 2
WIRE_32 = 5

SCALAR_WIRE = {
    "double": WIRE_64,
    "float": WIRE_32,
    "int64": WIRE_VARINT,
    "uint64": WIRE_VARINT,
    "int32": WIRE_VARINT,
    "fixed64": WIRE_64,
    "fixed32": WIRE_32,
    "bool": WIRE_VARINT,
    "string": WIRE_LEN,
    "bytes": WIRE_LEN,
    "uint32": WIRE_VARINT,
    "sfixed32": WIRE_32,
    "sfixed64": WIRE_64,
    "sint32": WIRE_VARINT,
    "sint64": WIRE_VARINT,
}


class ProtoError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_descriptors() -> dict[str, Any]:
    for path in _DESCRIPTOR_CANDIDATES:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("opencursor protobuf descriptors.json not found")


def _enum_value(type_name: str, value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    desc = load_descriptors()["enums"].get(type_name)
    if not desc:
        raise ProtoError(f"unknown enum {type_name}")
    name = str(value)
    for item in desc["values"]:
        if item["name"] == name or item["name"].endswith("_" + name.upper()) or item["name"].split("_")[-1] == name.upper():
            return int(item["no"])
        short = item["name"]
        # AGENT_MODE_AGENT <-> AGENT
        if short.endswith("_" + name):
            return int(item["no"])
    raise ProtoError(f"unknown enum value {value!r} for {type_name}")


def _encode_varint(n: int) -> bytes:
    if n < 0:
        n = n & ((1 << 64) - 1)
    out = bytearray()
    while True:
        to_write = n & 0x7F
        n >>= 7
        if n:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            break
    return bytes(out)


def _decode_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if i >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ProtoError("varint too long")


def _zigzag_encode(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _zigzag_decode(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _encode_tag(field_no: int, wire: int) -> bytes:
    return _encode_varint((field_no << 3) | wire)


def _encode_scalar(scalar: str, value: Any) -> bytes:
    if scalar == "bool":
        return _encode_varint(1 if value else 0)
    if scalar in ("int32", "int64", "uint32", "uint64"):
        return _encode_varint(int(value))
    if scalar == "sint32":
        n = int(value)
        return _encode_varint((n << 1) ^ (n >> 31))
    if scalar == "sint64":
        return _encode_varint(_zigzag_encode(int(value)))
    if scalar == "float":
        return struct.pack("<f", float(value))
    if scalar == "double":
        return struct.pack("<d", float(value))
    if scalar == "fixed32":
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if scalar == "sfixed32":
        return struct.pack("<i", int(value))
    if scalar == "fixed64":
        return struct.pack("<Q", int(value) & ((1 << 64) - 1))
    if scalar == "sfixed64":
        return struct.pack("<q", int(value))
    if scalar == "string":
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        return _encode_varint(len(raw)) + raw
    if scalar == "bytes":
        raw = value if isinstance(value, (bytes, bytearray)) else bytes(value)
        return _encode_varint(len(raw)) + raw
    raise ProtoError(f"unsupported scalar {scalar}")


def _is_present(value: Any) -> bool:
    return value is not None


def encode_message(type_name: str, data: Any) -> bytes:
    if type_name.startswith("google.protobuf."):
        return _encode_wkt(type_name, data)
    if data is None:
        return b""
    desc = load_descriptors()["messages"].get(type_name)
    if not desc:
        raise ProtoError(f"unknown message {type_name}")
    out = bytearray()
    for field in desc["fields"]:
        name = field["name"]
        if name not in data:
            continue
        value = data[name]
        if not _is_present(value):
            continue
        chunks = value if field["repeated"] and not (field["type"]["kind"] == "map") else [value]
        if field["type"]["kind"] == "map":
            if not isinstance(value, dict):
                continue
            for mk, mv in value.items():
                entry = _encode_map_entry(field["type"], mk, mv)
                out += _encode_tag(field["no"], WIRE_LEN)
                out += _encode_varint(len(entry)) + entry
            continue
        for item in chunks:
            out += _encode_field(field, item)
    return bytes(out)


def _encode_map_entry(map_type: dict[str, Any], key: Any, value: Any) -> bytes:
    out = bytearray()
    key_field = {"no": 1, "repeated": False, "type": map_type["key"]}
    val_field = {"no": 2, "repeated": False, "type": map_type["value"]}
    out += _encode_field(key_field, key)
    out += _encode_field(val_field, value)
    return bytes(out)


def _encode_field(field: dict[str, Any], value: Any) -> bytes:
    t = field["type"]
    kind = t["kind"]
    no = field["no"]
    if kind == "scalar":
        wire = SCALAR_WIRE[t["type"]]
        payload = _encode_scalar(t["type"], value)
        if t["type"] in ("string", "bytes"):
            return _encode_tag(no, WIRE_LEN) + payload
        return _encode_tag(no, wire) + payload
    if kind == "enum":
        return _encode_tag(no, WIRE_VARINT) + _encode_varint(_enum_value(t["type"], value))
    if kind == "message":
        inner = value if isinstance(value, (bytes, bytearray)) else encode_message(t["type"], value)
        return _encode_tag(no, WIRE_LEN) + _encode_varint(len(inner)) + inner
    raise ProtoError(f"cannot encode {kind}")


_VALUE_PROTO_KEYS = {
    "null_value",
    "number_value",
    "string_value",
    "bool_value",
    "struct_value",
    "list_value",
    "_oneof_kind",
    "kind",
}


def _encode_wkt(type_name: str, data: Any) -> bytes:
    if type_name == "google.protobuf.Value":
        return encode_protobuf_value(data)
    if type_name == "google.protobuf.Struct":
        return encode_protobuf_struct(data)
    if type_name == "google.protobuf.ListValue":
        return encode_protobuf_list(data)
    if type_name == "google.protobuf.Timestamp":
        data = data or {}
        return encode_message_raw(
            [
                (1, "int64", data.get("seconds", 0)),
                (2, "int32", data.get("nanos", 0)),
            ]
        )
    if type_name == "google.protobuf.Duration":
        data = data or {}
        return encode_message_raw(
            [
                (1, "int64", data.get("seconds", 0)),
                (2, "int32", data.get("nanos", 0)),
            ]
        )
    return b""


def encode_protobuf_value(data: Any) -> bytes:
    if isinstance(data, dict) and set(data) <= _VALUE_PROTO_KEYS and any(k in data for k in _VALUE_PROTO_KEYS - {"_oneof_kind", "kind"}):
        if "null_value" in data:
            return _encode_tag(1, WIRE_VARINT) + _encode_varint(int(data["null_value"] or 0))
        if "number_value" in data:
            return _encode_tag(2, WIRE_64) + struct.pack("<d", float(data["number_value"]))
        if "string_value" in data:
            raw = str(data["string_value"]).encode("utf-8")
            return _encode_tag(3, WIRE_LEN) + _encode_varint(len(raw)) + raw
        if "bool_value" in data:
            return _encode_tag(4, WIRE_VARINT) + _encode_varint(1 if data["bool_value"] else 0)
        if "struct_value" in data:
            inner = encode_protobuf_struct(data["struct_value"])
            return _encode_tag(5, WIRE_LEN) + _encode_varint(len(inner)) + inner
        if "list_value" in data:
            inner = encode_protobuf_list(data["list_value"])
            return _encode_tag(6, WIRE_LEN) + _encode_varint(len(inner)) + inner
        return b""
    if data is None:
        return _encode_tag(1, WIRE_VARINT) + _encode_varint(0)
    if isinstance(data, bool):
        return _encode_tag(4, WIRE_VARINT) + _encode_varint(1 if data else 0)
    if isinstance(data, (int, float)):
        return _encode_tag(2, WIRE_64) + struct.pack("<d", float(data))
    if isinstance(data, str):
        raw = data.encode("utf-8")
        return _encode_tag(3, WIRE_LEN) + _encode_varint(len(raw)) + raw
    if isinstance(data, (bytes, bytearray)):
        return data
    if isinstance(data, list):
        inner = encode_protobuf_list(data)
        return _encode_tag(6, WIRE_LEN) + _encode_varint(len(inner)) + inner
    if isinstance(data, dict):
        inner = encode_protobuf_struct(data)
        return _encode_tag(5, WIRE_LEN) + _encode_varint(len(inner)) + inner
    raw = str(data).encode("utf-8")
    return _encode_tag(3, WIRE_LEN) + _encode_varint(len(raw)) + raw


def encode_protobuf_struct(data: Any) -> bytes:
    fields = data
    if isinstance(data, dict) and "fields" in data and isinstance(data["fields"], dict) and set(data) <= {"fields"}:
        fields = data["fields"]
    if not isinstance(fields, dict):
        return b""
    out = bytearray()
    for key, value in fields.items():
        entry = _encode_map_entry(
            {
                "key": {"kind": "scalar", "type": "string", "code": 9},
                "value": {"kind": "message", "type": "google.protobuf.Value"},
            },
            key,
            value,
        )
        out += _encode_tag(1, WIRE_LEN) + _encode_varint(len(entry)) + entry
    return bytes(out)


def encode_protobuf_list(data: Any) -> bytes:
    values = data
    if isinstance(data, dict) and "values" in data:
        values = data["values"]
    if not isinstance(values, list):
        return b""
    out = bytearray()
    for item in values:
        inner = encode_protobuf_value(item)
        out += _encode_tag(1, WIRE_LEN) + _encode_varint(len(inner)) + inner
    return bytes(out)


def _decode_wkt(type_name: str, buf: bytes) -> Any:
    if type_name == "google.protobuf.Value":
        return decode_protobuf_value(buf)
    if type_name == "google.protobuf.Struct":
        return decode_protobuf_struct(buf)
    if type_name == "google.protobuf.ListValue":
        return decode_protobuf_list(buf)
    return {"_raw": buf}


def decode_protobuf_value(buf: bytes) -> Any:
    i = 0
    n = len(buf)
    result: Any = None
    while i < n:
        tag, i = _decode_varint(buf, i)
        no, wire = tag >> 3, tag & 7
        if wire == WIRE_VARINT:
            raw, i = _decode_varint(buf, i)
            if no == 1:
                result = None
            elif no == 4:
                result = bool(raw)
        elif wire == WIRE_64:
            raw = buf[i : i + 8]
            i += 8
            if no == 2:
                number = struct.unpack("<d", raw)[0]
                if number.is_integer() and abs(number) < 2**53:
                    result = int(number)
                else:
                    result = number
        elif wire == WIRE_LEN:
            ln, i = _decode_varint(buf, i)
            raw = buf[i : i + ln]
            i += ln
            if no == 3:
                result = raw.decode("utf-8", errors="replace")
            elif no == 5:
                result = decode_protobuf_struct(raw)
            elif no == 6:
                result = decode_protobuf_list(raw)
        elif wire == WIRE_32:
            i += 4
        else:
            break
    return result


def decode_protobuf_struct(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _decode_varint(buf, i)
        no, wire = tag >> 3, tag & 7
        if wire != WIRE_LEN:
            if wire == WIRE_VARINT:
                _, i = _decode_varint(buf, i)
            elif wire == WIRE_64:
                i += 8
            elif wire == WIRE_32:
                i += 4
            continue
        ln, i = _decode_varint(buf, i)
        entry = buf[i : i + ln]
        i += ln
        if no != 1:
            continue
        parsed = _decode_map_entry(
            {
                "key": {"kind": "scalar", "type": "string", "code": 9},
                "value": {"kind": "message", "type": "google.protobuf.Value"},
            },
            entry,
        )
        key = parsed.get("key")
        if key is not None:
            out[key] = parsed.get("value")
    return out


def decode_protobuf_list(buf: bytes) -> list[Any]:
    out: list[Any] = []
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _decode_varint(buf, i)
        no, wire = tag >> 3, tag & 7
        if wire != WIRE_LEN:
            if wire == WIRE_VARINT:
                _, i = _decode_varint(buf, i)
            elif wire == WIRE_64:
                i += 8
            elif wire == WIRE_32:
                i += 4
            continue
        ln, i = _decode_varint(buf, i)
        raw = buf[i : i + ln]
        i += ln
        if no == 1:
            out.append(decode_protobuf_value(raw))
    return out


def encode_message_raw(fields: list[tuple[int, str, Any]]) -> bytes:
    out = bytearray()
    for no, scalar, value in fields:
        if value in (None, 0, "", False) and scalar not in ("string", "bytes"):
            continue
        fake = {"no": no, "repeated": False, "type": {"kind": "scalar", "type": scalar, "code": 0}}
        out += _encode_field(fake, value)
    return bytes(out)


def decode_message(type_name: str, buf: bytes) -> Any:
    if type_name.startswith("google.protobuf."):
        return _decode_wkt(type_name, buf)
    desc = load_descriptors()["messages"].get(type_name)
    if not desc:
        raise ProtoError(f"unknown message {type_name}")
    by_no = {int(f["no"]): f for f in desc["fields"]}
    out: dict[str, Any] = {}
    i = 0
    n = len(buf)
    while i < n:
        key, i = _decode_varint(buf, i)
        field_no = key >> 3
        wire = key & 7
        field = by_no.get(field_no)
        if wire == WIRE_VARINT:
            val, i = _decode_varint(buf, i)
        elif wire == WIRE_64:
            val = buf[i : i + 8]
            i += 8
        elif wire == WIRE_LEN:
            ln, i = _decode_varint(buf, i)
            val = buf[i : i + ln]
            i += ln
        elif wire == WIRE_32:
            val = buf[i : i + 4]
            i += 4
        else:
            raise ProtoError(f"unknown wire type {wire}")
        if field is None:
            continue
        decoded = _decode_field_value(field, wire, val)
        name = field["name"]
        if field["type"]["kind"] == "map":
            entry = decoded
            if isinstance(entry, dict) and "key" in entry:
                out.setdefault(name, {})[entry["key"]] = entry.get("value")
            continue
        if field["repeated"]:
            out.setdefault(name, []).append(decoded)
        else:
            out[name] = decoded
            if field.get("oneof"):
                out[f"_oneof_{field['oneof']}"] = name
    return out


def _decode_field_value(field: dict[str, Any], wire: int, raw: Any) -> Any:
    t = field["type"]
    kind = t["kind"]
    if kind == "map":
        # raw is bytes of map entry
        entry_buf = raw if isinstance(raw, (bytes, bytearray)) else b""
        parsed = _decode_map_entry(t, bytes(entry_buf))
        return parsed
    if kind == "scalar":
        return _decode_scalar(t["type"], wire, raw)
    if kind == "enum":
        return int(raw) if not isinstance(raw, (bytes, bytearray)) else int.from_bytes(raw, "little")
    if kind == "message":
        return decode_message(t["type"], bytes(raw))
    return raw


def _decode_map_entry(map_type: dict[str, Any], buf: bytes) -> dict[str, Any]:
    i = 0
    key = None
    value = None
    while i < len(buf):
        tag, i = _decode_varint(buf, i)
        no, wire = tag >> 3, tag & 7
        if wire == WIRE_VARINT:
            raw, i = _decode_varint(buf, i)
        elif wire == WIRE_64:
            raw = buf[i : i + 8]
            i += 8
        elif wire == WIRE_LEN:
            ln, i = _decode_varint(buf, i)
            raw = buf[i : i + ln]
            i += ln
        elif wire == WIRE_32:
            raw = buf[i : i + 4]
            i += 4
        else:
            break
        if no == 1:
            key = _decode_type(map_type["key"], wire, raw)
        elif no == 2:
            value = _decode_type(map_type["value"], wire, raw)
    return {"key": key, "value": value}


def _decode_type(t: dict[str, Any], wire: int, raw: Any) -> Any:
    if t["kind"] == "scalar":
        return _decode_scalar(t["type"], wire, raw)
    if t["kind"] == "enum":
        return int(raw)
    if t["kind"] == "message":
        return decode_message(t["type"], bytes(raw))
    return raw


def _decode_scalar(scalar: str, wire: int, raw: Any) -> Any:
    if scalar == "bool":
        return bool(raw)
    if scalar in ("int32", "int64", "uint32", "uint64"):
        return int(raw)
    if scalar == "sint32":
        n = int(raw)
        return (n >> 1) ^ -(n & 1)
    if scalar == "sint64":
        return _zigzag_decode(int(raw))
    if scalar == "string":
        return bytes(raw).decode("utf-8", errors="replace")
    if scalar == "bytes":
        return bytes(raw)
    if scalar == "float":
        return struct.unpack("<f", bytes(raw))[0]
    if scalar == "double":
        return struct.unpack("<d", bytes(raw))[0]
    if scalar == "fixed32":
        return struct.unpack("<I", bytes(raw))[0]
    if scalar == "sfixed32":
        return struct.unpack("<i", bytes(raw))[0]
    if scalar == "fixed64":
        return struct.unpack("<Q", bytes(raw))[0]
    if scalar == "sfixed64":
        return struct.unpack("<q", bytes(raw))[0]
    return raw


def oneof_case(msg: dict[str, Any], name: str = "message") -> str | None:
    return msg.get(f"_oneof_{name}")
