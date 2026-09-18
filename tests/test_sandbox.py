from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from opencursor._local_executor import _exec_shell
from opencursor._sandbox import (
    SandboxRuntime,
    UNSUPPORTED_MESSAGE,
    effective_js_policy,
    helper_cli_document,
    js_policy_to_proto,
    locate_cursorsandbox,
    proto_to_js_policy,
    reset_sandbox_caches,
    sandbox_env,
    sandboxed_argv,
)
from opencursor.errors import ConfigurationError
from opencursor.types import AgentOptions

STUB = r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
policy_path = None
preflight = False
cmd = []
i = 0
while i < len(args):
    arg = args[i]
    if arg == "--policy" and i + 1 < len(args):
        policy_path = args[i + 1]
        i += 2
        continue
    if arg == "--preflight-only":
        preflight = True
        i += 1
        continue
    if arg == "--":
        cmd = args[i + 1 :]
        break
    i += 1

dump = os.environ.get("CURSOR_SANDBOX_STUB_DUMP")
if dump:
    policy = None
    if policy_path:
        with open(policy_path, encoding="utf-8") as fh:
            policy = json.load(fh)
    with open(dump, "w", encoding="utf-8") as fh:
        json.dump({"argv": args, "cmd": cmd, "preflight": preflight, "policy": policy}, fh)

if preflight:
    raise SystemExit(int(os.environ.get("CURSOR_SANDBOX_STUB_PREFLIGHT_EXIT", "0")))
if not cmd:
    raise SystemExit("cursorsandbox stub: missing command after --")
os.execvp(cmd[0], cmd)
"""


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reset_sandbox_caches()
    binary = tmp_path / "cursorsandbox"
    binary.write_text(STUB, encoding="utf-8")
    binary.chmod(0o755)
    dump = tmp_path / "dump.json"
    policies = tmp_path / "policies"
    policies.mkdir()
    monkeypatch.setenv("CURSOR_SANDBOX_BIN", str(binary))
    monkeypatch.setenv("CURSOR_SANDBOX_POLICY_DIR", str(policies))
    monkeypatch.setenv("CURSOR_SANDBOX_STUB_DUMP", str(dump))
    monkeypatch.delenv("CURSOR_SANDBOX_STUB_PREFLIGHT_EXIT", raising=False)
    return binary


def _enabled_options(cwd: Path) -> AgentOptions:
    return AgentOptions.model_validate(
        {
            "model": {"id": "composer-2"},
            "local": {"cwd": str(cwd), "sandboxOptions": {"enabled": True}},
        }
    )


def test_locate_uses_cursor_sandbox_bin(stub_bin: Path) -> None:
    found = locate_cursorsandbox(excluded_workspace=stub_bin.parent)
    assert found == stub_bin.resolve()


def test_runtime_enabled_after_preflight(stub_bin: Path, tmp_path: Path) -> None:
    runtime = SandboxRuntime.from_options(_enabled_options(tmp_path), tmp_path)
    assert runtime.requested is True
    assert runtime.supported is True
    assert runtime.enabled is True
    assert runtime.default_type == "workspace_readwrite"
    dump = json.loads((tmp_path / "dump.json").read_text(encoding="utf-8"))
    assert dump["preflight"] is True
    assert dump["cmd"] == ["/bin/true"]


def test_preflight_exit_2_is_unsupported(stub_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_SANDBOX_STUB_PREFLIGHT_EXIT", "2")
    runtime = SandboxRuntime.from_options(_enabled_options(tmp_path), tmp_path)
    assert runtime.supported is False
    assert runtime.last_failure is not None
    assert "exit code 2" in runtime.last_failure
    with pytest.raises(ConfigurationError, match="sandboxing is not supported"):
        runtime.raise_if_requested_unsupported()


def test_enabled_without_binary_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_sandbox_caches()
    monkeypatch.delenv("CURSOR_SANDBOX_BIN", raising=False)
    monkeypatch.delenv("CURSOR_SANDBOX_PATH", raising=False)
    monkeypatch.setattr("opencursor._sandbox.locate_cursorsandbox", lambda **kwargs: None)
    runtime = SandboxRuntime.from_options(_enabled_options(tmp_path), tmp_path)
    assert runtime.requested is True
    assert runtime.supported is False
    with pytest.raises(ConfigurationError) as exc:
        runtime.raise_if_requested_unsupported()
    assert str(exc.value) == UNSUPPORTED_MESSAGE


def test_disabled_stays_insecure_none(stub_bin: Path, tmp_path: Path) -> None:
    options = AgentOptions.model_validate(
        {
            "model": {"id": "composer-2"},
            "local": {"cwd": str(tmp_path), "sandboxOptions": {"enabled": False}},
        }
    )
    runtime = SandboxRuntime.from_options(options, tmp_path)
    assert runtime.requested is False
    assert runtime.enabled is False
    assert runtime.default_type == "insecure_none"
    runtime.raise_if_requested_unsupported()


def test_helper_policy_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_SANDBOX_POLICY_DIR", str(tmp_path / "policies"))
    (tmp_path / "policies").mkdir()
    doc = helper_cli_document({"type": "workspace_readwrite"}, tmp_path)
    sandbox = doc["sandbox"]
    assert sandbox["type"] == "workspace_readwrite"
    assert sandbox["cwd"] == str(tmp_path)
    assert sandbox["readBoundary"] == "system"
    assert sandbox["networkAccess"] is False
    assert sandbox["disableTmpWrite"] is False
    assert "/bin" in sandbox["hardcodedReadPaths"] or "/usr/bin" in sandbox["hardcodedReadPaths"]
    assert "networkPolicy" not in doc
    readonly = sandbox["additionalReadonlyPaths"]
    assert any(str(tmp_path / "policies") == key or key.endswith("policies") for key in readonly)


def test_proto_roundtrip_types() -> None:
    assert proto_to_js_policy({"type": 2})["type"] == "workspace_readwrite"
    assert proto_to_js_policy({"type": "TYPE_WORKSPACE_READONLY"})["type"] == "workspace_readonly"
    assert proto_to_js_policy({"type": 1})["type"] == "insecure_none"
    assert js_policy_to_proto({"type": "workspace_readwrite"}) == {"type": 2}
    net = proto_to_js_policy({"type": 2, "network_access": True})
    assert net["networkPolicy"]["default"] == "allow"


def test_sandbox_env_strips_linux_session_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opencursor._sandbox.sys.platform", "linux")
    env = sandbox_env(
        {
            "PATH": "/bin",
            "SSH_AUTH_SOCK": "/tmp/ssh",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "ELECTRON_RUN_AS_NODE": "1",
        },
        {"type": "workspace_readwrite"},
    )
    assert env["CURSOR_SANDBOX"] == "native"
    assert "SSH_AUTH_SOCK" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" not in env
    assert "ELECTRON_RUN_AS_NODE" not in env


def test_sandboxed_argv_uses_policy_and_sh() -> None:
    argv = sandboxed_argv(Path("/opt/cursorsandbox"), Path("/tmp/policy.json"), "echo hi")
    assert argv[:4] == ["/opt/cursorsandbox", "--policy", "/tmp/policy.json", "--"]
    assert argv[4:] == ["/bin/sh", "-c", "echo hi"]


def test_exec_shell_runs_through_stub(stub_bin: Path, tmp_path: Path) -> None:
    runtime = SandboxRuntime.from_options(_enabled_options(tmp_path), tmp_path)
    result = _exec_shell(tmp_path, {"command": "echo sandboxed", "timeout": 5000}, runtime)
    assert "success" in result
    assert result["success"]["stdout"].strip() == "sandboxed"
    assert result["success"]["sandbox_policy"]["type"] == 2
    dump = json.loads((tmp_path / "dump.json").read_text(encoding="utf-8"))
    assert dump["preflight"] is False
    assert dump["cmd"][:2] == ["/bin/sh", "-c"]
    assert dump["cmd"][2] == "echo sandboxed"
    assert dump["policy"]["sandbox"]["type"] == "workspace_readwrite"


def test_exec_shell_insecure_skips_helper(stub_bin: Path, tmp_path: Path) -> None:
    dump = tmp_path / "dump.json"
    if dump.exists():
        dump.unlink()
    runtime = SandboxRuntime.from_options(
        AgentOptions.model_validate(
            {"model": {"id": "x"}, "local": {"cwd": str(tmp_path), "sandboxOptions": {"enabled": False}}}
        ),
        tmp_path,
    )
    result = _exec_shell(tmp_path, {"command": "echo plain", "timeout": 5000}, runtime)
    assert result["success"]["stdout"].strip() == "plain"
    assert result["success"]["sandbox_policy"]["type"] == 1
    assert not dump.exists()


def test_requested_policy_workspace_without_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opencursor._sandbox.locate_cursorsandbox", lambda **kwargs: None)
    result = _exec_shell(
        tmp_path,
        {
            "command": "echo hi",
            "timeout": 1000,
            "requested_sandbox_policy": {"type": 2},
        },
        SandboxRuntime(
            requested=False,
            supported=False,
            binary=None,
            workspace=tmp_path,
            default_type="insecure_none",
        ),
    )
    assert "sandbox_unsupported" in result
    assert result["sandbox_unsupported"]["sandbox_policy_type"] == "workspace_readwrite"


def test_effective_policy_prefers_requested() -> None:
    runtime = SandboxRuntime(
        requested=True,
        supported=True,
        binary=Path("/bin/true"),
        workspace=Path("/tmp"),
        default_type="workspace_readwrite",
    )
    assert effective_js_policy(runtime, {"type": 1})["type"] == "insecure_none"
    assert effective_js_policy(runtime, None)["type"] == "workspace_readwrite"
