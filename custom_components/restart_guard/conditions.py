"""Decide whether an automation would actually *do* anything when it fires.

An automation can be triggered and still be a no-op: its `conditions:` block may
not pass, or every branch of a `choose` may be unreachable for that particular
trigger. Restarting through one of those is harmless, so there is no point
warning about it.

Bias: when we cannot work something out confidently we say it **will** run. A
wrong "safe to restart" loses a real automation run, which is much worse than a
warning you did not need.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition as condition_helper
from homeassistant.util import dt as dt_util

from .calc import as_list, date_certain_verdict, is_enabled

_LOGGER = logging.getLogger(__name__)

# condition keys that depend on the clock, so evaluating them "now" for a
# trigger that fires later can give the wrong answer
_ABSOLUTE_TIME_KEYS = ("after", "before")


@dataclass
class Verdict:
    """Whether a projected run would do anything, and why not."""

    will_run: bool
    reason: str | None = None
    # every decision point on the way to the answer, so a run that should have
    # been ruled out and wasn't can be read off the sensor instead of guessed at
    trace: list[str] = field(default_factory=list)


WILL_RUN = Verdict(True)


# ---------------------------------------------------------------------------
# Talking to Home Assistant's condition helper
# ---------------------------------------------------------------------------
# Conditions are built one at a time with the singular helpers, which are what
# Home Assistant documents for custom integrations since 2026.5, and ANDed here.
# The names moved over time, so they are resolved at run time rather than
# imported: older cores have `async_from_config`, the documented name is
# `async_condition_from_config`.
def _helper(*names: str) -> Any:
    for name in names:
        found = getattr(condition_helper, name, None)
        if found is not None:
            return found
    return None


def _condition_schema() -> Any:
    """Home Assistant's base condition schema, if this core has one."""
    try:
        from homeassistant.helpers import config_validation as cv
    except Exception:  # noqa: BLE001 - not available outside Home Assistant
        return None
    return getattr(cv, "CONDITION_SCHEMA", None)


async def _compile_one(hass: HomeAssistant, config: dict[str, Any]) -> Any:
    """One validated, ready-to-check condition.

    The configs come from `raw_config`, which is the YAML exactly as written:
    a template is still a plain string there, and an `entity_id` a bare string
    rather than a list. Two steps turn that into something checkable, and both
    are needed.

    The base schema comes first. It is what turns `value_template` text into a
    compiled Template and `entity_id` into a list. `async_validate_condition_
    config` does *not* do this - it assumes the schema has already run, because
    for the automation's own `conditions:` block it has, inside the automation
    integration. Skipping it left every branch template as raw text, the
    checker raised on it, and "could not tell" read as "it runs": on one
    instance a Lag BaOmer-only branch was reported as about to run every night.

    Then the condition's own validation, for anything platform-specific.
    """
    schema = _condition_schema()
    if schema is not None:
        config = schema(config)
    validate = _helper("async_validate_condition_config")
    build = _helper("async_condition_from_config", "async_from_config")
    if build is None:
        raise RuntimeError("no condition factory in this Home Assistant")
    validated = await validate(hass, config) if validate is not None else config
    return await build(hass, validated)


def _one_result(
    checker: Any, variables: dict[str, Any], notes: list[str] | None = None
) -> bool | None:
    """Run a single condition. None means "could not tell".

    Anything that goes wrong here used to vanish without a word, and "could not
    tell" is read as "it runs" - so a failure looked exactly like a condition
    that passed. Now it is written into the trace, and warned about.
    """
    kind = type(checker).__name__
    try:
        run = getattr(checker, "async_check", None)
        result = run(variables=variables) if run is not None else checker(variables)
    except Exception as err:  # noqa: BLE001 - missing trigger vars, template error, ...
        if notes is not None:
            notes.append(f"{kind} raised {type(err).__name__}: {err}")
        _LOGGER.warning("Restart Guard: %s raised while checking", kind, exc_info=True)
        return None
    # A coroutine would be a future core making `async_check` truly async.
    # Treating it as truthy would pass every condition, so refuse to guess.
    if hasattr(result, "__await__"):
        if notes is not None:
            notes.append(f"{kind} returned a coroutine, not an answer")
        return None
    answer = bool(result) if result is not None else None
    if notes is not None:
        notes.append(f"{kind} -> {result!r}")
    return answer


def _configs_of(checker: Any) -> list[dict[str, Any]] | None:
    """The condition configs behind a compiled automation condition."""
    configs = getattr(checker, "config", None)
    if isinstance(configs, list):
        return configs
    return None


def _walk(node: Any):
    """Every mapping inside a nested config structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value)


def uses_absolute_time(configs: Any) -> bool:
    """True if any condition compares against a wall-clock time.

    `weekday` is fine - we already project the right date - but `after` /
    `before` would be evaluated at the wrong moment.
    """
    for node in _walk(configs):
        if node.get("condition") == "time" and any(
            key in node for key in _ABSOLUTE_TIME_KEYS
        ):
            return True
    return False


def trigger_ids_of(config: Any) -> set[str]:
    """Trigger ids referenced by `condition: trigger` anywhere in a config."""
    found: set[str] = set()
    for node in _walk(config):
        if node.get("condition") == "trigger":
            for value in as_list(node.get("id")):
                found.add(str(value))
    return found


def _strip_trigger_conditions(configs: Any) -> list[dict[str, Any]]:
    """Everything except the `condition: trigger` entries."""
    return [
        node
        for node in as_list(configs)
        if isinstance(node, dict) and node.get("condition") != "trigger"
    ]


def _branch_options(step: Any) -> list[dict[str, Any]] | None:
    """The conditional branches of one *gate* step, or None if it always acts.

    A gate is a step that can end up doing nothing at all: a `choose` with no
    `default`, or an `if` with no `else`. Every other kind of step (a bare
    service call, a delay, a `repeat`, a `choose` that has a default, ...) does
    something whenever it is reached, so it can never be ruled out.
    """
    if not isinstance(step, dict):
        return None

    if "choose" in step:
        if as_list(step.get("default")):
            return None  # a default means something always happens
        return [
            option
            for option in as_list(step.get("choose"))
            if isinstance(option, dict) and is_enabled(option)
        ]

    if "if" in step and "then" in step:
        if as_list(step.get("else")):
            return None  # an else means something always happens
        return [{"conditions": step.get("if")}]

    return None


def _gate_blocks(actions: Any) -> list[list[dict[str, Any]]] | None:
    """Branch lists for the top-level steps, or None if any step always acts.

    Automations that route several triggers usually stack one gate per trigger
    id - six `choose:` steps in a row rather than a single one with six options
    - and each of those steps is independently skippable. So the whole sequence
    is a no-op exactly when *every* step in it is, which is what returning one
    branch list per step lets the caller work out.

    Anything that is not a gate makes the automation act unconditionally, and
    then there is nothing to reason about: None, and the caller assumes it runs.
    """
    steps = [step for step in as_list(actions) if isinstance(step, dict)]
    if not steps:
        return None

    blocks: list[list[dict[str, Any]]] = []
    for step in steps:
        if not is_enabled(step):
            continue  # a disabled step never runs, so it gates nothing
        options = _branch_options(step)
        if options is None:
            return None
        blocks.append(options)
    return blocks or None


class ConditionEvaluator:
    """Evaluates automation and choose-branch conditions, with a compile cache."""

    def __init__(self, hass: HomeAssistant, changes_before: Any = None) -> None:
        self._hass = hass
        self._cache: dict[str, Any] = {}
        # (entity_id, moment) -> "does this entity change in between?"
        self._changes_before = changes_before
        # what was seen while judging the current run; reset per verdict
        self._notes: list[str] = []

    def _note(self, text: str) -> None:
        if len(self._notes) < 40:
            self._notes.append(text)

    def _runs(self, why: str) -> Verdict:
        """'It will run', with the reason recorded rather than implied."""
        self._note(f"=> runs: {why}")
        return Verdict(True, None, list(self._notes))

    def _not(self, why: str) -> Verdict:
        self._note(f"=> ruled out: {why}")
        return Verdict(False, why, list(self._notes))

    async def _checker(self, configs: list[dict[str, Any]]) -> Any | None:
        """Compile (and cache) a condition checker, or None if it won't compile.

        The configs come from `raw_config`, which is *not* validated: an
        `entity_id` is still a bare string there, and handing that straight to
        the compiler makes Home Assistant iterate it one character at a time.
        So validate first, exactly as the automation integration does.
        """
        try:
            key = json.dumps(configs, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        if key in self._cache:
            cached = self._cache[key]
            self._note(
                "compiled (cached): "
                + (", ".join(type(one).__name__ for one in cached) if cached else "FAILED")
            )
            return cached
        try:
            checker = [
                await _compile_one(self._hass, config) for config in configs
            ]
        except Exception as err:  # noqa: BLE001 - unsupported shorthand, bad template, ...
            self._note(f"compile failed: {type(err).__name__}: {err}")
            _LOGGER.warning(
                "Restart Guard could not compile the conditions %s, so it will "
                "assume the run happens. This is worth reporting.",
                configs, exc_info=True,
            )
            checker = None
        else:
            self._note(
                "compiled: " + ", ".join(type(one).__name__ for one in checker)
            )
        self._cache[key] = checker
        return checker

    def entities_known(self, configs: Any) -> bool:
        """Every entity referenced by these conditions exists.

        A condition naming a missing entity evaluates to False, which would look
        exactly like "this won't run" and wrongly silence a warning. So treat an
        unknown entity as "cannot tell".
        """
        states = getattr(self._hass, "states", None)
        if states is None:
            return False  # cannot check, so cannot trust a negative result
        for node in _walk(configs):
            for entity_id in as_list(node.get("entity_id")):
                if (
                    isinstance(entity_id, str)
                    and "." in entity_id
                    and states.get(entity_id) is None
                ):
                    return False
        return True

    @staticmethod
    def _check(
        checker: Any,
        variables: dict[str, Any] | None = None,
        notes: list[str] | None = None,
    ) -> bool | None:
        """Run a checker. None means "could not tell".

        Takes either our own list of conditions, which are ANDed here, or a
        single checker - the automation's own compiled `conditions:` block,
        which Home Assistant built and which ANDs them itself.

        Three-valued, and the middle value is load-bearing. One condition that
        definitely fails makes the whole block fail, however many others could
        not be read; but if nothing fails and something was unreadable, the
        honest answer is "do not know", which the caller treats as "it runs".
        """
        values = variables or {}
        if not isinstance(checker, list):
            return _one_result(checker, values, notes)

        unknown = False
        for one in checker:
            result = _one_result(one, values, notes)
            if result is False:
                return False  # ANDed: one definite no settles it
            if result is None:
                unknown = True
        return None if unknown else True

    def _volatile(self, node: Any, until: Any) -> bool:
        """Does any entity this condition names change before the run?

        This is the difference between reading live state as a proxy and
        reading it as the answer. `binary_sensor.yidcal_no_melucha` is off at
        19:45 and on at 20:00; a condition requiring it to be on reads False
        now and True when the automation actually fires. Ruling the run out on
        the current value produces a green "safe to restart" a quarter of an
        hour before something that will definitely happen - which is the one
        answer this integration exists to never give.

        Only entities whose next change is *knowable* count here, so this costs
        nothing for the ordinary case of a condition on a helper or a switch.
        """
        if until is None or self._changes_before is None:
            return False
        for inner in _walk(node):
            for entity_id in as_list(inner.get("entity_id")):
                if not isinstance(entity_id, str) or "." not in entity_id:
                    continue
                try:
                    if self._changes_before(entity_id, until):
                        return True
                except Exception:  # noqa: BLE001 - unknown means unusable
                    return True
        return False

    def _judgeable(self, configs: Any, until: Any = None) -> list[dict[str, Any]]:
        """The conditions we can fairly evaluate now; the rest are dropped.

        Conditions are ANDed, so dropping some can only make the block MORE
        likely to pass. If what is left still evaluates False, the whole block
        is False whatever the dropped ones would have said - which is what lets
        a branch be ruled out even when it also compares against the clock.

        Dropped: anything measured against a wall-clock time, because "now" is
        the wrong moment to read it; anything naming an entity that does not
        exist, because a missing entity reads False without meaning it; and
        anything naming an entity that changes before the run, because then now
        is the wrong moment to read that too.
        """
        return [
            node
            for node in as_list(configs)
            if isinstance(node, dict)
            and not uses_absolute_time(node)
            and self.entities_known(node)
            and not self._volatile(node, until)
        ]

    @staticmethod
    def _item_trigger_ids(item: dict[str, Any]) -> list[str]:
        """Every trigger id that fires this automation at this moment.

        Two triggers can land on the same time, and the run has to be judged
        against all of them: ruling it out on one branch while another would
        have fired is exactly the wrong answer.
        """
        listed = item.get("trigger_ids")
        if isinstance(listed, list) and listed:
            return [str(value) for value in listed if value is not None]
        single = item.get("trigger_id")
        return [str(single)] if single is not None else []

    @staticmethod
    def _trigger_variables(trigger_id: Any) -> dict[str, Any]:
        """What Home Assistant puts in `trigger` when the automation fires.

        Without this, a `conditions:` block containing `condition: trigger` can
        never pass - `trigger.id` is missing, so it reads as False and the whole
        automation looks like a no-op. Automations that route several triggers
        commonly gate on the id at the top level, and every one of them was
        being silently skipped.
        """
        return {"trigger": {"id": trigger_id, "platform": "time"}}

    async def async_verdict(
        self, entity: Any, item: dict[str, Any], near: bool
    ) -> Verdict:
        """Would this projected run actually do something?

        `near` is whether the run is close enough that reading the house right
        now is a fair guess at how it will be then. Far-off runs are assumed to
        happen rather than judged on stale readings.
        """
        self._notes = []
        raw = getattr(entity, "raw_config", None)
        raw = raw if isinstance(raw, dict) else {}
        self._note(
            f"trigger ids {self._item_trigger_ids(item)}; raw_config keys "
            f"{sorted(raw)}; near={near}"
        )

        # ---- 1. structural: can this trigger reach any branch at all? -------
        verdict = self._structural_verdict(raw, item)
        if verdict is not None:
            return self._not(verdict.reason or "structural")
        self._note("structural: some branch is reachable")

        # ---- 1b. what the calendar alone settles --------------------------
        # Weekday and trigger-id conditions need nothing from the house, so
        # they hold for a run tomorrow as firmly as one today. Checked before
        # the bail below, because that is exactly the case they answer: an
        # automation gated on Sunday, firing at 00:01 on Monday.
        moment = dt_util.parse_datetime(str(item.get("at") or ""))
        if moment is not None:
            settled = date_certain_verdict(
                raw, set(self._item_trigger_ids(item)), dt_util.as_local(moment)
            )
            if settled is False:
                return self._not("the day it falls on rules it out")
            self._note(f"date-certain: {settled}")

        # Anything below evaluates live state, which is only a fair proxy for
        # what will be true at the trigger time if the trigger time is close.
        if not near:
            return self._runs("too far ahead to judge on live state")

        # ---- 2. the automation's own conditions block -----------------------
        until = dt_util.parse_datetime(str(item.get("at") or ""))
        compiled = getattr(entity, "_condition", None)
        ours = self._item_trigger_ids(item)
        self._note(f"top-level conditions: {type(compiled).__name__}")
        if compiled is not None:
            configs = _configs_of(compiled)
            judgeable = self._judgeable(configs, until) if configs else []
            if judgeable:
                # reuse the entity's own checker when nothing had to be dropped
                checker = (
                    compiled
                    if len(judgeable) == len(as_list(configs))
                    else await self._checker(judgeable)
                )
                # only rule the run out if it fails for every trigger that
                # could have fired it at this moment
                if checker is not None and all(
                    self._check(checker, self._trigger_variables(tid), self._notes)
                    is False
                    for tid in (ours or [None])
                ):
                    return self._not("automation conditions are not met")

        # ---- 3. the conditions on whichever choose branches could match -----
        return await self._async_branch_verdict(raw, item, until)

    def _structural_verdict(
        self, raw: dict[str, Any], item: dict[str, Any]
    ) -> Verdict | None:
        """Rule the run out purely from the shape of the config, no evaluation.

        If every top-level step is a gate, every branch across them is gated on
        `condition: trigger`, and none of those branches names this trigger,
        then firing it does nothing at all.
        """
        ours = set(self._item_trigger_ids(item))
        if not ours:
            return None

        blocks = _gate_blocks(raw.get("actions") or raw.get("action"))
        if blocks is None:
            return None

        for options in blocks:
            for option in options:
                ids = trigger_ids_of(
                    option.get("conditions") or option.get("condition")
                )
                if not ids:
                    return None  # an ungated branch could match anything
                if ids & ours:
                    return None  # one of this moment's triggers reaches a branch

        return Verdict(False, "no choose branch runs for this trigger")

    async def _async_branch_verdict(
        self, raw: dict[str, Any], item: dict[str, Any], until: Any = None
    ) -> Verdict:
        """False only if every branch this trigger could reach fails its checks.

        Works whether or not the trigger has an `id`. A trigger without one can
        never satisfy `condition: trigger`, so branches gated on an id are
        simply out of reach for it.
        """
        ours = self._item_trigger_ids(item)
        actions = raw.get("actions") or raw.get("action")
        blocks = _gate_blocks(actions)
        if blocks is None:
            steps = [s for s in as_list(actions) if isinstance(s, dict)]
            return self._runs(
                "a top-level step always acts: "
                + ", ".join("+".join(sorted(s)) for s in steps[:6])
            )
        self._note(f"gates: {[len(o) for o in blocks]} branches")

        reachable = 0
        for options in blocks:
            for option in options:
                configs = option.get("conditions") or option.get("condition")
                ids = trigger_ids_of(configs)
                matched = ids & set(ours)
                if ids and not matched:
                    continue  # this branch belongs to a different trigger
                reachable += 1
                label = option.get("alias") or f"branch {reachable}"

                others = _strip_trigger_conditions(configs)
                judgeable = self._judgeable(others, until)
                self._note(
                    f"{label}: ids {sorted(ids)}, {len(as_list(others))} other "
                    f"condition(s), {len(judgeable)} judgeable"
                )
                if not judgeable:
                    return self._runs(f"{label}: nothing in it could be judged now")
                checker = await self._checker(judgeable)
                if checker is None:
                    return self._runs(f"{label}: its conditions would not compile")
                # an ungated branch is reached by whichever trigger fired
                candidates = sorted(matched) or (ours or [None])
                results = [
                    self._check(checker, self._trigger_variables(tid), self._notes)
                    for tid in candidates
                ]
                if any(result is not False for result in results):
                    return self._runs(f"{label}: checked {results}")

        if reachable == 0:
            return self._not("no choose branch runs for this trigger")
        return self._not("choose branch conditions are not met")
