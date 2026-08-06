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
    CONF_OPEN_ON_TAP,
    CONF_SCHEDULER_PATH,
    CONF_TAP_ANSWERED,
    CONF_TRACK_SCHEDULES,
    CONF_WARN_WINDOW,
    DEFAULT_CHECK_CONDITIONS,
    DEFAULT_LOOKAHEAD,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_OPEN_ON_TAP,
    DEFAULT_SCHEDULER_PATH,
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
    """The options form.

    The scheduler dashboard path is only offered when schedules are being
    watched at all, because otherwise it is a box that cannot do anything.

    Home Assistant has no way to grey a field out based on another field: the
    frontend renders one static description of the form and never asks for a
    new one when a toggle moves. So the next best thing is to leave the field
    out entirely when the saved setting says it is useless. Turn schedules on,
    save, and reopen - the box is there. Its stored value survives in the
    meantime, because the options flow merges rather than replaces.
    """
    watching_schedules = bool(
        current.get(CONF_TRACK_SCHEDULES, DEFAULT_TRACK_SCHEDULES)
    )

    fields: dict[Any, Any] = {
            vol.Required(
                CONF_WARN_WINDOW,
                default=current.get(CONF_WARN_WINDOW, DEFAULT_WARN_WINDOW),
            ): _number(1, 60),
            # up to a full day: calendar-driven automations turn over at
            # sunset or candle lighting, which is routinely 12-20 hours out,
            # and a four-hour ceiling could never reach them
            vol.Required(
                CONF_LOOKAHEAD,
                default=current.get(CONF_LOOKAHEAD, DEFAULT_LOOKAHEAD),
            ): _number(10, 1440),
            vol.Required(
                CONF_MIN_INTERVAL,
                default=current.get(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL),
            ): _number(5, 720),
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
            vol.Required(
                CONF_OPEN_ON_TAP,
                default=current.get(CONF_OPEN_ON_TAP, DEFAULT_OPEN_ON_TAP),
            ): selector.BooleanSelector(),
        }

    if watching_schedules:
        # Optional because blank is a real answer: no dashboard set, so
        # schedule rows fall back to the entity dialog. `vol.Required` on a
        # text box refuses an empty string, which would make the setting
        # impossible to clear once filled in.
        fields[
            vol.Optional(
                CONF_SCHEDULER_PATH,
                description={
                    "suggested_value": current.get(
                        CONF_SCHEDULER_PATH, DEFAULT_SCHEDULER_PATH
                    )
                },
            )
        ] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

    return vol.Schema(fields)


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
            # Merged, not replaced. `tap_answered` is deliberately absent from
            # the form - it is bookkeeping, not a setting - and saving options
            # would otherwise wipe it and start asking everybody again.
            #
            # Saving also counts as answering: the toggle is right there in the
            # form, so whoever pressed submit has already had their say and
            # should not then be asked the same question by the banner.
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    **user_input,
                    CONF_TAP_ANSWERED: True,
                },
            )

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
