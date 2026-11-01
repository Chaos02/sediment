"""`sediment.import_history` on a real recorder (SQLite)."""

from __future__ import annotations

from functools import partial
from pathlib import Path
import sqlite3
import time

from homeassistant.components.recorder.db_schema import StateAttributes, States, StatesMeta
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest
from sqlalchemy import func, select

from custom_components.sediment.const import DOMAIN

from .common import DAY, integrity, midnight_days_ago, read, seed, snapshot
from .snapshots import make_snapshot, reseal, run_tool
from .test_integration import setup_entry
from .test_merge_tool import temp_series

NOW = midnight_days_ago(0)
ATTRS = {"unit_of_measurement": "C"}


def live_rows(first_day: int, last_day: int, step: int = 600):
    rows = []
    t = midnight_days_ago(first_day)
    i = 0
    while t < midnight_days_ago(last_day):
        rows.append((t + 3, f"{20 + (i % 3) * 0.1:.1f}", ATTRS))
        t += step
        i += 1
    return rows


def build_import(tmp_path: Path, *extra: str, only_import: bool = True) -> Path:
    """An import file: 40 to 2 days ago for two entities (one of them not live)."""
    series = {"sensor.both": temp_series(40, 2, step=600)}
    if only_import:
        series["sensor.only_import"] = temp_series(40, 30, step=300)
    snap = make_snapshot(tmp_path / "snap.db", series)
    out = tmp_path / "import.db"
    run_tool(out, [snap], NOW, *extra)
    return out


async def world(hass, tmp_path, **options):
    """Live rows for 3 days back, the takeover applied, the import folder allowed."""
    instance = get_instance(hass)
    live = await seed(instance, "sensor.both", live_rows(3, 0))
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    options.setdefault("takeover", True)
    options.setdefault("dry_run", False)
    options.setdefault("history_days", 1830)
    options.setdefault("profile_rules", "")
    entry = await setup_entry(hass, **options)
    return instance, entry, live


async def counts(instance):
    def q(s):
        return {
            "states": s.execute(select(func.count(States.state_id))).scalar(),
            "attrs": s.execute(select(func.count(StateAttributes.attributes_id))).scalar(),
            "meta": s.execute(select(func.count(StatesMeta.metadata_id))).scalar(),
        }

    return await read(instance, q)


async def rows_of(instance, entity_id):
    return await read(
        instance,
        lambda s: s.execute(
            select(States.state_id, States.last_updated_ts, States.old_state_id)
            .join(StatesMeta, StatesMeta.metadata_id == States.metadata_id)
            .where(StatesMeta.entity_id == entity_id)
            .order_by(States.last_updated_ts)
        ).all(),
    )


async def test_import_into_a_database_with_live_rows(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, live = await world(hass, tmp_path)
    before = {r[0]: r for r in await snapshot(instance)}
    oldest_live = min(r[3] for r in before.values() if r[0] in live)
    report = await entry.runtime_data.async_import_history(str(path), dry_run=False)
    assert report["errors"] == [] and report["rows_inserted"] > 0
    assert report["skipped_not_older_than_live"] > 0, "rows from the live period are skipped"
    after = {r[0]: r for r in await snapshot(instance)}
    # live rows are untouched, the first one keeps the pointer HA wrote
    assert all(after[i] == before[i] for i in before)
    new = [r for i, r in after.items() if i not in before]
    assert len(new) == report["rows_inserted"]
    assert all(r[3] < oldest_live for r in new if r[1] == before[live[0]][1]), "strictly older"
    # chains: inside the imported period, valid, same entity, earlier
    both = await rows_of(instance, "sensor.both")
    ids = [r[0] for r in both]
    assert both[ids.index(live[0])][2] == before[live[0]][4]
    imported_both = [r for r in both if r[0] not in before]
    assert imported_both[0][2] is None or imported_both[0][2] in {r[0] for r in imported_both}
    for r in imported_both[1:]:
        assert (r[2] in ids and r[2] < r[0]) or r[2] is None
    assert (await integrity(instance)) == {
        "fk": 0,
        "bad_chain": 0,
        "orphan_attrs": 0,
        "dangling_attrs": 0,
    }
    # the recorder's oldest-ts cache was reloaded
    assert instance.states_manager.oldest_ts == min(r[3] for r in after.values())


async def test_entity_only_in_the_import_is_created(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path)
    c0 = await counts(instance)
    report = await entry.runtime_data.async_import_history(str(path), dry_run=False)
    assert report["states_meta_created"] == 1
    assert (await counts(instance))["meta"] == c0["meta"] + 1
    rows = await rows_of(instance, "sensor.only_import")
    assert len(rows) == report["per_entity"]["sensor.only_import"] > 0
    assert (await integrity(instance))["fk"] == 0
    # the recorder knows the new id: a state written now goes to the same entity
    hass.states.async_set("sensor.only_import", "7")
    await hass.async_block_till_done()
    await read(instance, lambda s: None)
    assert len(await rows_of(instance, "sensor.only_import")) >= len(rows)
    assert (await counts(instance))["meta"] == c0["meta"] + 1, "no second metadata row"


async def test_imported_period_is_visible_through_the_history_api(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path)
    await entry.runtime_data.async_import_history(str(path), dry_run=False)
    start = dt_util.utc_from_timestamp(midnight_days_ago(40))
    end = dt_util.utc_from_timestamp(midnight_days_ago(30))
    states = await instance.async_add_executor_job(
        partial(
            get_significant_states,
            hass,
            start,
            end,
            ["sensor.only_import", "sensor.both"],
            None,
            include_start_time_state=False,
            significant_changes_only=False,
        )
    )
    assert len(states["sensor.only_import"]) > 5
    assert len(states["sensor.both"]) > 5
    assert states["sensor.only_import"][0].state in {"20.0", "20.1", "20.2"}


async def test_a_second_import_inserts_nothing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path)
    manager = entry.runtime_data
    first = await manager.async_import_history(str(path), dry_run=False)
    c1 = await counts(instance)
    again = await manager.async_import_history(str(path), dry_run=False)
    assert again["already_imported"] and again["rows_inserted"] == 0
    assert await counts(instance) == c1
    # even without the remembered hash the bound leaves nothing to insert
    manager.data["imports"].clear()
    third = await manager.async_import_history(str(path), dry_run=False)
    assert third["rows_inserted"] == 0
    assert third["skipped_not_older_than_live"] == first["rows_in_file"]
    assert await counts(instance) == c1


async def test_a_dry_run_writes_nothing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path)
    snap, c0 = await snapshot(instance), await counts(instance)
    report = await entry.runtime_data.async_import_history(str(path), dry_run=True)
    assert await snapshot(instance) == snap and await counts(instance) == c0
    assert report["dry_run"] and report["rows_would_insert"] > 0
    assert report["per_entity"]["sensor.only_import"] > 0
    assert sum(report["by_period"].values()) == report["rows_would_insert"]
    assert report["problems"] == []
    assert not entry.runtime_data.data.get("imports")
    assert (Path(hass.config.path("sediment/last_import.json"))).exists()


async def test_the_service_returns_a_report(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    _instance, _entry, _live = await world(hass, tmp_path)
    response = await hass.services.async_call(
        DOMAIN, "import_history", {"path": str(path)}, blocking=True, return_response=True
    )
    assert response["dry_run"] is True and response["rows_would_insert"] > 0
    response = await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"path": str(path), "dry_run": False},
        blocking=True,
        return_response=True,
    )
    assert response["rows_inserted"] > 0


async def test_boundary_with_the_oldest_live_row(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    """A row at exactly the oldest live timestamp is not inserted, one just before it is."""
    instance = get_instance(hass)
    t0 = midnight_days_ago(3) + 100.0
    await seed(instance, "sensor.edge", [(t0, "1.0", ATTRS), (t0 + 60, "1.1", ATTRS)])
    rows = [
        (t0 - 120, "0.8", ATTRS),
        (t0 - 60, "0.9", ATTRS),
        (t0, "1.0", ATTRS),
        (t0 + 60, "1.1", ATTRS),
    ]
    snap = make_snapshot(tmp_path / "s.db", {"sensor.edge": rows})
    out = tmp_path / "edge.db"
    # raw window larger than the data's age so the tool keeps every row
    run_tool(out, [snap], NOW, "--raw-days", "10")
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    entry = await setup_entry(
        hass, dry_run=False, takeover=True, history_days=1830, profile_rules=""
    )
    report = await entry.runtime_data.async_import_history(str(out), dry_run=False)
    assert report["rows_inserted"] == 2
    assert report["skipped_not_older_than_live"] == 2
    got = [r[1] for r in await rows_of(instance, "sensor.edge")]
    assert got == [t0 - 120, t0 - 60, t0, t0 + 60]


async def test_attributes_are_reused_not_duplicated(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path, only_import=False)  # every row carries ATTRS, live has them
    instance, entry, _live = await world(hass, tmp_path)
    c0 = await counts(instance)
    report = await entry.runtime_data.async_import_history(str(path), dry_run=False)
    assert report["attributes_created"] == 0 and report["attributes_reused"] >= 1
    assert (await counts(instance))["attrs"] == c0["attrs"]


async def test_history_length_limits_what_is_imported(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path, history_days=35)
    report = await entry.runtime_data.async_import_history(str(path), dry_run=False)
    assert report["skipped_would_be_deleted"] > 0
    oldest = min(r[1] for r in await rows_of(instance, "sensor.both"))
    assert oldest >= time.time() - 35 * DAY - 600


# ---------------------------------------------------------------- markers
async def test_imported_days_are_marked_thinned(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    _instance, entry, _live = await world(hass, tmp_path)
    manager = entry.runtime_data
    path = build_import(
        tmp_path, "--profile-map", (await manager.async_export_profile_map())["path"]
    )
    report = await manager.async_import_history(str(path), dry_run=False)
    assert report["days_marked_thinned"] > 0 and not report["thin_again"]
    day = int(midnight_days_ago(35) // DAY)
    assert manager.data["applied"][f"{day}|default"] == 900.0
    # the nightly job has nothing left to do for those days
    before = await snapshot(_instance)
    result = await manager.async_run(False)
    assert result["stats"]["errors"] == []
    old = [r for r in await snapshot(_instance) if r[3] < midnight_days_ago(30)]
    assert old and [r for r in before if r[3] < midnight_days_ago(30)] == old


async def test_other_profile_settings_leave_the_days_for_the_nightly_job(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    _instance, entry, _live = await world(hass, tmp_path)
    manager = entry.runtime_data
    path = build_import(
        tmp_path, "--profile-map", (await manager.async_export_profile_map())["path"]
    )
    manager.settings.rules_text = "sensor.zzz = coarse"  # the settings changed since the map
    report = await manager.async_import_history(str(path), dry_run=False)
    assert report["thin_again"] and report["days_marked_thinned"] == 0
    day = int(midnight_days_ago(35) // DAY)
    assert f"{day}|default" not in manager.data["applied"]


# ---------------------------------------------------------------- refusals
async def test_refused_when_the_recorder_would_purge_the_rows(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path, takeover=False)
    c0 = await counts(instance)
    with pytest.raises(ServiceValidationError, match="takeover"):
        await entry.runtime_data.async_import_history(str(path), dry_run=False)
    assert await counts(instance) == c0
    # a dry run reports the same problem instead of failing
    report = await entry.runtime_data.async_import_history(str(path), dry_run=True)
    assert any("takeover" in p for p in report["problems"])


async def test_refused_when_the_database_is_not_sqlite(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    instance, entry, _live = await world(hass, tmp_path)
    real = instance.__dict__.get("dialect_name")
    instance.__dict__["dialect_name"] = "postgresql"
    try:
        with pytest.raises(ServiceValidationError, match="SQLite"):
            await entry.runtime_data.async_import_history(str(path), dry_run=False)
    finally:
        if real is None:
            instance.__dict__.pop("dialect_name", None)
        else:
            instance.__dict__["dialect_name"] = real


async def test_refused_when_the_file_has_another_schema(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = '52' WHERE key = 'source_schema'")
    conn.commit()
    conn.close()
    reseal(path)
    _instance, entry, _live = await world(hass, tmp_path)
    with pytest.raises(ServiceValidationError, match="schema 52"):
        await entry.runtime_data.async_import_history(str(path), dry_run=False)


async def test_refused_when_the_file_is_damaged_or_not_an_import(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    _instance, entry, _live = await world(hass, tmp_path)
    manager = entry.runtime_data
    conn = sqlite3.connect(path)
    conn.execute("UPDATE states SET state = 'tampered' WHERE row_id = 1")
    conn.commit()
    conn.close()
    with pytest.raises(ServiceValidationError, match="damaged"):
        await manager.async_import_history(str(path), dry_run=False)
    plain = tmp_path / "plain.db"
    sqlite3.connect(plain).close()
    with pytest.raises(ServiceValidationError, match="import"):
        await manager.async_import_history(str(plain), dry_run=False)
    with pytest.raises(ServiceValidationError, match="does not exist"):
        await manager.async_import_history(str(tmp_path / "missing.db"), dry_run=False)


async def test_refused_for_a_path_that_is_not_allowed(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    entry = await setup_entry(hass, dry_run=False, takeover=True)  # folder not allowed
    with pytest.raises(ServiceValidationError, match="not allowed"):
        await entry.runtime_data.async_import_history(str(path), dry_run=False)


async def test_refused_while_a_job_runs(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    path = build_import(tmp_path)
    _instance, entry, _live = await world(hass, tmp_path)
    entry.runtime_data.running = True
    try:
        with pytest.raises(ServiceValidationError, match="busy"):
            await entry.runtime_data.async_import_history(str(path), dry_run=False)
    finally:
        entry.runtime_data.running = False
