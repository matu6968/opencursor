from __future__ import annotations

import re
import threading
from typing import Any

REDIRECT_NODE_TYPES = frozenset(
    {
        "file_redirect",
        "heredoc_redirect",
        "herestring_redirect",
        "heredoc",
        "here_string",
        "redirect",
        "redirection",
    }
)
REDIRECT_OPERATORS = frozenset({"<", ">", ">>", ">|", "<>", "<&", ">&", "&>", "&>>"})
_INPUT_RE = re.compile(r"(^|\s)[0-9]*<<?<")
_OUTPUT_GTGT_RE = re.compile(r"(^|\s)[0-9]*>>")
_OUTPUT_GT_RE = re.compile(r"(^|\s)[0-9]*>(\||&)?")
_OUTPUT_AMP_RE = re.compile(r"(^|\s)&>")
_OUTPUT_FD_RE = re.compile(r"(^|\s)[0-9]+>&[0-9]+")
_HEX_BYTE_RE = re.compile(r"^[0-9a-fA-F]{1,2}")
_HEX_U_RE = re.compile(r"^[0-9a-fA-F]{1,4}")
_HEX_U32_RE = re.compile(r"^[0-9a-fA-F]{1,8}")
_OCTAL_RE = re.compile(r"^[0-7]{1,3}")
_ANSI_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
_FAIL_CLOSED = (
    "Denied: this command could not be conclusively analyzed against your team's "
    "administrator command denylist, so it was blocked (fail-closed) and was not executed. "
    "It cannot be approved from this conversation; only a user can run it manually outside "
    "the agent. You may continue working on the task."
)

_lock = threading.Lock()
_parser: Any = None
_parser_failed = False


def _text(node: Any) -> str:
    raw = getattr(node, "text", None)
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _is_ws(ch: str) -> bool:
    return ch in " \t\n\r\u00a0\u200b\u200c\u200d\ufeff"


def collapse_ws(value: str) -> str:
    out: list[str] = []
    pending = False
    for ch in value:
        if _is_ws(ch):
            pending = True
            continue
        if pending and out:
            out.append(" ")
        pending = False
        out.append(ch)
    return "".join(out)


def _stars_only(value: str) -> bool:
    return bool(value) and all(ch == "*" for ch in value)


def _colon_rule(rule: str) -> tuple[str, str] | None:
    idx = rule.find(":")
    if idx <= 0:
        return None
    executable = rule[:idx].strip()
    if any(_is_ws(ch) for ch in executable):
        return None
    return executable, rule[idx + 1 :].strip()


def _glob_match(pattern: str, value: str) -> bool:
    trimmed = pattern.strip()
    escaped = re.escape(trimmed).replace(r"\*", ".*")
    try:
        return re.fullmatch(escaped, value) is not None
    except re.error:
        return trimmed == value


def unescape_shell_word(value: str) -> str:
    out: list[str] = []
    mode = "plain"
    i = 0
    while i < len(value):
        ch = value[i]
        if mode == "plain":
            if ch == "$" and i + 1 < len(value) and value[i + 1] == "'":
                mode = "ansi"
                i += 2
                continue
            if ch == "$" and i + 1 < len(value) and value[i + 1] == '"':
                mode = "double"
                i += 2
                continue
            if ch == "'":
                mode = "single"
                i += 1
                continue
            if ch == '"':
                mode = "double"
                i += 1
                continue
            if ch == "\\" and i + 1 < len(value):
                if value[i + 1] != "\n":
                    out.append(value[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        if mode == "single":
            if ch == "'":
                mode = "plain"
            else:
                out.append(ch)
            i += 1
            continue
        if mode == "double":
            if ch == '"':
                mode = "plain"
                i += 1
                continue
            if ch == "\\" and i + 1 < len(value):
                nxt = value[i + 1]
                if nxt in {'$', "`", '"', "\\", "\n"}:
                    if nxt != "\n":
                        out.append(nxt)
                    i += 2
                    continue
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            mode = "plain"
            i += 1
            continue
        if ch != "\\" or i + 1 >= len(value):
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        mapped = _ANSI_ESCAPES.get(nxt)
        if mapped is not None:
            out.append(mapped)
            i += 2
            continue
        if nxt == "x":
            hex_digits = _HEX_BYTE_RE.match(value[i + 2 :])
            digits = hex_digits.group(0) if hex_digits else None
            base, prefix = 16, 2
            unicode_cp = False
        elif nxt == "u":
            hex_digits = _HEX_U_RE.match(value[i + 2 :])
            digits = hex_digits.group(0) if hex_digits else None
            base, prefix = 16, 2
            unicode_cp = True
        elif nxt == "U":
            hex_digits = _HEX_U32_RE.match(value[i + 2 :])
            digits = hex_digits.group(0) if hex_digits else None
            base, prefix = 16, 2
            unicode_cp = True
        else:
            octal = _OCTAL_RE.match(value[i + 1 :])
            digits = octal.group(0) if octal else None
            base, prefix = 8, 1
            unicode_cp = False
        if digits:
            code = int(digits, base)
            if not unicode_cp or code <= 0x10FFFF:
                out.append(chr(code) if unicode_cp else chr(code & 0xFFFF))
                i += len(digits) + prefix
                continue
        out.append(nxt)
        i += 2
    return collapse_ws("".join(out))


def _rule_invalid(rule: str) -> str | None:
    trimmed = collapse_ws(rule).strip()
    if not trimmed:
        return "Rule cannot be empty"
    if len(trimmed) > 512:
        return "Rule cannot exceed 512 characters"
    if _stars_only(trimmed):
        return "Rule cannot match every command; be more specific than wildcards alone"
    if trimmed.startswith(":"):
        return "Colon rules need an executable before `:` (e.g. `aws:*s3 rm*`)"
    colon = _colon_rule(trimmed)
    if colon is not None:
        executable, args = colon
        if _stars_only(executable) and (not args or _stars_only(args)):
            return "Rule cannot match every command; narrow the executable or argument pattern"
    return None


def _rule_matches_form(rule: str, command: str) -> bool:
    colon = _colon_rule(rule)
    if colon is not None:
        executable, args = colon
        space = command.find(" ")
        name = command if space < 0 else command[:space]
        rest = "" if space < 0 else command[space + 1 :].strip()
        if _glob_match(executable, name) and _glob_match(args, rest):
            return True
    if "*" in rule:
        return _glob_match(rule, command)
    return command == rule or command.startswith(f"{rule} ")


def _rule_matches_command(rule: str, command: str) -> bool:
    normalized = collapse_ws(rule).strip()
    if not normalized or _rule_invalid(normalized) is not None:
        return False
    forms = [command.strip(), collapse_ws(command).strip(), unescape_shell_word(command).strip()]
    return any(_rule_matches_form(normalized, form) for form in forms)


def denylist_reason(command: str, structured: dict[str, Any], blocked: list[str] | None) -> str | None:
    rules = [item.strip() for item in (blocked or []) if str(item).strip()]
    if not rules:
        return None
    if structured.get("parsing_failed") or not structured.get("executable_commands"):
        return _FAIL_CLOSED
    forms = [command, *[item.get("full_text") or "" for item in structured.get("executable_commands") or []]]
    seen: set[str] = set()
    unique: list[str] = []
    for item in forms:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    candidates = [item.strip() for item in unique if item.strip()] or [""]
    for rule in rules:
        if any(_rule_matches_command(rule, candidate) for candidate in candidates):
            return (
                f"Denied: this command was blocked by administrator policy (denylist rule: {rule}) "
                "and was not executed. It cannot be approved from this conversation; only a user can "
                "run it manually outside the agent. You may continue working on the task."
            )
    return None


def _get_parser() -> Any | None:
    global _parser, _parser_failed
    if _parser_failed:
        return None
    if _parser is not None:
        return _parser
    try:
        import tree_sitter_bash as tsbash
        from tree_sitter import Language, Parser
    except ImportError:
        _parser_failed = True
        return None
    _parser = Parser(Language(tsbash.language()))
    return _parser


def _fd_number(node: Any | None) -> int | None:
    if node is None:
        return None
    text = _text(node)
    if text.isdigit():
        return int(text)
    return None


def _is_safe_fd_target(kind: str | None) -> bool:
    return kind in ("original-stdout", "original-stderr", "dev-null")


def _literal_redirect_target(node: Any | None) -> str | None:
    if node is None:
        return None
    if node.type == "word":
        return _text(node) if not node.named_children else None
    if node.type == "number":
        return _text(node)
    if node.type == "raw_string":
        text = _text(node)
        if len(text) < 2:
            return None
        return text[1:-1]
    if node.type == "string":
        named = list(node.named_children)
        if not named or any(child.type != "string_content" for child in named):
            return None
        return "".join(_text(child) for child in named)
    return None


def _is_dev_null_node(node: Any | None) -> bool:
    if node is None:
        return False
    if node.type == "word":
        return _text(node) == "/dev/null"
    if node.type == "raw_string":
        return _text(node) == "'/dev/null'"
    if node.type == "string":
        named = list(node.named_children)
        return len(named) == 1 and named[0].type == "string_content" and _text(named[0]) == "/dev/null"
    return False


def _split_redirect(node: Any) -> tuple[str, Any | None] | None:
    children = list(node.children)
    idx = next((i for i, child in enumerate(children) if child.type in REDIRECT_OPERATORS), -1)
    if idx < 0:
        return None
    operator = children[idx].type
    target = next((child for child in children[idx + 1 :] if child.type != "comment"), None)
    return operator, target


def _destination_fds(node: Any, operator: str) -> list[int]:
    if operator in ("&>", "&>>"):
        return [1, 2]
    fd_node = next((child for child in node.children if child.type == "file_descriptor"), None)
    number = _fd_number(fd_node)
    if number is not None:
        return [number]
    if operator.startswith("<"):
        return [0]
    return [1]


def _inside_command_substitution(node: Any) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "command_substitution":
            grand = parent.parent
            if grand is None or grand.type != "string":
                return True
        parent = parent.parent
    return False


def _heredoc_command_name(node: Any) -> str | None:
    parent = node.parent
    if parent is None or parent.type != "redirected_statement":
        return None
    command = next((child for child in parent.children if child.type == "command"), None)
    name = command.child_by_field_name("name") if command is not None else None
    return _text(name) if name is not None else None


def _legacy_redirect_flags(node: Any, legacy: dict[str, Any]) -> None:
    text = _text(node)
    if _INPUT_RE.search(text):
        legacy["has_input_redirect"] = True
    if (
        _OUTPUT_GTGT_RE.search(text)
        or _OUTPUT_GT_RE.search(text)
        or _OUTPUT_AMP_RE.search(text)
        or _OUTPUT_FD_RE.search(text)
    ):
        legacy["has_output_redirect"] = True
    if node.type in ("heredoc_redirect", "herestring_redirect"):
        legacy["has_input_redirect"] = True


def parse_shell_command(command: str) -> dict[str, Any]:
    failed = {
        "legacy": {"simple_commands": [], "has_input_redirect": False, "has_output_redirect": False},
        "structured": {
            "parsing_failed": True,
            "executable_commands": [],
            "has_redirects": False,
            "has_command_substitution": False,
            "redirects": [],
        },
    }
    with _lock:
        parser = _get_parser()
        if parser is None:
            return failed
        try:
            tree = parser.parse(command.encode("utf-8"))
        except Exception:
            return failed
        root = tree.root_node

    seen: set[int] = set()
    saw_redirect = False
    all_dev_null = True
    legacy: dict[str, Any] = {
        "simple_commands": [],
        "has_input_redirect": False,
        "has_output_redirect": False,
    }
    structured: dict[str, Any] = {
        "parsing_failed": False,
        "executable_commands": [],
        "has_redirects": False,
        "has_command_substitution": False,
        "redirects": [],
    }

    def mark_redirect(allowlisted: bool) -> None:
        nonlocal saw_redirect, all_dev_null
        saw_redirect = True
        structured["has_redirects"] = True
        all_dev_null = all_dev_null and allowlisted

    def fresh_fds() -> dict[int, str]:
        return {0: "original-stdin", 1: "original-stdout", 2: "original-stderr"}

    def apply_file_redirect(node: Any, fds: dict[int, str]) -> bool:
        split = _split_redirect(node)
        if split is None:
            return False
        operator, target = split
        dest = _destination_fds(node, operator)
        target_text = _literal_redirect_target(target)
        rec: dict[str, Any] = {
            "operator": operator,
            "destination_fds": dest,
            "target_node_type": target.type if target is not None else "",
        }
        if target_text is not None:
            rec["target_text"] = target_text
        structured["redirects"].append(rec)
        if operator == ">&":
            src = _fd_number(target)
            kind = fds.get(src) if src is not None else None
            if not _is_safe_fd_target(kind):
                for fd in dest:
                    fds[fd] = "unsafe"
                return False
            for fd in dest:
                fds[fd] = kind  # type: ignore[assignment]
            return True
        if operator == "<&":
            src = _fd_number(target)
            kind = fds.get(src) if src is not None else None
            if kind != "dev-null":
                for fd in dest:
                    fds[fd] = "unsafe"
                return False
            for fd in dest:
                fds[fd] = kind  # type: ignore[assignment]
            return True
        if not _is_dev_null_node(target):
            for fd in dest:
                fds[fd] = "unsafe"
            return False
        for fd in dest:
            fds[fd] = "dev-null"
        return True

    def handle_redirect(node: Any, fds: dict[int, str]) -> None:
        if node.id in seen:
            return
        seen.add(node.id)
        _legacy_redirect_flags(node, legacy)
        if node.type == "heredoc_redirect":
            name = _heredoc_command_name(node)
            safe = name is not None and not _inside_command_substitution(node)
            rec: dict[str, Any] = {
                "operator": "<<" if safe else "",
                "destination_fds": [0] if safe else [],
                "target_node_type": node.type,
            }
            if safe:
                rec["target_text"] = name
            structured["redirects"].append(rec)
            mark_redirect(safe)
            return
        if node.type != "file_redirect":
            structured["redirects"].append(
                {"operator": "", "destination_fds": [], "target_node_type": node.type}
            )
            mark_redirect(False)
            return
        mark_redirect(apply_file_redirect(node, fds))

    def walk(node: Any) -> None:
        fds = fresh_fds()
        for child in node.children:
            if child.type in REDIRECT_NODE_TYPES:
                handle_redirect(child, fds)
        if node.type in REDIRECT_NODE_TYPES and node.id not in seen:
            handle_redirect(node, fresh_fds())
        if node.type == "command":
            name = node.child_by_field_name("name")
            if name is None:
                structured["parsing_failed"] = True
            else:
                name_text = _text(name)
                legacy["simple_commands"].append(name_text)
                args = []
                full = name_text
                for arg in node.children_by_field_name("argument"):
                    args.append({"type": arg.type, "value": _text(arg)})
                    full += f" {_text(arg)}"
                structured["executable_commands"].append(
                    {"name": name_text, "args": args, "full_text": full}
                )
        if node.type in ("command_name", "simple_command"):
            text = _text(node)
            if node.type != "command_name" or text not in legacy["simple_commands"]:
                if node.type == "simple_command":
                    named = list(node.named_children)
                    first = named[0] if named else None
                    if first is not None and first.type == "command_name":
                        first_text = _text(first)
                        if first_text not in legacy["simple_commands"]:
                            legacy["simple_commands"].append(first_text)
                    else:
                        legacy["simple_commands"].append(text)
                else:
                    legacy["simple_commands"].append(text)
        if node.type in ("command_substitution", "process_substitution"):
            structured["has_command_substitution"] = True
        for child in node.children:
            walk(child)

    walk(root)
    if saw_redirect:
        structured["all_redirects_are_dev_null"] = all_dev_null
    return {"legacy": legacy, "structured": structured}


def parsing_result_proto(structured: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "parsing_failed": bool(structured.get("parsing_failed")),
        "executable_commands": [
            {
                "name": item.get("name") or "",
                "args": [
                    {"type": arg.get("type") or "", "value": arg.get("value") or ""}
                    for arg in item.get("args") or []
                ],
                "full_text": item.get("full_text") or "",
            }
            for item in structured.get("executable_commands") or []
        ],
        "has_redirects": bool(structured.get("has_redirects")),
        "has_command_substitution": bool(structured.get("has_command_substitution")),
        "redirects": [
            {
                "operator": item.get("operator") or "",
                "destination_fds": list(item.get("destination_fds") or []),
                "target_node_type": item.get("target_node_type") or "",
                **({"target_text": item["target_text"]} if item.get("target_text") is not None else {}),
            }
            for item in structured.get("redirects") or []
        ],
    }
    if "all_redirects_are_dev_null" in structured:
        out["all_redirects_are_dev_null"] = bool(structured["all_redirects_are_dev_null"])
    return out
