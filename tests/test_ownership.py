"""Second review round: profile flips, vacuous purge helper, dry run safety, unload, inference."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, PropertyMock, patch

from homeassistant.components.recorder.core import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest

from custom_components.sediment.const import DOMAIN
from custom_components.sediment.manager import SedimentManager

from .common import integrity, midnight_days_ago, seed, snapshot
from .test_integration import _flat_day, setup_entry
from .test_retention import _issue, _nightly_purge, _pts


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
async def test_real_nightly_task_deletes_unprotected_history(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    instance.async_nightly_tasks(dt_util.utcnow())
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()
    assert await _none_alive(instance, old)


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


async def test_rule_change_with_missing_metadata_never_gets_less_conservative(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    ids = await seed(instance, "sensor.meter", _noisy(midnight_days_ago(20)))
    hass.states.async_set("sensor.meter", "20", {"device_class": "energy"})
    entry = await setup_entry(hass, dry_run=False, profile_rules="device_class:energy = keep\n")
    manager = entry.runtime_data
    await manager.async_run(False)
    hass.states.async_remove("sensor.meter")
    # the rules change while the entity has no metadata: default would be less conservative
    manager.settings.rules_text = "device_class:energy = keep\nsensor.other = coarse\n"
    report = await manager.async_run(False)
    assert report["stats"]["rows_deleted"] == 0
    assert await _all_alive(instance, ids)


async def test_reprofile_does_not_thin_a_day_twice_in_one_run(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.noisy", _noisy(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    await manager.async_run(False)
    manager.settings.rules_text = "sensor.noisy = coarse"
    seen = []
    from custom_components.sediment import recorder_bridge

    orig = recorder_bridge.Job._thin_entity_day

    def spy(self, instance, mid, d0, d1, cfgs, abs_idx, flap):
        seen.append((mid, d0, d1))
        return orig(self, instance, mid, d0, d1, cfgs, abs_idx, flap)

    with patch.object(recorder_bridge.Job, "_thin_entity_day", spy):
        report = await manager.async_run(False)
    assert report["stats"]["reprofiled_entities"] == 1
    assert len(seen) == len(set(seen)), "every entity-day was handled once"
    assert manager.data["fail_counts"] == {}


# ---------------------------------------------------------------- N2: dry run with an owned takeover
async def test_dry_run_with_owned_takeover_only_counts_new_coarse_and_drop_rules(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    c_ids = await seed(instance, "sensor.c", _flat_day(midnight_days_ago(400), 100))
    d_ids = await seed(instance, "sensor.d", _flat_day(midnight_days_ago(400), 100))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    manager = entry.runtime_data
    assert manager.owned
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "dry_run": True,
            "profile_rules": "sensor.c = coarse\nsensor.d = drop\n",
        },
    )
    await hass.async_block_till_done()
    manager = entry.runtime_data
    report = await manager.async_run(None)
    assert report["mode"] == "dry_run"
    after = await _alive(instance)
    assert all(i in after for i in d_ids), "drop only counts in a dry run"
    assert all(i in after for i in c_ids), "the coarse horizon only counts in a dry run"
    assert report["stats"]["coarse_horizon_rows"] == 100


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


# ---------------------------------------------------------------- N3: unload while backing off
async def test_unload_does_not_hang_while_the_job_backs_off(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.x", _flat_day(midnight_days_ago(20), 300))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    with (
        patch("custom_components.sediment.manager.BACKOFF_SECONDS", 0.05),
        patch.object(Recorder, "backlog", new_callable=PropertyMock, return_value=5000),
    ):
        task = hass.async_create_task(manager.async_run(False))
        await asyncio.sleep(0.2)
        assert manager.running
        await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), 5)
        await asyncio.wait_for(task, 5)
    assert not manager.running


async def test_unload_gives_up_on_a_job_that_never_stops(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    manager._idle.clear()  # a job that never reports back
    with patch("custom_components.sediment.manager.UNLOAD_WAIT_SECONDS", 0.1):
        await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), 5)


# ---------------------------------------------------------------- N4: slices keep integrity
async def test_slices_keep_full_integrity(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    pts = _noisy(midnight_days_ago(21), 2880) + _noisy(midnight_days_ago(20), 2880)
    ids = await seed(instance, "sensor.huge", pts, null_old=frozenset({500, 3000}))
    entry = await setup_entry(hass, dry_run=False)
    with patch("custom_components.sediment.recorder_bridge.MAX_UNIT_ROWS", 250):
        report = await entry.runtime_data.async_run(False)
    assert report["stats"]["errors"] == []
    snap = {r[0]: r for r in await snapshot(instance)}
    for k in (500, 3000):  # HA's NULLs that survived stay NULL
        if ids[k] in snap:
            assert snap[ids[k]][4] is None
    integ = await integrity(instance)
    assert integ["fk"] == 0 and integ["bad_chain"] == 0 and integ["dangling_attrs"] == 0


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


# ---------------------------------------------------------------- N6: disable and enable
async def test_reenabling_with_dry_run_on_keeps_purge_off(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dry_run": True})
    await hass.async_block_till_done()
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert instance.auto_purge is False and instance.keep_days == 1830
    await _nightly_purge(hass, instance)
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


async def test_no_inference_when_rows_are_within_the_recorders_keep_days(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.recent", _pts(midnight_days_ago(5)))
    entry = await setup_entry(hass, dry_run=True, takeover=True)
    assert not entry.runtime_data._inferred_owned
    assert instance.auto_purge is True and instance.keep_days == 10


async def test_inferred_ownership_protects_when_the_guard_fails_too(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    old = await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    with patch("custom_components.sediment.compat.SUPPORTED_SCHEMA_VERSIONS", frozenset({99})):
        await setup_entry(hass, dry_run=False, takeover=True, history_days=1830)
    assert instance.auto_purge is False
    assert _issue(hass, "takeover_protecting") is not None
    await _nightly_purge(hass, instance)
    assert await _all_alive(instance, old)


async def test_no_inference_without_the_takeover_option(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.old", _pts(midnight_days_ago(100)))
    await setup_entry(hass, dry_run=True, takeover=False)
    assert instance.auto_purge is True


# ---------------------------------------------------------------- setup failures
async def test_failed_setup_leaves_no_timers_or_tasks(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    seen: list[SedimentManager] = []
    orig_setup = SedimentManager.async_setup

    async def spy(self):
        seen.append(self)
        await orig_setup(self)

    with (
        patch.object(SedimentManager, "async_setup", spy),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=RuntimeError("platform failed")),
        ),
    ):
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(domain=DOMAIN, data={}, options={"dry_run": True})
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
    manager = seen[0]
    assert manager._unsubs == [] and manager._closing
    assert all(task.done() for task in manager._tasks)


async def test_safety_net_issue_exists_even_if_the_store_cannot_be_read(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    with patch("custom_components.sediment.manager.Store") as store:
        store.return_value.async_load = AsyncMock(side_effect=OSError("disk gone"))
        entry = MockConfigEntry(
            domain=DOMAIN, data={}, options={"dry_run": False, "takeover": True}
        )
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert _issue(hass, "safety_net") is not None
