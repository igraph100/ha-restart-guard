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
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition as condition_helper

from .calc import as_list, is_enabled

_LOGGER = logging.getLogger(__name__)

# condition keys that depend on the clock, so evaluating them "now" for a
# trigger that fires later can give the wrong answer
_ABSOLUTE_TIME_KEYS = ("after", "before")


@dataclass
class Verdict:
    """Whether a projected run would do anything, and why not."""

    will_run: bool
    reason: str | None = None


WILL_RUN = Verdict(True)


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

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._cache: dict[str, Any] = {}

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
            return self._cache[key]
        try:
            validated = await condition_helper.async_validate_conditions_config(
                self._hass, configs
            )
            checker = await condition_helper.async_conditions_from_config(
                self._hass, validated, _LOGGER, "restart_guard"
            )
        except Exception:  # noqa: BLE001 - unsupported shorthand, bad template, ...
            _LOGGER.debug("Could not compile conditions %s", configs, exc_info=True)
            checker = None
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
    def _check(checker: Any, variables: dict[str, Any] | None = None) -> bool | None:
        """Run a compiled checker. None means "could not tell"."""
        try:
            result = checker.async_check(variables=variables or {})
        except Exception:  # noqa: BLE001 - missing trigger vars, template error, ...
            return None
        return bool(result) if result is not None else None

    def _judgeable(self, configs: Any) -> list[dict[str, Any]]:
        """The conditions we can fairly evaluate now; the rest are dropped.

        Conditions are ANDed, so dropping some can only make the block MORE
        likely to pass. If what is left still evaluates False, the whole block
        is False whatever the dropped ones would have said - which is what lets
        a branch be ruled out even when it also compares against the clock.

        Dropped: anything measured against a wall-clock time, because "now" is
        the wrong moment to read it, and anything naming an entity that does
        not exist, because a missing entity reads False without meaning it.
        """
        return [
            node
            for node in as_list(configs)
            if isinstance(node, dict)
            and not uses_absolute_time(node)
            and self.entities_known(node)
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
        self, entity: Any, item: dict[str, Any], same_day: bool
    ) -> Verdict:
        """Would this projected run actually do something?"""
        raw = getattr(entity, "raw_config", None)
        raw = raw if isinstance(raw, dict) else {}

        # ---- 1. structural: can this trigger reach any branch at all? -------
        verdict = self._structural_verdict(raw, item)
        if verdict is not None:
            return verdict

        # Anything below evaluates live state, which is only a fair proxy for
        # what will be true at the trigger time if that is still today.
        if not same_day:
            return WILL_RUN

        # ---- 2. the automation's own conditions block -----------------------
        compiled = getattr(entity, "_condition", None)
        ours = self._item_trigger_ids(item)
        if compiled is not None:
            configs = _configs_of(compiled)
            judgeable = self._judgeable(configs) if configs else []
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
                    self._check(checker, self._trigger_variables(tid)) is False
                    for tid in (ours or [None])
                ):
                    return Verdict(False, "automation conditions are not met")

        # ---- 3. the conditions on whichever choose branches could match -----
        return await self._async_branch_verdict(raw, item)

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
        self, raw: dict[str, Any], item: dict[str, Any]
    ) -> Verdict:
        """False only if every branch this trigger could reach fails its checks.

        Works whether or not the trigger has an `id`. A trigger without one can
        never satisfy `condition: trigger`, so branches gated on an id are
        simply out of reach for it.
        """
        ours = self._item_trigger_ids(item)
        blocks = _gate_blocks(raw.get("actions") or raw.get("action"))
        if blocks is None:
            return WILL_RUN

        reachable = 0
        for options in blocks:
            for option in options:
                configs = option.get("conditions") or option.get("condition")
                ids = trigger_ids_of(configs)
                matched = ids & set(ours)
                if ids and not matched:
                    continue  # this branch belongs to a different trigger
                reachable += 1

                others = _strip_trigger_conditions(configs)
                judgeable = self._judgeable(others)
                if not judgeable:
                    return WILL_RUN  # nothing here we could rule it out on
                checker = await self._checker(judgeable)
                if checker is None:
                    return WILL_RUN  # would not compile, so assume it runs
                # an ungated branch is reached by whichever trigger fired
                candidates = sorted(matched) or (ours or [None])
                if any(
                    self._check(checker, self._trigger_variables(tid)) is not False
                    for tid in candidates
                ):
                    return WILL_RUN  # passes for some trigger, or could not tell

        if reachable == 0:
            return Verdict(False, "no choose branch runs for this trigger")
        return Verdict(False, "choose branch conditions are not met")
