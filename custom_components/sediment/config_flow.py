"""Config flow and options flow.

Steps: history length, resolution (with custom tiers), profiles, purging and retention,
and a confirmation that shows the projected database size before anything is saved.
The same steps serve the options flow.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
import voluptuous as vol

from .const import (
    CONF_COARSE_HORIZON_DAYS,
    CONF_CUSTOM_TIERS,
    CONF_DRY_RUN,
    CONF_FLAP_COLLAPSE,
    CONF_HISTORY_CHOICE,
    CONF_HISTORY_DAYS,
    CONF_RAW_DAYS,
    CONF_RESOLUTION,
    CONF_RULES,
    CONF_SIZE_CAP_GB,
    CONF_TAKEOVER,
    DEFAULT_COARSE_HORIZON_DAYS,
    DEFAULT_DRY_RUN,
    DEFAULT_FLAP_COLLAPSE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_RAW_DAYS,
    DEFAULT_RESOLUTION,
    DEFAULT_TAKEOVER,
    DOMAIN,
    HISTORY_CHOICES,
    RESOLUTIONS,
)
from .manager import Settings, async_estimate, async_retention_owned
from .profiles import coarsen_oldest, parse_rules, resolution_ladder

CONF_CUSTOM_DAYS = "custom_days"
YEARS_TO_DAYS = {"1": 365, "2": 730, "5": 1830, "10": 3650}


def _number(
    minimum: float, maximum: float, unit: str | None = None, step: float = 1
) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def _choice_for_days(days: int) -> str:
    for key, value in YEARS_TO_DAYS.items():
        if value == days:
            return key
    return "custom"


class SedimentFlowSteps:
    """Steps shared by the config flow and the options flow."""

    hass: Any
    _opts: dict[str, Any]

    # implemented by the two flows
    async def _async_finish(self) -> ConfigFlowResult:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------ history length
    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input[CONF_HISTORY_CHOICE]
            days = (
                int(user_input.get(CONF_CUSTOM_DAYS) or DEFAULT_HISTORY_DAYS)
                if choice == "custom"
                else YEARS_TO_DAYS[choice]
            )
            self._opts[CONF_HISTORY_DAYS] = days
            return await self.async_step_resolution()
        days = int(self._opts.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
        schema = vol.Schema(
            {
                vol.Required(CONF_HISTORY_CHOICE): SelectSelector(
                    SelectSelectorConfig(
                        options=list(HISTORY_CHOICES),
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_HISTORY_CHOICE,
                    )
                ),
                vol.Optional(CONF_CUSTOM_DAYS): _number(30, 7300, "d"),
            }
        )
        schema = self.add_suggested_values_to_schema(
            schema, {CONF_HISTORY_CHOICE: _choice_for_days(days), CONF_CUSTOM_DAYS: days}
        )
        return self.async_show_form(step_id="history", data_schema=schema)

    # ------------------------------------------------------------ resolution
    async def async_step_resolution(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._opts[CONF_RESOLUTION] = user_input[CONF_RESOLUTION]
            cap = user_input.get(CONF_SIZE_CAP_GB)
            self._opts[CONF_SIZE_CAP_GB] = float(cap) if cap else None
            if user_input[CONF_RESOLUTION] == "custom":
                return await self.async_step_tiers()
            self._opts.pop(CONF_CUSTOM_TIERS, None)
            return await self.async_step_profiles()
        schema = vol.Schema(
            {
                vol.Required(CONF_RESOLUTION): SelectSelector(
                    SelectSelectorConfig(
                        options=list(RESOLUTIONS),
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_RESOLUTION,
                    )
                ),
                vol.Optional(CONF_SIZE_CAP_GB): _number(0, 100000, "GB", 0.5),
            }
        )
        schema = self.add_suggested_values_to_schema(
            schema,
            {
                CONF_RESOLUTION: self._opts.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
                CONF_SIZE_CAP_GB: self._opts.get(CONF_SIZE_CAP_GB),
            },
        )
        return self.async_show_form(step_id="resolution", data_schema=schema)

    async def async_step_tiers(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Custom tier lengths and bucket sizes: three tiers, the last one open ended."""
        if user_input is not None:
            tiers = []
            for i in (1, 2, 3):
                row = {"bucket_min": float(user_input[f"t{i}_bucket_min"])}
                row["until_days"] = float(user_input.get(f"t{i}_until_days") or 0)
                tiers.append(row)
            ok = tiers[0]["until_days"] < tiers[1]["until_days"] and (
                tiers[0]["bucket_min"] <= tiers[1]["bucket_min"] <= tiers[2]["bucket_min"]
            )
            if not ok:
                return self.async_show_form(
                    step_id="tiers",
                    data_schema=self._tiers_schema(user_input),
                    errors={"base": "invalid_tiers"},
                )
            self._opts[CONF_CUSTOM_TIERS] = tiers
            return await self.async_step_profiles()
        existing = self._opts.get(CONF_CUSTOM_TIERS) or [
            {"bucket_min": b / 60.0, "until_days": u or 0}
            for b, u in resolution_ladder("standard", None)
        ]
        values: dict[str, Any] = {}
        for i, row in enumerate(existing[:3], 1):
            values[f"t{i}_bucket_min"] = row["bucket_min"]
            values[f"t{i}_until_days"] = row["until_days"]
        return self.async_show_form(step_id="tiers", data_schema=self._tiers_schema(values))

    def _tiers_schema(self, values: dict[str, Any]) -> vol.Schema:
        schema = vol.Schema(
            {
                vol.Required("t1_bucket_min"): _number(1, 1440, "min"),
                vol.Required("t1_until_days"): _number(1, 7300, "d"),
                vol.Required("t2_bucket_min"): _number(1, 1440, "min"),
                vol.Required("t2_until_days"): _number(1, 7300, "d"),
                vol.Required("t3_bucket_min"): _number(1, 1440, "min"),
            }
        )
        return self.add_suggested_values_to_schema(schema, values)

    # ------------------------------------------------------------ profiles
    async def async_step_profiles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"rule_errors": ""}
        if user_input is not None:
            text = user_input.get(CONF_RULES, "") or ""
            _rules, problems = parse_rules(text)
            if problems:
                errors["base"] = "invalid_rules"
                placeholders["rule_errors"] = "; ".join(problems)
            else:
                self._opts[CONF_RULES] = text
                self._opts[CONF_FLAP_COLLAPSE] = bool(user_input[CONF_FLAP_COLLAPSE])
                return await self.async_step_purging()
        schema = vol.Schema(
            {
                vol.Optional(CONF_RULES): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Required(CONF_FLAP_COLLAPSE): BooleanSelector(),
            }
        )
        schema = self.add_suggested_values_to_schema(
            schema,
            user_input
            or {
                CONF_RULES: self._opts.get(CONF_RULES, ""),
                CONF_FLAP_COLLAPSE: self._opts.get(CONF_FLAP_COLLAPSE, DEFAULT_FLAP_COLLAPSE),
            },
        )
        return self.async_show_form(
            step_id="profiles",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------ purging and retention
    async def async_step_purging(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._opts[CONF_TAKEOVER] = bool(user_input[CONF_TAKEOVER])
            self._opts[CONF_RAW_DAYS] = int(user_input[CONF_RAW_DAYS])
            self._opts[CONF_DRY_RUN] = bool(user_input[CONF_DRY_RUN])
            if not self._opts[CONF_TAKEOVER] and await self._retention_owned():
                return await self.async_step_release_confirm()
            return await self.async_step_confirm()
        keep_days, auto_purge, auto_repack = self._recorder_values()
        schema = vol.Schema(
            {
                vol.Required(CONF_TAKEOVER): BooleanSelector(),
                vol.Required(CONF_RAW_DAYS): _number(2, 3650, "d"),
                vol.Required(CONF_DRY_RUN): BooleanSelector(),
            }
        )
        schema = self.add_suggested_values_to_schema(
            schema,
            {
                CONF_TAKEOVER: self._opts.get(CONF_TAKEOVER, DEFAULT_TAKEOVER),
                CONF_RAW_DAYS: self._opts.get(CONF_RAW_DAYS, keep_days or DEFAULT_RAW_DAYS),
                CONF_DRY_RUN: self._opts.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
            },
        )
        return self.async_show_form(
            step_id="purging",
            data_schema=schema,
            description_placeholders={
                "keep_days": str(keep_days),
                "auto_purge": "on" if auto_purge else "off",
                "auto_repack": "on" if auto_repack else "off",
            },
        )

    async def async_step_release_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Unticking the takeover hands purging back: ask before history is at risk."""
        if user_input is not None:
            if user_input.get("release"):
                return await self.async_step_confirm()
            self._opts[CONF_TAKEOVER] = True  # keep the takeover, go back to the choice
            return await self.async_step_purging()
        keep_days, _auto_purge, _auto_repack = self._recorder_values()
        return self.async_show_form(
            step_id="release_confirm",
            data_schema=vol.Schema({vol.Required("release", default=False): BooleanSelector()}),
            description_placeholders={"keep_days": str(keep_days)},
        )

    def _recorder_values(self) -> tuple[int, bool, bool]:
        """The recorder's own settings, from before Sediment's takeover if it is active."""
        startup = self.hass.data.get(DOMAIN, {}).get("startup")
        if startup:
            return (
                int(startup["keep_days"]),
                bool(startup["auto_purge"]),
                bool(startup["auto_repack"]),
            )
        inst = get_instance(self.hass)
        return int(inst.keep_days), bool(inst.auto_purge), bool(inst.auto_repack)

    def _manager(self) -> Any:
        return None

    async def _retention_owned(self) -> bool:
        return False

    # ------------------------------------------------------------ estimate and confirm
    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        settings = Settings.from_options(self._opts)
        manager = self._manager()
        measured = manager.data.get("measured") if manager else None
        if user_input is not None:
            if user_input.get("coarsen_oldest"):
                res, tiers = coarsen_oldest(settings.resolution, settings.custom_tiers)
                self._opts[CONF_RESOLUTION] = res
                self._opts[CONF_CUSTOM_TIERS] = tiers
                return await self.async_step_confirm()
            return await self._async_finish()
        est = await async_estimate(self.hass, settings, measured)
        over = est.over_cap(settings.size_cap_gb)
        placeholders = {
            "projected_gb": f"{est.total_gb:.1f}",
            "cap_gb": f"{settings.size_cap_gb:g}" if settings.size_cap_gb else "-",
            "years": f"{settings.history_days / 365.0:.1f}",
            "rows_m": f"{est.rows / 1e6:.1f}",
        }
        if over:
            can_coarsen = coarsen_oldest(settings.resolution, settings.custom_tiers)[1] != (
                self._tiers_now(settings)
            )
            if can_coarsen:
                return self.async_show_form(
                    step_id="confirm_over_cap",
                    data_schema=vol.Schema(
                        {vol.Optional("coarsen_oldest", default=False): BooleanSelector()}
                    ),
                    description_placeholders=placeholders,
                )
            return self.async_show_form(
                step_id="confirm_over_cap_final",
                data_schema=vol.Schema({}),
                description_placeholders=placeholders,
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    @staticmethod
    def _tiers_now(settings: Settings) -> list[dict]:
        ladder = resolution_ladder(settings.resolution, settings.custom_tiers)
        return [
            {"bucket_min": b / 60.0, "until_days": float(u) if u is not None else 0.0}
            for b, u in ladder
        ]

    async def async_step_confirm_over_cap(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_confirm(user_input or {})

    async def async_step_confirm_over_cap_final(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_finish()


class SedimentConfigFlow(SedimentFlowSteps, ConfigFlow, domain=DOMAIN):
    """Set up Sediment."""

    VERSION = 1

    def __init__(self) -> None:
        self._opts = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_history()

    async def _async_finish(self) -> ConfigFlowResult:
        self._opts.setdefault(CONF_COARSE_HORIZON_DAYS, DEFAULT_COARSE_HORIZON_DAYS)
        return self.async_create_entry(title="Sediment", data={}, options=self._opts)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SedimentOptionsFlow()


class SedimentOptionsFlow(SedimentFlowSteps, OptionsFlow):
    """Change Sediment's options."""

    def __init__(self) -> None:
        self._opts = {}

    def _manager(self) -> Any:
        return getattr(self.config_entry, "runtime_data", None)

    async def _retention_owned(self) -> bool:
        return await async_retention_owned(self.hass, self.config_entry)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._opts = dict(self.config_entry.options)
        return await self.async_step_history()

    async def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self._opts)
