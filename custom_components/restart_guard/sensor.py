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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from .calc import (
    AutomationInfo,
    key_match as _key_match,
    next_change_from_attributes,
    as_boolean,
    as_list,
    compute,
    soonest_ahead,
    state_edge,
    sun_slots,
    time_window_from_conditions,
    trigger_kind,
    two_valued_edge,
    window_from_attributes,
    weekdays_from_conditions,
)
from .conditions import ConditionEvaluator
from .schedules import collect as collect_schedules
from .const import (
    CONF_CHECK_CONDITIONS,
    CONF_LOOKAHEAD,
    CONF_MIN_INTERVAL,
    CONF_OPEN_ON_TAP,
    CONF_SCHEDULER_PATH,
    CONF_TAP_ANSWERED,
    CONF_TRACK_SCHEDULES,
    CONF_WARN_WINDOW,
    CONDITION_HORIZON,
    DEFAULT_CHECK_CONDITIONS,
    DEFAULT_LOOKAHEAD,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_OPEN_ON_TAP,
    DEFAULT_TAP_ANSWERED,
    DEFAULT_TRACK_SCHEDULES,
    DEFAULT_WARN_WINDOW,
    DOMAIN,
    NOTHING_DUE,
    RUN_DOMAINS,
    SCHEDULER_DOMAIN,
    SIGNAL_OPTIONS,
    STARTED_AT,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = dt.timedelta(seconds=30)
AUTOMATION_DOMAIN = "automation"
# How many upcoming runs to carry in the attributes. Far more than the banner
# shows, because `items` is also what people template against; `count` stays
# truthful either way, so nothing is misled by the trim.
MAX_ITEMS = 100

# `sun.sun` publishes the same times the sun trigger is scheduled from
SUN_ENTITY = "sun.sun"
SUN_ATTRIBUTES = {"sunrise": "next_rising", "sunset": "next_setting"}

# A `state` trigger is normally unknowable. Calendar integrations are the
# exception: every one of their entities schedules its own re-evaluation, and
# the moments they schedule against are published as timestamp sensors in the
# same config entry. So the entity's next change can be read off its siblings.
#
# Three tables describe one such integration:
#   edges    - entity -> the moments it turns on and off. Direction known, so
#              `to:` and `from:` resolve exactly.
#   changes  - entity -> the moments it is recomputed. Direction unknown, so a
#              directional trigger is only answerable when the entity is
#              two-valued and its current value gives the direction away.
#   default  - the same, for every other two-valued entity of that platform.
#              A table with one row per entity does not survive an integration
#              with a hundred and sixty of them.
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


# --------------------------------------------------------------------------
# YidCal (https://github.com/igraph100/YidCal)
# --------------------------------------------------------------------------
# Same idea, one integration further. YidCal publishes its zmanim as timestamp
# sensors in its own config entry, and every one of its binary sensors is
# recomputed at one of them.
#
# Most of them need nothing here: they publish a `Window_Start`/`Window_End`
# pair, which `window_from_attributes` reads first and which is better than
# anything a table could say. This exists for the ones that publish no window
# at all - and that is the large half, because the ~130 mirrors of
# `sensor.yidcal_holiday`'s flags carry no attributes whatsoever.
YIDCAL = "yidcal"
YIDCAL_EREV = "zman_erev"            # candle lighting
YIDCAL_MOTZI = "zman_motzi"          # havdalah
YIDCAL_SHKIA = "zman_shkia"          # sunset: the Hebrew date rolls, so do the flags
YIDCAL_ALOS = "alos"
YIDCAL_CHATZOS_DAY = "chatzos_hayom"
YIDCAL_CHATZOS_NIGHT = "chatzos_haleila"

# Where a family clearly turns over somewhere other than the day boundary.
# Kept short on purpose: a wrong entry here is a warning at the wrong minute,
# while a missing one just falls through to the default below.
YIDCAL_CHANGES: dict[str, tuple[str, ...]] = {
    "erev_after_chatzos": (YIDCAL_CHATZOS_DAY, YIDCAL_EREV),
    "erev_tisha_bav_after_chatzos": (YIDCAL_CHATZOS_DAY, YIDCAL_SHKIA),
    "tisha_bav_night": (YIDCAL_SHKIA, YIDCAL_MOTZI),
    # Nothing else. Two rows that used to live here were wrong, and both are
    # worth remembering rather than quietly deleting:
    #
    #   slichos          - turns on at havdalah and off at candle lighting,
    #                      which is `YIDCAL_DEFAULT` already. The row named
    #                      chatzos haleila and alos, neither of which is an
    #                      edge of it.
    #   longer_shachris  - runs 04:00 to 14:00 on the civil clock, so it has no
    #                      zman edges at all and cannot be expressed here. It
    #                      publishes its own window, which is the right answer.
    #
    # The comment above about a wrong entry costing a warning at the wrong
    # minute was written before either of those existed, and both proved it.
}

# Everything else: the day boundary. These are the four moments a flag can turn
# over on, and they are the same four YidCal itself walks when it works out a
# flag's window.
#
# Alos is in the list for a reason worth stating. Without it, a flag that turns
# over at dawn has no boundary before it, so the soonest is the *next
# evening* - asked at 02:00 about a 04:50 flip, the answer came back 20:05.
# Predicting late is the one answer this must never give: it reads as "safe to
# restart" for the fifteen hours in between, which is exactly the missed run
# the whole integration exists to prevent. Predicting early costs a warning
# nobody needed. The two errors are not the same size.
YIDCAL_DEFAULT = (YIDCAL_EREV, YIDCAL_MOTZI, YIDCAL_SHKIA, YIDCAL_ALOS)

YIDCAL_KEYS = sorted(YIDCAL_CHANGES, key=len, reverse=True)


# Kept per platform rather than pooled: two integrations are free to use the
# same suffix for moments of different kinds, and a shared set would silently
# apply one's rule to the other.
JEWISH_EVENT_KEYS = frozenset({CANDLE_KEY, HAVDALAH_KEY})
YIDCAL_EVENT_KEYS = frozenset({YIDCAL_EREV, YIDCAL_MOTZI})

# platform -> (edges, changes, default, keys). `default` is only ever applied
# to something two-valued: a binary sensor, or a boolean attribute.
BOUNDARY_PLATFORMS: dict[
    str, tuple[dict, dict, tuple[str, ...] | None, list[str], frozenset[str]]
] = {
    JEWISH_CALENDAR: (
        JEWISH_EDGES, JEWISH_CHANGES, None, JEWISH_KEYS, JEWISH_EVENT_KEYS,
    ),
    YIDCAL: (
        {}, YIDCAL_CHANGES, YIDCAL_DEFAULT, YIDCAL_KEYS, YIDCAL_EVENT_KEYS,
    ),
}

# Domains the per-platform default is applied to. A calendar integration's
# sensors and binary sensors are its output and all turn over on the day
# boundary; its `select`/`time`/`number` entities are configuration surfaces
# that change when somebody edits them, and predicting a zman for those would
# be noise.
PREDICTED_DOMAINS = ("binary_sensor", "sensor")

# An integration that publishes a row of flags as attributes may also publish
# one entity per flag. That entity knows its own window when the row it came
# from does not, so an `attribute:` trigger can be answered exactly instead of
# from the day boundary - but only if the two can be matched up. These are the
# two attributes a mirror uses to say what it mirrors. Deliberately a
# convention rather than a lookup table: any integration can adopt it, and
# nothing here has to know the integration exists.
MIRROR_SOURCE_ENTITY = "source_entity"
MIRROR_SOURCE_ATTRIBUTE = "source_attribute"

# Sentinel: this entity is rebuilt at local midnight rather than at any of the
# integration's published moments. A timestamp sensor holds one day's zman -
# `sensor.yidcal_zman_erev` is tonight's candle lighting - so it changes when
# the day rolls over, not when the zman it names arrives. Predicting sunset for
# it would be the wrong minute every single time.
MIDNIGHT: tuple[str, ...] = ("__midnight__",)

# Which published moments may be rolled forward when they have already passed.
#
# A solar zman recurs every day, so today's sunset that has already happened is
# tomorrow's to within a minute, and rolling it forward is right. A moment tied
# to an *event* is not like that: last Friday's candle lighting rolled forward
# a day is a Saturday evening that means nothing, and a boundary invented that
# way is a warning at a minute when nothing happens. Those are only used while
# they are still ahead of us.


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
        self._conditions = ConditionEvaluator(hass, self._changes_before)
        self._schedules_scanned = 0
        self._schedule_source = "not checked"
        self._value: float = NOTHING_DUE
        self._error: str | None = None
        self._scanned = 0
        self._total = 0
        self._trigger_kinds: dict[str, int] = {}
        self._predictions: dict[str, int] = {}
        self._prediction_log: list[dict[str, Any]] = []
        self._mirrors: dict[tuple[str, str], Any] | None = None
        self._declares: dict[str, bool] = {}

    async def async_added_to_hass(self) -> None:
        """Republish when a display option changes.

        Those changes deliberately don't reload the entry, so nothing else
        would tell the banner about them until the next poll - and a prompt
        that stays on screen for another half-minute after being answered
        reads as not having worked.
        """
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_OPTIONS, self.async_write_ha_state
            )
        )

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
            # read by the frontend module: may a row be tapped to open
            # whatever it is about
            "open_on_tap": self._option_bool(
                CONF_OPEN_ON_TAP, DEFAULT_OPEN_ON_TAP
            ),
            # whether anyone has answered the banner's prompt yet. Kept here
            # rather than in each browser's storage, so answering it on a phone
            # also answers it on the laptop.
            "tap_answered": self._option_bool(
                CONF_TAP_ANSWERED, DEFAULT_TAP_ANSWERED
            ),
            # where a schedule row leads. A schedule has no page of its own, so
            # this is whichever dashboard the user keeps their scheduler card
            # on; empty means the row opens the entity dialog instead.
            "scheduler_path": self._scheduler_path(),
            "automations_scanned": self._scanned,
            # what kinds of trigger were actually seen, so "it isn't warning me"
            # can be told apart from "it never saw that trigger at all"
            "trigger_kinds": self._trigger_kinds,
            # how `state` / `attribute` triggers were resolved: read off the
            # entity's own window, off its integration's published moments, off
            # a boolean attribute, or not at all
            "state_predictions": self._predictions,
            # `attribute:` triggers that produced no answer, and why. Empty is
            # the healthy state: a trigger that resolved is already counted in
            # `state_predictions` and needs no row here, and this is written to
            # the recorder on every update. So it costs nothing until something
            # is actually wrong, and then it says which link broke - the
            # attribute name, the value's type, the trigger's `to:`, or the
            # moment itself.
            "state_debug": self._prediction_log,
            "schedules_scanned": self._schedules_scanned,
            "schedule_conditions": self._schedule_source,
            "error": self._error,
        }

    async def async_update(self) -> None:
        """Recalculate. Runs in the event loop, so keep it cheap."""
        now = dt_util.now()
        try:
            self._predictions = {}
            self._prediction_log = []
            self._mirrors = None
            self._declares = {}
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
                self._attribute_change_next,
                self._pending_for_next,
                self._timer_finishes,
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

    def _scheduler_path(self) -> str:
        """The dashboard a schedule row should open, or "" for none.

        Normalised to a leading slash so `/lovelace/scheduler`, `lovelace/
        scheduler` and a stray trailing space all mean the same thing - the
        value is typed by hand into a text box, and a path that silently does
        nothing is a bad way to find out it was mistyped.
        """
        raw = str(self._options.get(CONF_SCHEDULER_PATH, "") or "").strip()
        if not raw:
            return ""
        return raw if raw.startswith("/") else f"/{raw}"

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
            # Near enough that reading the house now is a fair guess at how it
            # will be then - measured as a duration, not as "is it still today".
            moment = dt_util.parse_datetime(item["at"])
            near = moment is not None and (
                moment - now <= dt.timedelta(minutes=CONDITION_HORIZON)
            )
            verdict = await self._conditions.async_verdict(entity, item, near)
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
                    }
                )

        # Newest first. There used to be a "parked" flag here too - a run going
        # for over an hour was assumed to be idling in a wait_for_trigger and
        # sorted last, on its way to being treated as less urgent. That was
        # wrong: a three-hour delay is precisely what a restart destroys, and
        # taking a while is not evidence of doing nothing. Every run in
        # progress blocks now, so there is nothing left to rank them by.
        found.sort(
            key=lambda run: run["seconds_ago"] if run["seconds_ago"] is not None else 10**9
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
        # Some entities skip the window and name the single moment they next
        # change: a `schedule.*` helper's `next_event`, a running `timer.*`'s
        # `finishes_at`. Both are two-valued, so that moment plus the value
        # held now is a complete answer.
        announced = next_change_from_attributes(
            state.attributes, self._parse_moment
        )
        if announced is not None and announced > now:
            current = as_boolean(state.state)
            self._count_prediction("window")
            if current is None:
                return None if (to_state or from_state) else announced
            return two_valued_edge(current, to_state, from_state, announced)

        declared = bool(state.attributes.get(MIRROR_SOURCE_ATTRIBUTE))
        if turns_on or turns_off:
            answer = state_edge(to_state, from_state, turns_on, turns_off, now)
            # A *half* window is enough to get here: an entity that publishes
            # only an end has nothing to say about `to: "on"`, and returning
            # that silence would drop a run the boundary table could still have
            # placed. So only a real answer - or an entity that declares itself
            # a mirror, and is therefore the authority on its own flag - stops
            # here. Everything else falls through.
            if answer is not None or declared:
                self._count_prediction("window" if answer else "no edge")
                return answer

        # A declared mirror publishing nothing means "looked ahead, no edge",
        # which beats anything the day boundary could guess. Without that, this
        # would warn about Sukkos at every sunset in August.
        elif declared:
            self._count_prediction("no edge")
            return None

        # 2. its integration publishes the moments instead
        entry, edges, keys = self._boundary_plan(entity_id)
        if entry is None:
            self._count_prediction("unresolved")
            return None

        if edges is not None:
            self._count_prediction("boundary")
            return state_edge(
                to_state, from_state,
                self._soonest(entry, edges["on"], now),
                self._soonest(entry, edges["off"], now),
                now,
            )
        if keys is None:
            self._count_prediction("unresolved")
            return None

        self._count_prediction("boundary")
        moment = self._boundary_moment(entry, keys, now)

        # We know when this is recomputed, not what to. When the entity has
        # only two values that is enough anyway - `off` now can only become
        # `on` - so the direction is recovered from the current value.
        current = as_boolean(state.state)
        if current is not None:
            return two_valued_edge(current, to_state, from_state, moment)

        # Otherwise a `to:` naming one particular value would be reported every
        # single evening, which is noise - and a banner you learn to ignore
        # protects nobody.
        if to_state is not None or from_state is not None:
            return None
        return moment

    def _attribute_change_next(
        self, entity_id: str, attribute: str, to_state: Any, from_state: Any
    ) -> dt.datetime | None:
        """When a two-valued attribute next flips, if it can be known.

        A row of boolean attributes is a common way to publish a set of flags
        that are all recomputed together - one attribute per holiday, per day
        type, per mode. Watching one of those with an `attribute:` trigger is
        as predictable as watching the entity itself, and rather better: the
        flag has only two values, so the one it holds now says which way the
        next change has to go.

        An attribute holding anything else returns None, exactly as every
        `attribute:` trigger did before this existed.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        current = as_boolean(state.attributes.get(attribute))
        if current is None:
            self._count_prediction("unresolved")
            return None

        now = dt_util.now()

        # Best case: something mirrors this exact flag and publishes its
        # window. Then there is nothing to estimate - the mirror holds the
        # minute the flag turns over, and because the flag has two values the
        # next change is a start or an end depending on which it holds now.
        mirror = self._mirror_for(entity_id, attribute)
        if mirror is not None:
            starts, ends = window_from_attributes(
                mirror.attributes, self._parse_moment
            )
            edges = ends if current else starts
            exact = min((edge for edge in edges if edge > now), default=None)
            if exact is None:
                # the mirror exists and publishes nothing, so there is no edge
                # ahead worth reporting - see above, this is an answer
                self._count_prediction("no edge")
                return None
            if exact is not None:
                answer = two_valued_edge(current, to_state, from_state, exact)
                self._count_prediction("attribute" if answer else "ruled out")
                if answer is None and len(self._prediction_log) < 10:
                    self._prediction_log.append({
                        "entity_id": entity_id, "attribute": attribute,
                        "reads": state.attributes.get(attribute),
                        "parsed_as": current, "wants_to": to_state,
                        "wants_from": from_state, "via": mirror.entity_id,
                        "found": exact.isoformat(), "answer": None,
                    })
                return answer

        # Otherwise fall back to when the entity is next recomputed. Both
        # sources count, soonest wins: a window attribute describes when the
        # *state* turns over, which is not necessarily when a flag does, so
        # preferring one would be a guess and the sooner errs early.
        candidates: list[dt.datetime | None] = []
        turns_on, turns_off = window_from_attributes(
            state.attributes, self._parse_moment
        )
        candidates += [moment for moment in turns_on + turns_off if moment > now]

        entry, edges, keys = self._boundary_plan(entity_id)
        if entry is not None:
            if edges is not None:
                keys = tuple(edges.get("on", ())) + tuple(edges.get("off", ()))
            if keys:
                candidates.append(self._boundary_moment(entry, keys, now))

        moment = min(
            (found for found in candidates if found is not None), default=None
        )
        answer = two_valued_edge(current, to_state, from_state, moment)
        if moment is None:
            self._count_prediction("unresolved")
        else:
            self._count_prediction("attribute" if answer else "ruled out")
        # Only the ones that came back empty-handed. A resolved trigger is
        # visible in `items` and counted in `state_predictions` already.
        if answer is None and len(self._prediction_log) < 10:
            self._prediction_log.append({
                "entity_id": entity_id,
                "attribute": attribute,
                "reads": state.attributes.get(attribute),
                "parsed_as": current,
                "wants_to": to_state,
                "wants_from": from_state,
                "found": moment.isoformat() if moment else None,
                "answer": answer.isoformat() if answer else None,
            })
        return answer

    def _uses_mirrors(self, config_entry_id: Any) -> bool:
        """Does this integration publish per-flag mirrors that declare themselves?

        It decides how far the per-platform default can be trusted. Where the
        convention is in use, every flag that is about to turn on says so in
        its own window, and the default is only ever reached by the handful of
        entities that genuinely do turn over on the day boundary.

        Where it is not - an older version of the same integration - there is
        no way to tell a flag that comes on at tonight's candle lighting from
        one that comes on in eight months. Both read `off` and publish nothing.
        Answering the second with "the next boundary" is a countdown attached
        to an automation that will not run, which is worse than the silence
        this gave before the default existed.
        """
        if config_entry_id in self._declares:
            return self._declares[config_entry_id]
        found = False
        try:
            registry = er.async_get(self.hass)
            for other in er.async_entries_for_config_entry(
                registry, config_entry_id
            ):
                state = self.hass.states.get(other.entity_id)
                if state is not None and state.attributes.get(
                    MIRROR_SOURCE_ATTRIBUTE
                ):
                    found = True
                    break
        except Exception:  # noqa: BLE001 - assume the older shape
            found = False
        self._declares[config_entry_id] = found
        return found

    def _mirror_for(self, entity_id: str, attribute: str) -> Any:
        """The entity that declares itself the mirror of this attribute.

        Built once per update and only when something actually asks, since an
        install with no `attribute:` triggers should never pay for it.
        """
        if self._mirrors is None:
            self._mirrors = {}
            for state in self.hass.states.async_all():
                attrs = state.attributes
                source = attrs.get(MIRROR_SOURCE_ENTITY)
                flag = attrs.get(MIRROR_SOURCE_ATTRIBUTE)
                if source and flag:
                    self._mirrors.setdefault((str(source), str(flag)), state)
        return self._mirrors.get((entity_id, attribute))

    def _pending_for_next(
        self, entity_id: str, to_state: Any, from_state: Any, seconds: int
    ) -> dt.datetime | None:
        """When a `state ... for:` trigger fires, counting the delay.

        Two cases, and the first is the one that matters. If the entity is
        *already* sitting in the state being waited on, the countdown is
        running right now and completes at `last_changed + for` - and Home
        Assistant throws that countdown away on restart, so the run is lost
        silently. If it is not there yet, the run is the predicted change plus
        the delay, which is only knowable when the change itself is.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        held = dt.timedelta(seconds=seconds)
        now = dt_util.now()

        wanted = to_state if to_state is not None else None
        matches = wanted is None or any(
            str(value).lower() == str(state.state).lower()
            for value in as_list(wanted)
        )
        if matches and state.last_changed is not None:
            since = dt_util.as_local(state.last_changed)
            done = since + held
            # A state restored at startup was never *observed* changing, and
            # Home Assistant only arms a `for:` countdown on an observed
            # change. So a `last_changed` at or before the moment Home
            # Assistant finished starting means no countdown is running,
            # however recent it looks - it is just when everything came back.
            # Believing it produced a phantom run after every restart, for the
            # whole length of the delay.
            started = self.hass.data.get(STARTED_AT)
            restored = started is not None and since <= started
            if done > now and not restored:
                self._count_prediction("pending for")
                # `last_changed` carries microseconds; every other prediction
                # here is a whole second, and the banner shows minutes
                return done.replace(microsecond=0)
            if restored:
                self._count_prediction("for: not armed")

        change = self._state_change_next(entity_id, to_state, from_state)
        return None if change is None else change + held

    def _timer_finishes(self, entity_id: str) -> dt.datetime | None:
        """When a running timer reaches zero.

        A timer does not survive a restart either, so this is worth the same
        warning a clock trigger gets.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state != "active":
            return None
        moment = self._parse_moment(state.attributes.get("finishes_at"))
        if moment is not None:
            self._count_prediction("timer")
        return moment

    def _changes_before(self, entity_id: str, until: dt.datetime) -> bool:
        """Does this entity change between now and `until`?

        Reuses the same prediction the triggers use, so anything Restart Guard
        can foresee a trigger on it can also foresee a condition on. The
        counters are restored afterwards: these lookups are bookkeeping for the
        condition check and would otherwise inflate `state_predictions`.
        """
        saved = dict(self._predictions)
        try:
            moment = self._state_change_next(entity_id, None, None)
        except Exception:  # noqa: BLE001 - unknowable means it might move
            return True
        finally:
            self._predictions = saved
        return moment is not None and moment <= until

    def _count_prediction(self, outcome: str) -> None:
        """Tally how each state/attribute trigger was resolved, as a diagnostic.

        Same purpose as `trigger_kinds`: \"it isn't warning me\" needs to tell
        \"read, and not due\" apart from \"never resolved at all\".
        """
        self._predictions[outcome] = self._predictions.get(outcome, 0) + 1

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

    def _boundary_plan(
        self, entity_id: str
    ) -> tuple[Any, dict[str, tuple[str, ...]] | None, tuple[str, ...] | None]:
        """How to predict this entity's next change from its own integration.

        Returns (registry entry, on/off moments, any-change moments). The
        entity is matched on its unique_id, not its entity_id, so renaming it
        changes nothing.

        The per-platform default covers whatever the table does not name, so a
        calendar integration with a hundred and sixty entities does not need a
        hundred and sixty rows. Timestamp sensors are split off to midnight,
        and domains outside `PREDICTED_DOMAINS` get nothing.
        """
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is None:
            return None, None, None
        table = BOUNDARY_PLATFORMS.get(entry.platform)
        if table is None:
            return None, None, None

        edges, changes, default, keys, _events = table
        key = _key_match(entry.unique_id, entity_id, keys)
        if key is not None:
            if key in edges:
                return entry, edges[key], None
            return entry, None, changes[key]

        if default is None or entity_id.split(".", 1)[0] not in PREDICTED_DOMAINS:
            return None, None, None

        state = self.hass.states.get(entity_id)
        device_class = state.attributes.get("device_class") if state else None
        if str(device_class or "") == "timestamp":
            return entry, None, MIDNIGHT

        # Without the mirror convention, a two-valued entity that is currently
        # off tells us nothing about *which* boundary turns it on - see
        # `_uses_mirrors`. One that is on is a different matter: these windows
        # are short, so the next boundary really is where it ends.
        if (
            state is not None
            and as_boolean(state.state) is False
            and not self._uses_mirrors(getattr(entry, "config_entry_id", None))
        ):
            return None, None, None
        return entry, None, default

    def _boundary_moment(
        self, entry: Any, keys: tuple[str, ...], now: dt.datetime
    ) -> dt.datetime | None:
        """The next moment this entity is recomputed."""
        if keys is MIDNIGHT:
            return (now + dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        return self._soonest(entry, keys, now)

    def _soonest(
        self, entry: Any, keys: tuple[str, ...], now: dt.datetime
    ) -> dt.datetime | None:
        """The nearest moment published by these sibling sensors."""
        if entry is None:
            return None
        registry = er.async_get(self.hass)
        table = BOUNDARY_PLATFORMS.get(getattr(entry, "platform", ""))
        event_keys = table[4] if table else frozenset()
        recurring: list[dt.datetime | None] = []
        one_off: list[dt.datetime] = []
        for other in er.async_entries_for_config_entry(
            registry, entry.config_entry_id
        ):
            key = _key_match(other.unique_id, other.entity_id, keys)
            if key is None:
                continue
            moment = self._entity_moment(other.entity_id)
            if moment is None:
                continue
            if key in event_keys:
                if moment > now:
                    one_off.append(moment)
            else:
                recurring.append(moment)

        candidates = one_off + [soonest_ahead(recurring, now)]
        ahead = [moment for moment in candidates if moment is not None]
        return min(ahead) if ahead else None

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
