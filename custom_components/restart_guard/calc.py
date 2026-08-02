"""Work out when each automation is next due to fire.

Deliberately free of Home Assistant imports so it can be unit tested on its
own. Everything Home Assistant knows about the outside world (entity states,
sun times) arrives through the two callables passed into :func:`compute`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUN_EVENTS = ("sunrise", "sunset")
# old style is `trigger: calendar` with `event: start|end`; newer configs
# name the edge in the trigger itself
CALENDAR_KINDS = ("calendar", "calendar.event_started", "calendar.event_ended")

ResolveEntity = Callable[[str], "dt.time | dt.datetime | None"]
SunNext = Callable[[str, int], "dt.datetime | None"]
# (entity_id, to, from) -> when that state change next happens
StateNext = Callable[[str, Any, Any], "dt.datetime | None"]
# (entity_id, "start"|"end") -> when the calendar event begins or ends
CalendarNext = Callable[[str, str], "dt.datetime | None"]


@dataclass
class AutomationInfo:
    """What we need to know about one automation."""

    entity_id: str
    name: str
    triggers: list[dict[str, Any]] = field(default_factory=list)
    weekdays: set[str] | None = None
    # (after, before) from a `condition: time`, when it pins the run to a
    # window. A minute-by-minute trigger fenced into 20 minutes a week is not
    # the noisy automation the min-interval filter exists to suppress.
    window: tuple[dt.time, dt.time] | None = None


# --------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------
def trigger_kind(trigger: Any) -> str | None:
    """New syntax uses `trigger:`, pre-2024.10 syntax used `platform:`."""
    if not isinstance(trigger, dict):
        return None
    kind = trigger.get("trigger") or trigger.get("platform")
    return str(kind) if kind else None


def trigger_field(trigger: Any, name: str) -> Any:
    """A trigger's field, wherever the validated config happens to keep it.

    Home Assistant does not guarantee that a validated trigger is flat. A sun
    trigger came back as `{"trigger": "sun", ...}` with `event` and `offset`
    nested a level down, which made every sun trigger look eventless and got
    them all dropped without a word. Reading by name instead of by position
    survives that, and survives it moving again.
    """
    if not isinstance(trigger, dict):
        return None
    if trigger.get(name) is not None:
        return trigger[name]
    for value in trigger.values():
        if isinstance(value, dict) and value.get(name) is not None:
            return value[name]
    for value in trigger.values():
        if isinstance(value, dict):
            found = trigger_field(value, name)
            if found is not None:
                return found
    return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def parse_clock(value: Any) -> dt.time | None:
    """'07:00:00' / '7:00' / time -> time. Entity ids return None."""
    if isinstance(value, dt.time):
        return value
    if isinstance(value, dt.datetime):
        return value.timetz() if value.tzinfo else value.time()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "." in text or text[0].isalpha():
        return None
    bits = text.split(":")
    try:
        hour = int(bits[0])
        minute = int(bits[1]) if len(bits) > 1 else 0
        second = int(float(bits[2])) if len(bits) > 2 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    return dt.time(hour, minute, second)


def is_entity_id(value: Any) -> bool:
    return isinstance(value, str) and "." in value and value[0].isalpha()


def parse_offset(value: Any) -> int:
    """'-00:30:00' / '00:15' / 900 / timedelta / {minutes: 3} -> seconds."""
    if value in (None, ""):
        return 0
    if isinstance(value, dt.timedelta):
        return int(value.total_seconds())
    if isinstance(value, dict):
        # calendar triggers carry the offset as its parts rather than a string
        try:
            return int(
                dt.timedelta(
                    days=float(value.get("days") or 0),
                    hours=float(value.get("hours") or 0),
                    minutes=float(value.get("minutes") or 0),
                    seconds=float(value.get("seconds") or 0),
                ).total_seconds()
            )
        except (TypeError, ValueError):
            return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    sign = -1 if text.startswith("-") else 1
    bits = text.lstrip("+-").split(":")
    try:
        nums = [float(bit or 0) for bit in bits]
    except ValueError:
        return 0
    while len(nums) < 3:
        nums.append(0.0)
    return sign * int(nums[0] * 3600 + nums[1] * 60 + nums[2])


def weekdays_from(obj: Any) -> set[str] | None:
    if not isinstance(obj, dict):
        return None
    days = obj.get("weekday")
    if not days:
        return None
    parsed = {str(day).lower()[:3] for day in as_list(days)}
    parsed &= set(WEEKDAYS)
    return parsed or None


def weekdays_from_conditions(config: Any) -> set[str] | None:
    """Weekday limits declared in the automation's own conditions."""
    if not isinstance(config, dict):
        return None
    result: set[str] | None = None
    for cond in as_list(config.get("conditions") or config.get("condition")):
        if isinstance(cond, dict) and cond.get("condition") == "time":
            days = weekdays_from(cond)
            if days:
                result = days if result is None else (result & days)
    return result


def time_window_from_conditions(config: Any) -> tuple[dt.time, dt.time] | None:
    """The (after, before) window a `condition: time` pins the run to.

    Entity-based bounds (`after: input_datetime.x`) are skipped: they cannot be
    resolved here, and guessing a window would be worse than having none.
    """
    if not isinstance(config, dict):
        return None
    for cond in as_list(config.get("conditions") or config.get("condition")):
        if not isinstance(cond, dict) or cond.get("condition") != "time":
            continue
        after = parse_clock(cond.get("after"))
        before = parse_clock(cond.get("before"))
        if after is not None and before is not None:
            return (after, before)
    return None


def in_window(moment: dt.datetime, window: tuple[dt.time, dt.time] | None) -> bool:
    """Home Assistant's own after/before semantics, wrapping midnight included."""
    if window is None:
        return True
    after, before = window
    clock = moment.time()
    if after < before:
        return after <= clock < before
    return clock >= after or clock < before


def sun_slots(
    base: dt.datetime, offset: int, now: dt.datetime
) -> list[dt.datetime]:
    """Candidate fire times around a published sun event, soonest first.

    ``base`` is the next sunrise/sunset Home Assistant publishes, so it is
    always in the future. That alone is not enough to work out when a sun
    trigger fires:

    * a negative offset pulls the run back, possibly to before ``now`` - the
      real next run is then a day later;
    * a positive offset applied to *tomorrow's* event hides one still due
      today, because today's event has passed but today's event *plus* the
      offset has not.

    So a span of days either side is considered and the earliest one still
    ahead of us wins. The span grows with the offset: a twelve-hour offset can
    push a run clean past the neighbouring day, so looking only one day out
    would find nothing at all and read as "nothing due". A sun event moves by
    about a minute per day, which is far finer than anything here needs.
    """
    shift = dt.timedelta(seconds=offset)
    span = abs(offset) // 86400 + 2
    candidates = [
        base + dt.timedelta(days=day) + shift
        for day in range(-span, span + 1)
    ]
    return sorted(moment for moment in candidates if moment > now)


# "..._Start" / "..._End", or the same with a space. One integration alone
# publishes Window_Start, Next_Window_Start, Next_Motzi_Window_Start,
# Next_Off_Window_Start, Erev_Window_Start and "Next Window Start" - listing
# them was never going to hold, so the shape is matched instead of the name.
# Over-matching costs a warning nobody needed; under-matching loses a run.
_START_SUFFIXES = ("_start", " start")
_END_SUFFIXES = ("_end", " end")

# names whose two halves don't end in start/end at all
_EXTRA_STARTS = frozenset({"starts_at", "start_time"})
_EXTRA_ENDS = frozenset({"ends_at", "end_time"})


def window_from_attributes(
    attributes: Any, parse: Callable[[Any], "dt.datetime | None"]
) -> tuple[list["dt.datetime"], list["dt.datetime"]]:
    """Every "turns on" and "turns off" moment this entity publishes.

    An entity can announce several - a current window and a next one, an
    ordinary one and an early-Shabbos one - and which matters depends on the
    moment being asked about, so all of them come back and the caller picks.

    A start with no matching end is kept too. Half a window still says when the
    entity changes, and the alternative is discarding a moment we were told
    about because a second one was missing.
    """
    if not isinstance(attributes, dict):
        return [], []

    starts: list[dt.datetime] = []
    ends: list[dt.datetime] = []
    for key, value in attributes.items():
        lowered = str(key).lower()
        if lowered.endswith(_START_SUFFIXES) or lowered in _EXTRA_STARTS:
            moment = parse(value)
            if moment is not None:
                starts.append(moment)
        elif lowered.endswith(_END_SUFFIXES) or lowered in _EXTRA_ENDS:
            moment = parse(value)
            if moment is not None:
                ends.append(moment)
    return sorted(starts), sorted(ends)


def soonest_ahead(
    candidates: Iterable["dt.datetime | None"], now: dt.datetime
) -> "dt.datetime | None":
    """The nearest of these moments still ahead of us.

    A published time that has already passed today is not useless - a sensor
    that turns over at sunset turns over again at the next one - so a stale
    value rolls forward a day rather than being discarded. A day's drift on a
    solar time is about a minute, far finer than this needs. Times that are
    already in the future are taken as they are.
    """
    ahead = []
    for moment in candidates:
        if moment is None:
            continue
        ahead.append(moment if moment > now else moment + dt.timedelta(days=1))
    return min(ahead) if ahead else None


def _wants(value: Any, state: str) -> bool:
    """True if a trigger's `to:` or `from:` mentions this state."""
    return any(str(item).lower() == state for item in as_list(value))


def state_edge(
    to_state: Any,
    from_state: Any,
    turns_on: Any,
    turns_off: Any,
    now: dt.datetime,
) -> "dt.datetime | None":
    """When a `state` trigger on an on/off entity next fires.

    `to:` decides it outright. Failing that `from:` does, by implication - a
    trigger leaving "off" is waiting for the same moment as one arriving at
    "on". With neither, any change counts, so whichever edge comes first wins.

    Only ever returns a moment still ahead of `now`: a window that has already
    closed says nothing about the next one.
    """
    if to_state is not None or from_state is not None:
        on = _wants(to_state, "on") or _wants(from_state, "off")
        off = _wants(to_state, "off") or _wants(from_state, "on")
        # a `to:` we do not recognise is not an invitation to guess
        if not on and not off:
            return None
    else:
        on = off = True

    # either side may be a single moment or several, since an entity can
    # publish more than one window
    candidates: list[dt.datetime] = []
    if on:
        candidates += as_list(turns_on)
    if off:
        candidates += as_list(turns_off)
    ahead = sorted(
        moment for moment in candidates if moment is not None and moment > now
    )
    return ahead[0] if ahead else None


def pattern_hit(value: int, spec: Any) -> bool:
    if spec in (None, "*", "**"):
        return True
    text = str(spec).strip()
    if text.startswith("/"):
        try:
            step = int(text[1:])
        except ValueError:
            return False
        return step > 0 and value % step == 0
    try:
        return value == int(text)
    except ValueError:
        return False


def is_enabled(obj: Any) -> bool:
    return not (isinstance(obj, dict) and obj.get("enabled") is False)


# --------------------------------------------------------------------------
# occurrence maths
# --------------------------------------------------------------------------
def _combine(day: dt.date, clock: dt.time, tz: dt.tzinfo) -> dt.datetime:
    """Wall-clock time on a given day, DST-correct via zoneinfo."""
    return dt.datetime.combine(day, clock.replace(tzinfo=None), tzinfo=tz)


def clock_occurrences(
    clock: dt.time, now: dt.datetime, tz: dt.tzinfo, weekdays: set[str] | None
) -> list[dt.datetime]:
    found = []
    for shift in (0, 1, 2):
        day = (now + dt.timedelta(days=shift)).date()
        if weekdays and WEEKDAYS[day.weekday()] not in weekdays:
            continue
        found.append(_combine(day, clock, tz))
    return found


def pattern_occurrences(
    trigger: dict[str, Any],
    now: dt.datetime,
    lookahead: int,
    weekdays: set[str] | None,
    min_interval: int,
    window: tuple[dt.time, dt.time] | None = None,
) -> list[dt.datetime]:
    """time_pattern matches inside the lookahead window.

    Patterns that fire more often than ``min_interval`` minutes are ignored:
    warning about an every-5-minutes automation is just noise.
    """
    hours, minutes, seconds = (
        trigger_field(trigger, "hours"),
        trigger_field(trigger, "minutes"),
        trigger_field(trigger, "seconds"),
    )
    if hours is None and minutes is None and seconds is None:
        return []
    if not pattern_hit(0, seconds):
        return []  # only fires on non-zero seconds, never restart-critical

    # How often does it *actually* act? A `minutes: "*"` pattern fenced by a
    # `condition: time` into a 20-minute window is not the every-minute nuisance
    # this filter exists to suppress, so only count hits the window allows.
    per_day = 0
    base = now.replace(second=0, microsecond=0)
    for step in range(1440):
        moment = base + dt.timedelta(minutes=step)
        if not (pattern_hit(moment.hour, hours) and pattern_hit(moment.minute, minutes)):
            continue
        if not in_window(moment, window):
            continue
        per_day += 1
    if per_day and (1440.0 / per_day) < min_interval:
        return []

    found = []
    for step in range(1, lookahead + 2):
        moment = base + dt.timedelta(minutes=step)
        if not pattern_hit(moment.hour, hours):
            continue
        if not pattern_hit(moment.minute, minutes):
            continue
        if weekdays and WEEKDAYS[moment.weekday()] not in weekdays:
            continue
        found.append(moment)
    return found


def _resolved_slots(
    value: Any,
    offset: int,
    now: dt.datetime,
    tz: dt.tzinfo,
    weekdays: set[str] | None,
    resolve_entity: ResolveEntity,
) -> list[dt.datetime]:
    """Turn one `at:` value into candidate datetimes."""
    clock = parse_clock(value)
    if clock is not None:
        slots = clock_occurrences(clock, now, tz, weekdays)
    elif is_entity_id(value):
        resolved = resolve_entity(value)
        if resolved is None:
            return []
        if isinstance(resolved, dt.datetime):
            slots = [resolved if resolved.tzinfo else resolved.replace(tzinfo=tz)]
        else:
            slots = clock_occurrences(resolved, now, tz, weekdays)
    else:
        return []
    if offset:
        slots = [slot + dt.timedelta(seconds=offset) for slot in slots]
    return slots


def trigger_slots(
    trigger: dict[str, Any],
    now: dt.datetime,
    tz: dt.tzinfo,
    lookahead: int,
    weekdays: set[str] | None,
    resolve_entity: ResolveEntity,
    sun_next: SunNext,
    min_interval: int,
    window: tuple[dt.time, dt.time] | None = None,
    state_next: StateNext | None = None,
    calendar_next: CalendarNext | None = None,
) -> list[dt.datetime]:
    """Candidate fire times for a single trigger."""
    kind = trigger_kind(trigger)

    if kind == "time":
        slots: list[dt.datetime] = []
        for at in as_list(trigger_field(trigger, "at")):
            if isinstance(at, dict):
                slots += _resolved_slots(
                    at.get("entity_id") or at.get("at"),
                    parse_offset(at.get("offset")),
                    now, tz, weekdays, resolve_entity,
                )
            else:
                slots += _resolved_slots(at, 0, now, tz, weekdays, resolve_entity)
        return slots

    if kind == "time_pattern":
        return pattern_occurrences(
            trigger, now, lookahead, weekdays, min_interval, window
        )

    if kind == "sun":
        event = str(trigger_field(trigger, "event") or "").lower()
        if event not in SUN_EVENTS:
            # a sun trigger we cannot read is not a sun trigger we can ignore
            _LOGGER.warning(
                "Restart Guard could not find the sun event in %s, so this "
                "trigger will not be reported", trigger,
            )
            return []
        moment = sun_next(event, parse_offset(trigger_field(trigger, "offset")))
        return [moment] if moment else []

    if kind in CALENDAR_KINDS:
        # A calendar entity publishes the start and end of the event it is on
        # or waiting for, so a run driven by one is as knowable as a clock.
        if calendar_next is None:
            return []
        if kind == "calendar":
            which = str(trigger_field(trigger, "event") or "start").lower()
        else:
            which = "end" if kind.endswith("ended") else "start"
        offset = parse_offset(trigger_field(trigger, "offset"))
        # the new-style trigger says which side of the event it means here,
        # rather than by the sign of the offset
        if str(trigger_field(trigger, "offset_type") or "").lower() == "before":
            offset = -abs(offset)
        slots = []
        for entity_id in as_list(trigger_field(trigger, "entity_id")):
            if not is_entity_id(entity_id):
                continue
            moment = calendar_next(str(entity_id), "end" if which == "end" else "start")
            if moment is not None:
                slots.append(moment + dt.timedelta(seconds=offset))
        return slots

    if kind == "state":
        # Most state triggers are unknowable - a door opens when it opens. But
        # some entities announce when they next change, and those runs are as
        # predictable as any clock. Anything we cannot resolve returns nothing,
        # exactly as before, so this only ever adds warnings.
        if state_next is None:
            return []
        # `for:` means the run happens some time after the change, and Home
        # Assistant drops that pending countdown on restart. Predicting the
        # change but not the delay would report the wrong minute, so leave it.
        if trigger_field(trigger, "for") is not None:
            return []
        # an attribute trigger watches something other than the state itself
        if trigger_field(trigger, "attribute") is not None:
            return []
        slots = []
        for entity_id in as_list(trigger_field(trigger, "entity_id")):
            if not is_entity_id(entity_id):
                continue
            moment = state_next(
                str(entity_id),
                trigger_field(trigger, "to"),
                trigger_field(trigger, "from"),
            )
            if moment is not None:
                slots.append(moment)
        return slots

    return []


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def compute(
    automations: Iterable[AutomationInfo],
    now: dt.datetime,
    tz: dt.tzinfo,
    lookahead: int,
    resolve_entity: ResolveEntity,
    sun_next: SunNext,
    min_interval: int = 60,
    state_next: StateNext | None = None,
    calendar_next: CalendarNext | None = None,
) -> list[dict[str, Any]]:
    """Upcoming runs inside the lookahead window, soonest first."""
    horizon = now + dt.timedelta(minutes=lookahead)
    items: list[dict[str, Any]] = []
    # where each (automation, moment) landed, so a second trigger firing at the
    # same moment adds its id to that entry instead of being thrown away
    at_slot: dict[tuple[str, str], int] = {}

    for auto in automations:
        for index, trigger in enumerate(auto.triggers):
            if not is_enabled(trigger):
                continue
            days = weekdays_from(trigger) or auto.weekdays
            try:
                slots = trigger_slots(
                    trigger, now, tz, lookahead, days,
                    resolve_entity, sun_next, min_interval, auto.window,
                    state_next, calendar_next,
                )
            except Exception:  # noqa: BLE001 - one bad trigger must not kill the sensor
                # Never silently: a swallowed error here reads exactly like
                # "nothing is due", which is the one wrong answer that costs a
                # real automation run.
                _LOGGER.warning(
                    "Restart Guard could not work out when %s fires from %s",
                    auto.entity_id, trigger, exc_info=True,
                )
                continue
            for slot in slots:
                if not (now < slot <= horizon):
                    continue
                # a run the time condition rules out never happens
                if not in_window(slot, auto.window):
                    continue
                key = (auto.entity_id, slot.isoformat())
                trigger_id = trigger.get("id")
                seen_at = at_slot.get(key)
                if seen_at is not None:
                    # Two triggers on the same automation at the same moment is
                    # normal (a morning one and a festival one both at 09:15).
                    # One entry is right for the list, but the condition check
                    # has to know about both, or it judges the run by one
                    # branch and rules it out while the other would have fired.
                    ids = items[seen_at]["trigger_ids"]
                    if trigger_id is not None and trigger_id not in ids:
                        ids.append(trigger_id)
                    continue
                at_slot[key] = len(items)
                items.append({
                    "entity_id": auto.entity_id,
                    "alias": auto.name,
                    "at": slot.isoformat(),
                    "at_ts": int(slot.timestamp()),
                    "when": slot.strftime("%H:%M"),
                    "minutes": round((slot - now).total_seconds() / 60.0, 1),
                    # so condition checks can tell which choose branch applies
                    "trigger_id": trigger_id,
                    "trigger_ids": [] if trigger_id is None else [trigger_id],
                    "trigger_index": index,
                })

    items.sort(key=lambda item: item["minutes"])
    return items
