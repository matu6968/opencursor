from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Awaitable

from opencursor import messages


class RunInteractionAccumulator:
    """Minimal accumulator: collects assistant / user / tool_call / thinking messages as coarse turns."""

    def __init__(
        self,
        *,
        on_step: Callable[..., Any] | None = None,
        on_delta: Callable[..., Any] | None = None,
    ) -> None:
        self._on_step = on_step
        self._on_delta = on_delta
        self._turns: list[dict[str, Any]] = []

    def conversation(self) -> list[dict[str, Any]]:
        return list(self._turns)

    async def apply_interaction_update(self, data: dict[str, Any]) -> None:
        if self._on_delta:
            maybe = self._on_delta({"update": data})
            if hasattr(maybe, "__await__"):
                await maybe  # type: ignore[func-returns-value]

    async def push_sdk_message(self, msg: messages.SDKMessage) -> None:
        self._turns.append(dict(msg))
        if self._on_step:
            maybe = self._on_step({"step": {"type": "message", "message": msg}})
            if hasattr(maybe, "__await__"):
                await maybe  # type: ignore[func-returns-value]

    async def flush_pending_step(self) -> None:
        return


async def accumulate_sdk_message_stream(stream: AsyncIterator[messages.SDKMessage]) -> list[dict[str, Any]]:
    acc = RunInteractionAccumulator()
    async for m in stream:
        await acc.push_sdk_message(m)
    return acc.conversation()
