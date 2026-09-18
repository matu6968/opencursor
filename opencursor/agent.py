from __future__ import annotations

import warnings
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from opencursor import errors
from opencursor._auth import resolve_api_key
from opencursor._cloud_agent import CloudAgent, CloudRun, create_cloud_agent, resume_cloud_agent
from opencursor._cloud_api import CloudApiClient
from opencursor._sync_runtime import run_sync_or_awaitable as _run_sync_or_awaitable
from opencursor._local_runtime import (
    LocalRun,
    archive_local_agent,
    create_local_agent,
    delete_local_agent,
    get_local_agent,
    get_local_run,
    get_local_usage,
    list_local_agent_messages,
    list_local_agents,
    list_local_runs,
    resume_local_agent,
    unarchive_local_agent,
)
from opencursor._run_api import agent_usage_from_v1, empty_agent_usage
from opencursor.types import (
    AgentOperationOptions,
    AgentOptions,
    AgentUsage,
    GetAgentMessagesOptions,
    GetAgentOptions,
    GetRunOptionsLocal,
    GetRunOptionsCloud,
    GetUsageOptions,
    ListAgentsCloudOptions,
    ListAgentsLocalOptions,
    ListResult,
    ListRunsCloudOptions,
    ListRunsLocalOptions,
    LocalAgentOptions,
    CloudAgentOptions,
    ModelSelection,
    RunResult,
    SDKAgentInfoCloud,
    SDKAgentInfoLocal,
    SDKUserMessage,
    SendOptions,
)


@runtime_checkable
class SDKAgent(Protocol):
    agent_id: str
    agentId: str
    model: Any

    async def send(self, message: str | SDKUserMessage, options: SendOptions | None = None) -> CloudRun | LocalRun: ...

    def close(self) -> None: ...

    async def reload(self) -> None: ...

    async def list_artifacts(self) -> list[Any]: ...

    async def listArtifacts(self) -> list[Any]: ...

    async def download_artifact(self, path: str) -> bytes: ...

    async def downloadArtifact(self, path: str) -> bytes: ...

    async def getUsage(self, options: GetUsageOptions | None = None) -> AgentUsage: ...

    async def get_usage(self, options: GetUsageOptions | None = None) -> AgentUsage: ...

    async def list_messages(self, options: GetAgentMessagesOptions | None = None) -> list[Any]: ...

    async def aclose(self) -> None: ...

    def __enter__(self) -> SDKAgent: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    async def __aenter__(self) -> SDKAgent: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...


def _is_cloud(options: AgentOptions | None, agent_id: str | None) -> bool:
    if options and options.cloud is not None:
        return True
    if options and options.local is not None:
        return False
    if agent_id:
        return agent_id.startswith("bc-")
    return False


def _map_v1_agent_to_sdk_info(t: dict[str, Any]) -> SDKAgentInfoCloud:
    repos_raw = t.get("repos")
    repo_urls: list[str] | None = None
    if isinstance(repos_raw, list):
        repo_urls = []
        for r in repos_raw:
            if isinstance(r, dict) and r.get("url"):
                repo_urls.append(str(r["url"]))
    updated = datetime.fromisoformat(str(t["updatedAt"]).replace("Z", "+00:00"))
    created = datetime.fromisoformat(str(t["createdAt"]).replace("Z", "+00:00"))
    metadata = t.get("metadata") if isinstance(t.get("metadata"), dict) else None
    return SDKAgentInfoCloud(
        agentId=str(t["id"]),
        name=str(t.get("name") or ""),
        summary=str(t.get("name") or ""),
        lastModified=int(updated.timestamp() * 1000),
        createdAt=int(created.timestamp() * 1000),
        archived=str(t.get("status")) == "ARCHIVED",
        runtime="cloud",
        env=t.get("env") if isinstance(t.get("env"), dict) else None,
        repos=repo_urls,
        metadata={str(k): str(v) for k, v in metadata.items()} if metadata else None,
    )


def _wants_local_runtime(options: Any, agent_id: str | None = None) -> bool:
    runtime = None
    if isinstance(options, dict):
        runtime = options.get("runtime")
    elif options is not None:
        runtime = getattr(options, "runtime", None)
    if runtime == "local":
        return True
    if runtime == "cloud":
        return False
    if agent_id:
        return not str(agent_id).startswith("bc-")
    return True


def _coerce_agent_options(
    options: AgentOptions | Mapping[str, Any] | None = None,
    *,
    model: str | ModelSelection | Mapping[str, Any] | None = None,
    api_key: str | None = None,
    name: str | None = None,
    local: LocalAgentOptions | Mapping[str, Any] | None = None,
    cloud: CloudAgentOptions | Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> AgentOptions:
    payload: dict[str, Any] = {}
    if isinstance(options, AgentOptions):
        payload = options.model_dump(exclude_none=True)
    elif isinstance(options, Mapping):
        payload = dict(options)
    extra_payload = dict(extra or {})
    extra_payload.pop("client", None)
    payload.update(extra_payload)
    if model is not None:
        payload["model"] = model
    if api_key is not None:
        payload["api_key"] = api_key
    if name is not None:
        payload["name"] = name
    if local is not None:
        payload["local"] = local
    if cloud is not None:
        payload["cloud"] = cloud
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return AgentOptions.model_validate(payload) if payload else AgentOptions()


def _coerce_send_message(message: str | Mapping[str, Any] | SDKUserMessage) -> str | SDKUserMessage:
    if isinstance(message, str) or isinstance(message, SDKUserMessage):
        return message
    if isinstance(message, Mapping):
        return SDKUserMessage.from_value(message) if hasattr(SDKUserMessage, "from_value") else SDKUserMessage.model_validate(dict(message))
    return message


def _merge_list_options(options: Any, kwargs: dict[str, Any]) -> Any:
    if not kwargs:
        return options
    if options is None:
        return kwargs
    if isinstance(options, Mapping):
        return {**dict(options), **kwargs}
    dumped = options.model_dump() if hasattr(options, "model_dump") else {}
    return {**dumped, **kwargs}


def _raise_if_auth_run_error(result: RunResult) -> None:
    err = result.error
    if result.status != "error" or err is None:
        return
    code = (err.code or "").lower()
    message = err.message or ""
    if code in {"unauthenticated", "unauthorized"} or "invalid user api key" in message.lower():
        raise errors.AuthenticationError(
            message or "Invalid User API Key",
            code="unauthenticated",
            is_retryable=False,
        )


class Agent:
    """Create / resume agents and list resources."""

    def __init__(self) -> None:
        raise RuntimeError("Agent is not instantiable")

    @staticmethod
    def create(
        options: AgentOptions | Mapping[str, Any] | None = None,
        *,
        client: Any = None,
        model: str | ModelSelection | Mapping[str, Any] | None = None,
        api_key: str | None = None,
        name: str | None = None,
        local: LocalAgentOptions | Mapping[str, Any] | None = None,
        cloud: CloudAgentOptions | Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        **extra: Any,
    ) -> SDKAgent:
        _ = client
        opts = _coerce_agent_options(
            options,
            model=model,
            api_key=api_key,
            name=name,
            local=local,
            cloud=cloud,
            idempotency_key=idempotency_key,
            extra=extra,
        )
        return _run_sync_or_awaitable(Agent._acreate(opts))

    @staticmethod
    async def _acreate(options: AgentOptions) -> SDKAgent:
        if not _is_cloud(options, options.agentId):
            return await create_local_agent(options)
        return await create_cloud_agent(options)

    @staticmethod
    def resume(
        agent_id: str,
        options: AgentOptions | Mapping[str, Any] | None = None,
        *,
        client: Any = None,
        model: str | ModelSelection | Mapping[str, Any] | None = None,
        api_key: str | None = None,
        name: str | None = None,
        local: LocalAgentOptions | Mapping[str, Any] | None = None,
        cloud: CloudAgentOptions | Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        **extra: Any,
    ) -> SDKAgent:
        _ = client
        opts = _coerce_agent_options(
            options,
            model=model,
            api_key=api_key,
            name=name,
            local=local,
            cloud=cloud,
            idempotency_key=idempotency_key,
            extra=extra,
        )
        return _run_sync_or_awaitable(Agent._aresume(agent_id, opts))

    @staticmethod
    async def _aresume(agent_id: str, options: AgentOptions) -> SDKAgent:
        merged = options.model_copy(update={"agentId": agent_id}, deep=True)
        if not _is_cloud(merged, agent_id):
            return await resume_local_agent(agent_id, merged)
        return await resume_cloud_agent(agent_id, merged)

    @staticmethod
    def prompt(
        message: str | Mapping[str, Any] | SDKUserMessage,
        options: AgentOptions | Mapping[str, Any] | None = None,
        *,
        client: Any = None,
        model: str | ModelSelection | Mapping[str, Any] | None = None,
        api_key: str | None = None,
        name: str | None = None,
        local: LocalAgentOptions | Mapping[str, Any] | None = None,
        cloud: CloudAgentOptions | Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        **extra: Any,
    ) -> RunResult:
        _ = client
        opts = _coerce_agent_options(
            options,
            model=model,
            api_key=api_key,
            name=name,
            local=local,
            cloud=cloud,
            idempotency_key=idempotency_key,
            extra=extra,
        )
        return _run_sync_or_awaitable(Agent._prompt(message, opts))

    @staticmethod
    async def _prompt(
        message: str | Mapping[str, Any] | SDKUserMessage,
        options: AgentOptions | None = None,
    ) -> RunResult:
        opts = options or AgentOptions()
        ag = await Agent._acreate(opts)
        try:
            run = await ag.send(_coerce_send_message(message))
            result = await run.wait()
            _raise_if_auth_run_error(result)
            return result
        finally:
            await ag.aclose()

    @staticmethod
    def list(
        options: ListAgentsCloudOptions | ListAgentsLocalOptions | dict[str, Any] | None = None,
        *,
        client: Any = None,
        **kwargs: Any,
    ) -> ListResult[SDKAgentInfoCloud | SDKAgentInfoLocal]:
        _ = client
        return _run_sync_or_awaitable(Agent._alist(_merge_list_options(options, kwargs)))

    @staticmethod
    async def _alist(
        options: ListAgentsCloudOptions | ListAgentsLocalOptions | dict[str, Any] | None = None,
    ) -> ListResult[SDKAgentInfoCloud | SDKAgentInfoLocal]:
        opts: Any = options if options is not None else ListAgentsLocalOptions()
        if _wants_local_runtime(opts):
            if isinstance(opts, dict):
                opts = ListAgentsLocalOptions.model_validate({**opts, "runtime": "local"})
            elif getattr(opts, "runtime", None) != "local":
                opts = ListAgentsLocalOptions.model_validate(opts.model_dump() if hasattr(opts, "model_dump") else {})
            return await list_local_agents(opts)
        if isinstance(opts, dict):
            opts = ListAgentsCloudOptions.model_validate({**opts, "runtime": "cloud"})
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            params = {
                "limit": opts.limit,
                "cursor": opts.cursor,
                "prUrl": opts.prUrl,
                "includeArchived": opts.includeArchived,
            }
            data = await client.list_agents(params)
            items = [_map_v1_agent_to_sdk_info(x) for x in (data.get("items") or []) if isinstance(x, dict)]
            return ListResult(items=items, nextCursor=data.get("nextCursor"))
        finally:
            await client.aclose()

    @staticmethod
    def listRuns(
        agent_id: str,
        options: ListRunsCloudOptions | ListRunsLocalOptions | dict[str, Any] | None = None,
        *,
        client: Any = None,
        **kwargs: Any,
    ) -> ListResult[CloudRun | LocalRun]:
        _ = client
        return _run_sync_or_awaitable(Agent._alistRuns(agent_id, _merge_list_options(options, kwargs)))

    @staticmethod
    async def _alistRuns(
        agent_id: str,
        options: ListRunsCloudOptions | ListRunsLocalOptions | dict[str, Any] | None = None,
    ) -> ListResult[CloudRun | LocalRun]:
        opts: Any = options
        if opts is None:
            opts = ListRunsLocalOptions() if not agent_id.startswith("bc-") else ListRunsCloudOptions()
        if _wants_local_runtime(opts, agent_id):
            if isinstance(opts, dict):
                opts = ListRunsLocalOptions.model_validate({**opts, "runtime": "local"})
            return await list_local_runs(agent_id, opts)
        if isinstance(opts, dict):
            opts = ListRunsCloudOptions.model_validate({**opts, "runtime": "cloud"})
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            data = await client.list_runs(agent_id, {"limit": opts.limit, "cursor": opts.cursor})
            runs = [CloudRun.from_summary(api_key, x) for x in (data.get("items") or []) if isinstance(x, dict)]
            return ListResult(items=runs, nextCursor=data.get("nextCursor"))
        finally:
            await client.aclose()

    @staticmethod
    def getRun(
        run_id: str,
        options: GetRunOptionsCloud | GetRunOptionsLocal | dict[str, Any],
        *,
        client: Any = None,
    ) -> CloudRun | LocalRun:
        _ = client
        return _run_sync_or_awaitable(Agent._agetRun(run_id, options))

    @staticmethod
    async def _agetRun(run_id: str, options: GetRunOptionsCloud | GetRunOptionsLocal | dict[str, Any]) -> CloudRun | LocalRun:
        if _wants_local_runtime(options):
            if isinstance(options, dict):
                options = GetRunOptionsLocal.model_validate({**options, "runtime": "local"})
            return await get_local_run(run_id, options)  # type: ignore[arg-type]
        if isinstance(options, dict):
            options = GetRunOptionsCloud.model_validate(options)
        api_key = resolve_api_key(options.apiKey)
        client = CloudApiClient(api_key)
        try:
            run = await client.get_run(options.agentId, run_id)
            return CloudRun.from_summary(api_key, run)
        finally:
            await client.aclose()

    @staticmethod
    def cancelRun(
        run_id: str,
        options: GetRunOptionsCloud | GetRunOptionsLocal | dict[str, Any],
        *,
        client: Any = None,
    ) -> None:
        _ = client
        return _run_sync_or_awaitable(Agent._acancelRun(run_id, options))

    @staticmethod
    async def _acancelRun(run_id: str, options: GetRunOptionsCloud | GetRunOptionsLocal | dict[str, Any]) -> None:
        run = await Agent.getRun(run_id, options)
        try:
            await run.cancel()
        finally:
            aclose = getattr(run, "aclose", None)
            if callable(aclose):
                await aclose()

    @staticmethod
    def getUsage(
        agent_id: str,
        options: GetUsageOptions | dict[str, Any] | None = None,
        *,
        client: Any = None,
        **kwargs: Any,
    ) -> AgentUsage:
        _ = client
        return _run_sync_or_awaitable(Agent._agetUsage(agent_id, _merge_list_options(options, kwargs)))

    @staticmethod
    async def _agetUsage(agent_id: str, options: GetUsageOptions | dict[str, Any] | None = None) -> AgentUsage:
        opts = options or GetUsageOptions()
        if isinstance(opts, dict):
            opts = GetUsageOptions.model_validate(opts)
        if _wants_local_runtime(opts, agent_id):
            return await get_local_usage(agent_id, opts)
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            data = await client.get_agent_usage(agent_id, opts.runId)
            if not data:
                return empty_agent_usage()
            return agent_usage_from_v1(data)
        except errors.AgentNotFoundError:
            raise
        except errors.CursorSdkError as exc:
            if exc.status == 404:
                return empty_agent_usage()
            raise
        finally:
            await client.aclose()

    @staticmethod
    def get(
        agent_id: str,
        options: GetAgentOptions | None = None,
        *,
        client: Any = None,
        cwd: str | None = None,
        api_key: str | None = None,
    ) -> SDKAgentInfoCloud | SDKAgentInfoLocal:
        _ = client
        opts = options or GetAgentOptions()
        if cwd is not None or api_key is not None:
            payload = opts.model_dump() if hasattr(opts, "model_dump") else {}
            if cwd is not None:
                payload["cwd"] = cwd
            if api_key is not None:
                payload["api_key"] = api_key
            opts = GetAgentOptions.model_validate(payload)
        return _run_sync_or_awaitable(Agent._aget(agent_id, opts))

    @staticmethod
    async def _aget(agent_id: str, options: GetAgentOptions | None = None) -> SDKAgentInfoCloud | SDKAgentInfoLocal:
        opts = options or GetAgentOptions()
        if not agent_id.startswith("bc-"):
            return await get_local_agent(agent_id, opts)
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            raw = await client.get_agent(agent_id)
            return _map_v1_agent_to_sdk_info(raw)
        finally:
            await client.aclose()

    @staticmethod
    def archive(agent_id: str, options: AgentOperationOptions | None = None, *, client: Any = None) -> None:
        _ = client
        return _run_sync_or_awaitable(Agent._aarchive(agent_id, options))

    @staticmethod
    async def _aarchive(agent_id: str, options: AgentOperationOptions | None = None) -> None:
        opts = options or AgentOperationOptions()
        if not agent_id.startswith("bc-"):
            await archive_local_agent(agent_id, opts)
            return
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            await client.archive_agent(agent_id)
        finally:
            await client.aclose()

    @staticmethod
    def unarchive(agent_id: str, options: AgentOperationOptions | None = None, *, client: Any = None) -> None:
        _ = client
        return _run_sync_or_awaitable(Agent._aunarchive(agent_id, options))

    @staticmethod
    async def _aunarchive(agent_id: str, options: AgentOperationOptions | None = None) -> None:
        opts = options or AgentOperationOptions()
        if not agent_id.startswith("bc-"):
            await unarchive_local_agent(agent_id, opts)
            return
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            await client.unarchive_agent(agent_id)
        finally:
            await client.aclose()

    @staticmethod
    def delete(agent_id: str, options: AgentOperationOptions | None = None, *, client: Any = None) -> None:
        _ = client
        return _run_sync_or_awaitable(Agent._adelete(agent_id, options))

    @staticmethod
    async def _adelete(agent_id: str, options: AgentOperationOptions | None = None) -> None:
        opts = options or AgentOperationOptions()
        if not agent_id.startswith("bc-"):
            await delete_local_agent(agent_id, opts)
            return
        api_key = resolve_api_key(opts.apiKey)
        client = CloudApiClient(api_key)
        try:
            await client.delete_agent(agent_id)
        finally:
            await client.aclose()

    @staticmethod
    async def archive_agent(agent_id: str, options: AgentOperationOptions | None = None) -> None:
        warnings.warn(
            "Agent.archive_agent is deprecated; use Agent.archive(agent_id) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await Agent.archive(agent_id, options)

    @staticmethod
    async def unarchive_agent(agent_id: str, options: AgentOperationOptions | None = None) -> None:
        warnings.warn(
            "Agent.unarchive_agent is deprecated; use Agent.unarchive(agent_id) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await Agent.unarchive(agent_id, options)

    @staticmethod
    async def delete_agent(agent_id: str, options: AgentOperationOptions | None = None) -> None:
        warnings.warn(
            "Agent.delete_agent is deprecated; use Agent.delete(agent_id) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await Agent.delete(agent_id, options)

    class messages:  # noqa: A001
        @staticmethod
        def list(agent_id: str, options: GetAgentMessagesOptions | None = None) -> Any:
            return _run_sync_or_awaitable(Agent.messages._alist(agent_id, options))

        @staticmethod
        async def _alist(agent_id: str, options: GetAgentMessagesOptions | None = None) -> list[Any]:
            opts = options or GetAgentMessagesOptions()
            if _wants_local_runtime(opts, agent_id):
                return await list_local_agent_messages(agent_id, opts)
            api_key = resolve_api_key(opts.apiKey)
            client = CloudApiClient(api_key)
            try:
                return await client.list_agent_messages(
                    agent_id,
                    limit=opts.limit,
                    offset=opts.offset,
                )
            except errors.CursorSdkError as exc:
                if exc.status == 404:
                    return []
                raise
            finally:
                await client.aclose()

    list_runs = listRuns
    get_run = getRun
    cancel_run = cancelRun
    get_usage = getUsage


class _AgentLifecycle:
    """Deprecated. Use `Agent.archive(id)` / `Agent.unarchive(id)` / `Agent.delete(id)`."""

    async def archive(self, agent_id: str, options: AgentOperationOptions | None = None) -> None:
        warnings.warn(
            "Agent.lifecycle.archive is deprecated; use Agent.archive(agent_id) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await Agent.archive(agent_id, options)

    async def unarchive(self, agent_id: str, options: AgentOperationOptions | None = None) -> None:
        warnings.warn(
            "Agent.lifecycle.unarchive is deprecated; use Agent.unarchive(agent_id) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await Agent.unarchive(agent_id, options)

    async def delete(self, agent_id: str, options: AgentOperationOptions | None = None) -> None:
        warnings.warn(
            "Agent.lifecycle.delete is deprecated; use Agent.delete(agent_id) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await Agent.delete(agent_id, options)


Agent.lifecycle = _AgentLifecycle()
