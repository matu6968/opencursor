from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, AsyncIterator

from opencursor import errors, messages
from opencursor.types import AgentMessage, AgentUsage, RunStreamEvent, RunUsage, TokenUsage


def empty_token_usage() -> TokenUsage:
    return TokenUsage(
        inputTokens=0,
        outputTokens=0,
        cacheReadTokens=0,
        cacheWriteTokens=0,
        totalTokens=0,
    )


def empty_agent_usage() -> AgentUsage:
    return AgentUsage(usage=empty_token_usage(), runs=[])


def token_usage_from_mapping(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    keys = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "input_tokens", "output_tokens")
    if not any(k in raw for k in keys):
        return None
    input_tokens = int(raw.get("inputTokens") or raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("outputTokens") or raw.get("output_tokens") or 0)
    cache_read = int(raw.get("cacheReadTokens") or raw.get("cache_read_tokens") or 0)
    cache_write = int(raw.get("cacheWriteTokens") or raw.get("cache_write_tokens") or 0)
    total = int(raw.get("totalTokens") or raw.get("total_tokens") or (input_tokens + output_tokens + cache_read + cache_write))
    usage = TokenUsage(
        inputTokens=input_tokens,
        outputTokens=output_tokens,
        cacheReadTokens=cache_read,
        cacheWriteTokens=cache_write,
        totalTokens=total,
    )
    reasoning = raw.get("reasoningTokens") if raw.get("reasoningTokens") is not None else raw.get("reasoning_tokens")
    if reasoning is not None:
        usage.reasoningTokens = int(reasoning)
    return usage


def token_usage_from_turn_ended(ended: dict[str, Any]) -> TokenUsage | None:
    if not any(ended.get(k) is not None for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")):
        return None
    input_tokens = int(ended.get("input_tokens") or 0)
    output_tokens = int(ended.get("output_tokens") or 0)
    cache_read = int(ended.get("cache_read_tokens") or 0)
    cache_write = int(ended.get("cache_write_tokens") or 0)
    usage = TokenUsage(
        inputTokens=input_tokens,
        outputTokens=output_tokens,
        cacheReadTokens=cache_read,
        cacheWriteTokens=cache_write,
        totalTokens=input_tokens + output_tokens + cache_read + cache_write,
    )
    if ended.get("reasoning_tokens") is not None:
        usage.reasoningTokens = int(ended["reasoning_tokens"])
    return usage


def sum_token_usage(items: list[TokenUsage]) -> TokenUsage | None:
    if not items:
        return None
    out = empty_token_usage()
    reasoning = 0
    has_reasoning = False
    for item in items:
        out.inputTokens += item.inputTokens
        out.outputTokens += item.outputTokens
        out.cacheReadTokens += item.cacheReadTokens
        out.cacheWriteTokens += item.cacheWriteTokens
        out.totalTokens += item.totalTokens
        if item.reasoningTokens is not None:
            has_reasoning = True
            reasoning += item.reasoningTokens
    if has_reasoning:
        out.reasoningTokens = reasoning
    return out


def agent_usage_from_v1(payload: dict[str, Any]) -> AgentUsage:
    total = token_usage_from_mapping(payload.get("totalUsage") or payload.get("usage")) or empty_token_usage()
    cost_raw = payload.get("cost")
    cost = None
    if isinstance(cost_raw, dict) and ("rawCostCents" in cost_raw or "chargedCents" in cost_raw):
        from opencursor.types import UsageCost

        cost = UsageCost(
            rawCostCents=float(cost_raw.get("rawCostCents") or 0),
            chargedCents=float(cost_raw.get("chargedCents") or 0),
        )
    runs: list[RunUsage] = []
    for item in payload.get("runs") or []:
        if not isinstance(item, dict):
            continue
        usage = token_usage_from_mapping(item.get("usage"))
        if usage is None:
            continue
        run_cost = None
        rc = item.get("cost")
        if isinstance(rc, dict) and ("rawCostCents" in rc or "chargedCents" in rc):
            from opencursor.types import UsageCost

            run_cost = UsageCost(
                rawCostCents=float(rc.get("rawCostCents") or 0),
                chargedCents=float(rc.get("chargedCents") or 0),
            )
        runs.append(RunUsage(runId=str(item.get("id") or item.get("runId") or ""), usage=usage, cost=run_cost))
    return AgentUsage(usage=total, cost=cost, runs=runs)


def ensure_run_operation(run: Any, operation: str) -> None:
    if not run.supports(operation):
        raise errors.UnsupportedRunOperationError(operation, run.unsupported_reason(operation))


def assistant_text_from_message(message: messages.SDKMessage) -> str:
    if message.get("type") != "assistant":
        return ""
    payload = message.get("message")
    if hasattr(payload, "content"):
        content = getattr(payload, "content")
    elif isinstance(payload, dict):
        content = payload.get("content")
    else:
        content = payload.get("content") if hasattr(payload, "get") else None
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "".join(parts)


async def iter_run_messages(run: Any) -> AsyncIterator[messages.SDKMessage]:
    ensure_run_operation(run, "stream")
    async for message in run.stream():
        yield message


async def iter_run_text(run: Any) -> AsyncIterator[str]:
    async for message in iter_run_messages(run):
        text = assistant_text_from_message(message)
        if text:
            yield text


async def run_text(run: Any) -> str:
    result = await run.wait()
    if result.result:
        return result.result
    parts: list[str] = []
    async for chunk in iter_run_text(run):
        parts.append(chunk)
    return "".join(parts)


async def iter_run_events(run: Any) -> AsyncIterator[dict[str, Any]]:
    async for message in iter_run_messages(run):
        yield {"type": "sdk_message", "sdk_message": message}


def agent_messages_from_rows(rows: list[Any]) -> list[AgentMessage]:
    out: list[AgentMessage] = []
    for item in rows:
        if isinstance(item, AgentMessage):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(AgentMessage.from_json(item))
    return out


def _json_ready(value: Any) -> Any:
    dumped = getattr(value, "model_dump", None)
    if callable(dumped):
        return dumped()
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def conversation_json_from_turns(turns: list[Any]) -> str:
    return json.dumps(_json_ready(turns))


def run_stream_event_from_sdk_message(message: messages.SDKMessage, offset: str | None = None) -> RunStreamEvent:
    return RunStreamEvent(kind="sdk_message", offset=offset, sdk_message=message)


def run_stream_event_from_local_payload(payload: Any, offset: str | None = None) -> RunStreamEvent | None:
    try:
        decoded = messages.decode_local_run_stream_event(payload)
    except errors.ConfigurationError:
        return None
    kind = decoded.get("type")
    if kind == "sdk_message":
        return RunStreamEvent(kind="sdk_message", offset=offset, sdk_message=decoded.get("message"))
    if kind == "result":
        return RunStreamEvent(kind="result", offset=offset, result=dict(decoded), result_is_full=False)
    if kind == "done":
        return RunStreamEvent(kind="done", offset=offset, done=dict(decoded))
    return None
