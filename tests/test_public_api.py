from __future__ import annotations

import asyncio
import json
import warnings

from opencursor import Agent, Cursor, CursorSDKError, CursorSdkError, CursorAgentError, PermissionDeniedError, AuthenticationError
from opencursor._auth import FileCredentialStore, parse_stored_sdk_credentials, resolve_default_api_key
from opencursor._custom_tools import CUSTOM_USER_TOOLS_PROVIDER, custom_tool_definitions, execute_custom_tool
from opencursor._local_executor import _conversation_mode, _exec_glob, _exec_pi_find, _workspace_paths, resolve_allowed_tools
from opencursor._run_api import empty_agent_usage
from opencursor._sdk_config import clear_default_network_config_for_tests, get_default_use_http1_for_agent
from opencursor._settings import load_project_rules
from opencursor.agent import _is_cloud
from opencursor.types import (
    AgentOperationOptions,
    AgentOptions,
    GetRunOptionsLocal,
    ListRunsLocalOptions,
    LocalAgentOptions,
    SendOptions,
)


def test_default_runtime_is_local() -> None:
    assert _is_cloud(None, None) is False
    assert _is_cloud(AgentOptions(), None) is False
    assert _is_cloud(AgentOptions.model_validate({"local": {"cwd": "."}}), None) is False
    assert _is_cloud(AgentOptions.model_validate({"cloud": {}}), None) is True
    assert _is_cloud(None, "bc-123") is True
    assert _is_cloud(None, "agent-local") is False


def test_create_without_local_or_cloud_uses_local_store(tmp_path, monkeypatch) -> None:
    asyncio.run(_test_create_without_local_or_cloud_uses_local_store(tmp_path, monkeypatch))


async def _test_create_without_local_or_cloud_uses_local_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    agent = await Agent.create(AgentOptions.model_validate({"model": {"id": "model-1"}}))
    try:
        assert agent.agent_id == agent.agentId
        assert not str(agent.agentId).startswith("bc-")
        listed = await Agent.list()
        assert [a.agentId for a in listed.items] == [agent.agentId]
        assert listed.next_cursor == (listed.nextCursor or "")
        assert listed.has_next_page() is bool(listed.next_cursor)
        usage = await Agent.getUsage(agent.agentId)
        assert usage.usage.totalTokens == 0
        assert usage.usage.total_tokens == 0
        assert usage.usage.totalTokens == 0
        assert usage.runs == []
        assert Agent.list_runs is Agent.listRuns
        assert Agent.get_run is Agent.getRun
        assert Agent.cancel_run is Agent.cancelRun
        assert Agent.get_usage is Agent.getUsage
        empty = empty_agent_usage()
        assert empty.usage.inputTokens == 0
        instance_usage = await agent.getUsage()
        assert instance_usage.usage.totalTokens == 0
    finally:
        await agent.aclose()


def test_run_helpers_and_cancel_run_alias(tmp_path, monkeypatch) -> None:
    asyncio.run(_test_run_helpers_and_cancel_run_alias(tmp_path, monkeypatch))


async def _test_run_helpers_and_cancel_run_alias(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    options = AgentOptions.model_validate({"local": {"cwd": str(tmp_path)}, "model": {"id": "model-1"}})
    agent = await Agent.create(options)
    try:
        run = await agent.send("hello")
        assert run.agentId == agent.agentId
        result = await run.wait()
        assert result.status == "error"
        assert result.agent_id == agent.agent_id
        assert result.duration_ms == result.durationMs
        assert result.usage is None
        texts = [chunk async for chunk in run.iter_text()]
        assert texts == []
        events = [event async for event in run.events()]
        assert any(event.get("sdk_message", {}).get("type") == "user" for event in events)
        listed = await agent.list_messages()
        assert listed
        assert listed[0].type == "user"
        assert listed[0].agent_id == agent.agent_id
        raw = await run.conversation_json()
        loaded = json.loads(raw)
        assert isinstance(loaded, list)
        observed = [event async for event in run.observe()]
        assert any(
            event.kind == "sdk_message"
            and (
                getattr(event.sdk_message, "type", None) == "user"
                or (hasattr(event.sdk_message, "get") and event.sdk_message.get("type") == "user")
            )
            for event in observed
        )
        await Agent.cancelRun(run.id, GetRunOptionsLocal(cwd=str(tmp_path)))
        runs = await Agent.list_runs(agent.agent_id, ListRunsLocalOptions(cwd=str(tmp_path)))
        assert [item.id for item in runs.items] == [run.id]
    finally:
        await agent.aclose()


def test_cursor_configure_http1_default() -> None:
    clear_default_network_config_for_tests()
    try:
        Cursor.configure({"local": {"useHttp1ForAgent": True}})
        assert get_default_use_http1_for_agent() is True
        Cursor.configure({"local": {"useHttp1ForAgent": None}})
        assert get_default_use_http1_for_agent() is None
    finally:
        clear_default_network_config_for_tests()


def test_file_credential_store_and_key_order(tmp_path, monkeypatch) -> None:
    asyncio.run(_test_file_credential_store_and_key_order(tmp_path, monkeypatch))


async def _test_file_credential_store_and_key_order(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    path = tmp_path / "auth.json"
    store = FileCredentialStore(path)
    await store.save(
        {
            "version": 1,
            "backendUrl": "https://api2.cursor.sh",
            "apiKey": "stored-key",
            "createdAtMs": 1,
            "email": "user@example.com",
        }
    )
    loaded = await store.load()
    assert loaded is not None
    assert loaded["apiKey"] == "stored-key"
    status = await Cursor.auth.status({"store": store})
    assert status["status"] == "logged-in"
    assert status["email"] == "user@example.com"
    monkeypatch.setenv("CURSOR_API_KEY", "env-key")
    assert resolve_default_api_key(None) == "env-key"
    assert resolve_default_api_key("explicit") == "explicit"
    await Cursor.auth.logout({"store": store})
    assert await store.load() is None
    parsed = parse_stored_sdk_credentials({"version": 2})
    assert parsed is None


def test_glob_and_workspace_paths(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("y", encoding="utf-8")
    extra = tmp_path / "other"
    extra.mkdir()
    globbed = _exec_glob(tmp_path, {"glob_pattern": "*.py", "target_directory": str(tmp_path / "src")})
    assert globbed["success"]["total_files"] == 1
    assert globbed["success"]["files"][0].endswith("a.py")
    found = _exec_pi_find(tmp_path, {"pattern": "*.txt", "path": str(tmp_path / "src")})
    assert "b.txt" in found["success"]["output"]
    opts = AgentOptions.model_validate({"local": {"cwd": str(tmp_path), "dirs": [str(extra)]}})
    paths = _workspace_paths(opts)
    assert str(tmp_path.resolve()) in paths
    assert str(extra.resolve()) in paths
    assert _conversation_mode(AgentOptions.model_validate({"mode": "plan"})) == "AGENT_MODE_PLAN"
    assert _conversation_mode(AgentOptions()) == "AGENT_MODE_AGENT"


def test_custom_tools_and_setting_sources(tmp_path) -> None:
    asyncio.run(_test_custom_tools_and_setting_sources(tmp_path))


async def _test_custom_tools_and_setting_sources(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("# hello", encoding="utf-8")
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "style.mdc").write_text("be brief", encoding="utf-8")
    loaded = load_project_rules([tmp_path])
    assert any(item["full_path"].endswith("AGENTS.md") for item in loaded)
    assert any(item["full_path"].endswith("style.mdc") for item in loaded)

    called: list[dict] = []

    def _echo(args, context):
        called.append({"args": args, "context": context})
        return "pong"

    tools = {"echo": {"description": "echo", "execute": _echo}}
    defs = custom_tool_definitions(tools)
    assert defs[0]["provider_identifier"] == CUSTOM_USER_TOOLS_PROVIDER
    assert defs[0]["name"] == f"{CUSTOM_USER_TOOLS_PROVIDER}-echo"
    result = await execute_custom_tool(
        tools,
        {"provider_identifier": CUSTOM_USER_TOOLS_PROVIDER, "tool_name": "echo", "args": {"q": 1}, "tool_call_id": "t1"},
    )
    assert result["success"]["content"][0]["text"]["text"] == "pong"
    assert called[0]["context"]["toolCallId"] == "t1"
    opts = AgentOptions.model_validate({"local": {"cwd": str(tmp_path), "customTools": tools}})
    allowed = resolve_allowed_tools(opts)
    assert "mcp_tool_call" in allowed


def test_error_aliases() -> None:
    assert CursorSDKError is CursorSdkError
    assert PermissionDeniedError is AuthenticationError


def test_cloud_option_fields_roundtrip() -> None:
    opts = AgentOptions.model_validate(
        {
            "cloud": {
                "env": {"type": "CLOUD_ENVIRONMENT_TYPE_POOL", "name": "prod"},
                "repos": [{"url": "https://github.com/acme/app", "startingRef": "main", "prUrl": "https://github.com/acme/app/pull/1"}],
                "envVars": {"A": "1"},
                "metadata": {"team": "sdk"},
                "openAsCursorGithubApp": True,
                "agentServeAgent": "slug",
            },
            "mode": "plan",
            "idempotencyKey": "k1",
            "local": {"dirs": ["/tmp"], "customTools": {}},
        }
    )
    assert opts.cloud is not None
    assert opts.cloud.envVars == {"A": "1"}
    assert opts.cloud.metadata == {"team": "sdk"}
    assert opts.cloud.env is not None
    assert opts.cloud.env.type == "pool"
    assert opts.cloud.env.name == "prod"
    assert opts.cloud.repos is not None
    assert opts.cloud.repos[0].url.endswith("/app")
    assert opts.cloud.repos[0].starting_ref == "main"
    assert opts.cloud.repos[0].pr_url.endswith("/pull/1")
    assert opts.mode == "plan"
    assert opts.idempotencyKey == "k1"
    assert opts.local is not None
    assert opts.local.dirs == ["/tmp"]


def test_agent_options_accept_official_python_kwargs() -> None:
    opts = AgentOptions(
        api_key="sk-test",
        model="composer-2.5",
        local=LocalAgentOptions(cwd=".", setting_sources=[]),
    )
    assert opts.apiKey == "sk-test"
    assert opts.model is not None
    assert opts.model.id == "composer-2.5"
    assert opts.local is not None
    assert opts.local.cwd == "."
    assert opts.local.settingSources == []
    send = SendOptions(model="composer-2")
    assert send.model is not None
    assert send.model.id == "composer-2"


def test_sync_prompt_accepts_string_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    try:
        Agent.prompt(
            "hello",
            AgentOptions(
                api_key="sk-fake",
                model="composer-2.5",
                local=LocalAgentOptions(cwd=str(tmp_path)),
            ),
        )
    except CursorAgentError as exc:
        assert exc.message
        assert type(exc).__name__ != "ValidationError"
    except Exception as exc:
        assert type(exc).__name__ != "ValidationError"
        assert "model_type" not in str(exc)


def test_cursor_me_models_repositories_match_official_sync_shape(monkeypatch) -> None:
    import inspect

    from opencursor._cloud_api import CloudApiClient

    async def fake_get_me(self) -> dict:
        return {
            "apiKeyName": "test",
            "createdAt": "2026-04-29T19:12:25.199Z",
            "userId": 147216663,
            "userEmail": "wierzejskimateusz8@gmail.com",
            "userFirstName": "Mateusz",
            "userLastName": "Wierzejski",
        }

    async def fake_list_models(self) -> dict:
        return {
            "items": [
                {
                    "id": "default",
                    "displayName": "Auto",
                    "description": "",
                    "parameters": [
                        {
                            "id": "effort",
                            "displayName": "Effort",
                            "values": [{"value": "low", "displayName": "Low"}],
                        }
                    ],
                    "variants": [
                        {
                            "params": [],
                            "displayName": "Auto",
                            "description": "",
                            "isDefault": True,
                        }
                    ],
                }
            ]
        }

    async def fake_list_repositories(self) -> dict:
        return {"items": [{"url": "https://github.com/pi-apps-go/pi-apps"}]}

    async def fake_aclose(self) -> None:
        return None

    monkeypatch.setenv("CURSOR_API_KEY", "sk-test")
    monkeypatch.setattr(CloudApiClient, "get_me", fake_get_me)
    monkeypatch.setattr(CloudApiClient, "list_models", fake_list_models)
    monkeypatch.setattr(CloudApiClient, "list_repositories", fake_list_repositories)
    monkeypatch.setattr(CloudApiClient, "aclose", fake_aclose)

    user = Cursor.me()
    assert inspect.iscoroutine(user) is False
    assert user.api_key_name == "test"
    assert user.created_at == "2026-04-29T19:12:25.199Z"
    assert user.user_id == 147216663
    assert user.user_email == "wierzejskimateusz8@gmail.com"
    assert str(user).startswith("SDKUser(")
    assert repr(user).startswith("SDKUser(")
    assert "api_key_name=" in repr(user)
    assert "user_first_name=" in repr(user)

    models = Cursor.models.list()
    assert inspect.iscoroutine(models) is False
    assert models[0].id == "default"
    assert models[0].display_name == "Auto"
    assert models[0].variants[0].is_default is True
    assert "display_name=" in repr(models[0])
    assert "ModelParameterDefinitionValue(" in repr(models[0])
    assert "ModelParameterDefValue(" not in repr(models[0])

    repos = Cursor.repositories.list()
    assert inspect.iscoroutine(repos) is False
    assert repos[0].url == "https://github.com/pi-apps-go/pi-apps"


def test_deprecated_agent_aliases_and_context_manager(tmp_path, monkeypatch) -> None:
    asyncio.run(_test_deprecated_agent_aliases_and_context_manager(tmp_path, monkeypatch))


async def _test_deprecated_agent_aliases_and_context_manager(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    options = AgentOptions.model_validate({"local": {"cwd": str(tmp_path)}, "model": {"id": "model-1"}})
    op_opts = AgentOperationOptions(cwd=str(tmp_path))

    agent = await Agent.create(options)
    try:
        assert agent.__enter__() is agent
        agent.__exit__(None, None, None)
    except Exception:
        await agent.aclose()
        raise

    async with await Agent.create(options) as agent:
        agent_id = agent.agent_id

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        await Agent.archive_agent(agent_id, op_opts)
        await Agent.unarchive_agent(agent_id, op_opts)
        await Agent.lifecycle.archive(agent_id, op_opts)
        await Agent.lifecycle.unarchive(agent_id, op_opts)
        await Agent.delete_agent(agent_id, op_opts)

    messages = [str(item.message) for item in caught if issubclass(item.category, DeprecationWarning)]
    assert any("Agent.archive_agent is deprecated" in text for text in messages)
    assert any("Agent.unarchive_agent is deprecated" in text for text in messages)
    assert any("Agent.lifecycle.archive is deprecated" in text for text in messages)
    assert any("Agent.lifecycle.unarchive is deprecated" in text for text in messages)
    assert any("Agent.delete_agent is deprecated" in text for text in messages)


def test_sync_create_kwargs_and_context_manager(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    with Agent.create(
        model="model-1",
        local=LocalAgentOptions(cwd=str(tmp_path)),
        client="ignored",
    ) as agent:
        assert agent.agent_id.startswith("agent-")
        run = agent.send("hello")
        result = run.wait()
        assert result.status == "error"
        assert result.agent_id == agent.agent_id
        assert result.duration_ms == result.durationMs
        artifacts = agent.list_artifacts()
        assert artifacts == []
