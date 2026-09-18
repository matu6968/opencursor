#!/usr/bin/env python3
"""Extract agent.v1 / aiserver.v1 protobuf schemas from the @cursor/sdk webpack bundle."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SCALAR_PROTO = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    6: "fixed64",
    7: "fixed32",
    8: "bool",
    9: "string",
    12: "bytes",
    13: "uint32",
    15: "sfixed32",
    16: "sfixed64",
    17: "sint32",
    18: "sint64",
}

IDENT = r"[A-Za-z_$][\w$]*"

WKT = {
    "Struct": "google.protobuf.Struct",
    "Value": "google.protobuf.Value",
    "ListValue": "google.protobuf.ListValue",
    "Timestamp": "google.protobuf.Timestamp",
    "Duration": "google.protobuf.Duration",
    "Any": "google.protobuf.Any",
    "Empty": "google.protobuf.Empty",
    "FieldMask": "google.protobuf.FieldMask",
    "BoolValue": "google.protobuf.BoolValue",
    "BytesValue": "google.protobuf.BytesValue",
    "DoubleValue": "google.protobuf.DoubleValue",
    "FloatValue": "google.protobuf.FloatValue",
    "Int32Value": "google.protobuf.Int32Value",
    "Int64Value": "google.protobuf.Int64Value",
    "StringValue": "google.protobuf.StringValue",
    "UInt32Value": "google.protobuf.UInt32Value",
    "UInt64Value": "google.protobuf.UInt64Value",
}

SKIP_MODULES = {
    "aiserver/v1/chat_pb.js",
    "aiserver/v1/dashboard_pb.js",
    "aiserver/v1/dashboard_connect.js",
    "aiserver/v1/docs_pb.js",
    "aiserver/v1/tools_pb.js",
    "aiserver/v1/usage_pb.js",
}


def _short(path: str) -> str:
    marker = "generated/"
    return path.split(marker, 1)[-1] if marker in path else path


class JSParser:
    def __init__(self, src: str) -> None:
        self.s = src
        self.i = 0
        self.n = len(src)

    def skip(self) -> None:
        s, i, n = self.s, self.i, self.n
        while i < n and s[i] in " \t\r\n":
            i += 1
        self.i = i

    def parse_value(self):
        self.skip()
        s, i, n = self.s, self.i, self.n
        if i >= n:
            return None
        c = s[i]
        if c == "{":
            return self.parse_object()
        if c == "[":
            return self.parse_array()
        if c == '"':
            return self.parse_string()
        if c == "'" :
            return self.parse_string(quote="'")
        if s.startswith("!0", i):
            self.i += 2
            return True
        if s.startswith("!1", i):
            self.i += 2
            return False
        if s.startswith("void 0", i):
            self.i += 6
            return None
        if c == "-" or c.isdigit():
            return self.parse_number()
        return self.parse_ident_expr()

    def parse_string(self, quote: str = '"') -> str:
        s, i, n = self.s, self.i, self.n
        assert s[i] == quote
        i += 1
        out = []
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                i += 1
                break
            out.append(c)
            i += 1
        self.i = i
        return "".join(out)

    def parse_number(self):
        s, i, n = self.s, self.i, self.n
        j = i
        if s[j] == "-":
            j += 1
        while j < n and (s[j].isdigit() or s[j] in ".eE+"):
            j += 1
        token = s[i:j]
        self.i = j
        if "." in token or "e" in token.lower():
            return float(token)
        return int(token)

    def parse_object(self) -> dict:
        assert self.s[self.i] == "{"
        self.i += 1
        obj = {}
        while True:
            self.skip()
            if self.i < self.n and self.s[self.i] == "}":
                self.i += 1
                return obj
            key = self.parse_key()
            self.skip()
            if self.i < self.n and self.s[self.i] == ":":
                self.i += 1
                obj[key] = self.parse_value()
            else:
                obj[key] = {"$id": key}
            self.skip()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "}":
                self.i += 1
                return obj
            raise ValueError(f"bad object at {self.i}: {self.s[self.i:self.i+40]!r}")

    def parse_array(self) -> list:
        assert self.s[self.i] == "["
        self.i += 1
        arr = []
        while True:
            self.skip()
            if self.i < self.n and self.s[self.i] == "]":
                self.i += 1
                return arr
            arr.append(self.parse_value())
            self.skip()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "]":
                self.i += 1
                return arr
            raise ValueError(f"bad array at {self.i}: {self.s[self.i:self.i+40]!r}")

    def parse_key(self) -> str:
        self.skip()
        if self.s[self.i] in "\"'":
            return self.parse_string(self.s[self.i])
        return self.parse_ident()

    def parse_ident(self) -> str:
        s, i, n = self.s, self.i, self.n
        j = i
        if j < n and (s[j].isalpha() or s[j] in "_$"):
            j += 1
            while j < n and (s[j].isalnum() or s[j] in "_$"):
                j += 1
        self.i = j
        return s[i:j]

    def parse_ident_expr(self):
        parts = [self.parse_ident()]
        while True:
            self.skip()
            if self.i < self.n and self.s[self.i] == ".":
                self.i += 1
                parts.append(self.parse_ident())
                continue
            if self.i < self.n and self.s[self.i] == "(":
                self.i += 1
                args = []
                while True:
                    self.skip()
                    if self.i < self.n and self.s[self.i] == ")":
                        self.i += 1
                        break
                    args.append(self.parse_value())
                    self.skip()
                    if self.i < self.n and self.s[self.i] == ",":
                        self.i += 1
                return {"$call": ".".join(parts), "args": args}
            break
        if len(parts) == 1:
            return {"$id": parts[0]}
        return {"$ref": parts}


def parse_field_list(raw: str) -> list:
    p = JSParser(raw)
    val = p.parse_value()
    if not isinstance(val, list):
        raise TypeError("expected array")
    return val


def split_webpack_modules(bundle: str) -> dict[str, str]:
    starts = list(re.finditer(r'"((?:\.\./)*proto/dist/generated/[^"]+\.js)"\(e,t,n\)\{', bundle))
    out = {}
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(bundle)
        out[_short(m.group(1))] = bundle[m.start() : end]
    return out


def proto_file_for_module(mod: str) -> str:
    return mod.replace("_pb.js", ".proto").replace("_connect.js", "_connect.proto")


def local_name(type_name: str) -> str:
    parts = type_name.split(".")
    if len(parts) >= 2 and parts[1][0].isdigit() is False and parts[0] in {"agent", "aiserver"}:
        return ".".join(parts[2:]) if len(parts) > 2 else parts[-1]
    return parts[-1]


def package_of(type_name: str) -> str:
    parts = type_name.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return parts[0]


class ModuleInfo:
    def __init__(self, name: str, body: str) -> None:
        self.name = name
        self.body = body
        self.exports: dict[str, str] = {}  # exportName -> local ident
        self.aliases: dict[str, str] = {}  # local ident -> imported module
        self.ident_type: dict[str, str] = {}  # local ident -> typeName (message or enum)
        self.messages: dict[str, dict] = {}  # typeName -> {fields, ident}
        self.enums: dict[str, list] = {}  # typeName -> [{no,name}]
        self.services: dict[str, dict] = {}

    def parse(self) -> None:
        body = self.body
        em = re.search(r"n\.d\(t,\{([^}]+)\}\)", body)
        if em:
            for exp, ident in re.findall(r"([^:,]+):\(\)=>[\s]*([A-Za-z0-9_$]+)", em.group(1)):
                self.exports[exp.strip()] = ident.strip()
        for ident, path in re.findall(rf'({IDENT})=n\("([^"]+)"\)', body):
            if "proto/dist/generated" in path or path == "@bufbuild/protobuf":
                self.aliases[ident] = _short(path)
        for ident, tname in re.findall(rf'({IDENT})\.typeName="([^"]+)"', body):
            self.ident_type[ident] = tname
        for ident, raw in re.findall(
            rf'({IDENT})\.fields=\w+\.proto3\.util\.newFieldList\(\(\(\)=>(\[.*?\])\)\)',
            body,
        ):
            tname2 = self.ident_type.get(ident)
            if not tname2:
                continue
            try:
                fields = parse_field_list(raw)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"{self.name} {tname2} field parse failed: {exc}") from exc
            self.messages[tname2] = {"ident": ident, "fields": fields}
        # empty field lists
        for ident, tname in self.ident_type.items():
            if tname in self.messages:
                continue
            if re.search(rf"{re.escape(ident)}\.fields=\w+\.proto3\.util\.newFieldList\(\(\(\)=>\[\]\)\)", body):
                self.messages[tname] = {"ident": ident, "fields": []}
        for tname, raw in re.findall(
            r'setEnumType\(\w+,"([^"]+)",(\[.*?\])\)',
            body,
        ):
            try:
                values = parse_field_list(raw)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"{self.name} enum {tname} parse failed: {exc}") from exc
            self.enums[tname] = values
            # enum ident is first arg of setEnumType
        for ident, tname in re.findall(rf'setEnumType\(({IDENT}),"([^"]+)"', body):
            self.ident_type[ident] = tname
        for tname, raw in re.findall(r'typeName:"([^"]+)",methods:(\{.*?\})\}', body):
            try:
                methods = JSParser(raw).parse_object()
            except Exception:
                continue
            self.services[tname] = methods


def resolve_type_ref(val, mod: ModuleInfo, modules: dict[str, ModuleInfo]) -> dict:
    if isinstance(val, int):
        proto = SCALAR_PROTO.get(val)
        if proto is None:
            raise ValueError(f"unknown scalar {val}")
        return {"kind": "scalar", "type": proto, "code": val}
    if isinstance(val, dict) and "$id" in val:
        ident = val["$id"]
        tname = mod.ident_type.get(ident)
        if not tname:
            raise KeyError(f"{mod.name}: unresolved ident {ident}")
        kind = "enum" if tname in mod.enums else "message"
        # enum might live in this module
        if tname in modules.get(mod.name, mod).enums or tname in mod.enums:
            kind = "enum" if tname in mod.enums else kind
        return {"kind": kind, "type": tname}
    if isinstance(val, dict) and "$ref" in val:
        parts = val["$ref"]
        if len(parts) == 2:
            alias, exp = parts
            other_name = mod.aliases.get(alias)
            if other_name == "@bufbuild/protobuf" or (other_name and other_name.endswith("protobuf")):
                wkt = WKT.get(exp)
                if not wkt:
                    raise KeyError(f"{mod.name}: unknown WKT {exp}")
                return {"kind": "message", "type": wkt, "file": "google/protobuf/" + wkt.split(".")[-1].lower() + ".proto"}
            if not other_name:
                raise KeyError(f"{mod.name}: unknown alias {alias} for {parts}")
            other = modules[other_name]
            ident = other.exports.get(exp)
            if not ident:
                raise KeyError(f"{mod.name}: {other_name} has no export {exp}")
            tname = other.ident_type.get(ident)
            if not tname:
                raise KeyError(f"{mod.name}: {other_name}.{exp} ident {ident} has no type")
            kind = "enum" if tname in other.enums else "message"
            return {"kind": kind, "type": tname, "file": proto_file_for_module(other_name)}
        raise KeyError(f"bad ref {parts}")
    if isinstance(val, dict) and val.get("$call", "").endswith("getEnumType"):
        args = val.get("args") or []
        if not args:
            raise KeyError("getEnumType with no args")
        return resolve_type_ref(args[0], mod, modules) | {"kind": "enum"}
    raise KeyError(f"unresolved T {val!r} in {mod.name}")


def normalize_fields(fields: list, mod: ModuleInfo, modules: dict[str, ModuleInfo]) -> list[dict]:
    out = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        item = {
            "no": f.get("no"),
            "name": f.get("name"),
            "opt": bool(f.get("opt")),
            "repeated": bool(f.get("repeated")),
            "oneof": f.get("oneof"),
        }
        kind = f.get("kind")
        if kind == "scalar":
            item["type"] = resolve_type_ref(f.get("T"), mod, modules)
        elif kind in {"message", "enum"}:
            item["type"] = resolve_type_ref(f.get("T"), mod, modules)
            if kind == "enum":
                item["type"]["kind"] = "enum"
        elif kind == "map":
            k = resolve_type_ref(f.get("K"), mod, modules)
            vraw = f.get("V")
            if isinstance(vraw, dict) and "kind" in vraw:
                inner = vraw.get("T")
                v = resolve_type_ref(inner, mod, modules)
                if vraw.get("kind") == "enum":
                    v["kind"] = "enum"
            else:
                v = resolve_type_ref(vraw, mod, modules)
            item["type"] = {"kind": "map", "key": k, "value": v}
        else:
            item["type"] = {"kind": "unknown", "raw": str(kind)}
        out.append(item)
    return out


def type_to_proto(t: dict, current_pkg: str) -> str:
    kind = t["kind"]
    if kind == "scalar":
        return t["type"]
    if kind in {"message", "enum"}:
        name = t["type"]
        pkg = package_of(name)
        local = local_name(name)
        if pkg == current_pkg:
            return local
        return f".{name}"
    if kind == "map":
        k = type_to_proto(t["key"], current_pkg)
        v = type_to_proto(t["value"], current_pkg)
        return f"map<{k}, {v}>"
    return "bytes"


def collect_imports(fields: list, current_file: str, current_pkg: str) -> set[str]:
    imps: set[str] = set()

    def walk(t: dict) -> None:
        if t["kind"] in {"message", "enum"}:
            f = t.get("file")
            if f and f != current_file:
                imps.add(f)
            elif t["kind"] in {"message", "enum"}:
                # same package other file: file filled at resolve time for cross-module only
                pass
        elif t["kind"] == "map":
            walk(t["key"])
            walk(t["value"])

    for f in fields:
        walk(f["type"])
    return imps


def emit_proto_file(
    mod_name: str,
    messages: dict[str, list],
    enums: dict[str, list],
    services: dict[str, dict],
    type_file: dict[str, str],
) -> str:
    pkg = None
    for tname in list(messages) + list(enums):
        pkg = package_of(tname)
        break
    if pkg is None:
        if "agent/" in mod_name:
            pkg = "agent.v1"
        else:
            pkg = "aiserver.v1"
    current_file = proto_file_for_module(mod_name)

    # nest types under parent message when typeName has extra components
    top_messages: dict[str, dict] = defaultdict(lambda: {"fields": [], "nested_msgs": {}, "nested_enums": {}, "oneofs": defaultdict(list)})
    top_enums: dict[str, list] = {}

    for tname, fields in messages.items():
        local = local_name(tname)
        parts = local.split(".")
        if len(parts) == 1:
            top_messages[parts[0]]["fields"] = fields
            top_messages[parts[0]]["type_name"] = tname
        elif len(parts) == 2:
            parent, child = parts
            top_messages[parent]["nested_msgs"][child] = fields
            top_messages[parent]["type_name"] = f"{package_of(tname)}.{parent}"
        else:
            # deeper: put remaining as nested message name with underscores avoided — nest 2 levels
            parent, *rest = parts
            child = "_".join(rest) if False else rest[0]
            # keep as nested of parent using last two
            if len(parts) == 3:
                top_messages[parts[0]]["nested_msgs"].setdefault(parts[1], [])
                # store as sibling nested under parent.child via synthetic
                # Use fully nested: parent { child { grandchild } } is hard; flatten extra as Parent_Child.
                key = ".".join(parts[:-1])
                # We'll emit as nested inside the first parent using remaining dotted as a nested message
            top_messages[parts[0]]["nested_msgs"][".".join(parts[1:])] = fields

    for tname, values in enums.items():
        local = local_name(tname)
        parts = local.split(".")
        if len(parts) == 1:
            top_enums[parts[0]] = values
        else:
            parent = parts[0]
            child = ".".join(parts[1:])
            top_messages[parent]["nested_enums"][child] = values
            top_messages[parent]["type_name"] = f"{package_of(tname)}.{parent}"

    imports: set[str] = set()
    for fields in messages.values():
        imports |= collect_imports(fields, current_file, pkg)
    # also import files of same-package types not in this file
    for fields in messages.values():
        for f in fields:
            def consider(t: dict) -> None:
                if t["kind"] in {"message", "enum"}:
                    tn = t["type"]
                    owner = type_file.get(tn)
                    if owner and owner != current_file:
                        imports.add(owner)
                elif t["kind"] == "map":
                    consider(t["key"])
                    consider(t["value"])
            consider(f["type"])

    lines = [
        '// Code generated by scripts/extract_protos.py from @cursor/sdk@1.0.31. DO NOT EDIT.',
        'syntax = "proto3";',
        f"package {pkg};",
        "",
    ]
    for imp in sorted(imports):
        lines.append(f'import "{imp}";')
    if imports:
        lines.append("")

    def emit_enum(name: str, values: list, indent: int) -> None:
        pad = "  " * indent
        lines.append(f"{pad}enum {name} {{")
        seen = set()
        for v in values:
            vn = v.get("name") or f"VALUE_{v.get('no')}"
            no = v.get("no", 0)
            if vn in seen:
                continue
            seen.add(vn)
            lines.append(f"{pad}  {vn} = {no};")
        lines.append(f"{pad}}}")

    def emit_fields(fields: list, indent: int) -> None:
        pad = "  " * indent
        oneofs: dict[str, list] = defaultdict(list)
        regular = []
        for f in fields:
            if f.get("oneof"):
                oneofs[f["oneof"]].append(f)
            else:
                regular.append(f)
        for f in regular:
            t = type_to_proto(f["type"], pkg)
            label = ""
            if f["repeated"] and f["type"]["kind"] != "map":
                label = "repeated "
            elif f["opt"] and f["type"]["kind"] == "scalar":
                label = "optional "
            elif f["opt"] and f["type"]["kind"] == "enum":
                label = "optional "
            lines.append(f"{pad}{label}{t} {f['name']} = {f['no']};")
        for oname, ofs in oneofs.items():
            lines.append(f"{pad}oneof {oname} {{")
            for f in ofs:
                t = type_to_proto(f["type"], pkg)
                lines.append(f"{pad}  {t} {f['name']} = {f['no']};")
            lines.append(f"{pad}}}")

    emitted_parents = set()
    for ename, values in sorted(top_enums.items()):
        emit_enum(ename, values, 0)
        lines.append("")

    for mname, info in sorted(top_messages.items()):
        if mname in top_enums and not info.get("fields") and not info.get("nested_msgs") and not info.get("nested_enums"):
            continue
        lines.append(f"message {mname.split('.')[0] if False else mname} {{")
        # nested enums first
        for nname, values in sorted(info["nested_enums"].items()):
            if "." in nname:
                # flatten remaining: emit as nested message? keep as enum with last component inside a nested dummy
                parts = nname.split(".")
                # only handle Parent.Child enums (len 1 after parent strip)
                emit_enum(parts[-1], values, 1)
            else:
                emit_enum(nname, values, 1)
        nested_groups: dict[str, dict] = defaultdict(lambda: {"fields": [], "enums": {}, "msgs": {}})
        leftover_nested = {}
        for nname, nfields in info["nested_msgs"].items():
            if "." in nname:
                leftover_nested[nname] = nfields
            else:
                nested_groups[nname]["fields"] = nfields
        for nname, nfields in leftover_nested.items():
            parts = nname.split(".")
            if len(parts) == 2:
                nested_groups[parts[0]]["msgs"][parts[1]] = nfields
            else:
                nested_groups[nname.replace(".", "_")]["fields"] = nfields
        for nname, ninfo in sorted(nested_groups.items()):
            lines.append(f"  message {nname} {{")
            for cn, cf in sorted(ninfo["msgs"].items()):
                lines.append(f"    message {cn} {{")
                emit_fields(cf, 3)
                lines.append("    }")
            emit_fields(ninfo["fields"], 2)
            lines.append("  }")
        emit_fields(info["fields"], 1)
        lines.append("}")
        lines.append("")
        emitted_parents.add(mname)

    # services
    METHOD_KIND = {
        0: "unary",
        1: "server_streaming",
        2: "client_streaming",
        3: "bidi_streaming",
        "Unary": "unary",
        "ServerStreaming": "server_streaming",
        "ClientStreaming": "client_streaming",
        "BiDiStreaming": "bidi_streaming",
    }
    # services often live in connect modules; skip incomplete ones here

    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    bundle = repo / "node_modules/@cursor/sdk/dist/esm/index.js"
    if not bundle.exists():
        print(f"missing {bundle}", file=sys.stderr)
        return 1
    text = bundle.read_text(encoding="utf-8", errors="replace")
    raw_modules = split_webpack_modules(text)
    modules: dict[str, ModuleInfo] = {}
    for name, body in raw_modules.items():
        if name in SKIP_MODULES:
            continue
        info = ModuleInfo(name, body)
        info.parse()
        modules[name] = info

    resolved_messages: dict[str, dict] = {}  # typeName -> {file, fields}
    resolved_enums: dict[str, dict] = {}
    type_file: dict[str, str] = {}
    errors = []

    for name, info in modules.items():
        pfile = proto_file_for_module(name)
        for tname in info.messages:
            type_file[tname] = pfile
        for tname in info.enums:
            type_file[tname] = pfile

    for name, info in modules.items():
        for tname, meta in info.messages.items():
            try:
                fields = normalize_fields(meta["fields"], info, modules)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name} {tname}: {exc}")
                continue
            resolved_messages[tname] = {"file": proto_file_for_module(name), "fields": fields, "module": name}
        for tname, values in info.enums.items():
            resolved_enums[tname] = {"file": proto_file_for_module(name), "values": values, "module": name}

    out_dir = Path(__file__).resolve().parents[1] / "proto"
    if out_dir.exists():
        for old in out_dir.rglob("*.proto"):
            old.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    by_module: dict[str, dict] = defaultdict(lambda: {"messages": {}, "enums": {}})
    for tname, meta in resolved_messages.items():
        by_module[meta["module"]]["messages"][tname] = meta["fields"]
    for tname, meta in resolved_enums.items():
        by_module[meta["module"]]["enums"][tname] = meta["values"]

    written = []
    for mod_name, payload in sorted(by_module.items()):
        if mod_name.endswith("_connect.js"):
            continue
        text_out = emit_proto_file(mod_name, payload["messages"], payload["enums"], {}, type_file)
        dest = out_dir / proto_file_for_module(mod_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text_out, encoding="utf-8")
        written.append(str(dest.relative_to(out_dir)))

    # services as JSON (connect modules + inline)
    services = {}
    for info in modules.values():
        services.update(info.services)
    # AgentService / BidiService are reliable from string search even if parse failed
    desc = {
        "sdkVersion": "1.0.31",
        "messages": {
            k: {"file": v["file"], "fields": v["fields"]} for k, v in resolved_messages.items()
        },
        "enums": {
            k: {"file": v["file"], "values": v["values"]} for k, v in resolved_enums.items()
        },
        "services": {
            "agent.v1.AgentService": {
                "methods": {
                    "Run": {"kind": "bidi_streaming", "input": "agent.v1.AgentClientMessage", "output": "agent.v1.AgentServerMessage"},
                    "RunSSE": {"kind": "server_streaming", "input": "aiserver.v1.BidiRequestId", "output": "agent.v1.AgentServerMessage"},
                    "RunPoll": {"kind": "server_streaming", "input": "aiserver.v1.BidiPollRequest", "output": "aiserver.v1.BidiPollResponse"},
                }
            },
            "aiserver.v1.BidiService": {
                "methods": {
                    "BidiAppend": {"kind": "unary", "input": "aiserver.v1.BidiAppendRequest", "output": "aiserver.v1.BidiAppendResponse"},
                }
            },
        },
        "errors": errors,
    }
    desc_path = out_dir / "descriptors.json"
    desc_path.write_text(json.dumps(desc, indent=2), encoding="utf-8")
    readme = out_dir / "README.md"
    readme.write_text(
        "# Extracted Cursor agent protobufs\n\n"
        "Generated from `@cursor/sdk@1.0.31` webpack descriptors (`scripts/extract_protos.py`).\n"
        "These are reconstructed `agent.v1` / `aiserver.v1` schemas for native clients that skip `cursor-sdk-bridge`.\n",
        encoding="utf-8",
    )
    print(f"wrote {len(written)} proto files, {len(resolved_messages)} messages, {len(resolved_enums)} enums")
    print(f"errors: {len(errors)}")
    for e in errors[:20]:
        print(" ", e)
    return 0 if len(errors) < 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
