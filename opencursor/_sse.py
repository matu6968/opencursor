from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class SseEvent:
    id: str | None
    event: str | None
    data: str


def _parse_sse_block(block: str) -> SseEvent | None:
    block = block.strip("\n")
    if not block:
        return None
    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []
    for line in block.split("\n"):
        if line.startswith("id: "):
            event_id = line[4:].strip() or None
        elif line.startswith("event: "):
            event_name = line[7:].strip() or None
        elif line.startswith("data: "):
            data_lines.append(line[6:])
    if not data_lines and event_name in ("done", "heartbeat"):
        return SseEvent(id=event_id, event=event_name, data="")
    if not data_lines:
        return SseEvent(id=event_id, event=event_name, data="")
    return SseEvent(id=event_id, event=event_name, data="\n".join(data_lines))


async def iter_sse_events(
    byte_iter: AsyncIterator[bytes],
    *,
    encoding: str = "utf-8",
) -> AsyncIterator[SseEvent]:
    buffer = ""
    decoder = ""
    async for chunk in byte_iter:
        buffer += chunk.decode(encoding, errors="replace")
        while True:
            sep = buffer.find("\n\n")
            if sep == -1:
                break
            raw_block = buffer[:sep]
            buffer = buffer[sep + 2 :]
            ev = _parse_sse_block(raw_block)
            if ev is not None:
                yield ev
