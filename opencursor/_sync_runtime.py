"""Private process-wide asyncio loop so documented sync Agent/Run APIs can drive the in-process runtime.

Not a Node bridge and not a public Client. Used only when no event loop is already running.
"""

from __future__ import annotations

import atexit
import asyncio
import threading
from collections.abc import AsyncIterator, Coroutine, Iterator
from typing import Any, TypeVar

T = TypeVar("T")


class _BackgroundLoop:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="opencursor-loop", daemon=True)
            self._thread.start()
            self._ready.wait(timeout=10)
            if self._loop is None:
                raise RuntimeError("failed to start opencursor background loop")
            return self._loop

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        self._loop = None

    def submit(self, coro: Coroutine[Any, Any, T]) -> T:
        loop = self.start()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    def stop(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            if loop is None or not loop.is_running():
                self._thread = None
                return
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=5)
            self._thread = None


_LOOP = _BackgroundLoop()
atexit.register(_LOOP.stop)


def loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_sync_or_awaitable(coro: Coroutine[Any, Any, T]) -> T | Coroutine[Any, Any, T]:
    if loop_running():
        return coro
    return _LOOP.submit(coro)


def iterate_async(agen: AsyncIterator[T]) -> Iterator[T] | AsyncIterator[T]:
    if loop_running():
        return agen

    def _iter() -> Iterator[T]:
        try:
            while True:
                try:
                    yield _LOOP.submit(agen.__anext__())  # type: ignore[arg-type]
                except StopAsyncIteration:
                    break
        finally:
            aclose = getattr(agen, "aclose", None)
            if callable(aclose):
                try:
                    _LOOP.submit(aclose())
                except Exception:
                    pass

    return _iter()


def close_sync(aclose_coro: Coroutine[Any, Any, Any]) -> None:
    if loop_running():
        aclose_coro.close()
        return
    _LOOP.submit(aclose_coro)
