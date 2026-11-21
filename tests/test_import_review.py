"""Importer review round: interrupted runs, a file that lies, profile maps, paths, doomed rows."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.sediment import importer
from custom_components.sediment.const import DOMAIN

from .common import integrity, midnight_days_ago
from .snapshots import make_snapshot, reseal, run_tool
from .test_importer import NOW, build_import, rows_of, world
from .test_integration import setup_entry
from .test_merge_tool import temp_series


async def call(hass, path, dry=False):
    return await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"path": str(path), "dry_run": dry},
        blocking=True,
        return_response=True,
    )


def two_entities(tmp_path):
    snap = make_snapshot(
        tmp_path / "s.db",
        {
            "sensor.a": temp_series(40, 30, step=600),
            "sensor.b": temp_series(20, 10, step=600),
        },
    )
    out = tmp_path / "import.db"
    run_tool(out, [snap], NOW)
    return out


# ---------------------------------------------------------------- I1
async def test_rerun_after_an_interruption_converges_to_one_clean_run(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = two_entities(tmp_path)
    instance, _entry, _live = await world(hass, tmp_path)
    clean = await call(hass, out, dry=True)
    orig = importer.ImportJob._entity

    def flaky(self, inst, conn, eid, bound):
        if eid == "sensor.b":
            raise RuntimeError("power cut")
        return orig(self, inst, conn, eid, bound)

    with patch.object(importer.ImportJob, "_entity", flaky):
        first = await call(hass, out)
    assert first["errors"]
    assert len(await rows_of(instance, "sensor.a")) > 0 and not await rows_of(instance, "sensor.b")
    second = await call(hass, out)  # the documented recovery: run it again
    assert second["errors"] == []
    assert len(await rows_of(instance, "sensor.b")) == clean["per_entity"]["sensor.b"]
    assert len(await rows_of(instance, "sensor.a")) == clean["per_entity"]["sensor.a"]
    assert (await integrity(instance))["fk"] == 0
    # the remembered bound is gone once the file is done, and a third run is a no-op
    assert not _entry.runtime_data.data["import_runs"]
    assert (await call(hass, out))["rows_inserted"] == 0


async def test_a_changed_chain_column_is_detected(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = build_import(tmp_path, only_import=False)
    c = sqlite3.connect(out)
    first, last = c.execute("SELECT MIN(row_id), MAX(row_id) FROM states").fetchone()
    c.execute("UPDATE states SET old_row_id = ? WHERE row_id = ?", (last, first))
    c.commit()
    c.close()
    _instance, _entry, _live = await world(hass, tmp_path)
    with pytest.raises(ServiceValidationError, match="damaged"):
        await call(hass, out)


async def test_links_that_do_not_point_back_inside_the_entity_become_null(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    snap = make_snapshot(
        tmp_path / "s.db",
        {"sensor.a": temp_series(40, 35, step=600), "sensor.b": temp_series(40, 35, step=600)},
    )
    out = tmp_path / "import.db"
    run_tool(out, [snap], NOW)
    c = sqlite3.connect(out)
    a_rows = [
        r[0] for r in c.execute("SELECT row_id FROM states WHERE entity_id='sensor.a' ORDER BY ts")
    ]
    b_rows = [
        r[0] for r in c.execute("SELECT row_id FROM states WHERE entity_id='sensor.b' ORDER BY ts")
    ]
    c.execute(
        "UPDATE states SET old_row_id = ? WHERE row_id = ?", (a_rows[-1], a_rows[0])
    )  # forward
    c.execute(
        "UPDATE states SET old_row_id = ? WHERE row_id = ?", (a_rows[5], b_rows[5])
    )  # other entity
    c.execute("UPDATE states SET old_row_id = row_id WHERE row_id = ?", (a_rows[7],))  # itself
    c.commit()
    c.close()
    reseal(out)
    instance, entry, _live = await world(hass, tmp_path)
    report = await call(hass, out)
    assert report["chains_rejected"] == 3 and report["errors"] == []
    assert (await integrity(instance)) == {
        "fk": 0,
        "bad_chain": 0,
        "orphan_attrs": 0,
        "dangling_attrs": 0,
    }
    assert entry.runtime_data.owned


async def test_days_are_not_marked_when_an_entity_changed_profile_since_the_map(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    _instance, entry, _live = await world(hass, tmp_path)
    manager = entry.runtime_data
    mp = (await manager.async_export_profile_map())["path"]
    out = build_import(tmp_path, "--profile-map", mp)
    # the signature stays the same, but this entity resolves differently now
    hass.states.async_set("sensor.both", "1", {"device_class": "temperature"})
    manager.settings.rules_text = ""
    with patch(
        "custom_components.sediment.manager.build_profile_map",
        return_value={"sensor.both": ("keep", None), "sensor.only_import": ("default", None)},
    ):
        report = await manager.async_import_history(str(out), dry_run=False)
    assert report["thin_again"] and report["profile_mismatches"] == 1
    assert report["days_marked_thinned"] == 0


async def test_a_file_under_www_is_refused(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = build_import(tmp_path)
    www = Path(hass.config.path("www"))
    www.mkdir(parents=True, exist_ok=True)
    target = www / "import.db"
    target.write_bytes(out.read_bytes())
    hass.config.allowlist_external_dirs.add(str(www))  # even if the folder is allowed
    await setup_entry(hass, dry_run=False, takeover=True)
    link = Path(hass.config.path("sediment")) / "link.db"
    try:
        with pytest.raises(ServiceValidationError, match="without a login") as err:
            await call(hass, target, dry=True)
        assert "<config>/sediment/" in str(err.value)
        # a symlink into www is no way around it
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            os.remove(link)
        os.symlink(target, link)
        with pytest.raises(ServiceValidationError, match="without a login"):
            await call(hass, link, dry=True)
    finally:
        target.unlink()
        if link.is_symlink():
            link.unlink()


# ---------------------------------------------------------------- I5
async def test_rows_the_nightly_job_would_delete_are_not_imported(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    snap = make_snapshot(
        tmp_path / "s.db",
        {
            "sensor.normal": temp_series(40, 30, step=600),
            "sensor.coarse": temp_series(40, 30, step=600),
            "sensor.doomed": temp_series(40, 30, step=600),
        },
    )
    out = tmp_path / "import.db"
    run_tool(out, [snap], NOW)  # made before the user wrote the rules below
    instance, entry, _live = await world(
        hass,
        tmp_path,
        profile_rules="sensor.coarse = coarse\nsensor.doomed = drop",
        coarse_horizon_days=35,
    )
    manager = entry.runtime_data
    dry = await manager.async_import_history(str(out), dry_run=True)
    assert dry["skipped_would_be_deleted"] > 0
    assert "sensor.doomed" not in dry["per_entity"]
    assert "sensor.normal" in dry["per_entity"] and "sensor.coarse" in dry["per_entity"]
    report = await manager.async_import_history(str(out), dry_run=False)
    assert report["skipped_would_be_deleted"] == dry["skipped_would_be_deleted"]
    assert report["rows_inserted"] == dry["rows_would_insert"]
    assert await rows_of(instance, "sensor.doomed") == []
    coarse_rows = await rows_of(instance, "sensor.coarse")
    assert coarse_rows and min(r[1] for r in coarse_rows) >= midnight_days_ago(35) - 700
    normal_rows = await rows_of(instance, "sensor.normal")
    assert min(r[1] for r in normal_rows) < midnight_days_ago(36)
