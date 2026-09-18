from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from opencursor import errors
from opencursor.types import AgentOptions

UNSUPPORTED_MESSAGE = (
    "Local SDK sandboxing was requested, but sandboxing is not supported in this environment. "
    "Disable `local.sandboxOptions.enabled` or remove `~/.cursor/sandbox.json` to run without sandboxing."
)

LINUX_SESSION_ENV_STRIP = (
    "SSH_AUTH_SOCK",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
)

PREFLIGHT_TIMEOUT_S = 15.0
POLICY_CLEANUP_INTERVAL_S = 900.0
POLICY_MAX_AGE_S = 3600.0

TYPE_TO_PROTO = {
    "insecure_none": 1,
    "workspace_readwrite": 2,
    "workspace_readonly": 3,
}

PROTO_TO_TYPE = {
    0: "insecure_none",
    1: "insecure_none",
    2: "workspace_readwrite",
    3: "workspace_readonly",
    "0": "insecure_none",
    "1": "insecure_none",
    "2": "workspace_readwrite",
    "3": "workspace_readonly",
    "TYPE_UNSPECIFIED": "insecure_none",
    "TYPE_INSECURE_NONE": "insecure_none",
    "TYPE_WORKSPACE_READWRITE": "workspace_readwrite",
    "TYPE_WORKSPACE_READONLY": "workspace_readonly",
    "insecure_none": "insecure_none",
    "workspace_readwrite": "workspace_readwrite",
    "workspace_readonly": "workspace_readonly",
}

READ_BOUNDARY_TO_NAME = {
    0: None,
    1: "system",
    2: "workspace",
    "0": None,
    "1": "system",
    "2": "workspace",
    "READ_BOUNDARY_MODE_UNSPECIFIED": None,
    "READ_BOUNDARY_MODE_SYSTEM": "system",
    "READ_BOUNDARY_MODE_WORKSPACE": "workspace",
    "system": "system",
    "workspace": "workspace",
}

HARDCODED_READ_PATHS: tuple[tuple[str, str, str], ...] = (
    ("linux", "global", "/bin"),
    ("linux", "global", "/sbin"),
    ("linux", "global", "/usr/bin"),
    ("linux", "global", "/usr/sbin"),
    ("linux", "global", "/usr/local/bin"),
    ("linux", "global", "/lib"),
    ("linux", "global", "/lib64"),
    ("linux", "global", "/usr/lib"),
    ("linux", "global", "/usr/lib64"),
    ("linux", "global", "/usr/local/lib"),
    ("linux", "global", "/usr/libexec"),
    ("linux", "global", "/usr/share"),
    ("linux", "global", "/etc/ld.so.cache"),
    ("linux", "global", "/etc/ld.so.conf"),
    ("linux", "global", "/etc/ld.so.conf.d"),
    ("linux", "global", "/etc/ssl/certs"),
    ("linux", "global", "/etc/ssl/openssl.cnf"),
    ("linux", "global", "/etc/ssl/cert.pem"),
    ("linux", "global", "/etc/ssl/ca-bundle.pem"),
    ("linux", "global", "/etc/ssl/certs/ca-certificates.crt"),
    ("linux", "global", "/etc/pki/tls/certs"),
    ("linux", "global", "/etc/pki/tls/openssl.cnf"),
    ("linux", "global", "/etc/pki/ca-trust/extracted"),
    ("linux", "global", "/etc/resolv.conf"),
    ("linux", "global", "/etc/hosts"),
    ("linux", "global", "/etc/nsswitch.conf"),
    ("linux", "global", "/etc/gai.conf"),
    ("linux", "global", "/etc/alternatives"),
    ("linux", "global", "/etc/profile"),
    ("linux", "global", "/etc/bash.bashrc"),
    ("linux", "global", "/etc/zsh/zshenv"),
    ("linux", "global", "/etc/zsh/zprofile"),
    ("linux", "global", "/etc/zsh/zshrc"),
    ("linux", "global", "/etc/zsh/zlogin"),
    ("linux", "global", "/etc/gitconfig"),
    ("linux", "home", ".bashrc"),
    ("linux", "home", ".bash_profile"),
    ("linux", "home", ".profile"),
    ("linux", "home", ".zshenv"),
    ("linux", "home", ".zprofile"),
    ("linux", "home", ".zshrc"),
    ("linux", "home", ".zlogin"),
    ("linux", "home", ".gitconfig"),
    ("linux", "bundled", "ripgrep"),
    ("darwin", "global", "/bin"),
    ("darwin", "global", "/usr/bin"),
    ("darwin", "global", "/usr/lib"),
    ("darwin", "global", "/usr/libexec"),
    ("darwin", "global", "/usr/share"),
    ("darwin", "global", "/System/Library"),
    ("darwin", "global", "/System/Cryptexes"),
    ("darwin", "global", "/Library/Apple"),
    ("darwin", "global", "/private/etc/ssl/cert.pem"),
    ("darwin", "global", "/private/etc/ssl/certs"),
    ("darwin", "global", "/private/etc/hosts"),
    ("darwin", "global", "/private/etc/resolv.conf"),
    ("darwin", "global", "/etc/profile"),
    ("darwin", "global", "/etc/bashrc"),
    ("darwin", "global", "/etc/zshenv"),
    ("darwin", "global", "/etc/zprofile"),
    ("darwin", "global", "/etc/zshrc"),
    ("darwin", "global", "/etc/zlogin"),
    ("darwin", "global", "/etc/gitconfig"),
    ("darwin", "global", "/dev/null"),
    ("darwin", "global", "/dev/zero"),
    ("darwin", "global", "/dev/random"),
    ("darwin", "global", "/dev/urandom"),
    ("darwin", "global", "/dev/tty"),
    ("darwin", "home", ".bashrc"),
    ("darwin", "home", ".bash_profile"),
    ("darwin", "home", ".profile"),
    ("darwin", "home", ".zshenv"),
    ("darwin", "home", ".zprofile"),
    ("darwin", "home", ".zshrc"),
    ("darwin", "home", ".zlogin"),
    ("darwin", "home", ".gitconfig"),
    ("darwin", "bundled", "ripgrep"),
)

_preflight_cache: dict[tuple[str, str], tuple[bool, str | None]] = {}
_last_policy_cleanup = 0.0


class SandboxUnsupportedError(RuntimeError):
    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.name = "SandboxUnsupportedError"


@dataclass
class SandboxRuntime:
    requested: bool
    supported: bool
    binary: Path | None
    workspace: Path
    default_type: str
    last_failure: str | None = None

    @property
    def enabled(self) -> bool:
        return self.requested and self.supported and self.default_type != "insecure_none"

    def raise_if_requested_unsupported(self) -> None:
        if self.requested and not self.supported:
            raise errors.ConfigurationError(UNSUPPORTED_MESSAGE, is_retryable=False)

    @classmethod
    def from_options(cls, options: AgentOptions | None, workspace: Path | None = None) -> SandboxRuntime:
        cwd = workspace if workspace is not None else workspace_from_options(options)
        requested = sandbox_requested(options)
        binary = locate_cursorsandbox(excluded_workspace=cwd)
        platform_ok = sys.platform == "darwin" or sys.platform.startswith("linux")
        if not requested:
            return cls(
                requested=False,
                supported=bool(binary) and platform_ok,
                binary=binary,
                workspace=cwd,
                default_type="insecure_none",
            )
        ok, reason = sandbox_supported(binary, cwd)
        return cls(
            requested=True,
            supported=ok,
            binary=binary,
            workspace=cwd,
            default_type="workspace_readwrite" if ok else "insecure_none",
            last_failure=reason,
        )


def reset_sandbox_caches() -> None:
    _preflight_cache.clear()


def sandbox_requested(options: AgentOptions | None) -> bool:
    local = getattr(options, "local", None) if options is not None else None
    sandbox = getattr(local, "sandboxOptions", None) if local is not None else None
    return bool(sandbox is not None and getattr(sandbox, "enabled", False))


def workspace_from_options(options: AgentOptions | None) -> Path:
    local = getattr(options, "local", None) if options is not None else None
    raw = getattr(local, "cwd", None) if local is not None else None
    if isinstance(raw, list):
        raw = raw[0] if raw else "."
    return Path(raw or ".").expanduser().resolve()


def node_platform_id() -> str:
    plat = sys.platform
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "ia32",
        "i686": "ia32",
    }.get(machine, machine)
    return f"{plat}-{arch}"


def sandbox_binary_name() -> str:
    return sdk_binary_name("cursorsandbox")


def ripgrep_binary_name() -> str:
    return sdk_binary_name("rg")


def sdk_binary_name(stem: str) -> str:
    return f"{stem}.exe" if sys.platform == "win32" else stem


def locate_cursorsandbox(*, excluded_workspace: Path | None = None) -> Path | None:
    return locate_sdk_package_binary(
        sandbox_binary_name(),
        env_names=("CURSOR_SANDBOX_BIN", "CURSOR_SANDBOX_PATH"),
        excluded_workspace=excluded_workspace,
    )


def locate_ripgrep(*, excluded_workspace: Path | None = None) -> Path | None:
    return locate_sdk_package_binary(
        ripgrep_binary_name(),
        env_names=("CURSOR_RIPGREP_PATH",),
        excluded_workspace=excluded_workspace,
    )


def locate_sdk_package_binary(
    name: str,
    *,
    env_names: tuple[str, ...] = (),
    excluded_workspace: Path | None = None,
) -> Path | None:
    for env_name in env_names:
        env = (os.environ.get(env_name) or "").strip()
        if not env:
            continue
        path = Path(env).expanduser()
        if _is_executable(path):
            return path.resolve()
    which = shutil.which(name)
    if which:
        found = Path(which)
        if _is_executable(found):
            return found.resolve()
    plat = node_platform_id()
    packages = (f"@cursor/sdk-{plat}", f"@cursor/february-{plat}")
    excluded = _excluded_dirs(excluded_workspace)
    starts: list[tuple[Path, bool]] = []
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        try:
            starts.append((Path(argv0).resolve().parent, False))
        except OSError:
            pass
    try:
        starts.append((Path(sys.executable).resolve().parent, True))
    except OSError:
        pass
    seen: set[Path] = set()
    for start, exclude_workspace_helpers in starts:
        current = start
        root = current.anchor
        while True:
            if current in seen:
                break
            seen.add(current)
            for package in packages:
                candidate = current / "node_modules" / package / "bin" / name
                if _is_executable(candidate) and not (
                    exclude_workspace_helpers and _under_any(candidate, excluded)
                ):
                    return candidate.resolve()
            parent = current.parent
            if parent == current or str(current) == root:
                break
            current = parent
    return None


def sandbox_supported(binary: Path | None, workspace: Path) -> tuple[bool, str | None]:
    if sys.platform == "win32":
        return False, "Windows sandbox helper only provides network proxy, not filesystem isolation"
    if binary is None:
        return False, "Sandbox binary path was not configured"
    if not _is_executable(binary):
        return False, f"Sandbox binary not found at {binary}"
    if sys.platform == "darwin":
        return True, None
    if sys.platform.startswith("linux"):
        return _linux_preflight(binary, workspace)
    return False, f"sandbox helper is not supported on {sys.platform}"


def policy_type_name(value: Any) -> str:
    if value is None:
        return "insecure_none"
    if isinstance(value, str) and value.startswith("TYPE_"):
        mapped = PROTO_TO_TYPE.get(value)
        if mapped:
            return mapped
        return value.removeprefix("TYPE_").lower()
    return PROTO_TO_TYPE.get(value, "insecure_none")


def proto_to_js_policy(proto: Mapping[str, Any] | None) -> dict[str, Any]:
    if not proto:
        return {"type": "insecure_none"}
    name = policy_type_name(proto.get("type"))
    if name == "insecure_none":
        out: dict[str, Any] = {"type": "insecure_none"}
        for src, dst in (
            ("allowlist_escalated", "allowlistEscalated"),
            ("enable_shared_build_cache", "enableSharedBuildCache"),
            ("debug_output_dir", "debugOutputDir"),
            ("capture_denies", "captureDenies"),
        ):
            if proto.get(src) is not None:
                out[dst] = proto[src]
        return out
    out = {
        "type": name,
        "networkPolicy": _js_network_policy(proto),
        "additionalReadwritePaths": list(proto.get("additional_readwrite_paths") or []),
        "additionalReadonlyPaths": list(proto.get("additional_readonly_paths") or []),
        "disableTmpWrite": bool(proto.get("disable_tmp_write") or False),
        "networkPolicyStrict": bool(proto.get("network_policy_strict") or False),
    }
    for src, dst in (
        ("skip_statsig_defaults", "skipStatsigDefaults"),
        ("enable_shared_build_cache", "enableSharedBuildCache"),
        ("debug_output_dir", "debugOutputDir"),
        ("capture_denies", "captureDenies"),
    ):
        if proto.get(src) is not None:
            out[dst] = proto[src]
    boundary = READ_BOUNDARY_TO_NAME.get(proto.get("read_boundary"))
    if boundary:
        out["readBoundary"] = boundary
        extra = list(proto.get("additional_read_paths") or [])
        if boundary == "workspace" and extra:
            out["additionalReadPaths"] = extra
    return out


def js_policy_to_proto(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    name = (policy or {}).get("type") or "insecure_none"
    body: dict[str, Any] = {"type": TYPE_TO_PROTO.get(str(name), 1)}
    if name == "insecure_none":
        return body
    net = policy.get("networkPolicy") if policy else None
    if isinstance(net, Mapping):
        body["network_access"] = _network_enabled(net)
    return body


def effective_js_policy(
    runtime: SandboxRuntime | None,
    requested_proto: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if requested_proto:
        return proto_to_js_policy(requested_proto)
    if runtime is None:
        return {"type": "insecure_none"}
    return {"type": runtime.default_type}


def sandbox_is_on(policy: Mapping[str, Any] | None) -> bool:
    return bool(policy) and policy.get("type") not in (None, "insecure_none")


def helper_cli_document(policy: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    sandbox = _helper_sandbox_block(policy, workspace)
    doc: dict[str, Any] = {"sandbox": sandbox}
    network = _cli_network_policy(policy.get("networkPolicy") if isinstance(policy.get("networkPolicy"), Mapping) else None)
    if network is not None:
        doc["networkPolicy"] = network
    if policy.get("networkPolicyStrict") is False:
        doc["networkPolicyStrict"] = False
    return doc


def write_policy_file(document: Mapping[str, Any]) -> Path:
    directory = _policy_dir()
    path = directory / f"sandbox-policy-{secrets.token_hex(8)}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def sandbox_env(base: Mapping[str, str], policy: Mapping[str, Any]) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key != "ELECTRON_RUN_AS_NODE"}
    if sys.platform.startswith("linux"):
        for key in LINUX_SESSION_ENV_STRIP:
            env.pop(key, None)
    if policy.get("enableSharedBuildCache"):
        env.update(_shared_build_cache_env())
    env["CURSOR_SANDBOX"] = "native"
    return env


def sandboxed_argv(binary: Path, policy_path: Path, command: str) -> list[str]:
    if sys.platform == "win32":
        return [str(binary), "--policy", str(policy_path), "--", "cmd.exe", "/c", command]
    return [str(binary), "--policy", str(policy_path), "--", "/bin/sh", "-c", command]


def run_sandboxed(
    *,
    command: str,
    working_directory: Path,
    workspace: Path,
    policy: Mapping[str, Any],
    binary: Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    document = helper_cli_document(policy, workspace)
    policy_path = write_policy_file(document)
    argv = sandboxed_argv(binary, policy_path, command)
    return subprocess.run(
        argv,
        cwd=str(working_directory),
        env=sandbox_env(env or os.environ, policy),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        stdin=subprocess.DEVNULL,
    )


def sandboxed_exec_argv(binary: Path, policy_path: Path, argv: list[str]) -> list[str]:
    return [str(binary), "--policy", str(policy_path), "--", *argv]


def run_sandboxed_argv(
    *,
    argv: list[str],
    working_directory: Path,
    workspace: Path,
    policy: Mapping[str, Any],
    binary: Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    document = helper_cli_document(policy, workspace)
    policy_path = write_policy_file(document)
    full = sandboxed_exec_argv(binary, policy_path, argv)
    return subprocess.run(
        full,
        cwd=str(working_directory),
        env=sandbox_env(env or os.environ, policy),
        capture_output=True,
        timeout=timeout_s,
        stdin=subprocess.DEVNULL,
    )


def _linux_preflight(binary: Path, workspace: Path) -> tuple[bool, str | None]:
    key = (str(binary.resolve()), str(workspace.resolve()))
    cached = _preflight_cache.get(key)
    if cached is not None:
        return cached
    policy = {"type": "workspace_readwrite"}
    document = helper_cli_document(policy, workspace)
    policy_path = write_policy_file(document)
    argv = [str(binary), "--policy", str(policy_path), "--preflight-only", "--", "/bin/true"]
    env = sandbox_env(os.environ, policy)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        result = (False, f"Sandbox binary not found at {binary}")
        _preflight_cache[key] = result
        return result
    except subprocess.TimeoutExpired:
        result = (False, "Linux preflight failed: timeout")
        _preflight_cache[key] = result
        return result
    except OSError as exc:
        result = (False, f"Linux preflight failed: {exc}")
        _preflight_cache[key] = result
        return result
    if proc.returncode == 0:
        result = (True, None)
        _preflight_cache[key] = result
        return result
    stderr = (proc.stderr or "").strip() or "none"
    if proc.returncode == 2:
        reason = f"Linux preflight failed with exit code 2 (unsupported kernel features). stderr: {stderr}"
    else:
        reason = (
            f"Linux preflight failed: unknown error. Exit status: {proc.returncode}. stderr: {stderr}"
        )
    result = (False, reason)
    _preflight_cache[key] = result
    return result


def _helper_sandbox_block(policy: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    policy_dir = str(_policy_dir())
    readonly = list(policy.get("additionalReadonlyPaths") or [])
    if policy_dir not in readonly:
        readonly.append(policy_dir)
    mapped = _paths_to_glob_mapping(readonly)
    boundary = policy.get("readBoundary") or "system"
    extra_reads = _normalize_additional_read_paths(list(policy.get("additionalReadPaths") or []))
    hardcoded = _hardcoded_read_paths()
    merged_reads: list[str] = []
    seen: set[str] = set()
    for path in [*hardcoded, *extra_reads]:
        if path not in seen:
            seen.add(path)
            merged_reads.append(path)
    block: dict[str, Any] = {
        "cwd": str(workspace),
        "readBoundary": boundary,
        "hardcodedReadPaths": merged_reads,
        "networkAccess": _network_enabled(policy.get("networkPolicy") if isinstance(policy.get("networkPolicy"), Mapping) else None),
    }
    if mapped:
        block["additionalReadonlyPaths"] = mapped
    name = str(policy.get("type") or "workspace_readwrite")
    if name == "workspace_readonly":
        return {"type": name, **block}
    return {
        "type": name,
        **block,
        "additionalReadwritePaths": list(policy.get("additionalReadwritePaths") or []),
        "disableTmpWrite": bool(policy.get("disableTmpWrite") or False),
    }


def _js_network_policy(proto: Mapping[str, Any]) -> dict[str, Any]:
    source = proto.get("network_policy")
    if isinstance(source, Mapping) and source:
        out: dict[str, Any] = {}
        if source.get("version") is not None:
            out["version"] = source["version"]
        action = source.get("default_action")
        if action in (1, "1", "DEFAULT_ACTION_ALLOW", "allow"):
            out["default"] = "allow"
        elif action in (2, "2", "DEFAULT_ACTION_DENY", "deny"):
            out["default"] = "deny"
        if source.get("deny"):
            out["deny"] = list(source["deny"])
        if source.get("allow"):
            out["allow"] = list(source["allow"])
        logging = source.get("logging")
        if isinstance(logging, Mapping) and logging:
            log: dict[str, Any] = {}
            if logging.get("decision_log_path"):
                log["decisionLogPath"] = logging["decision_log_path"]
            if logging.get("log_format") == "jsonl":
                log["logFormat"] = "jsonl"
            if log:
                out["logging"] = log
        if out:
            out.setdefault("version", 1)
            return out
    if proto.get("network_access"):
        return {"version": 1, "default": "allow"}
    return {"version": 1, "default": "deny"}


def _network_enabled(policy: Mapping[str, Any] | None) -> bool:
    if not policy:
        return False
    if policy.get("default") == "allow":
        return True
    allow = policy.get("allow")
    return bool(allow)


def _cli_network_policy(policy: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not policy or not _network_enabled(policy):
        return None
    deny = policy.get("deny") or []
    if policy.get("default") != "allow" or deny:
        out = {"version": int(policy.get("version") or 1)}
        if policy.get("default") is not None:
            out["default"] = policy["default"]
        if policy.get("allow"):
            out["allow"] = list(policy["allow"])
        if deny:
            out["deny"] = list(deny)
        if policy.get("logging"):
            out["logging"] = policy["logging"]
        return out
    return None


def _paths_to_glob_mapping(paths: list[str]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for raw in paths:
        path = Path(raw)
        is_dir = False
        try:
            is_dir = path.exists() and path.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            mapping.setdefault(str(path), []).append("**")
            continue
        parent = str(path.parent) if str(path.parent) else "/"
        name = path.name
        mapping.setdefault(parent, [])
        if name:
            mapping[parent].extend([name, f"{name}/**"])
        else:
            mapping[parent].append("**")
    return mapping


def _normalize_additional_read_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    home = str(Path.home())
    for raw in paths:
        value = raw.strip()
        if not value:
            continue
        if value == "~":
            value = home
        elif value.startswith("~/") or value.startswith("~\\"):
            value = str(Path.home() / value[2:])
        value = value.replace("/\\**", "").replace("/**", "").replace("/*", "")
        name = Path(value).name
        if "*" in name or "?" in name:
            value = str(Path(value).parent)
        if not value or value == ".":
            continue
        normalized = os.path.normpath(value)
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _hardcoded_read_paths() -> list[str]:
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    home = Path.home()
    bundled = {"ripgrep": None}
    rg = locate_ripgrep()
    if rg is not None:
        bundled["ripgrep"] = str(rg)
    out: list[str] = []
    seen: set[str] = set()
    for item_plat, scope, path in HARDCODED_READ_PATHS:
        if item_plat != plat:
            continue
        resolved: str | None
        if scope == "global":
            resolved = path
        elif scope == "home":
            candidate = home / path
            try:
                resolved = str(candidate) if candidate.is_file() else None
            except OSError:
                resolved = None
        else:
            raw = bundled.get(path)
            resolved = None
            if raw:
                candidate = Path(raw)
                try:
                    resolved = str(candidate) if candidate.is_file() else None
                except OSError:
                    resolved = None
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def _policy_dir() -> Path:
    global _last_policy_cleanup
    env = (os.environ.get("CURSOR_SANDBOX_POLICY_DIR") or "").strip()
    directory = Path(env).expanduser().resolve() if env else Path.home() / ".cursor" / "sandbox-policies"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    now = time.time()
    if now - _last_policy_cleanup >= POLICY_CLEANUP_INTERVAL_S:
        _last_policy_cleanup = now
        _cleanup_old_policies(directory)
    return directory


def _cleanup_old_policies(directory: Path) -> None:
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith("sandbox-policy-"):
            continue
        path = directory / name
        try:
            st = path.stat()
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > POLICY_MAX_AGE_S:
                path.unlink()
        except OSError:
            continue


def _shared_build_cache_env() -> dict[str, str]:
    root = Path(os.environ.get("TMPDIR") or "/tmp") / "cursor-sandbox-cache" / secrets.token_hex(16)
    return {
        "NPM_CONFIG_CACHE": str(root / "npm"),
        "PNPM_STORE_PATH": str(root / "pnpm-store"),
        "GOCACHE": str(root / "go-build"),
        "GOMODCACHE": str(root / "go-mod"),
        "CARGO_TARGET_DIR": str(root / "cargo-target"),
        "PIP_CACHE_DIR": str(root / "pip"),
        "UV_CACHE_DIR": str(root / "uv"),
        "BUN_INSTALL_CACHE_DIR": str(root / "bun"),
        "YARN_CACHE_FOLDER": str(root / "yarn"),
        "npm_config_devdir": str(root / "node-gyp"),
        "PLAYWRIGHT_BROWSERS_PATH": str(root / "playwright"),
        "PUPPETEER_CACHE_DIR": str(root / "puppeteer"),
        "TURBO_CACHE_DIR": str(root / "turbo"),
        "GRADLE_USER_HOME": str(root / "gradle"),
        "CONDA_PKGS_DIRS": str(root / "conda"),
        "POETRY_CACHE_DIR": str(root / "poetry"),
        "GEM_SPEC_CACHE": str(root / "gem-specs"),
        "BUNDLE_PATH": str(root / "bundle"),
        "COMPOSER_HOME": str(root / "composer"),
        "HOMEBREW_CACHE": str(root / "homebrew"),
    }


def _is_executable(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if not path.is_file():
        return False
    if sys.platform == "win32":
        return True
    return bool(st.st_mode & 0o111)


def _git_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _excluded_dirs(workspace: Path | None) -> list[Path]:
    out: list[Path] = []
    if workspace is None:
        return out
    resolved = workspace.resolve()
    out.append(resolved)
    git = _git_root(resolved)
    if git is not None:
        out.append(git)
    return out


def _under_any(path: Path, directories: list[Path]) -> bool:
    resolved = path.resolve()
    for directory in directories:
        try:
            resolved.relative_to(directory.resolve())
            return True
        except ValueError:
            continue
    return False
