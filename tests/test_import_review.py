"""Importer review round: interrupted runs, a file that lies, profile maps, paths, doomed rows."""

from __future__ import annotations

import json
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
from .snapshots import make_snapshot, merge_history, reseal, run_tool
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


async def test_the_bound_is_measured_once_per_file(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = two_entities(tmp_path)
    _instance, entry, live = await world(hass, tmp_path)
    manager = entry.runtime_data
    orig = importer.ImportJob._entity

    def stop(self, inst, conn, eid, bound):
        raise RuntimeError("stop")

    with patch.object(importer.ImportJob, "_entity", stop):
        await call(hass, out)
    run = next(iter(manager.data["import_runs"].values()))
    assert run["global_oldest"] is not None and orig is not None and live


# ---------------------------------------------------------------- I2
async def test_a_changed_attribute_text_is_detected(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = build_import(tmp_path, only_import=False)
    c = sqlite3.connect(out)
    c.execute('UPDATE attributes SET shared_attrs = \'{"unit_of_measurement":"EVIL"}\'')
    c.commit()
    c.close()
    _instance, _entry, _live = await world(hass, tmp_path)
    with pytest.raises(ServiceValidationError, match="damaged"):
        await call(hass, out)


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


async def test_rows_with_impossible_times_are_rejected(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = build_import(tmp_path, only_import=False)
    clean = tmp_path / "clean.db"
    clean.write_bytes(out.read_bytes())
    instance, _entry, _live = await world(hass, tmp_path)
    baseline = (await call(hass, clean, dry=True))["rows_would_insert"]
    c = sqlite3.connect(out)
    ids = [
        r[0]
        for r in c.execute("SELECT row_id FROM states WHERE entity_id='sensor.both' ORDER BY ts")
    ]
    c.execute("UPDATE states SET last_changed_ts = ts + 5 WHERE row_id = ?", (ids[3],))
    c.execute("UPDATE states SET ts = 1e999 WHERE row_id = ?", (ids[4],))  # infinity
    long_state = "x" * 300
    c.execute("UPDATE states SET state = ? WHERE row_id = ?", (long_state, ids[6]))
    c.commit()
    c.close()
    reseal(out)
    report = await call(hass, out)
    assert report["rejected_rows"] == 2
    assert report["rows_inserted"] == baseline - 2
    assert report["states_too_long"] == 1
    rows = await rows_of(instance, "sensor.both")
    assert all(r[1] < 1e12 for r in rows)
    assert (await integrity(instance))["fk"] == 0


# ---------------------------------------------------------------- I3
def test_tool_treats_what_it_cannot_see_as_keep(tmp_path, capsys) -> None:
    rows = temp_series(40, 30, step=60)
    snap = make_snapshot(tmp_path / "s.db", {"sensor.tado_temp": rows})
    rules = tmp_path / "rules.txt"
    rules.write_text("integration:tado = coarse\n")
    out = tmp_path / "import.db"
    manifest = run_tool(out, [snap], NOW, "--rules", str(rules))
    c = sqlite3.connect(out)
    n = c.execute("SELECT COUNT(*) FROM states").fetchone()[0]
    prof = c.execute("SELECT profile FROM entities").fetchone()[0]
    c.close()
    assert prof == "keep" and n == len(rows), "kept in full because the rule cannot be evaluated"
    assert manifest["profile_mode"] == "rules" and manifest["undecidable_entities"] == 1
    assert "cannot see" in capsys.readouterr().out


def test_tool_uses_a_profile_map(tmp_path) -> None:
    rows = temp_series(40, 30, step=60)
    snap = make_snapshot(tmp_path / "s.db", {"sensor.tado_temp": rows, "sensor.other": rows})
    rules = tmp_path / "rules.txt"
    rules.write_text("integration:tado = keep\n")
    assignments = {"sensor.tado_temp": ("keep", None), "sensor.other": ("default", None)}
    sig = "abc123"
    doc = {
        "format": "sediment-profile-map",
        "version": 1,
        "signature": sig,
        "map_hash": merge_history.load_core()[1].profile_map_hash(sig, assignments),
        "entities": {e: {"profile": p, "flap": f} for e, (p, f) in assignments.items()},
    }
    pm = tmp_path / "map.json"
    pm.write_text(json.dumps(doc))
    out = tmp_path / "import.db"
    manifest = run_tool(out, [snap], NOW, "--rules", str(rules), "--profile-map", str(pm))
    c = sqlite3.connect(out)
    counts_ = dict(c.execute("SELECT entity_id, COUNT(*) FROM states GROUP BY entity_id"))
    c.close()
    assert counts_["sensor.tado_temp"] == len(rows) and counts_["sensor.other"] < len(rows)
    assert manifest["profile_mode"] == "map" and manifest["profile_signature"] == sig
    assert manifest["profile_map_hash"] == doc["map_hash"] and manifest["undecidable_entities"] == 0
    # a map that was edited is refused
    doc["entities"]["sensor.other"]["profile"] = "drop"
    pm.write_text(json.dumps(doc))
    with pytest.raises(SystemExit, match="map hash"):
        run_tool(tmp_path / "again.db", [snap], NOW, "--profile-map", str(pm))


async def test_export_profile_map_writes_the_resolved_profiles(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    hass.states.async_set("sensor.t", "1", {"device_class": "temperature"})
    hass.states.async_set("sensor.u", "1", {})
    _instance, entry, _live = await world(
        hass, tmp_path, profile_rules="device_class:temperature = coarse\nsensor.u = keep"
    )
    response = await hass.services.async_call(
        DOMAIN, "export_profile_map", blocking=True, return_response=True
    )
    path = Path(hass.config.path("sediment/profile_map.json"))
    assert response["path"] == str(path) and path.exists()
    doc = json.loads(path.read_text())
    assert doc["signature"] == entry.runtime_data._profile_sig()
    assert doc["entities"]["sensor.t"]["profile"] == "coarse"
    assert doc["entities"]["sensor.u"]["profile"] == "keep"
    assert doc["entities"]["sensor.both"]["profile"] == "default"
    assignments = {e: (v["profile"], v["flap"]) for e, v in doc["entities"].items()}
    assert doc["map_hash"] == merge_history.load_core()[1].profile_map_hash(
        doc["signature"], assignments
    )


async def test_days_are_marked_only_for_a_matching_map(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    _instance, entry, _live = await world(hass, tmp_path)
    manager = entry.runtime_data
    # made without a map: never marked, whatever the settings
    plain = build_import(tmp_path)
    report = await manager.async_import_history(str(plain), dry_run=False)
    assert report["thin_again"] and report["days_marked_thinned"] == 0
    assert "profile map" in report["note"]


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


# ---------------------------------------------------------------- I4
async def test_the_sediment_folder_needs_no_allowlist(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = build_import(tmp_path)
    folder = Path(hass.config.path("sediment"))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "import.db"
    target.write_bytes(out.read_bytes())
    # no allowlist entry for tmp_path here
    await setup_entry(hass, dry_run=False, takeover=True, profile_rules="")
    try:
        response = await call(hass, target, dry=True)
    finally:
        target.unlink()
    assert response["rows_would_insert"] > 0


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


async def test_an_other_folder_is_refused_with_advice(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, tmp_path
) -> None:
    out = build_import(tmp_path)
    await setup_entry(hass, dry_run=False, takeover=True)
    with pytest.raises(ServiceValidationError, match=r"<config>/sediment/"):
        await call(hass, out, dry=True)


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
