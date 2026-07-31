"""Upcoming runs from the Scheduler component.

https://github.com/nielsfaber/scheduler-component creates one `switch.schedule_*`
entity per schedule, and helpfully publishes a `next_trigger` attribute. That is
already an absolute local timestamp with weekdays and timeslots resolved, so
there is no clock arithmetic to redo here.

The trap: a **disabled** schedule keeps publishing `next_trigger` anyway, often
pointing at a time in the past. Only `state == "on"` schedules can actually fire.

Deliberately free of Home Assistant imports so it can be unit tested on its own.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Iterable

STATE_ON = "on"
SOURCE = "schedule"

# Scheduler's own condition language (see its const.py), not HA conditions
MATCH_IS = "is"
MATCH_NOT = "not"
MATCH_BELOW = "below"
MATCH_ABOVE = "above"
CONDITION_AND = "and"
CONDITION_OR = "or"

_MISSING = object()

# what the Scheduler component names a schedule you never gave a name to
_GENERIC_NAME = re.compile(r"^Scheduler Schedule #?\w*$", re.IGNORECASE)

ParseDateTime = Callable[[str], "dt.datetime | None"]
ResolveName = Callable[[str], str]


def is_schedule(state: Any) -> bool:
    """A Scheduler entity, identified by what it publishes rather than its id."""
    attrs = getattr(state, "attributes", None) or {}
    return "next_trigger" in attrs and "timeslots" in attrs


def label(state: Any, resolve_name: ResolveName | None = None) -> str:
    """A name worth showing.

    Unnamed schedules are called "Scheduler Schedule #002d2d", which tells you
    nothing in a warning, so fall back to what the schedule actually controls.
    """
    attrs = getattr(state, "attributes", None) or {}
    name = str(attrs.get("friendly_name") or "").strip()
    if name and not _GENERIC_NAME.match(name):
        return name

    targets = [entity for entity in (attrs.get("entities") or []) if entity]
    if resolve_name is not None:
        targets = [resolve_name(entity) for entity in targets]
    shown = ", ".join(str(target) for target in targets[:2])
    if len(targets) > 2:
        shown += f" +{len(targets) - 2}"

    tags = [tag for tag in (attrs.get("tags") or []) if tag]
    if shown and tags:
        return f"Schedule: {shown} ({tags[0]})"
    if shown:
        return f"Schedule: {shown}"
    if tags:
        return f"Schedule ({tags[0]})"
    return name or str(getattr(state, "entity_id", "schedule"))


def _observed(state: Any, attribute: Any) -> Any:
    """The value a condition is comparing against."""
    if not attribute or attribute == "state":
        return getattr(state, "state", _MISSING)
    attrs = getattr(state, "attributes", None) or {}
    return attrs.get(attribute, _MISSING)


def _compare(actual: Any, expected: Any, match_type: Any) -> bool | None:
    """Apply one Scheduler match. None means "cannot tell"."""
    if match_type == MATCH_IS:
        return str(actual) == str(expected)
    if match_type == MATCH_NOT:
        return str(actual) != str(expected)
    if match_type in (MATCH_ABOVE, MATCH_BELOW):
        try:
            left = float(actual)
            right = float(expected)
        except (TypeError, ValueError):
            return None
        return left > right if match_type == MATCH_ABOVE else left < right
    return None  # a match type we don't know about


def evaluate_slot(slot: Any, get_state: Callable[[str], Any]) -> bool | None:
    """Would this timeslot's conditions let it act?

    True / False / None, where None means "cannot tell" and the caller should
    assume it will run.
    """
    if not isinstance(slot, dict):
        return None
    conditions = slot.get("conditions") or []
    if not conditions:
        return True  # no conditions means it always acts

    results: list[bool] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            return None
        entity_id = condition.get("entity_id")
        if not entity_id:
            return None
        state = get_state(entity_id)
        if state is None:
            return None  # unknown entity, so no evidence either way
        actual = _observed(state, condition.get("attribute"))
        if actual is _MISSING:
            return None
        outcome = _compare(actual, condition.get("value"), condition.get("match_type"))
        if outcome is None:
            return None
        results.append(outcome)

    if not results:
        return True
    if slot.get("condition_type") == CONDITION_OR:
        return any(results)
    return all(results)


def next_slot_of(state: Any, schedule: Any) -> Any:
    """The timeslot that the next trigger will use, or None if unclear."""
    if not isinstance(schedule, dict):
        return None
    slots = schedule.get("timeslots")
    if not isinstance(slots, list) or not slots:
        return None
    index = (getattr(state, "attributes", None) or {}).get("next_slot")
    if not isinstance(index, int) or not 0 <= index < len(slots):
        return None
    return slots[index]


def collect(
    states: Iterable[Any],
    now: dt.datetime,
    lookahead: int,
    parse: ParseDateTime,
    resolve_name: ResolveName | None = None,
    get_schedule: Callable[[str], Any] | None = None,
    get_state: Callable[[str], Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Scheduler runs due inside the lookahead window.

    Returns (items, skipped, scanned). A schedule is skipped when the timeslot
    it is about to use has conditions that cannot pass right now, so it would
    fire and do nothing.
    """
    horizon = now + dt.timedelta(minutes=lookahead)
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    scanned = 0

    for state in states:
        if not is_schedule(state):
            continue
        scanned += 1

        # a disabled schedule still advertises a next_trigger, but cannot fire
        if getattr(state, "state", None) != STATE_ON:
            continue

        attrs = state.attributes or {}
        raw = attrs.get("next_trigger")
        if not raw:
            continue
        try:
            moment = parse(str(raw))
        except Exception:  # noqa: BLE001 - a malformed value must not break the sensor
            continue
        if moment is None:
            continue
        if moment.tzinfo is None and now.tzinfo is not None:
            moment = moment.replace(tzinfo=now.tzinfo)

        if not (now < moment <= horizon):
            continue

        item = {
            "entity_id": getattr(state, "entity_id", ""),
            "alias": label(state, resolve_name),
            "at": moment.isoformat(),
            "at_ts": int(moment.timestamp()),
            "when": moment.strftime("%H:%M"),
            "minutes": round((moment - now).total_seconds() / 60.0, 1),
            "source": SOURCE,
            "tags": list(attrs.get("tags") or []),
        }

        # would it actually act? only skip on a definite "no"
        if get_schedule is None or get_state is None:
            item["condition_check"] = "not checked"
        else:
            try:
                schedule = get_schedule(item["entity_id"])
                if schedule is None:
                    item["condition_check"] = "no schedule definition"
                else:
                    slot = next_slot_of(state, schedule)
                    if slot is None:
                        item["condition_check"] = "next slot unknown"
                    else:
                        verdict = evaluate_slot(slot, get_state)
                        if verdict is False:
                            skipped.append(
                                {**item, "reason": "schedule conditions are not met"}
                            )
                            continue
                        item["condition_check"] = (
                            "conditions pass" if verdict else "cannot tell"
                        )
            except Exception as err:  # noqa: BLE001 - never let this hide a warning
                item["condition_check"] = f"error: {err}"

        items.append(item)

    items.sort(key=lambda item: item["minutes"])
    skipped.sort(key=lambda item: item["minutes"])
    return items, skipped, scanned
