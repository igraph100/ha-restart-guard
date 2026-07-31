"""Config and options flow for Restart Guard."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CHECK_CONDITIONS,
    CONF_LOOKAHEAD,
    CONF_MIN_INTERVAL,
    CONF_STALE_RUN,
    CONF_TRACK_SCHEDULES,
    CONF_WARN_WINDOW,
    DEFAULT_CHECK_CONDITIONS,
    DEFAULT_LOOKAHEAD,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_STALE_RUN,
    DEFAULT_TRACK_SCHEDULES,
    DEFAULT_WARN_WINDOW,
    DOMAIN,
)


def _number(minimum: int, maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="min",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_WARN_WINDOW,
                default=current.get(CONF_WARN_WINDOW, DEFAULT_WARN_WINDOW),
            ): _number(1, 60),
            vol.Required(
                CONF_LOOKAHEAD,
                default=current.get(CONF_LOOKAHEAD, DEFAULT_LOOKAHEAD),
            ): _number(10, 240),
            vol.Required(
                CONF_MIN_INTERVAL,
                default=current.get(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL),
            ): _number(5, 720),
            vol.Required(
                CONF_STALE_RUN,
                default=current.get(CONF_STALE_RUN, DEFAULT_STALE_RUN),
            ): _number(1, 1440),
            vol.Required(
                CONF_CHECK_CONDITIONS,
                default=current.get(
                    CONF_CHECK_CONDITIONS, DEFAULT_CHECK_CONDITIONS
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_TRACK_SCHEDULES,
                default=current.get(
                    CONF_TRACK_SCHEDULES, DEFAULT_TRACK_SCHEDULES
                ),
            ): selector.BooleanSelector(),
        }
    )


class RestartGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Restart Guard", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return RestartGuardOptionsFlow()


class RestartGuardOptionsFlow(OptionsFlow):
    """Let the user retune the windows without reinstalling."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
