"""Recorder compatibility guard and the purge takeover.

Sediment writes to tables it does not own and flips two attributes of the recorder
instance. Both are only safe against recorder code it was tested with, so every startup
(with the takeover ticked) runs `check_compat` first. Any mismatch means: no takeover, a
Repairs issue, and the recorder stays exactly as it was.

This module imports nothing from the recorder at module level, so a recorder that moved
things around cannot stop the integration from loading.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import logging
import math
import time
from typing import Any

from .const import SUPPORTED_SCHEMA_VERSIONS

_LOGGER = logging.getLogger(__name__)

REQUIRED_ATTRS = (
    "keep_days",
    "auto_purge",
    "auto_repack",
    "backlog",
    "max_bind_vars",
    "queue_task",
    "get_session",
    "engine",
    "database_engine",
    "states_manager",
    "state_attributes_manager",
    "event_data_manager",
    "recorder_runs_manager",
    "states_meta_manager",
    "event_type_manager",
)
REQUIRED_METHODS = (
    ("states_manager", "evict_purged_entity_ids"),
    ("states_meta_manager", "evict_purged"),
    ("event_type_manager", "evict_purged"),
    ("states_manager", "evict_purged_state_ids"),
    ("states_manager", "load_from_db"),
    ("recorder_runs_manager", "load_from_db"),
    ("state_attributes_manager", "evict_purged"),
    ("event_data_manager", "evict_purged"),
)
PURGE_TASK_FIELDS = ("purge_before", "repack", "apply_filter")


def dialect_problem(instance: Any) -> str | None:
    """Sediment writes only to SQLite for now; say why anything else is refused."""
    name = getattr(instance, "dialect_name", None)
    text = str(getattr(name, "value", name) or "").lower()
    if not text:
        text = str(getattr(instance, "db_url", "") or "").split(":", 1)[0].lower()
    if text.startswith("sqlite"):
        return None
    return f"the recorder database is {text or 'unknown'}, Sediment supports SQLite only for now"


def check_compat(
    instance: Any,
    schema_versions: frozenset[int] | None = None,
    require_sqlite: bool = True,
) -> list[str]:
    """Return the reasons the takeover and the DB writes are not safe (empty = ok).

    `require_sqlite=False` is for read-only work (a dry run).
    """
    allowed = SUPPORTED_SCHEMA_VERSIONS if schema_versions is None else schema_versions
    problems: list[str] = []
    if require_sqlite and (reason := dialect_problem(instance)):
        problems.append(reason)

    try:
        db_schema = importlib.import_module("homeassistant.components.recorder.db_schema")
        version = getattr(db_schema, "SCHEMA_VERSION", None)
    except Exception as err:
        problems.append(f"cannot import the recorder schema ({err})")
        version = None
    else:
        if version not in allowed:
            problems.append(
                f"recorder schema version {version} is not in the tested set {sorted(allowed)}"
            )
        for model in ("States", "StateAttributes", "StatesMeta", "Events", "EventData"):
            if not hasattr(db_schema, model):
                problems.append(f"recorder schema has no {model} model")

    for name in REQUIRED_ATTRS:
        if not hasattr(instance, name):
            problems.append(f"recorder has no attribute '{name}'")
    for owner, method in REQUIRED_METHODS:
        obj = getattr(instance, owner, None)
        if obj is None or not callable(getattr(obj, method, None)):
            problems.append(f"recorder.{owner}.{method} is missing")
    if hasattr(instance, "keep_days") and not isinstance(instance.keep_days, int):
        problems.append("recorder.keep_days is not an integer")
    if hasattr(instance, "auto_purge") and not isinstance(instance.auto_purge, bool):
        problems.append("recorder.auto_purge is not a bool")
    if getattr(instance, "use_legacy_events_index", False):
        problems.append("the recorder still has legacy event-linked state rows")

    # The nightly purge must still read both attributes at call time.
    nightly = getattr(type(instance), "async_nightly_tasks", None)
    if nightly is not None:
        try:
            src = inspect.getsource(nightly)
        except OSError, TypeError:
            src = None
        if src is not None and not ("self.auto_purge" in src and "self.keep_days" in src):
            problems.append("recorder nightly task no longer reads auto_purge and keep_days")

    try:
        tasks = importlib.import_module("homeassistant.components.recorder.tasks")
        base = tasks.RecorderTask
        code = base.run.__code__
        sig = list(code.co_varnames[: code.co_argcount])
        if sig != ["self", "instance"]:
            problems.append(f"RecorderTask.run signature is {sig}")
        if not dataclasses.is_dataclass(base):
            problems.append("RecorderTask is not a dataclass")
        purge_fields = tuple(f.name for f in dataclasses.fields(tasks.PurgeTask))
        if purge_fields != PURGE_TASK_FIELDS:
            problems.append(f"PurgeTask fields are {purge_fields}")
    except Exception as err:
        problems.append(f"cannot inspect RecorderTask/PurgeTask ({err})")

    try:
        helpers = importlib.import_module("homeassistant.helpers.recorder")
        sig = inspect.signature(helpers.session_scope).parameters
        var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.values())
        if not var_kw and not {"session", "read_only"} <= set(sig):
            problems.append("session_scope lost its session/read_only arguments")
        repack = importlib.import_module("homeassistant.components.recorder.repack")
        if not callable(getattr(repack, "repack_database", None)):
            problems.append("recorder.repack.repack_database is missing")
    except Exception as err:
        problems.append(f"cannot inspect the recorder helpers ({err})")
    return problems


def snapshot_values(instance: Any) -> dict[str, Any]:
    """The recorder's purge settings as configured at startup."""
    return {
        "auto_purge": getattr(instance, "auto_purge", None),
        "keep_days": getattr(instance, "keep_days", None),
        "auto_repack": getattr(instance, "auto_repack", None),
    }


def oldest_row_age_days(instance: Any) -> float | None:
    """Age in days of the oldest `states` row. Plain SQL, so it works if models moved."""
    from sqlalchemy import text

    session = instance.get_session()
    try:
        ts = session.execute(text("SELECT MIN(last_updated_ts) FROM states")).scalar()
    finally:
        session.close()
    return None if ts is None else (time.time() - float(ts)) / 86400.0


class Takeover:
    """Hold the recorder's own purge settings aside while Sediment owns purging.

    `startup` is what the recorder was configured with when Home Assistant started, taken
    before Sediment changed anything (the manager keeps it in `hass.data` so a reload does
    not mistake Sediment's own values for the originals).
    """

    def __init__(self, instance: Any, startup: dict[str, Any] | None = None) -> None:
        self._instance = instance
        self.startup = startup or snapshot_values(instance)
        self.active = False

    @property
    def originals(self) -> dict[str, Any] | None:
        """The values to go back to, while the takeover is applied."""
        return self.startup if self.active else None

    def engage(self, horizon_days: int) -> None:
        """Disable the recorder's auto purge and align keep_days with the horizon."""
        inst = self._instance
        if not self.active:
            _LOGGER.info(
                "Sediment takes over purging; recorder had auto_purge=%s keep_days=%s "
                "auto_repack=%s",
                self.startup["auto_purge"],
                self.startup["keep_days"],
                self.startup["auto_repack"],
            )
        inst.auto_purge = False
        inst.keep_days = int(horizon_days)
        self.active = True

    def restore(self) -> None:
        """Give the recorder its startup values back (hands purging back to Home Assistant)."""
        inst = self._instance
        if (
            inst.auto_purge != self.startup["auto_purge"]
            or inst.keep_days != self.startup["keep_days"]
        ):
            _LOGGER.info(
                "Sediment released purging; recorder has auto_purge=%s keep_days=%s again",
                self.startup["auto_purge"],
                self.startup["keep_days"],
            )
        inst.auto_purge = self.startup["auto_purge"]
        inst.keep_days = self.startup["keep_days"]
        self.active = False

    def protect(self, oldest_age_days: float | None = None, horizon_days: float = 0) -> str | None:
        """Keep the recorder from purging old history when the guard failed.

        Turns `auto_purge` off if the attribute exists and, in any case, raises `keep_days`
        to the largest of its startup value, the history length and the age of the oldest
        row plus a day, so that a `recorder.purge` call without `keep_days` (a service call,
        an automation) cannot wipe the history either. Returns what was done, or None.
        """
        inst = self._instance
        done = []
        if hasattr(inst, "auto_purge"):
            inst.auto_purge = False
            done.append("auto_purge")
        if hasattr(inst, "keep_days"):
            floor = math.ceil(oldest_age_days) + 1 if oldest_age_days is not None else 36500
            keep = int(self.startup.get("keep_days") or 0)
            inst.keep_days = max(keep, int(horizon_days), floor)
            done.append("keep_days")
        return "+".join(done) or None
