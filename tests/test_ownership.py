"""Second review round: profile flips, vacuous purge helper, dry run safety, unload, inference."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest

from .common import midnight_days_ago, seed, snapshot
from .test_integration import _flat_day, setup_entry
from .test_retention import _nightly_purge, _pts


def _noisy(d, n=2800):
    return [
        (d + 30 * i + 1, f"{20 + (i % 3) * 0.5:.1f}", {"unit_of_measurement": "kWh"})
        for i in range(n)
    ]


async def _alive(instance) -> set[int]:
    return {r[0] for r in await snapshot(instance)}


async def _all_alive(instance, ids) -> bool:
    alive = await _alive(instance)
    return all(i in alive for i in ids)


async def _none_alive(instance, ids) -> bool:
    alive = await _alive(instance)
    return not any(i in alive for i in ids)


# ---------------------------------------------------------------- N7: the purge helper
async def test_purge_helper_deletes_unprotected_history(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Positive control: without Sediment the old rows really are purged."""
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    recent = await seed(instance, "sensor.recent", _pts(midnight_days_ago(2)))
    assert instance.auto_purge is True and instance.keep_days == 10
    await _nightly_purge(hass, instance)
    after = await _alive(instance)
    assert not any(i in after for i in old)
    assert all(i in after for i in recent)


@pytest.mark.parametrize("enable_nightly_purge", [True])
async def test_real_nightly_task_leaves_an_owned_history_alone(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    instance.async_nightly_tasks(dt_util.utcnow())
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()
    assert await _all_alive(instance, old)


# ---------------------------------------------------------------- N1: a transient flip
async def test_missing_metadata_for_one_night_does_not_rethin_a_keep_entity(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.meter", _noisy(midnight_days_ago(20)))
    # a YAML sensor without unique_id: its device class is only known from its live state
    hass.states.async_set("sensor.meter", "20", {"device_class": "energy"})
    entry = await setup_entry(hass, dry_run=False, profile_rules="device_class:energy = keep\n")
    manager = entry.runtime_data
    report = await manager.async_run(False)
    assert report["stats"]["rows_deleted"] == 0
    assert manager.data["profile_map"] == {"sensor.meter": "keep|None"}
    # its integration has not come up yet at 04:30
    hass.states.async_remove("sensor.meter")
    report = await manager.async_run(False)
    assert report["stats"]["reprofiled_entities"] == 0
    assert report["stats"]["rows_deleted"] == 0
    assert await _all_alive(instance, ids)
    # a day that becomes eligible that night is not thinned either
    assert manager.data["profile_map"] == {"sensor.meter": "keep|None"}


async def test_dry_run_never_shortens_the_horizon_of_the_last_real_run(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.old", _flat_day(midnight_days_ago(300), 100))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    await entry.runtime_data.async_run(False)
    assert entry.runtime_data.data["real_horizon_days"] == 1830.0
    kept = await _alive(instance)  # thinned by the real run, none deleted by the horizon
    assert any(i in kept for i in ids)
    # a dry run trying a shorter history: 100 days would delete the 300-day-old rows
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "dry_run": True, "history_days": 100}
    )
    await hass.async_block_till_done()
    assert instance.keep_days == 1830, "the recorder is not told a shorter history either"
    await entry.runtime_data.async_run(None)
    assert await _alive(instance) == kept
    # a real run with the shorter history does delete
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dry_run": False})
    await hass.async_block_till_done()
    await entry.runtime_data.async_run(None)
    assert await _none_alive(instance, ids)


# ---------------------------------------------------------------- N5: purge without keep_days
async def test_protected_recorder_survives_a_purge_service_call_without_keep_days(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    assert await hass.config_entries.async_unload(entry.entry_id)
    instance.auto_purge, instance.keep_days = True, 10  # a restart
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert instance.auto_purge is False and instance.keep_days == 1830
    await hass.services.async_call("recorder", "purge", {}, blocking=True)
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()
    assert await _all_alive(instance, old)


# ---------------------------------------------------------------- ownership inferred from data
async def test_lost_store_is_inferred_from_old_rows_and_protects(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    # takeover ticked, nothing in the store (it was lost), dry run on, history 100+ days old
    entry = await setup_entry(hass, dry_run=True, takeover=True, history_days=1830)
    manager = entry.runtime_data
    assert not manager.owned and manager._inferred_owned
    assert instance.auto_purge is False and instance.keep_days == 1830
    await _nightly_purge(hass, instance)
    assert await _all_alive(instance, old)
