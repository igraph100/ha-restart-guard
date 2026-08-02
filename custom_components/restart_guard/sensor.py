"""The Restart Guard sensor: minutes until the next timed automation."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from .calc import (
    AutomationInfo,
    as_list,
    compute,
    soonest_ahead,
    state_edge,
    sun_slots,
    time_window_from_conditions,
    trigger_kind,
    window_from_attributes,
    weekdays_from_conditions,
)
from .conditions import ConditionEvaluator
from .schedules import collect as collect_schedules
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
    NOTHING_DUE,
    RUN_DOMAINS,
    SCHEDULER_DOMAIN,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = dt.timedelta(seconds=30)
AUTOMATION_DOMAIN = "automation"
# how many upcoming runs to carry in the attributes; the banner lists six
MAX_ITEMS = 100

# `sun.sun` publishes the same times the sun trigger is scheduled from
SUN_ENTITY = "sun.sun"
SUN_ATTRIBUTES = {"sunrise": "next_rising", "sunset": "next_setting"}

# A `state` trigger is normally unknowable. The native jewish_calendar
# integration is an exception: every one of its entities schedules its own
# re-evaluation, and the moments it schedules against are published as sensors.
# The two tables below are those schedules, transcribed from the integration's
# `next_update_fn` (sensor.py) and `_update_times` (binary_sensor.py).
JEWISH_CALENDAR = "jewish_calendar"
CANDLE_KEY = "upcoming_candle_lighting"
HAVDALAH_KEY = "upcoming_havdalah"
SHKIA_KEY = "shkia"
NETZ_KEY = "netz_hachama"

# Entities whose direction is known, so `to:` / `from:` can pick an edge.
JEWISH_EDGES: dict[str, dict[str, tuple[str, ...]]] = {
    "issur_melacha_in_effect": {"on": (CANDLE_KEY,), "off": (HAVDALAH_KEY,)},
}

# Entities that change value at a known moment, but not in a known direction.
# `holiday` really updates at "candle lighting, else havdalah, else shkia";
# taking the soonest of the three is the same answer on every ordinary day and
# errs earlier, which is the safe direction for a warning.
JEWISH_CHANGES: dict[str, tuple[str, ...]] = {
    "date": (SHKIA_KEY,),
    "omer_count": (SHKIA_KEY,),
    "daf_yomi": (SHKIA_KEY,),
    "weekly_portion": (HAVDALAH_KEY,),
    "holiday": (CANDLE_KEY, HAVDALAH_KEY, SHKIA_KEY),
    "upcoming_candle_lighting": (HAVDALAH_KEY,),
    "upcoming_havdalah": (HAVDALAH_KEY,),
    "upcoming_shabbat_candle_lighting": (HAVDALAH_KEY,),
    "upcoming_shabbat_havdalah": (HAVDALAH_KEY,),
    "erev_shabbat_hag": (CANDLE_KEY, HAVDALAH_KEY, NETZ_KEY),
    "motzei_shabbat_hag": (CANDLE_KEY, HAVDALAH_KEY, NETZ_KEY),
}

# longest first, so upcoming_candle_lighting is not matched as candle_lighting
JEWISH_KEYS = sorted(
    set(JEWISH_EDGES) | set(JEWISH_CHANGES), key=len, reverse=True
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor."""
    async_add_entities([RestartGuardSensor(hass, entry)], True)


class RestartGuardSensor(SensorEntity):
    """How long until the next time-based automation runs."""

    _attr_has_entity_name = False
    _attr_name = "Restart guard"
    _attr_icon = "mdi:restart-alert"
    _attr_native_unit_of_measurement = "min"
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_next_timed_automation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Restart Guard",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._items: list[dict[str, Any]] = []
        self._skipped: list[dict[str, Any]] = []
        self._running: list[dict[str, Any]] = []
        self._conditions = ConditionEvaluator(hass)
        self._schedules_scanned = 0
        self._schedule_source = "not checked"
        self._value: float = NOTHING_DUE
        self._error: str | None = None
        self._scanned = 0
        self._total = 0
        self._trigger_kinds: dict[str, int] = {}

    # -- options ----------------------------------------------------------
    @property
    def _options(self) -> dict[str, Any]:
        return {**self._entry.data, **self._entry.options}

    def _option(self, key: str, default: int) -> int:
        try:
            return int(float(self._options.get(key, default)))
        except (TypeError, ValueError):
            return default

    # -- entity -----------------------------------------------------------
    @property
    def native_value(self) -> float:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            # marker so the frontend module can find this entity even if renamed
            "restart_guard": True,
            # so "did my copy actually land?" is answerable at a glance
            "version": VERSION,
            "items": self._items,
            "count": self._total,
            "next_at": self._items[0]["at"] if self._items else None,
            # would fire but do nothing, so deliberately not warned about
            "skipped": self._skipped,
            "skipped_count": len(self._skipped),
            # automations / scripts part-way through a run right now
            "running": self._running,
            "running_count": len(self._running),
            # every run in progress blocks now, however long it has been going
            "blocking_runs": len(self._running),
            "warn_window": self._option(CONF_WARN_WINDOW, DEFAULT_WARN_WINDOW),
            "lookahead": self._option(CONF_LOOKAHEAD, DEFAULT_LOOKAHEAD),
            "automations_scanned": self._scanned,
            # what kinds of trigger were actually seen, so "it isn't warning me"
            # can be told apart from "it never saw that trigger at all"
            "trigger_kinds": self._trigger_kinds,
            "schedules_scanned": self._schedules_scanned,
            "schedule_conditions": self._schedule_source,
            "error": self._error,
        }

    async def async_update(self) -> None:
        """Recalculate. Runs in the event loop, so keep it cheap."""
        now = dt_util.now()
        try:
            self._running = self._collect_running(now)
        except Exception:  # noqa: BLE001 - never let this break the sensor
            _LOGGER.exception("Restart Guard could not read in-progress runs")
            self._running = []
        try:
            automations = self._collect_automations()
            self._describe_triggers(automations)
            items = compute(
                automations,
                now,
                now.tzinfo or dt_util.DEFAULT_TIME_ZONE,
                self._option(CONF_LOOKAHEAD, DEFAULT_LOOKAHEAD),
                self._resolve_entity_time,
                self._sun_next,
                self._option(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL),
                self._state_change_next,
                self._calendar_moment,
            )
        except Exception as err:  # noqa: BLE001 - a bad automation must not kill the sensor
            _LOGGER.exception("Restart Guard could not work out the next run")
            self._error = str(err)
            self._total = 0
            self._items = []
            self._skipped = []
            self._value = NOTHING_DUE
            return

        skipped: list[dict[str, Any]] = []
        if self._option_bool(CONF_CHECK_CONDITIONS, DEFAULT_CHECK_CONDITIONS):
            try:
                items, skipped = await self._async_drop_no_ops(items, now)
            except Exception:  # noqa: BLE001 - fall back to warning about everything
                _LOGGER.exception("Restart Guard could not evaluate conditions")
                skipped = []

        # Scheduler component schedules. They carry no conditions, so they are
        # merged after the condition filtering rather than through it.
        self._schedules_scanned = 0
        if self._option_bool(CONF_TRACK_SCHEDULES, DEFAULT_TRACK_SCHEDULES):
            try:
                if self._option_bool(CONF_CHECK_CONDITIONS, DEFAULT_CHECK_CONDITIONS):
                    check = self._schedule_lookup()
                else:
                    check = None
                    self._schedule_source = "disabled by option"
                schedule_items, schedule_skipped, self._schedules_scanned = (
                    collect_schedules(
                        self.hass.states.async_all("switch"),
                        now,
                        self._option(CONF_LOOKAHEAD, DEFAULT_LOOKAHEAD),
                        dt_util.parse_datetime,
                        self._friendly_name,
                        check,
                        self.hass.states.get,
                    )
                )
                items = sorted(items + schedule_items, key=lambda i: i["minutes"])
                skipped = sorted(
                    skipped + schedule_skipped, key=lambda i: i["minutes"]
                )
            except Exception:  # noqa: BLE001 - scheduler is optional, never fatal
                _LOGGER.exception("Restart Guard could not read Scheduler schedules")

        self._error = None
        # Attributes are written to the recorder every time this updates, and
        # a day-long lookahead can turn up hundreds of runs. The banner shows
        # six, so carrying every one would cost database for nothing. `count`
        # stays truthful, so nothing that reads it is misled by the trim.
        self._total = len(items)
        self._items = items[:MAX_ITEMS]
        self._skipped = skipped[:MAX_ITEMS]
        self._value = items[0]["minutes"] if items else NOTHING_DUE

    def _schedule_lookup(self):
        """entity_id -> Scheduler schedule definition, or None if unavailable.

        The Scheduler component keeps its schedules on a coordinator in
        `hass.data["scheduler"]`, and the switch entity's unique_id is the
        schedule_id, so the entity registry gives us the mapping.
        """
        data = self.hass.data.get(SCHEDULER_DOMAIN)
        if data is None:
            self._schedule_source = "scheduler not in hass.data"
            return None
        coordinator = data.get("coordinator") if isinstance(data, dict) else None
        if coordinator is None:
            self._schedule_source = (
                f"no coordinator (hass.data['{SCHEDULER_DOMAIN}'] is "
                f"{type(data).__name__}, keys="
                f"{list(data)[:6] if isinstance(data, dict) else 'n/a'})"
            )
            return None
        if not hasattr(coordinator, "async_get_schedule"):
            self._schedule_source = (
                f"coordinator has no async_get_schedule ({type(coordinator).__name__})"
            )
            return None
        self._schedule_source = "ok"
        registry = er.async_get(self.hass)

        def lookup(entity_id: str) -> Any:
            entry = registry.async_get(entity_id)
            if entry is None or not entry.unique_id:
                return None
            try:
                return coordinator.async_get_schedule(entry.unique_id)
            except Exception:  # noqa: BLE001 - unknown id, changed API, ...
                return None

        return lookup

    def _friendly_name(self, entity_id: str) -> str:
        """Display name for an entity a schedule targets."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return entity_id
        return str(state.attributes.get("friendly_name") or entity_id)

    def _option_bool(self, key: str, default: bool) -> bool:
        value = self._options.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    async def _async_drop_no_ops(
        self, items: list[dict[str, Any]], now: dt.datetime
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split projected runs into ones that will act and ones that won't.

        An automation whose conditions don't pass, or whose `choose` has no
        branch for this trigger, fires and does nothing - restarting through
        that is harmless, so it should not warn.
        """
        component = self._automation_component()
        entities = (
            {entity.entity_id: entity for entity in list(component.entities)}
            if component is not None
            else {}
        )

        keep: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in items:
            entity = entities.get(item["entity_id"])
            if entity is None:
                keep.append(item)
                continue
            same_day = dt_util.parse_datetime(item["at"]) is not None and (
                dt_util.parse_datetime(item["at"]).date() == now.date()
            )
            verdict = await self._conditions.async_verdict(entity, item, same_day)
            if verdict.will_run:
                keep.append(item)
            else:
                skipped.append({**item, "reason": verdict.reason})
        return keep, skipped

    # -- automations / scripts part-way through a run ----------------------
    def _collect_running(self, now: dt.datetime) -> list[dict[str, Any]]:
        """Anything with a run in progress right now.

        Restarting mid-run cuts the remaining steps, so a `delay`,
        `wait_template`, `wait_for_trigger`, `repeat` or slow action all matter -
        and the `current` attribute covers every one of them without us having
        to understand the action script at all.
        """
        parked_after = self._option(CONF_STALE_RUN, DEFAULT_STALE_RUN) * 60
        found: list[dict[str, Any]] = []

        for domain in RUN_DOMAINS:
            for state in self.hass.states.async_all(domain):
                try:
                    current = int(state.attributes.get("current") or 0)
                except (TypeError, ValueError):
                    continue
                if current < 1:
                    continue

                started = state.attributes.get("last_triggered")
                seconds: int | None = None
                if started is not None:
                    parsed = dt_util.parse_datetime(str(started))
                    if parsed is not None:
                        seconds = max(0, int((now - dt_util.as_local(parsed)).total_seconds()))

                found.append(
                    {
                        "entity_id": state.entity_id,
                        "alias": state.attributes.get("friendly_name") or state.entity_id,
                        "current": current,
                        "seconds_ago": seconds,
                        # a run idling for ages is almost certainly parked in a
                        # wait_for_trigger: worth showing, not worth blocking on
                        "parked": bool(
                            parked_after and seconds is not None and seconds > parked_after
                        ),
                    }
                )

        found.sort(
            key=lambda run: (
                run["parked"],
                run["seconds_ago"] if run["seconds_ago"] is not None else 10**9,
            )
        )
        return found

    # -- reading the live automations -------------------------------------
    def _automation_component(self) -> Any:
        """The automation EntityComponent, however this core version stores it."""
        component = self.hass.data.get(AUTOMATION_DOMAIN)
        if component is not None and hasattr(component, "entities"):
            return component
        return (self.hass.data.get(DATA_INSTANCES) or {}).get(AUTOMATION_DOMAIN)

    def _collect_automations(self) -> list[AutomationInfo]:
        """Read trigger config straight off the running automation entities.

        Better than parsing automations.yaml: blueprint inputs are already
        substituted in `_trigger_config`, so blueprint-based automations are
        visible too.
        """
        component = self._automation_component()
        if component is None:
            self._scanned = 0
            return []

        found: list[AutomationInfo] = []
        scanned = 0
        # copy before iterating: the underlying dict can change under async
        for entity in list(component.entities):
            scanned += 1
            if entity.state != STATE_ON:
                continue  # switched-off automations cannot fire
            triggers = _triggers_for(entity)
            if not triggers:
                continue
            found.append(
                AutomationInfo(
                    entity_id=entity.entity_id,
                    name=entity.name or entity.entity_id,
                    triggers=triggers,
                    weekdays=weekdays_from_conditions(
                        getattr(entity, "raw_config", None)
                    ),
                    window=time_window_from_conditions(
                        getattr(entity, "raw_config", None)
                    ),
                )
            )
        self._scanned = scanned
        return found

    # -- callbacks handed to calc.compute ---------------------------------
    def _resolve_entity_time(self, entity_id: str) -> dt.time | dt.datetime | None:
        """Resolve `at: input_datetime.wake_up` and timestamp sensors."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        attrs = state.attributes

        # input_datetime with a time component only
        if attrs.get("has_time") and not attrs.get("has_date"):
            return dt_util.parse_time(state.state)

        # input_datetime carrying a date: it exposes an epoch timestamp
        timestamp = attrs.get("timestamp")
        if attrs.get("has_date") and timestamp is not None:
            try:
                return dt_util.as_local(dt_util.utc_from_timestamp(float(timestamp)))
            except (TypeError, ValueError):
                return None

        parsed = dt_util.parse_datetime(state.state)
        if parsed is not None:
            return dt_util.as_local(parsed)
        return dt_util.parse_time(state.state)

    def _describe_triggers(self, automations: list[AutomationInfo]) -> None:
        """Count the trigger kinds seen, as a diagnostic.

        "It isn't warning me" has two very different causes: the trigger was
        understood and judged not due, or it was never recognised at all. This
        tells them apart at a glance, which is otherwise surprisingly hard.
        """
        kinds: dict[str, int] = {}
        for auto in automations:
            for trigger in auto.triggers:
                kind = trigger_kind(trigger) or "unknown"
                kinds[kind] = kinds.get(kind, 0) + 1
        self._trigger_kinds = dict(sorted(kinds.items()))

    def _state_change_next(
        self, entity_id: str, to_state: Any, from_state: Any
    ) -> dt.datetime | None:
        """When a watched entity next changes state, if it can be known.

        Most `state` triggers are unknowable and stay that way. Two kinds are
        not: an entity that publishes its own window, and one whose integration
        publishes the moments it flips. Anything else returns None, which is
        exactly what happened before this existed.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        now = dt_util.now()

        # 1. the entity publishes its own window
        turns_on, turns_off = window_from_attributes(
            state.attributes, self._parse_moment
        )
        if turns_on or turns_off:
            return state_edge(to_state, from_state, turns_on, turns_off, now)

        # 2. its integration publishes the moments instead
        key, entry = self._jewish_key(entity_id)
        if key is None:
            return None

        edges = JEWISH_EDGES.get(key)
        if edges is not None:
            return state_edge(
                to_state, from_state,
                self._soonest(entry, edges["on"], now),
                self._soonest(entry, edges["off"], now),
                now,
            )

        keys = JEWISH_CHANGES.get(key)
        if keys is None:
            return None
        # We know when this changes, not what it changes to. A `to:` naming a
        # particular holiday would then be reported every single evening, which
        # is noise - and a banner you learn to ignore protects nobody.
        if to_state is not None or from_state is not None:
            return None
        return self._soonest(entry, keys, now)

    def _calendar_moment(self, entity_id: str, which: str) -> dt.datetime | None:
        """When the event this calendar is on, or waiting for, starts or ends.

        A calendar entity publishes the current event while one is running and
        the next one otherwise, so the same two attributes answer both.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        key = "end_time" if which == "end" else "start_time"
        return self._parse_moment(state.attributes.get(key))

    def _jewish_key(self, entity_id: str) -> tuple[str | None, Any]:
        """Which jewish_calendar entity this is, and its registry entry."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != JEWISH_CALENDAR:
            return None, None
        name = f"{entry.unique_id or ''}|{entity_id}"
        for key in JEWISH_KEYS:
            if name.endswith(key):
                return key, entry
        return None, None

    def _soonest(
        self, entry: Any, keys: tuple[str, ...], now: dt.datetime
    ) -> dt.datetime | None:
        """The nearest moment published by these sibling sensors."""
        if entry is None:
            return None
        registry = er.async_get(self.hass)
        found: list[dt.datetime | None] = []
        for other in er.async_entries_for_config_entry(
            registry, entry.config_entry_id
        ):
            name = f"{other.unique_id or ''}|{other.entity_id}"
            if any(name.endswith(key) for key in keys):
                found.append(self._entity_moment(other.entity_id))
        return soonest_ahead(found, now)

    def _entity_moment(self, entity_id: str) -> dt.datetime | None:
        """A timestamp entity's value."""
        state = self.hass.states.get(entity_id)
        return self._parse_moment(state.state) if state else None

    @staticmethod
    def _parse_moment(value: Any) -> dt.datetime | None:
        """An attribute or state that might be a moment in time."""
        if isinstance(value, dt.datetime):
            return dt_util.as_local(value)
        if not value or str(value) in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        parsed = dt_util.parse_datetime(str(value))
        return dt_util.as_local(parsed) if parsed else None

    def _sun_next(self, event: str, offset: int) -> dt.datetime | None:
        """Next sunrise/sunset with the trigger's offset applied.

        Reads the `next_rising` / `next_setting` attributes `sun.sun` already
        publishes, in preference to calling the astral helper. Two reasons:
        those are the very numbers Home Assistant schedules the trigger from,
        so the prediction cannot drift from what actually happens; and reading
        a state attribute has no way to fail the way a helper call does.

        The astral helper stays as a fallback for the case where `sun.sun` is
        missing, and any failure is now logged rather than swallowed.
        """
        base = self._sun_event_time(event)
        if base is not None:
            slots = sun_slots(base, offset, dt_util.now())
            return slots[0] if slots else None

        _LOGGER.debug("sun.sun has no %s, falling back to the astral helper", event)
        try:
            moment = get_astral_event_next(
                self.hass, event, offset=dt.timedelta(seconds=offset)
            )
        except Exception:  # noqa: BLE001 - sun not set up, bad latitude, etc.
            _LOGGER.warning(
                "Restart Guard could not work out the next %s, so sun triggers "
                "will not be reported", event, exc_info=True,
            )
            return None
        return dt_util.as_local(moment) if moment else None

    def _sun_event_time(self, event: str) -> dt.datetime | None:
        """`sun.sun`'s published time for sunrise or sunset."""
        attribute = SUN_ATTRIBUTES.get(event)
        if attribute is None:
            return None
        state = self.hass.states.get(SUN_ENTITY)
        if state is None:
            return None
        raw = state.attributes.get(attribute)
        parsed = dt_util.parse_datetime(str(raw)) if raw else None
        return dt_util.as_local(parsed) if parsed else None


def _triggers_for(entity: Any) -> list[dict[str, Any]]:
    """Trigger list for an automation entity, blueprints included.

    `_trigger_config` is the validated, blueprint-substituted trigger list on
    AutomationEntity. Automations that failed validation are represented by
    UnavailableAutomationEntity, which has no trigger config at all - hence the
    fall back to the public `raw_config`.
    """
    resolved = getattr(entity, "_trigger_config", None)
    if resolved:
        return [trigger for trigger in resolved if isinstance(trigger, dict)]
    raw = getattr(entity, "raw_config", None) or {}
    if not isinstance(raw, dict):
        return []
    listed = as_list(raw.get("triggers") or raw.get("trigger"))
    return [trigger for trigger in listed if isinstance(trigger, dict)]
