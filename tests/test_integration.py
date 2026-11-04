"""Integration tests on a real recorder (SQLite): takeover, guard, dry run, real run."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

from homeassistant.components.recorder.db_schema import (
    EventData,
    Events,
    EventTypes,
    RecorderRuns,
    States,
    StatesMeta,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance, session_scope
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from sqlalchemy import select

from custom_components.sediment.const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from custom_components.sediment.recorder_bridge import run_on_recorder

from .common import (
    DAY,
    attribute_count,
    integrity,
    midnight_days_ago,
    read,
    seed,
    snapshot,
    table_counts,
)

OPTIONS = {
    "history_days": 1830,
    "resolution": "standard",
    "flapping_collapse": True,
    "takeover": False,
    "raw_days": 10,
    "dry_run": True,
    "profile_rules": "sensor.droppy = drop\n",
}


async def setup_entry(hass: HomeAssistant, **overrides):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={**OPTIONS, **overrides})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def seed_world(hass: HomeAssistant, instance) -> dict:
    """An old day (20 days back, thinned by tier 1), and recent days inside the raw window."""
    d = midnight_days_ago(20)
    ids: dict = {}
    # continuous sensor: 30 s samples, quantum 0.1, one spike, one persistent step
    pts = []
    for i in range(2880):
        v = 20.0 + (0.1 if i % 2 else 0.0)
        if i >= 2000:
            v += 10.0
        if i == 1000:
            v = 45.0
        pts.append(
            (d + 30 * i + 1, f"{v:.1f}", {"unit_of_measurement": "C", "state_class": "measurement"})
        )
    ids["temp"] = await seed(instance, "sensor.temp", pts, null_old=frozenset({1000}))
    # binary sensor: 6 transitions among many same-state rows
    pts = []
    state = "off"
    transitions = {20: "on", 55: "off", 90: "on", 91: "off", 120: "on", 140: "off"}
    for i in range(144):
        state = transitions.get(i, state)
        pts.append((d + 600 * i + 2, state, {"attr": i % 5}))
    ids["door"] = await seed(instance, "binary_sensor.door", pts)
    # counter with state_class total_increasing
    pts = [
        (d + 60 * i + 3, f"{100 + (i // 3) * 0.5:.1f}", {"state_class": "total_increasing"})
        for i in range(1400)
    ]
    ids["energy"] = await seed(instance, "sensor.energy_total", pts)
    # sensor with an unavailable gap of 20 minutes
    pts = []
    for i in range(400):
        st = "unavailable" if 100 <= i < 140 else f"{5 + (i % 3) * 0.1:.1f}"
        pts.append((d + 30 * i + 4, st, {"unit_of_measurement": "V"}))
    ids["gappy"] = await seed(instance, "sensor.gappy", pts)
    # drop profile entity, old and recent
    pts = [(d + 60 * i + 5, str(i), None) for i in range(50)]
    now = time.time()
    pts += [(now - 3600 + 60 * i, str(i), None) for i in range(10)]
    ids["droppy"] = await seed(instance, "sensor.droppy", pts)
    # recent days inside the raw window (and complete days for the dry run)
    for back in (1, 2, 3):
        r = midnight_days_ago(back)
        pts = [
            (r + 30 * i + 6, f"{20 + (i % 7) * 0.1:.1f}", {"unit_of_measurement": "C"})
            for i in range(2880)
        ]
        ids[f"recent{back}"] = await seed(instance, "sensor.temp", pts)
    hass.states.async_set("sensor.energy_total", "100", {"state_class": "total_increasing"})
    hass.states.async_set("sensor.temp", "20", {"state_class": "measurement"})
    return ids


# ---------------------------------------------------------------- takeover and guard
async def test_takeover_sets_and_restores(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    before = (instance.auto_purge, instance.keep_days, instance.auto_repack)
    assert before[0] is True
    entry = await setup_entry(hass, takeover=True, history_days=400, dry_run=False)
    assert instance.auto_purge is False
    assert instance.keep_days == 400
    manager = entry.runtime_data
    assert manager.takeover.originals == {
        "auto_purge": before[0],
        "keep_days": before[1],
        "auto_repack": before[2],
    }
    assert ir.async_get(hass).async_get_issue(DOMAIN, "takeover_blocked") is None
    # unloading (a reload, a restart) leaves the thinned history protected: the next setup
    # applies the takeover again
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert instance.auto_purge is False and instance.keep_days == 400
    # removing the integration is what hands purging back
    assert await hass.config_entries.async_remove(entry.entry_id) is not None
    await hass.async_block_till_done()
    assert (instance.auto_purge, instance.keep_days, instance.auto_repack) == before


async def test_takeover_not_engaged_during_dry_run(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """A dry run changes nothing: the guard runs, the recorder keeps purging."""
    instance = get_instance(hass)
    before = (instance.auto_purge, instance.keep_days)
    entry = await setup_entry(hass, takeover=True, history_days=400, dry_run=True)
    assert (instance.auto_purge, instance.keep_days) == before
    assert not entry.runtime_data.takeover.active
    assert ir.async_get(hass).async_get_issue(DOMAIN, "takeover_blocked") is None
    # leaving dry run engages it
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dry_run": False})
    await hass.async_block_till_done()
    assert instance.auto_purge is False and instance.keep_days == 400


async def test_guard_runs_during_dry_run(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        await setup_entry(hass, takeover=True, dry_run=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "takeover_blocked") is not None


async def test_untick_in_options_restores(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    entry = await setup_entry(hass, takeover=True, history_days=400, dry_run=False)
    assert instance.auto_purge is False
    hass.config_entries.async_update_entry(entry, options={**entry.options, "takeover": False})
    await hass.async_block_till_done()
    assert instance.auto_purge is True
    assert instance.keep_days != 400


async def test_guard_mismatch_leaves_recorder_untouched(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    before = (instance.auto_purge, instance.keep_days, instance.auto_repack)
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        entry = await setup_entry(hass, takeover=True, dry_run=False)
    assert (instance.auto_purge, instance.keep_days, instance.auto_repack) == before
    assert not entry.runtime_data.takeover.active
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "takeover_blocked")
    assert issue is not None
    assert "schema version" in issue.translation_placeholders["reasons"]


async def test_guard_missing_attribute(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from custom_components.sediment.compat import check_compat

    instance = get_instance(hass)
    assert check_compat(instance) == []

    class Fake:
        keep_days = 10

    problems = check_compat(Fake())
    assert any("auto_purge" in p for p in problems)
    assert any("states_manager" in p for p in problems)

    instance.use_legacy_events_index = True
    try:
        assert any("legacy" in p for p in check_compat(instance))
    finally:
        instance.use_legacy_events_index = False


def test_arg_names_ignores_unresolvable_annotations() -> None:
    """HA annotates session_scope with a name imported only for type checking.

    On the live runtime inspect.signature() raised NameError there, the guard
    reported a problem and the job never ran. The test plugin wraps
    session_scope, so only a direct check of the helper shows it.
    """
    import contextlib
    import inspect

    from custom_components.sediment.compat import arg_names

    ns: dict = {"contextlib": contextlib}
    src = (
        "@contextlib.contextmanager\n"
        "def session_scope(*, hass=None, session: NotImportedSession = None,"
        " exception_filter=None, read_only: bool = False):\n"
        "    yield session\n"
    )
    # dont_inherit: keep this module's "from __future__ import annotations" out,
    # so the annotation stays unevaluated the way HA's module has it.
    exec(compile(src, "<ha_helpers_recorder>", "exec", dont_inherit=True), ns)
    with pytest.raises(NameError):
        inspect.signature(ns["session_scope"])
    names, var_kw = arg_names(ns["session_scope"])
    assert {"session", "read_only"} <= names
    assert not var_kw


async def test_job_refused_when_guard_fails(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed_world(hass, instance)
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    snap = await snapshot(instance)
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        assert await manager.async_run(False) is None
    assert await snapshot(instance) == snap
    assert ids


# ---------------------------------------------------------------- dry run
async def test_dry_run_deletes_nothing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed_world(hass, instance)
    entry = await setup_entry(hass)
    manager = entry.runtime_data
    snap = await snapshot(instance)
    counts = await table_counts(instance)
    report = await manager.async_run(True)
    assert report is not None and report["mode"] == "dry_run"
    assert await snapshot(instance) == snap
    assert await table_counts(instance) == counts
    assert report["stats"]["rows_deleted"] == 0
    assert report["stats"]["dropped_rows"] > 0  # would delete the drop entity's old rows
    assert report["stats"]["days_simulated"] >= 1
    assert os.path.exists(hass.config.path("sediment/last_report.json"))
    assert manager.data["measured"]["continuous"]
    shares = manager.data["measured"]["continuous"]
    assert all(0 < s <= 1 for s in shares.values())
    assert hass.states.get("sensor.sediment_projected_size") is not None


async def test_dry_run_is_default_job_mode(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed_world(hass, instance)
    entry = await setup_entry(hass)  # dry_run option True
    snap = await snapshot(instance)
    report = await entry.runtime_data.async_run(None)
    assert report["mode"] == "dry_run"
    assert await snapshot(instance) == snap


# ---------------------------------------------------------------- real run
async def test_real_run_thins_old_day_and_keeps_integrity(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed_world(hass, instance)
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    before = await snapshot(instance)
    cutoff = time.time() - 10 * DAY
    recent_before = [r for r in before if r[3] >= cutoff]
    attrs_before = await attribute_count(instance)

    report = await manager.async_run(False)
    assert report is not None and report["mode"] == "run"
    assert report["stats"]["errors"] == []

    after = await snapshot(instance)
    after_ids = {r[0] for r in after}
    # rows inside the raw window are byte for byte untouched
    # (HA's own purge makes the same exception: the first kept row after a deleted run of
    # a drop entity loses its pointer)
    drop_first = ids["droppy"][50]
    expected = [
        (r[0], r[1], r[2], r[3], None, r[5]) if r[0] == drop_first else r for r in recent_before
    ]
    assert [r for r in after if r[3] >= cutoff] == expected

    # the continuous day collapsed, spike and step rows survived
    temp_after = [i for i in ids["temp"] if i in after_ids]
    assert len(temp_after) < 0.1 * len(ids["temp"])
    assert ids["temp"][1000] in after_ids  # spike
    assert ids["temp"][1999] in after_ids and ids["temp"][2000] in after_ids  # step
    assert ids["temp"][0] in after_ids  # first row of the series stays

    # every transition of the binary sensor survived, same-state rows went
    door_ids = ids["door"]
    transition_idx = [20, 55, 90, 91, 120, 140]
    for i in transition_idx:
        assert door_ids[i] in after_ids
    assert len([i for i in door_ids if i in after_ids]) <= len(transition_idx) + 3

    # edges of the unavailable gap stay
    g = ids["gappy"]
    assert g[99] in after_ids and g[100] in after_ids and g[140] in after_ids

    # drop entity: old rows gone, recent rows (inside raw window) stay
    dropped = ids["droppy"]
    assert not any(i in after_ids for i in dropped[:50])
    assert all(i in after_ids for i in dropped[50:])

    # HA's NULL old_state_id on the spike row stays NULL
    row = {r[0]: r for r in after}[ids["temp"][1000]]
    assert row[4] is None

    # counter: bucket end values stay exact
    from custom_components.sediment import codec

    en = ids["energy"]
    raw_vals = {r[0]: r for r in before}
    t_raw = [raw_vals[i][3] for i in en]
    v_raw = [float(raw_vals[i][2]) for i in en]
    kept_pos = [k for k, i in enumerate(en) if i in after_ids]
    tot, bad = codec.counter_exact(t_raw, v_raw, kept_pos, 900.0)
    assert tot > 0 and bad == 0
    assert len(kept_pos) < 0.2 * len(en)

    # integrity
    chk = await integrity(instance)
    assert chk == {"fk": 0, "bad_chain": 0, "orphan_attrs": 0, "dangling_attrs": 0}
    assert await attribute_count(instance) <= attrs_before
    # entities all still have rows or at least their meta
    n_meta = await read(instance, lambda s: len(s.execute(select(StatesMeta.metadata_id)).all()))
    assert n_meta >= 5


async def test_second_run_changes_nothing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed_world(hass, instance)
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    await manager.async_run(False)
    snap = await snapshot(instance)
    report = await manager.async_run(False)
    assert report["stats"]["rows_deleted"] == 0
    assert await snapshot(instance) == snap


async def test_old_day_is_thinned_through_both_tiers(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """A day 200 days back needs tier 1 and tier 2 (1 h buckets) in one pass."""
    instance = get_instance(hass)
    d = midnight_days_ago(200)
    pts = [
        (d + 30 * i + 1, f"{20 + (i % 3) * 0.1:.1f}", {"unit_of_measurement": "C"})
        for i in range(2880)
    ]
    ids = await seed(instance, "sensor.old", pts)
    # a later row keeps the day from being the series end
    await seed(
        instance, "sensor.old", [(midnight_days_ago(1) + 5, "20.0", {"unit_of_measurement": "C"})]
    )
    entry = await setup_entry(hass, dry_run=False)
    await entry.runtime_data.async_run(False)
    after = {r[0] for r in await snapshot(instance)}
    kept = [i for i in ids if i in after]
    assert 2 < len(kept) < 300
    applied = entry.runtime_data.data["applied"]
    assert max(v for k, v in applied.items() if k.endswith("|default")) == 3600.0
    assert (await integrity(instance))["fk"] == 0


async def test_row_before_raw_window_boundary_keeps_next_row_untouched(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The last row of a day is kept when the next row lies inside the raw window."""
    instance = get_instance(hass)
    d = midnight_days_ago(20)
    pts = [
        (d + 60 * i + 1, f"{1 + (i % 2) * 0.1:.1f}", {"unit_of_measurement": "C"})
        for i in range(1000)
    ]
    ids = await seed(instance, "sensor.quiet", pts)
    recent = await seed(
        instance, "sensor.quiet", [(time.time() - DAY, "1.0", {"unit_of_measurement": "C"})]
    )
    entry = await setup_entry(hass, dry_run=False)
    before = {r[0]: r for r in await snapshot(instance)}
    await entry.runtime_data.async_run(False)
    after = {r[0]: r for r in await snapshot(instance)}
    assert after[recent[0]] == before[recent[0]]
    assert ids[-1] in after  # last row of the day, pointed at by the recent row
    assert (await integrity(instance))["fk"] == 0


# ---------------------------------------------------------------- takeover run: horizon and cleanup
async def test_takeover_run_deletes_horizon_and_old_events(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    now = time.time()
    d_old = midnight_days_ago(100)
    old_pts = [
        (d_old + 60 * i + 1, f"{3 + (i % 2) * 0.1:.1f}", {"unit_of_measurement": "C"})
        for i in range(500)
    ]
    old_ids = await seed(instance, "sensor.horizon", old_pts)
    d_mid = midnight_days_ago(20)
    mid_pts = [
        (d_mid + 60 * i + 1, f"{3 + (i % 2) * 0.1:.1f}", {"unit_of_measurement": "C"})
        for i in range(500)
    ]
    mid_ids = await seed(instance, "sensor.horizon", mid_pts)

    def add_misc(inst):
        with session_scope(session=inst.get_session()) as s:
            et = EventTypes(event_type="sediment_test")
            s.add(et)
            s.flush()
            ed_old = EventData(shared_data="{}", hash=1)
            ed_new = EventData(shared_data='{"a": 1}', hash=2)
            s.add_all([ed_old, ed_new])
            s.flush()
            s.add(
                Events(
                    event_type_id=et.event_type_id,
                    time_fired_ts=now - 30 * DAY,
                    data_id=ed_old.data_id,
                )
            )
            s.add(
                Events(
                    event_type_id=et.event_type_id,
                    time_fired_ts=now - 1 * DAY,
                    data_id=ed_new.data_id,
                )
            )
            sm = StatisticsMeta(
                statistic_id="sensor.horizon", source="recorder", has_mean=True, has_sum=False
            )
            s.add(sm)
            s.flush()
            s.add(StatisticsShortTerm(metadata_id=sm.id, start_ts=now - 30 * DAY, mean=1.0))
            s.add(StatisticsShortTerm(metadata_id=sm.id, start_ts=now - 1 * DAY, mean=1.0))

    await run_on_recorder(instance, add_misc)
    c0 = await table_counts(instance)
    recent_before = await read(
        instance,
        lambda s: s.execute(
            select(Events.event_id).where(Events.time_fired_ts >= now - 2 * DAY)
        ).all(),
    )
    assert len(recent_before) >= 1
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=60)
    manager = entry.runtime_data
    assert manager.takeover.active
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == []
    after = {r[0] for r in await snapshot(instance)}
    assert not any(i in after for i in old_ids), "beyond the 60 day horizon: deleted"
    assert len([i for i in mid_ids if i in after]) >= 2, "inside the horizon: thinned, not deleted"
    assert len([i for i in mid_ids if i in after]) < len(mid_ids)
    c1 = await table_counts(instance)
    assert c0["old_events"] == 1 and c1["old_events"] == 0
    assert c0["old_short_term"] == 1 and c1["old_short_term"] == 0
    assert c1["runs"] >= 1  # the current run is never deleted
    assert (await integrity(instance))["fk"] == 0
    assert report["stats"]["events_deleted"] == 1
    assert recent_before[0] in await read(
        instance,
        lambda s: s.execute(
            select(Events.event_id).where(Events.time_fired_ts >= now - 2 * DAY)
        ).all(),
    )


async def test_takeover_dry_run_deletes_nothing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    now = time.time()
    d_old = midnight_days_ago(100)
    await seed(instance, "sensor.horizon", [(d_old + 60 * i + 1, "1.0", None) for i in range(100)])

    def add_event(inst):
        with session_scope(session=inst.get_session()) as s:
            et = EventTypes(event_type="sediment_test")
            s.add(et)
            s.flush()
            s.add(Events(event_type_id=et.event_type_id, time_fired_ts=now - 30 * DAY))

    await run_on_recorder(instance, add_event)
    entry = await setup_entry(hass, takeover=True, history_days=60, dry_run=True)
    snap, counts = await snapshot(instance), await table_counts(instance)
    report = await entry.runtime_data.async_run(True)
    assert await snapshot(instance) == snap
    assert await table_counts(instance) == counts
    assert report["stats"]["horizon_rows"] == 100
    assert report["stats"]["events_deleted"] == 1  # would delete


async def test_old_recorder_run_removed(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    from datetime import UTC, datetime, timedelta

    def add_run(inst):
        with session_scope(session=inst.get_session()) as s:
            start = datetime.now(UTC) - timedelta(days=40)
            s.add(RecorderRuns(start=start, end=start + timedelta(hours=1)))

    await run_on_recorder(instance, add_run)
    c0 = await table_counts(instance)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=400)
    await entry.runtime_data.async_run(False)
    c1 = await table_counts(instance)
    assert c1["runs"] == c0["runs"] - 1


async def test_sensors_exist(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    await setup_entry(hass)
    assert hass.states.get("sensor.sediment_db_size") is not None
    proj = hass.states.get("sensor.sediment_projected_size")
    assert proj is not None
    assert proj.attributes["unit_of_measurement"] == "GB"


async def test_projection_comes_from_the_store_during_startup(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, hass_storage
) -> None:
    # entities have no state while Home Assistant starts, so a projection computed then is wrong
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"projected_gb": 7.5, "projected_options": {"old": True}},
    }
    hass.set_state(CoreState.not_running)
    entry = await setup_entry(hass)
    assert entry.runtime_data.data["projected_options"] == {"old": True}
    assert hass.states.get("sensor.sediment_projected_size").state == "7.5"


async def test_projection_recomputed_on_reload_with_new_options(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, hass_storage
) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"projected_gb": 7.5, "projected_options": {"old": True}},
    }
    entry = await setup_entry(hass)  # hass is running here, as after an options change
    assert entry.runtime_data.data["projected_options"] == dict(entry.options)
    assert entry.runtime_data.data["projected_gb"] != 7.5


async def test_job_report_and_sensor_share_the_projection(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    await seed_world(hass, get_instance(hass))
    entry = await setup_entry(hass)
    report = await entry.runtime_data.async_run(True)
    sensor = hass.states.get("sensor.sediment_projected_size").state
    assert report["projected_gb"] == entry.runtime_data.data["projected_gb"]
    assert float(sensor) == report["projected_gb"]


# ---------------------------------------------------------------- pointers across days
def _flat_day(d: float, n: int = 1000, step: float = 60.0) -> list:
    return [
        (d + step * i + 1, f"{1 + (i % 2) * 0.1:.1f}", {"unit_of_measurement": "C"})
        for i in range(n)
    ]


async def test_two_old_days_relink_across_the_day_boundary(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed(
        instance,
        "sensor.two",
        _flat_day(midnight_days_ago(21), 1400) + _flat_day(midnight_days_ago(20), 1400),
    )
    entry = await setup_entry(hass, dry_run=False)
    report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == []
    after = {r[0]: r for r in await snapshot(instance)}
    assert len([i for i in ids if i in after]) < 0.2 * len(ids)
    assert (await integrity(instance)) == {
        "fk": 0,
        "bad_chain": 0,
        "orphan_attrs": 0,
        "dangling_attrs": 0,
    }
    # the first row of day 2 points at a surviving row (the last kept row of day 1)
    first_of_second = ids[1400]
    if first_of_second in after:
        assert after[first_of_second][4] in after


async def test_raw_window_pointer_into_the_day_keeps_the_target(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """A raw-window row that points into an old day protects the row it points at."""
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.pointer", _flat_day(midnight_days_ago(20)))
    target = ids[500]

    def add_recent(inst):
        with session_scope(session=inst.get_session()) as s:
            meta = s.execute(
                select(StatesMeta).where(StatesMeta.entity_id == "sensor.pointer")
            ).scalar_one()
            row = States(
                metadata_id=meta.metadata_id,
                state="1.0",
                last_updated_ts=time.time() - DAY,
                old_state_id=target,
            )
            s.add(row)
            s.flush()
            return row.state_id

    recent = await run_on_recorder(instance, add_recent)
    entry = await setup_entry(hass, dry_run=False)
    before_recent = {r[0]: r for r in await snapshot(instance)}[recent]
    report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == []
    assert report["stats"]["protected"] >= 1
    after = {r[0]: r for r in await snapshot(instance)}
    assert target in after
    assert after[recent] == before_recent
    assert (await integrity(instance))["fk"] == 0


async def test_older_row_pointing_at_deleted_row_is_moved(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """An odd pointer from another old row (not the next in line) is moved, not left dangling."""
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.odd", _flat_day(midnight_days_ago(20)))
    other = await seed(
        instance, "sensor.odd", [(midnight_days_ago(15) + 5, "1.0", {"unit_of_measurement": "C"})]
    )

    def point(inst):
        with session_scope(session=inst.get_session()) as s:
            s.get(States, other[0]).old_state_id = ids[501]

    await run_on_recorder(instance, point)
    entry = await setup_entry(hass, dry_run=False)
    report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == []
    after = {r[0]: r for r in await snapshot(instance)}
    assert other[0] in after
    assert after[other[0]][4] is None or after[other[0]][4] in after
    assert (await integrity(instance))["fk"] == 0


# ---------------------------------------------------------------- other classes and failure modes
async def test_climate_attribute_series(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
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
    report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == []
    after = {r[0] for r in await snapshot(instance)}
    kept = [i for i in ids if i in after]
    assert ids[600] in after
    assert len(kept) < 0.3 * len(ids)
    assert (await integrity(instance))["fk"] == 0


async def test_failure_aborts_rolls_back_and_quarantines(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from custom_components.sediment import recorder_bridge

    instance = get_instance(hass)
    await seed(instance, "sensor.boom", _flat_day(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    snap = await snapshot(instance)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    with patch.object(recorder_bridge.Job, "_delete_states", boom):
        report = await manager.async_run(False)
    assert report["stats"]["errors"]
    assert await snapshot(instance) == snap, "the entity-day transaction rolled back"
    assert not any(k.endswith("|default") for k in manager.data["applied"])
    assert len(manager.data["quarantine"]) == 1
    # the next run skips the quarantined entity-day instead of failing on it every night
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == []
    assert report["stats"]["quarantined"] == 1
    assert await snapshot(instance) == snap
    assert len(report["quarantine"]) == 1
    # lifting the quarantine does not redo a day that was marked done; the entity-day
    # stays as it is until the user clears Sediment's stored state
    manager.data["quarantine"].clear()
    manager.data["applied"].clear()
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == []
    assert report["stats"]["rows_deleted"] > 0


async def test_deletes_in_many_chunks_keep_foreign_keys(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """More deleted rows than bind variables: chunks must not strand a reference."""
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.chunky", _flat_day(midnight_days_ago(20), 1400))
    entry = await setup_entry(hass, dry_run=False)
    with patch.object(instance, "max_bind_vars", 40):
        report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == []
    assert report["stats"]["rows_deleted"] > 3 * 40
    after = {r[0] for r in await snapshot(instance)}
    assert len([i for i in ids if i in after]) < 0.2 * len(ids)
    assert (await integrity(instance)) == {
        "fk": 0,
        "bad_chain": 0,
        "orphan_attrs": 0,
        "dangling_attrs": 0,
    }


async def test_interrupted_job_resumes(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from custom_components.sediment.recorder_bridge import Job, JobConfig

    instance = get_instance(hass)
    for name in ("a", "b", "c"):
        await seed(instance, f"sensor.res_{name}", _flat_day(midnight_days_ago(20), 600))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    s = manager.settings
    from custom_components.sediment.manager import build_meta

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
    with patch("custom_components.sediment.recorder_bridge.BATCH_SECONDS", 0):
        job = Job(cfg, manager.data)
        for _ in range(2):  # two units of work, then the job object is lost
            assert not await run_on_recorder(instance, job.step)
        assert manager.data["progress"]  # remembered where it stopped
        done = [r for r in await snapshot(instance)]
        job2 = Job(cfg, manager.data)
        while not await run_on_recorder(instance, job2.step):
            pass
    assert job2.stats.errors == []
    assert (await integrity(instance))["fk"] == 0
    after = await snapshot(instance)
    assert len(after) < len(done)
    # all three entities thinned, none thinned twice (second run does nothing)
    report = await manager.async_run(False)
    assert report["stats"]["rows_deleted"] == 0
