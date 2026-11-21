"""Repair issue helpers."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------- the manager
def delete_issues(hass: HomeAssistant, *issue_ids: str) -> None:
    for issue_id in issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
