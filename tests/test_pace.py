"""Run timers and the falling-behind issue."""

from __future__ import annotations

from unittest.mock import PropertyMock, patch

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance

from custom_components.sediment import manager as manager_module
from custom_components.sediment.const import DOMAIN

from .common import midnight_days_ago, seed
from .test_integration import _flat_day, setup_entry


def _issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, "falling_behind")


async def test_finished_run_logs_its_timers_and_leaves_nothing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations, caplog
) -> None:
    await seed(get_instance(hass), "sensor.t", _flat_day(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    report = await entry.runtime_data.async_run(False)
    assert "Sediment run finished" in caplog.text
    assert "plan " in caplog.text and "repack " in caplog.text
    assert report["stats"]["unfinished_days"] == 0
    assert _issue(hass) is None


async def test_give_up_raises_falling_behind_and_a_finished_run_clears_it(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    await seed(get_instance(hass), "sensor.t", _flat_day(midnight_days_ago(20)))
    entry = await setup_entry(hass, dry_run=False)
    manager = entry.runtime_data
    with (
        patch.object(Recorder, "backlog", new_callable=PropertyMock, return_value=10**6),
        patch.object(manager_module, "BACKOFF_SECONDS", 0.0),
        patch.object(manager_module, "BACKOFF_GIVE_UP_SECONDS", -1.0),
    ):
        report = await manager.async_run(False)
    assert report["stats"]["unfinished_days"] > 0
    assert _issue(hass) is not None
    assert manager.data["last_run"]["unfinished_days"] > 0
    report = await manager.async_run(False)
    assert report["stats"]["unfinished_days"] == 0
    assert _issue(hass) is None
