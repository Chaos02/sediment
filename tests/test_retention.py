"""Who owns purging, and what happens to thinned history when that changes.

The scenarios come from an independent review: every route that put the recorder's own
purge back used to hand the thinned history to it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import time
from unittest.mock import patch

from homeassistant.components.recorder.db_schema import (
    EventData,
    Events,
    EventTypes,
    StatesMeta,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.tasks import PurgeTask
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance, session_scope
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from sqlalchemy import func, select

from custom_components.sediment.compat import Takeover
from custom_components.sediment.const import DOMAIN
from custom_components.sediment.recorder_bridge import run_on_recorder

from .common import DAY, integrity, midnight_days_ago, read, seed, snapshot
from .test_integration import _flat_day, setup_entry


def _pts(d, n=200):
    return [
        (d + 60 * i + 1, f"{3 + (i % 2) * 0.1:.1f}", {"unit_of_measurement": "C"}) for i in range(n)
    ]


async def _nightly_purge(hass, instance):
    """What the recorder does at 04:12, then drain its queue.

    The test plugin replaces `Recorder.async_nightly_tasks` with a no-op unless a test is
    parametrised with `enable_nightly_purge=True`, so this queues the purge the same way
    the real method does. `test_purge_helper_deletes_unprotected_history` is the control
    that shows it really deletes when nothing protects the history.
    """
    if instance.auto_purge:
        instance.queue_task(
            PurgeTask(dt_util.utcnow() - timedelta(days=instance.keep_days), False, False)
        )
    for _ in range(5):
        await run_on_recorder(instance, lambda inst: None)
        await hass.async_block_till_done()


async def _all_alive(instance, ids) -> bool:
    alive = {r[0] for r in await snapshot(instance)}
    return all(i in alive for i in ids)


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


# ---------------------------------------------------------------- 1. ways to lose the takeover
async def test_dry_run_toggle_keeps_the_takeover(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    assert instance.auto_purge is False and instance.keep_days == 1830
    assert entry.runtime_data.owned
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dry_run": True})
    await hass.async_block_till_done()
    assert instance.auto_purge is False and instance.keep_days == 1830
    await _nightly_purge(hass, instance)
    after = {r[0] for r in await snapshot(instance)}
    assert all(i in after for i in old), "the 100-day-old history survived the nightly purge"


async def test_dry_run_pauses_thinning_but_housekeeping_runs(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    now = time.time()
    flat = await seed(instance, "sensor.flat", _flat_day(midnight_days_ago(20)))
    beyond = await seed(instance, "sensor.beyond", _pts(midnight_days_ago(100)))
    droppy = await seed(instance, "sensor.droppy", _pts(midnight_days_ago(30)))

    def add_event(inst):
        with session_scope(session=inst.get_session()) as s:
            et = EventTypes(event_type="old_event")
            s.add(et)
            s.flush()
            s.add(Events(event_type_id=et.event_type_id, time_fired_ts=now - 30 * DAY))

    await run_on_recorder(instance, add_event)
    entry = await setup_entry(
        hass,
        dry_run=False,
        takeover=True,
        history_days=60,
        profile_rules="sensor.droppy = drop",
    )
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dry_run": True})
    await hass.async_block_till_done()
    report = await entry.runtime_data.async_run(None)
    after = {r[0] for r in await snapshot(instance)}
    assert report["mode"] == "dry_run" and report["stats"]["housekeeping"]
    assert all(i in after for i in flat), "thinning is paused"
    assert all(i in after for i in droppy), "a drop rule under test is not applied"
    assert not any(i in after for i in beyond), "the history length still applies"
    assert report["stats"]["events_deleted"] == 1


async def test_guard_failure_at_start_keeps_purge_off(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    assert await hass.config_entries.async_unload(entry.entry_id)
    # a restart: the recorder is back to its configured values
    instance.auto_purge, instance.keep_days = True, 10
    # ... on a Home Assistant version Sediment does not know yet
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert instance.auto_purge is False
    assert instance.keep_days == 1830, "a purge call without keep_days cannot wipe the history"
    assert _issue(hass, "takeover_protecting") is not None
    assert _issue(hass, "takeover_blocked") is None
    await _nightly_purge(hass, instance)
    after = {r[0] for r in await snapshot(instance)}
    assert all(i in after for i in old)
    # and no thinning happens
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        assert await entry.runtime_data.async_run(False) is None


async def test_guard_failure_never_applied_leaves_recorder_alone(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        entry = await setup_entry(hass, dry_run=False, takeover=True)
    assert instance.auto_purge is True and instance.keep_days == 10
    assert not entry.runtime_data.owned
    assert _issue(hass, "takeover_blocked") is not None


async def test_guard_failure_at_job_time_does_not_restore(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    manager = entry.runtime_data
    with patch("custom_components.sediment.manager.check_compat", return_value=["boom"]):
        assert await manager.async_run(False) is None
    assert instance.auto_purge is False and manager.takeover.active and manager.owned
    assert _issue(hass, "takeover_protecting") is not None


def test_protect_falls_back_to_keep_days() -> None:
    class Fake:
        keep_days = 10

    fake = Fake()
    takeover = Takeover(fake, {"auto_purge": True, "keep_days": 10, "auto_repack": True})
    assert takeover.protect(oldest_age_days=99.2) == "keep_days"
    assert fake.keep_days == 101
    fake.keep_days = 10
    assert takeover.protect(None) == "keep_days" and fake.keep_days == 36500
    fake.keep_days = 10
    assert takeover.protect(50, horizon_days=1830) == "keep_days" and fake.keep_days == 1830

    class Both:
        auto_purge = True
        keep_days = 10

    both = Both()
    protected = Takeover(both, {"auto_purge": True, "keep_days": 10, "auto_repack": True})
    assert protected.protect(100, horizon_days=1830) == "auto_purge+keep_days"
    assert both.auto_purge is False and both.keep_days == 1830


async def test_unload_restores_nothing_while_owned_remove_restores_and_tells(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=400)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert instance.auto_purge is False and instance.keep_days == 400
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert instance.auto_purge is False and entry.runtime_data.owned
    with patch("custom_components.sediment.manager.persistent_notification.async_create") as notify:
        assert await hass.config_entries.async_remove(entry.entry_id) is not None
        await hass.async_block_till_done()
    assert instance.auto_purge is True and instance.keep_days == 10
    assert notify.call_count == 1
    assert "10 days" in notify.call_args.args[1]
    assert notify.call_args.kwargs["notification_id"] == "sediment_removed"


async def test_disabling_keeps_the_history_protected_and_tells(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=400)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dry_run": True})
    await hass.async_block_till_done()
    assert instance.auto_purge is False and entry.runtime_data.owned
    with patch("custom_components.sediment.manager.persistent_notification.async_create") as notify:
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        await hass.async_block_till_done()
    # nothing was handed back: purging stays off until the next restart
    assert instance.auto_purge is False and instance.keep_days == 400
    assert notify.call_count == 1
    assert "restart" in notify.call_args.args[1]
    assert notify.call_args.kwargs["notification_id"] == "sediment_disabled"
    await _nightly_purge(hass, instance)
    assert await _all_alive(instance, old)
    # enabling again engages the owned takeover, also with dry run on
    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert instance.auto_purge is False and instance.keep_days == 400
    assert entry.runtime_data.owned
    await _nightly_purge(hass, instance)
    assert await _all_alive(instance, old)


async def test_untick_asks_for_confirmation_then_releases(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=400)
    flow = hass.config_entries.options
    r = await flow.async_init(entry.entry_id)
    r = await flow.async_configure(r["flow_id"], {"history_choice": "custom", "custom_days": 400})
    r = await flow.async_configure(r["flow_id"], {"resolution": "standard"})
    r = await flow.async_configure(r["flow_id"], {"profile_rules": "", "flapping_collapse": True})
    assert r["step_id"] == "purging"
    # the form shows the values from before the takeover
    assert r["description_placeholders"]["keep_days"] == "10"
    untick = {"takeover": False, "raw_days": 10, "dry_run": False}
    r = await flow.async_configure(r["flow_id"], untick)
    assert r["step_id"] == "release_confirm"
    assert r["description_placeholders"]["keep_days"] == "10"
    # declining keeps the takeover and goes back
    r = await flow.async_configure(r["flow_id"], {"release": False})
    assert r["step_id"] == "purging"
    r = await flow.async_configure(r["flow_id"], untick)
    assert r["step_id"] == "release_confirm"
    r = await flow.async_configure(r["flow_id"], {"release": True})
    assert r["step_id"] == "confirm"
    assert instance.auto_purge is False, "nothing is released before the flow is finished"
    r = await flow.async_configure(r["flow_id"], {})
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()
    assert instance.auto_purge is True and instance.keep_days == 10
    assert not entry.runtime_data.owned


async def test_safety_net_issue(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    assert instance.auto_purge is True
    entry = await setup_entry(hass, dry_run=False, takeover=True)
    assert _issue(hass, "safety_net") is not None
    # not ticked: no advice needed
    hass.config_entries.async_update_entry(entry, options={**entry.options, "takeover": False})
    await hass.async_block_till_done()
    assert _issue(hass, "safety_net") is None


async def test_no_safety_net_issue_when_yaml_already_disables_purge(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    instance.auto_purge = False  # recorder: auto_purge: false in configuration.yaml
    await setup_entry(hass, dry_run=False, takeover=True)
    assert _issue(hass, "safety_net") is None


# ---------------------------------------------------------------- 2. attribute cache
async def _climate_job(hass, prefill: int):
    from custom_components.sediment.manager import build_meta
    from custom_components.sediment.recorder_bridge import Job, JobConfig

    instance = get_instance(hass)
    d = midnight_days_ago(20)
    pts = []
    for i in range(1200):
        temp = 20.0 + (0.1 if i % 2 else 0.0)
        if i == 600:
            temp = 25.0
        pts.append(
            (
                d + 60 * i + 1,
                "heat",
                {"current_temperature": temp, "temperature": 21.0, "hvac_action": "idle"},
            )
        )
    ids = await seed(instance, "climate.living", pts)
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    s = manager.settings
    cfg = JobConfig(
        dry_run=False,
        now=time.time(),
        raw_days=10.0,
        horizon_days=1830.0,
        coarse_horizon_days=365.0,
        schedules=s.schedules(),
        rules=[],
        flap_default=True,
        takeover=False,
        meta=build_meta(hass),
    )
    job = Job(cfg, manager.data)

    def prep(inst):
        with session_scope(session=inst.get_session(), read_only=True) as ses:
            job._emap = dict(
                ses.execute(select(StatesMeta.metadata_id, StatesMeta.entity_id)).all()
            )
            mid = next(m for m, e in job._emap.items() if e == "climate.living")
            rows = Job._load_rows(ses, mid, d, d + DAY)
            job._entity_info(ses, inst, mid, rows)
        job._attr_cache.clear()
        for k in range(prefill):  # attributes cached from earlier entity-days of this job
            job._attr_cache[-1 - k] = {}

    await run_on_recorder(instance, prep)
    while not await run_on_recorder(instance, job.step):
        pass
    after = {r[0] for r in await snapshot(instance)}
    return ids, after, job


async def test_climate_control(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    ids, after, job = await _climate_job(hass, 0)
    assert job.stats.errors == []
    assert ids[600] in after


async def test_full_attribute_cache_does_not_wipe_attributes(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    ids, after, job = await _climate_job(hass, 199_999)
    assert job.stats.errors == []
    assert ids[600] in after, "the 25 degree spike survives a cache at its limit"
    assert len([i for i in ids if i in after]) < 0.3 * len(ids)


# ---------------------------------------------------------------- 3 and 4. cleanup
async def test_cleanup_respects_the_recorders_original_keep_days(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    instance.keep_days = 60  # the user kept 60 days of events
    now = time.time()

    def add(inst):
        with session_scope(session=inst.get_session()) as s:
            et = EventTypes(event_type="automation_triggered")
            s.add(et)
            s.flush()
            ed = EventData(shared_data='{"x":1}', hash=7)
            s.add(ed)
            s.flush()
            for age in (20, 70):
                s.add(
                    Events(
                        event_type_id=et.event_type_id,
                        time_fired_ts=now - age * DAY,
                        data_id=ed.data_id,
                    )
                )
            sm = StatisticsMeta(
                statistic_id="sensor.x", source="recorder", has_mean=True, has_sum=False
            )
            s.add(sm)
            s.flush()
            for age in (20, 70):
                s.add(StatisticsShortTerm(metadata_id=sm.id, start_ts=now - age * DAY, mean=1.0))

    await run_on_recorder(instance, add)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830, raw_days=10)
    assert entry.runtime_data.takeover.originals["keep_days"] == 60
    await entry.runtime_data.async_run(False)

    def counts(s):
        ev = s.execute(
            select(Events.time_fired_ts).where(Events.time_fired_ts < now - 15 * DAY)
        ).all()
        st = s.execute(
            select(StatisticsShortTerm.start_ts).where(
                StatisticsShortTerm.start_ts < now - 15 * DAY
            )
        ).all()
        return len(ev), len(st)

    # the recorder with keep_days=60 would have kept the 20-day-old rows and deleted the 70-day ones
    assert await read(instance, counts) == (1, 1)


async def test_cleanup_loops_until_the_backlog_is_gone(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    now = time.time()

    def add(inst):
        with session_scope(session=inst.get_session()) as s:
            sm = StatisticsMeta(
                statistic_id="sensor.y", source="recorder", has_mean=True, has_sum=False
            )
            s.add(sm)
            s.flush()
            for i in range(500):
                s.add(
                    StatisticsShortTerm(
                        metadata_id=sm.id, start_ts=now - 20 * DAY - 300 * i, mean=1.0
                    )
                )

    await run_on_recorder(instance, add)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    with patch.object(instance, "max_bind_vars", 10):
        report = await entry.runtime_data.async_run(False)
    left = await read(
        instance,
        lambda s: s.execute(
            select(func.count(StatisticsShortTerm.id)).where(
                StatisticsShortTerm.start_ts < now - 11 * DAY
            )
        ).scalar(),
    )
    assert report["stats"]["short_term_deleted"] == 500 and left == 0


async def test_raw_days_can_exceed_sixty(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"history_days": 1830, "raw_days": 90, "takeover": False, "dry_run": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"history_choice": "5"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"resolution": "standard"})
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"profile_rules": "", "flapping_collapse": True}
    )
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"takeover": False, "raw_days": 90, "dry_run": True}
    )
    assert r["step_id"] == "confirm"


async def test_stale_meta_and_event_types_are_purged_under_takeover(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)

    def add(inst):
        with session_scope(session=inst.get_session()) as s:
            s.add(StatesMeta(entity_id="sensor.gone"))
            s.add(EventTypes(event_type="never_used"))

    await run_on_recorder(instance, add)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    report = await entry.runtime_data.async_run(False)
    assert report["stats"]["stale_meta_deleted"] >= 1
    assert report["stats"]["stale_event_types_deleted"] >= 1
    names = await read(
        instance, lambda s: [r[0] for r in s.execute(select(StatesMeta.entity_id)).all()]
    )
    assert "sensor.gone" not in names
    # entities that still exist are recorded again after their id was purged
    hass.states.async_set("sensor.fresh", "1")
    await hass.async_block_till_done()
    await run_on_recorder(instance, lambda inst: None)
    names = await read(
        instance, lambda s: [r[0] for r in s.execute(select(StatesMeta.entity_id)).all()]
    )
    assert "sensor.fresh" in names
    assert (await integrity(instance))["fk"] == 0


# ---------------------------------------------------------------- 5. failures
async def test_cleanup_runs_even_when_thinning_fails_and_three_failures_raise_an_issue(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from custom_components.sediment import recorder_bridge

    instance = get_instance(hass)
    now = time.time()
    await seed(instance, "sensor.boom", _flat_day(midnight_days_ago(20)))

    def add_event(inst):
        with session_scope(session=inst.get_session()) as s:
            et = EventTypes(event_type="old_event")
            s.add(et)
            s.flush()
            s.add(Events(event_type_id=et.event_type_id, time_fired_ts=now - 30 * DAY))

    await run_on_recorder(instance, add_event)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    manager = entry.runtime_data

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    with patch.object(recorder_bridge.Job, "_delete_states", boom):
        report = await manager.async_run(False)
        assert report["stats"]["errors"]
        assert report["stats"]["events_deleted"] == 1, "cleanup still ran"
        assert _issue(hass, "job_failing") is None
        manager.data["quarantine"].clear()
        await manager.async_run(False)
        manager.data["quarantine"].clear()
        await manager.async_run(False)
    assert manager.data["failed_jobs"] == 3
    assert _issue(hass, "job_failing") is not None
    manager.data["quarantine"].clear()
    manager.data["applied"].clear()
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == []
    assert _issue(hass, "job_failing") is None


async def test_transient_database_errors_retry_before_quarantine(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from sqlalchemy.exc import OperationalError

    from custom_components.sediment import recorder_bridge

    instance = get_instance(hass)
    ids = await seed(instance, "sensor.locked", _flat_day(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data

    def locked(*args, **kwargs):
        raise OperationalError("DELETE", {}, Exception("database is locked"))

    with patch.object(recorder_bridge.Job, "_delete_states", locked):
        await manager.async_run(False)
        assert manager.data["quarantine"] == {} and len(manager.data["fail_counts"]) == 1
        await manager.async_run(False)
        assert manager.data["quarantine"] == {}
        await manager.async_run(False)
    assert len(manager.data["quarantine"]) == 1 and manager.data["fail_counts"] == {}
    # the quarantined entity-day can be released again
    await hass.services.async_call(DOMAIN, "clear_quarantine", blocking=True)
    assert manager.data["quarantine"] == {}
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == [] and report["stats"]["rows_deleted"] > 0
    after = {r[0] for r in await snapshot(instance)}
    assert len([i for i in ids if i in after]) < len(ids)


async def test_a_transient_error_that_clears_leaves_no_trace(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from sqlalchemy.exc import OperationalError

    from custom_components.sediment import recorder_bridge

    instance = get_instance(hass)
    await seed(instance, "sensor.blip", _flat_day(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    orig = recorder_bridge.Job._delete_states

    def locked(*args, **kwargs):
        raise OperationalError("DELETE", {}, Exception("database is locked"))

    with patch.object(recorder_bridge.Job, "_delete_states", locked):
        await manager.async_run(False)
    assert len(manager.data["fail_counts"]) == 1
    with patch.object(recorder_bridge.Job, "_delete_states", orig):
        report = await manager.async_run(False)
    assert report["stats"]["errors"] == [] and report["stats"]["rows_deleted"] > 0
    assert manager.data["fail_counts"] == {} and manager.data["quarantine"] == {}


# ---------------------------------------------------------------- 7. store race
async def test_unload_waits_for_the_job_in_flight(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    for name in ("a", "b", "c", "d"):
        await seed(instance, f"sensor.race_{name}", _flat_day(midnight_days_ago(20), 600))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    with patch("custom_components.sediment.recorder_bridge.BATCH_SECONDS", 0):
        task = hass.async_create_task(manager.async_run(False))
        for _ in range(3):
            await asyncio.sleep(0)
        assert manager.running
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert not manager.running and manager._idle.is_set()
    await task  # finished without an exception
    assert (await integrity(instance))["fk"] == 0


# ---------------------------------------------------------------- dialect
async def test_non_sqlite_refuses_writes_but_allows_a_dry_run(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.x", _flat_day(midnight_days_ago(5)))
    snap = await snapshot(instance)
    real = instance.__dict__.get("dialect_name")
    instance.__dict__["dialect_name"] = "mysql"
    try:
        entry = await setup_entry(hass, dry_run=False, takeover=True)
        manager = entry.runtime_data
        assert _issue(hass, "unsupported_database") is not None
        assert instance.auto_purge is True and not manager.owned
        assert await manager.async_run(False) is None
        assert await snapshot(instance) == snap
        report = await manager.async_run(True)  # read only
        assert report is not None and report["stats"]["rows_deleted"] == 0
        assert await snapshot(instance) == snap
    finally:
        if real is None:
            instance.__dict__.pop("dialect_name", None)
        else:
            instance.__dict__["dialect_name"] = real


# ---------------------------------------------------------------- state class
async def test_known_state_class_survives_an_unavailable_refresh(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from custom_components.sediment.profiles import EntityMeta
    from custom_components.sediment.recorder_bridge import Job, JobConfig

    instance = get_instance(hass)
    d = midnight_days_ago(20)
    pts = [
        (d + 60 * i + 1, f"{100 + i * 0.5:.1f}", {"unit_of_measurement": "kWh"}) for i in range(300)
    ]
    await seed(instance, "sensor.meter", pts)
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    cfg = JobConfig(
        dry_run=False,
        now=time.time(),
        raw_days=10.0,
        horizon_days=1830.0,
        coarse_horizon_days=365.0,
        schedules=manager.settings.schedules(),
        rules=[],
        flap_default=True,
        takeover=False,
        meta={"sensor.meter": EntityMeta(state_class=None)},  # unavailable right now
    )
    # last week it was known to be a counter
    manager.data["entities"]["sensor.meter"] = {
        "cls": "counter",
        "params": {},
        "ts": 0.0,
        "state_class": "total_increasing",
    }
    job = Job(cfg, manager.data)

    def run(inst):
        with session_scope(session=inst.get_session(), read_only=True) as ses:
            job._emap = dict(
                ses.execute(select(StatesMeta.metadata_id, StatesMeta.entity_id)).all()
            )
            mid = next(m for m, e in job._emap.items() if e == "sensor.meter")
            rows = Job._load_rows(ses, mid, d, d + DAY)
            return job._entity_info(ses, inst, mid, rows)

    info = await run_on_recorder(instance, run)
    assert info["cls"] == "counter" and info["state_class"] == "total_increasing"
    assert info["ts"] == cfg.now


# ---------------------------------------------------------------- unit size
async def test_big_entity_day_is_processed_in_slices(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.huge", _flat_day(midnight_days_ago(20), 1400))
    entry = await setup_entry(hass, dry_run=False)
    with patch("custom_components.sediment.recorder_bridge.MAX_UNIT_ROWS", 300):
        report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == [] and report["stats"]["oversize"] == []
    assert report["stats"]["entity_days"] >= 4, "the day was cut into several units"
    after = {r[0] for r in await snapshot(instance)}
    assert len([i for i in ids if i in after]) < 0.2 * len(ids)
    assert (await integrity(instance))["fk"] == 0


async def test_unsliceable_entity_day_is_reported_not_processed(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.huge", _flat_day(midnight_days_ago(20), 1400))
    one_day = [
        {"bucket_min": 1440, "until_days": 100},
        {"bucket_min": 1440, "until_days": 200},
        {"bucket_min": 1440, "until_days": 0},
    ]
    entry = await setup_entry(hass, dry_run=False, resolution="custom", custom_tiers=one_day)
    snap = await snapshot(instance)
    with patch("custom_components.sediment.recorder_bridge.MAX_UNIT_ROWS", 300):
        report = await entry.runtime_data.async_run(False)
    assert len(report["stats"]["oversize"]) == 1
    assert await snapshot(instance) == snap and ids


# ---------------------------------------------------------------- profile changes
async def test_changed_profile_thins_old_days_again(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    pts = [
        (
            midnight_days_ago(20) + 30 * i + 1,
            f"{20 + (i % 3) * 0.5:.1f}",
            {"unit_of_measurement": "C"},
        )
        for i in range(2800)
    ]
    ids = await seed(instance, "sensor.noisy", pts)
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    await manager.async_run(False)
    first = len([i for i in ids if i in {r[0] for r in await snapshot(instance)}])
    assert manager.data["profile_map"] == {}
    # the user now treats this sensor as coarse: hourly buckets
    manager.settings.rules_text = "sensor.noisy = coarse"
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == []
    assert report["stats"]["reprofiled_entities"] == 1
    second = len([i for i in ids if i in {r[0] for r in await snapshot(instance)}])
    assert second < first
    assert manager.data["profile_map"] == {"sensor.noisy": "coarse|None"}
    # nothing changed since: nothing to do
    report = await manager.async_run(False)
    assert report["stats"]["reprofiled_entities"] == 0
    # a finer profile cannot bring rows back
    manager.settings.rules_text = ""
    await manager.async_run(False)
    third = len([i for i in ids if i in {r[0] for r in await snapshot(instance)}])
    assert third <= second
    assert (await integrity(instance))["fk"] == 0


async def test_dry_run_reports_profile_changes_without_touching_anything(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.noisy", _flat_day(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    await manager.async_run(False)
    manager.settings.rules_text = "sensor.noisy = coarse"
    snap = await snapshot(instance)
    report = await manager.async_run(True)
    assert report["stats"]["reprofiled_entities"] == 1
    assert await snapshot(instance) == snap
    assert manager.data["profile_map"] == {}
