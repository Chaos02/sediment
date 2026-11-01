"""Third review round: known metadata beats the stored profile, the inference notice, untick."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance

from custom_components.sediment.const import DOMAIN

from .common import midnight_days_ago, seed, snapshot
from .test_integration import _flat_day, setup_entry


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def _alive(instance):
    return {r[0] for r in await snapshot(instance)}


async def test_fixed_metadata_overrides_the_stored_profile(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    await seed(instance, "sensor.t", _flat_day(midnight_days_ago(1), 10))
    hass.states.async_set("sensor.t", "1", {"device_class": "temperature"})
    entry = await setup_entry(
        hass, dry_run=False, profile_rules="device_class:temperature = drop\n"
    )
    manager = entry.runtime_data
    await manager.async_run(False)
    assert manager.data["profile_map"].get("sensor.t") == "drop|None"
    # the device class is fixed so the drop rule no longer applies; the rules are unchanged
    hass.states.async_set("sensor.t", "1", {"device_class": "humidity"})
    ids = await seed(instance, "sensor.t", _flat_day(midnight_days_ago(20), 50))
    report = await manager.async_run(False)
    assert report["stats"]["errors"] == []
    after = await _alive(instance)
    assert any(i in after for i in ids), "no longer dropped"
    assert "sensor.t" not in manager.data["profile_map"], "the stored profile follows"


async def test_missing_metadata_still_uses_the_stored_profile(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    hass.states.async_set("sensor.t", "1", {"device_class": "temperature"})
    entry = await setup_entry(
        hass, dry_run=False, profile_rules="device_class:temperature = keep\n"
    )
    manager = entry.runtime_data
    await manager.async_run(False)
    hass.states.async_remove("sensor.t")  # no metadata tonight
    ids = await seed(instance, "sensor.t", _flat_day(midnight_days_ago(20), 200))
    await manager.async_run(False)
    alive = await _alive(instance)
    assert all(i in alive for i in ids)


async def test_inferred_protection_raises_an_issue_and_can_be_given_up(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    # e.g. the user just lowered purge_keep_days from 90 to 10; a fresh Sediment
    ids = await seed(instance, "sensor.old", _flat_day(midnight_days_ago(60), 20))
    assert instance.auto_purge is True and instance.keep_days == 10
    entry = await setup_entry(hass, dry_run=True, takeover=True)
    assert not entry.runtime_data.owned
    assert instance.auto_purge is False and instance.keep_days > 60
    issue = _issue(hass, "retention_inferred")
    assert issue is not None
    assert issue.translation_placeholders["keep_days"] == "10"
    assert int(issue.translation_placeholders["days"]) >= 60
    alive = await _alive(instance)
    assert all(i in alive for i in ids)
    # unticking goes through the release confirmation, then the recorder purges again
    flow = hass.config_entries.options
    r = await flow.async_init(entry.entry_id)
    r = await flow.async_configure(r["flow_id"], {"history_choice": "5"})
    r = await flow.async_configure(r["flow_id"], {"resolution": "standard"})
    r = await flow.async_configure(r["flow_id"], {"profile_rules": "", "flapping_collapse": True})
    r = await flow.async_configure(
        r["flow_id"], {"takeover": False, "raw_days": 10, "dry_run": True}
    )
    assert r["step_id"] == "release_confirm"
    r = await flow.async_configure(r["flow_id"], {"release": True})
    r = await flow.async_configure(r["flow_id"], {})
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()
    assert instance.auto_purge is True and instance.keep_days == 10
    assert _issue(hass, "retention_inferred") is None


async def test_no_inference_issue_for_a_normal_install(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    await setup_entry(hass, dry_run=True, takeover=True)
    assert _issue(hass, "retention_inferred") is None


async def test_unticking_asks_for_confirmation_while_the_entry_is_not_loaded(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    entry = await setup_entry(hass, dry_run=False, takeover=True, history_days=400)
    assert entry.runtime_data.owned
    assert await hass.config_entries.async_unload(entry.entry_id)
    flow = hass.config_entries.options
    r = await flow.async_init(entry.entry_id)
    r = await flow.async_configure(r["flow_id"], {"history_choice": "custom", "custom_days": 400})
    r = await flow.async_configure(r["flow_id"], {"resolution": "standard"})
    r = await flow.async_configure(r["flow_id"], {"profile_rules": "", "flapping_collapse": True})
    r = await flow.async_configure(
        r["flow_id"], {"takeover": False, "raw_days": 10, "dry_run": False}
    )
    assert r["step_id"] == "release_confirm"
    assert instance.auto_purge is False, "nothing is released by asking"
