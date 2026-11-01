"""Config flow, options flow and the service."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sediment.const import DOMAIN

from .common import midnight_days_ago, seed


async def _to_purging(hass, flow_id, resolution="standard", cap=None, rules=""):
    r = await hass.config_entries.flow.async_configure(flow_id, {"history_choice": "5"})
    assert r["step_id"] == "resolution"
    data = {"resolution": resolution}
    if cap:
        data["size_cap_gb"] = cap
    r = await hass.config_entries.flow.async_configure(flow_id, data)
    if resolution == "custom":
        assert r["step_id"] == "tiers"
        r = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                "t1_bucket_min": 15,
                "t1_until_days": 100,
                "t2_bucket_min": 60,
                "t2_until_days": 500,
                "t3_bucket_min": 360,
            },
        )
    assert r["step_id"] == "profiles"
    return await hass.config_entries.flow.async_configure(
        flow_id, {"profile_rules": rules, "flapping_collapse": True}
    )


async def test_full_flow_creates_entry(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "history"
    r = await _to_purging(hass, r["flow_id"], cap=500)
    assert r["step_id"] == "purging"
    ph = r["description_placeholders"]
    assert ph["keep_days"] == "10" and ph["auto_purge"] == "on"
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"takeover": False, "raw_days": 10, "dry_run": True}
    )
    assert r["step_id"] == "confirm"
    assert float(r["description_placeholders"]["projected_gb"]) > 0
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    opts = r["options"]
    assert opts["history_days"] == 1830
    assert opts["resolution"] == "standard"
    assert opts["dry_run"] is True and opts["takeover"] is False
    assert opts["size_cap_gb"] == 500
    await hass.async_block_till_done()


async def test_invalid_rules_rejected(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    r = await _to_purging(hass, r["flow_id"], rules="sensor.a = nonsense")
    assert r["step_id"] == "profiles"
    assert r["errors"] == {"base": "invalid_rules"}
    assert "nonsense" in r["description_placeholders"]["rule_errors"]


async def test_over_cap_offers_coarser_oldest_tier(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    instance = get_instance(hass)
    # give the estimate real rows to work with
    pts = [(midnight_days_ago(1) + 10 * i, f"{20 + (i % 9) * 0.1:.1f}", None) for i in range(8000)]
    await seed(instance, "sensor.busy", pts)
    hass.states.async_set("sensor.busy", "20.0", {"state_class": "measurement"})
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    r = await _to_purging(hass, r["flow_id"], cap=0.001)
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"takeover": False, "raw_days": 10, "dry_run": True}
    )
    assert r["step_id"] == "confirm_over_cap"
    first = float(r["description_placeholders"]["projected_gb"])
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"coarsen_oldest": True})
    # the oldest tier is one step wider and the projection was recalculated
    assert r["step_id"] in ("confirm_over_cap", "confirm_over_cap_final")
    second = float(r["description_placeholders"]["projected_gb"])
    assert second <= first
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["options"]["resolution"] == "custom"
    tiers = r["options"]["custom_tiers"]
    assert (
        tiers[0]["bucket_min"] == 15.0 and tiers[1]["bucket_min"] == 60.0
    )  # newer tiers untouched
    assert tiers[2]["bucket_min"] == 1440.0
    await hass.async_block_till_done()


async def test_custom_tiers_validated(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(r["flow_id"], {"history_choice": "2"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"resolution": "custom"})
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {
            "t1_bucket_min": 60,
            "t1_until_days": 500,
            "t2_bucket_min": 15,
            "t2_until_days": 100,
            "t3_bucket_min": 5,
        },
    )
    assert r["step_id"] == "tiers" and r["errors"] == {"base": "invalid_tiers"}


async def test_options_flow_shows_current_recorder_values(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "history_days": 730,
            "resolution": "fine",
            "takeover": True,
            "raw_days": 10,
            "dry_run": True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["step_id"] == "history"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"history_choice": "2"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"resolution": "fine"})
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"profile_rules": "", "flapping_collapse": False}
    )
    assert r["step_id"] == "purging"
    # the takeover is active, so the form shows the values from before it
    assert r["description_placeholders"]["keep_days"] == "10"
    assert r["description_placeholders"]["auto_purge"] == "on"
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"takeover": False, "raw_days": 10, "dry_run": False}
    )
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["takeover"] is False and entry.options["dry_run"] is False
    await hass.async_block_till_done()


async def test_run_now_service(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"history_days": 730, "raw_days": 10, "dry_run": True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "run_now", {"dry_run": True}, blocking=True)
    assert entry.runtime_data.last_report["mode"] == "dry_run"
