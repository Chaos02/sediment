"""Running work on the recorder thread."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from homeassistant.components.recorder.tasks import RecorderTask


# ---------------------------------------------------------------- recorder thread plumbing
@dataclass(slots=True)
class CallableTask(RecorderTask):
    """Run a function on the recorder thread and hand the result back to the event loop."""

    fn: Callable[[Any], Any]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future

    def run(self, instance: Any) -> None:
        try:
            result = self.fn(instance)
        except BaseException as err:
            self.loop.call_soon_threadsafe(_set_exception, self.future, err)
        else:
            self.loop.call_soon_threadsafe(_set_result, self.future, result)


def _set_result(fut: asyncio.Future, result: Any) -> None:
    if not fut.done():
        fut.set_result(result)


def _set_exception(fut: asyncio.Future, err: BaseException) -> None:
    if not fut.done():
        fut.set_exception(err)


def run_on_recorder(instance: Any, fn: Callable[[Any], Any]) -> asyncio.Future:
    """Queue `fn(instance)` on the recorder thread; await the returned future."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    instance.queue_task(CallableTask(fn, loop, fut))
    return fut


def chunks(items: list[int], size: int) -> Iterator[list[int]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]
