"""Where a run spends its time (logged), and whether it left work undone (repair issue)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import session_scope

from .const import DOMAIN, ISSUE_FALLING_BEHIND
from .profiles import DAY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .profiles import Schedule


class Timers:
    """Wall seconds per kind of work on the recorder thread.

    `lap()` starts a stretch, `add(name)` books the time since the last lap or add under
    `name` and starts the next one. One `time.monotonic()` call per booking, no per-row timing.
    """

    def __init__(self) -> None:
        # plan: selecting and deciding; delete: relink, protect and delete statements;
        # commit: closing the session; step: every step on the recorder thread, in total;
        # repack: the database repack, which runs inside a step
        self.seconds: dict[str, float] = {
            "plan": 0.0,
            "delete": 0.0,
            "commit": 0.0,
            "step": 0.0,
            "repack": 0.0,
        }
        self._t = time.monotonic()

    def lap(self) -> None:
        self._t = time.monotonic()

    def add(self, name: str) -> None:
        now = time.monotonic()
        self.seconds[name] = self.seconds.get(name, 0.0) + now - self._t
        self._t = now

    @contextmanager
    def write_scope(self, instance: Any) -> Iterator[Any]:
        """A write session. What runs after the last booking inside it, up to and including
        the commit, is booked as `commit`."""
        self.lap()
        try:
            with session_scope(session=instance.get_session()) as session:
                yield session
        finally:
            self.add("commit")


def run_log_line(timers: Timers, seconds: float, backoff: float, queue_wait: float) -> str:
    """The INFO line at the end of a real run: where its time went, in seconds.

    plan: selecting and deciding; delete: relink, protect and delete statements; commit:
    closing the write sessions; recorder other: the rest of the job's steps on the recorder
    thread; recorder wait: the event loop waiting for a step beyond its run time (recorder
    queue); backoff: sleeps while the recorder queue was too long; other: everything else.
    """
    t = timers.seconds
    inside = t["plan"] + t["delete"] + t["commit"]
    other = max(0.0, seconds - t["step"] - queue_wait - backoff)
    return (
        f"total {seconds:.1f} s: plan {t['plan']:.1f} s, delete {t['delete']:.1f} s, "
        f"commit {t['commit']:.1f} s, recorder other {max(0.0, t['step'] - inside):.1f} s, "
        f"recorder wait {queue_wait:.1f} s, backoff {backoff:.1f} s, other {other:.1f} s, "
        f"repack {t['repack']:.1f} s"
    )


def note_unfinished(hass: HomeAssistant, days: int) -> None:
    """Raise the falling-behind issue when a real run ended with thinning left, else clear it.

    The run's own facts decide: the thinning pass runs through every day older than the raw
    window, so days left mean the run stopped early (the recorder backlog did not drain, or
    the thinning phase failed). A run that gets through them all clears the issue.
    """
    if days > 0:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_FALLING_BEHIND,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_FALLING_BEHIND,
            translation_placeholders={"days": str(days)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_FALLING_BEHIND)


def unfinished_days(
    min_ts: float | None,
    now: float,
    cutoff: float,
    schedules: dict[str, Schedule],
    applied: dict[str, float],
) -> int:
    """Days older than the raw window that still have thinning to do for some profile."""
    if min_ts is None:
        return 0
    count = 0
    for day in range(int(min_ts // DAY), int(cutoff // DAY)):
        age = (now - (day + 1) * DAY) / DAY
        for profile, sched in schedules.items():
            k = sched.target_index(age)
            if (
                k is not None
                and k >= 0
                and applied.get(f"{day}|{profile}", 0.0) < sched.tiers[k].cfg.bucket
            ):
                count += 1
                break
    return count
