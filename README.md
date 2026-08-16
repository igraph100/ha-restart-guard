# Restart Guard

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=igraph100&repository=ha-restart-guard&category=integration)

[![HACS Custom][hacs-badge]][hacs-link]
[![Downloads][downloads-badge]][releases-link]
[![Latest release][release-badge]][releases-link]
[![Validate][validate-badge]][validate-link]
[![License][license-badge]](LICENSE)

**Home Assistant warns you before you restart it, if a timed automation is about to
run — or is already running.**

You restart at 06:59, and the 07:00 automation never fires. Home Assistant does not
replay missed time triggers, and it does not resume an automation that was part-way
through a `delay`. Restart Guard puts that information in front of you *in Home
Assistant's own restart dialog*, and makes the Restart row need a second, deliberate
tap when it matters.

**[Install](#installation)** · [Screenshots](#what-it-looks-like) · [What else is guarded](#what-else-is-guarded) · [When it stays quiet](#when-it-stays-quiet) · [Scheduler](#scheduler-support) · [Options](#options) · [The entity](#the-entity) · [⚠️ What it cannot catch](#what-it-cannot-catch)

---

## What it looks like

Open the 3-dot menu → **Restart Home Assistant**. The dialog is core's own; the banner
at the top is this integration.

| ⛔ Something is due | ✅ Nothing is due |
|:---|:---|
| ![Restart dialog warning that an automation runs in 3 minutes][img-warning] | ![Restart dialog reporting nothing scheduled][img-safe] |
| **It names the run.** Which automation, the clock time, and how long you have — not just a count. | **Green means clear.** Nothing in the next 60 minutes, and nothing part-way through a `delay`. |
| ![Restart dialog armed for a second tap][img-armed] | ![Restart dialog reporting the next run is 58 minutes away][img-soon] |
| **The first tap is swallowed.** The row arms itself and asks for a second, deliberate tap. **Quick reload** is never blocked — it interrupts nothing. | **Still green, but it tells you what's coming.** Outside the warning window you get the next run anyway, so "safe" is never a dead end. |

> [!WARNING]
> **A green "Safe to restart" does not mean nothing will happen.**
> This only predicts things driven by a **clock**. A door sensor, a motion sensor or a
> power meter could change one second from now and fire an automation, and nothing can
> know that in advance. Read [What it cannot catch](#what-it-cannot-catch) before you
> rely on it.

Time format follows your Home Assistant setup, 12 or 24 hour.

---

## Installation

### HACS (recommended)

1. In HACS, open the ⋮ menu → **Custom repositories**
2. Add `https://github.com/igraph100/ha-restart-guard` with category **Integration**
3. Install **Restart Guard**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Restart Guard**
5. Hard-refresh the browser (Ctrl/Cmd+Shift+R). In the Companion app, reset the
   frontend cache — see [After installing](#after-installing)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=igraph100&repository=ha-restart-guard&category=integration)

### Manual

1. Copy `custom_components/restart_guard/` into your `config/custom_components/` folder
2. Restart Home Assistant
3. **Settings → Devices & Services → Add Integration → Restart Guard**

[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=restart_guard)

### After installing

The banner is a frontend module, so the browser has to fetch it once:

- **Desktop:** hard refresh — Ctrl+Shift+R, or Cmd+Shift+R on macOS
- **Android app:** Settings → Companion app → Troubleshooting → **Reset frontend cache**
- **iOS app:** Settings → Companion App → Debug → **Reset frontend cache**

---

## What else is guarded

The restart dialog is the obvious way to restart, but far from the only one. The same
check runs, with its own confirmation, for every restart-class action that goes through
the Home Assistant frontend:

- the **HACS** "Restart Home Assistant" button
- a **restart-required repair**, and repair fix flows generally
- a **core, OS or Supervisor update** that reboots when it finishes
- **host reboot** and **host shutdown** (`hassio.host_reboot` / `hassio.host_shutdown`)
- `homeassistant.restart` from **Developer Tools**, a dashboard button, or a script you
  trigger yourself
- `update.install` on the Home Assistant Core update entity

![A HACS restart-required repair, interrupted by the same warning][img-update]

---

## When it stays quiet

A triggered automation isn't necessarily an automation that *does* anything. Restart
Guard checks two things before warning you, so you don't get trained to ignore it.

**The `conditions:` block.** If it doesn't pass right now, firing the trigger is a
no-op:

```yaml
conditions:
  - condition: state
    entity_id: input_boolean.office_occupied
    state: "on"       # off? then nothing happens, so no warning
```

**Which branch the trigger can actually reach.** A *gate* is a step that can end up
doing nothing: a `choose` with no `default`, or an `if` with no `else`. Automations that
route several triggers often stack one gate per trigger id rather than using a single
`choose`, and each of those is independently skippable — so the sequence is a no-op
exactly when every step in it is.

Take a three-trigger automation whose branches are gated on trigger id plus a weekday
template:

```yaml
triggers:
  - id: night        # 22:00
  - id: late         # 23:59
  - id: morning      # 06:00
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: night
          - condition: template
            value_template: "{{ now().weekday() < 5 }}"   # weekdays only
        sequence: [...]
    default: []       # nothing happens if no branch matches
```

On a Saturday the 22:00 trigger fires, no branch matches it, `default` is empty — so
restarting at 21:59 at the weekend is genuinely safe, and you won't be warned. On a
Tuesday you will be.

Trigger ids are optional. A trigger without an `id` can never satisfy
`condition: trigger`, so branches gated on one are simply out of its reach, and
branches gated on ordinary conditions are evaluated as usual. An automation whose
branches all test one entity goes quiet if that entity is `unavailable`, because then
no branch can match.

The evaluation uses live state, which raises an obvious problem: a condition on an
entity that flips between now and the trigger would be judged on the wrong value. A
sensor reading `off` at 19:45 and `on` at 20:00 would rule out a 20:00 run that is
certain to happen — a green "safe to restart" a quarter of an hour before something
unavoidable, which is the one answer this exists never to give.

So a condition naming an entity whose next change *is* knowable — by any of the means
in [Predictable state changes](#predictable-state-changes) — is not used to rule a run
out when that change falls before the trigger. The condition is set aside and the run
warns instead. For an ordinary helper or switch, whose changes nobody can foresee, this
costs nothing and the old caveat still applies.

For those unforeseeable ones there's a blunter rule: conditions are only evaluated for
runs **within the next six hours**. Past that, the run is assumed to happen. Reading a
switch now says a good deal about a run at teatime and very little about one tomorrow
evening.

Six hours is a duration, deliberately, and not "is it still today". A date boundary
measures the wrong thing in both directions — 23:15 to 00:00 is forty-five minutes and
two different days, so an hourly automation with a failing condition used to warn every
night in that hour; 00:05 to 23:55 is twenty-three hours and one day, and used to be
ruled out on a reading most of a day stale. With the default 60-minute lookahead nothing
can fall outside six hours anyway, so this only shows up if you've raised it.

Conditions that need nothing from the house are exempt, because they're just as true
tomorrow: `weekday`, and `condition: trigger`. An automation gated on Sunday, firing at
00:01 on Monday, is ruled out on the day it lands on however far away it is.

**A running `for:` countdown always warns**, however far off it is. Everything else on
the banner is judged by how soon it is, because a restart only *delays* it — Home
Assistant re-arms a clock trigger on the way back up, so a run due in twenty minutes
still happens at twenty minutes. A `for:` countdown is not re-armed: Home Assistant only
starts one when it *sees* the state change, and after a restart it never saw it. So
restarting doesn't postpone that run, it cancels it — which makes distance irrelevant and
puts it in the same class as an automation that's already mid-run. Those rows say
*"restarting cancels it"* and are counted in `countdown_runs`.

**When in doubt, it warns.** A wrong "safe to restart" costs you a real automation run,
so anything that can't be judged confidently counts as *will run*:

| Situation | Why it still warns |
|---|---|
| The run is more than six hours away | Live state now is a poor proxy for then |
| A branch uses `condition: time` with `after:` / `before:` | Evaluating minutes early gives the wrong answer |
| `choose` has a non-empty `default:` | Something always happens |
| A step that always acts — a bare service call, a `delay`, a `repeat` | It runs whatever the trigger was |
| `choose` with a `default:`, or `if` with an `else:` | Something always happens |
| Blueprint automations | The action config isn't visible |
| A condition or template won't compile or errors | No answer, so assume the worst |

Skipped runs aren't hidden — they're listed in the `skipped` attribute with a reason, so
you can check the logic instead of trusting it blindly.

---

## Scheduler support

If you have [scheduler-component](https://github.com/nielsfaber/scheduler-component)
installed, its schedules are watched alongside your automations — they're clock-driven,
so a restart can miss them in exactly the same way. Nothing to configure; it's detected
automatically and does nothing if the component isn't there.

Schedules are read from the `next_trigger` attribute the component already publishes,
which has weekdays and timeslots resolved, so there's no clock arithmetic to redo.

**Only enabled schedules count.** A disabled schedule keeps publishing a `next_trigger`
anyway, frequently one already in the past — on the instance this was built against, 29
of 32 schedules were switched off and every one still advertised a next trigger. Reading
that attribute without checking the switch state would bury you in warnings about
schedules that cannot fire.

Schedules you never named appear as "Scheduler Schedule #002d2d", which is useless in a
warning, so those fall back to what the schedule controls — *"Schedule: Office Climate"*,
or *"Schedule: Office Light 1 (Weekday Schedule)"* when it carries a tag.

Schedule **conditions are checked too**, the same as automation conditions. Scheduler
has its own small condition language — `is` / `not` / `above` / `below`, combined with
`and` or `or`, per timeslot — and the guard evaluates the conditions of whichever
timeslot the next trigger will use. A schedule whose conditions can't pass would fire
and do nothing, so it goes into `skipped` instead of the banner.

The same fail-safe rule applies: an unknown entity, a missing attribute, a match type
we don't recognise, or a non-numeric value in an `above`/`below` comparison all count as
*will run*.

Turn it off with the **Also watch Scheduler schedules** option; the condition checking
follows the **Stay quiet when conditions can't pass** option.

---

## Options

**Settings → Devices & Services → Restart Guard → Configure**

| Option | Default | What it does |
|---|---|---|
| Warn if an automation is due within | 6 min | How close counts as too close to restart |
| Look ahead at most | 60 min | Automations further out than this aren't tracked. Up to 1440, since calendar-driven runs turn over 12-20 hours out |
| Ignore automations repeating more often than | 60 min | Stops an every-5-minutes `time_pattern` from warning constantly |
| Stay quiet when conditions can't pass | on | Skip automations that would fire and do nothing |
| Also watch Scheduler schedules | on | Include scheduler-component schedules |
| Open on tap | on | Tap an automation, script or schedule in the warning to go straight to it |
| Dashboard your scheduler card is on | empty | Only affects schedules. Format `/dashboard/view`, e.g. `/lovelace/scheduler`. Only appears when the switch above is on |

**Tapping a row.** An automation opens its editor, a script opens its own. The first tap
asks whether you want this; the answer is stored with the integration rather than in the
browser, so answering it on your phone answers it on the laptop too, and the toggle above
changes your mind later. One consequence worth knowing in a shared house: whoever
answers "no" turns it off for everybody.

A schedule has no page of its own — the Scheduler component is backend-only, and what you
edit a schedule with is a card on whichever dashboard its owner put it on. There's nothing
to discover and no id to link to, so if you want schedule rows to go somewhere, name the
dashboard yourself: `/lovelace/scheduler`, or wherever your card lives. Left empty, those
rows open the entity dialog instead.

---

## The entity

`sensor.restart_guard` — minutes until the next timed automation, or `9999` when
nothing is due inside the lookahead window. Prefer the `count` attribute over comparing
against `9999`.

| Attribute | Meaning |
|---|---|
| `items` | Upcoming runs: `alias`, `entity_id`, `when`, `minutes`, `at`, `at_ts`, `trigger_id`, `source` (`automation` or `schedule`), and for schedules a `condition_check` saying why it wasn't skipped |
| `count` | How many are inside the window (`0` = nothing due) |
| `skipped` | Runs that would fire but do nothing, each with a `reason` |
| `skipped_count` | How many were skipped |
| `running` | Runs in progress: `alias`, `entity_id`, `current`, `seconds_ago` |
| `running_count` | How many are mid-run |
| `countdown_runs` | How many upcoming runs are an armed `for:` countdown — a restart cancels these rather than delaying them |
| `blocking_runs` | Same number as `running_count`. Every run in progress blocks, however long it has been going — kept as its own name because that is the question being asked |
| `warn_window`, `lookahead` | Current options, read by the frontend module |
| `open_on_tap`, `tap_answered`, `scheduler_path` | The tap settings, read by the frontend module. `tap_answered` is whether anyone has been asked yet |
| `automations_scanned` | How many automation entities were examined |
| `trigger_kinds` | Count of each trigger kind recognised, e.g. `{"sun": 2, "time": 16}`. Tells "understood, not due" apart from "never recognised" |
| `state_predictions` | How each predicted state change was worked out — the entity's own window, its integration's published moments, a boolean attribute, a running `for:` countdown, or a timer |
| `state_debug` | `attribute:` triggers that produced no answer, and why. Empty is the healthy state |
| `version` | The running version, so you can confirm an update actually landed |
| `schedules_scanned` | How many Scheduler entities were examined |
| `schedule_conditions` | Diagnostic: `ok`, `disabled by option`, `scheduler not in hass.data`, `no coordinator (…)` |
| `error` | Populated only if a calculation failed |

A dashboard card, if you want it visible outside the dialog:

```yaml
type: conditional
conditions:
  - condition: numeric_state
    entity: sensor.restart_guard
    below: 7
card:
  type: markdown
  content: >-
    ## ⚠️ {{ states('sensor.restart_guard') }} min to next automation
    {% for i in state_attr('sensor.restart_guard','items') or [] %}
    - **{{ i.when }}** {{ i.alias }}
    {%- endfor %}
```

---

## What it understands

### Covered

| Trigger | How |
|---|---|
| `time` with a literal `HH:MM:SS` | Computed in your timezone, DST-correct |
| `time` pointing at `input_datetime` / a timestamp sensor | Resolved against live state, offsets included |
| `time` with `weekday:` | Projected onto the right date |
| `time_pattern` | Unless it repeats more often than the min interval |
| `sun` with `offset` | `sun.sun`'s own `next_rising` / `next_setting`, the same times the trigger is scheduled from |
| `calendar` event start / end, with an offset | The calendar entity's own `start_time` / `end_time` |
| `state` with `for:` | The countdown already running (`last_changed` + the delay), or the predicted change plus it — but only a countdown Home Assistant actually saw start, see below. A running countdown always warns, see [When it stays quiet](#when-it-stays-quiet) |
| `timer.*` finishing | The timer's own `finishes_at`, while it is running |
| `state` on an `attribute:` | The same published windows, read for that attribute rather than the state |
| `state` on an entity that publishes when it next changes | The entity's own window attributes, or times published by its integration — see [Predictable state changes](#predictable-state-changes) |
| Anything **mid-run** — `delay`, `wait_template`, `wait_for_trigger`, `repeat`, slow actions | The `current` attribute, so the action script doesn't need parsing |
| `conditions:` blocks and `choose` branch conditions | Evaluated, see [When it stays quiet](#when-it-stays-quiet) |
| Scheduler component schedules | The `next_trigger` attribute, enabled schedules only |
| Scheduler per-timeslot conditions | Scheduler's own `is`/`not`/`above`/`below` language |

**A `for:` countdown only counts if Home Assistant watched it start.** A state trigger
listens for state-change *events*, and it starts listening when the automation is set up.
A state restored at startup produces no such event, so no countdown is armed and the run
never comes — however recent `last_changed` looks, because what it records is when
everything came back. Taking it at face value invented a run after every single restart,
for the whole length of the delay. So a `last_changed` at or before the moment Home
Assistant finished starting is ignored, and the real next occurrence is used instead.
Those show up as `for: not armed` in `state_predictions`.

A `condition: time` block narrows things down in two ways. `weekday:` limits the days, so
a "Sundays only" automation won't warn you on a Thursday. `after:`/`before:` limits the
hours: runs outside the window are never reported, and — importantly — the min-interval
filter measures the repeat rate *inside* the window. That is what lets a pattern like

```yaml
triggers:
  - trigger: time_pattern
    minutes: "*"
conditions:
  - condition: time
    weekday: [thu]
    after: "21:59:59"
    before: "22:21:00"
```

warn you, even though its trigger alone would fire 1440 times a day. Judged on the
trigger it looks like pure noise; judged with the condition it is 21 runs a week, all on
one evening. Automations that are switched off are ignored, since they can't fire.

### Predictable state changes

A `state` trigger is normally unknowable — a door opens when it opens. But some entities
announce when they next change, and a run driven by one of those is as predictable as
any clock. Two sources are used, in that order:

**The entity's own attributes.** Any attribute ending in `_start` or `_end` (or the
spaced form), plus `starts_at`/`ends_at` and `start_time`/`end_time`, is read as a
window. Matching the shape rather than a fixed list of names matters: integrations
name these inconsistently, and one may publish several windows at once — a current
and a next, an ordinary and an early variant. All are kept and the soonest one still
ahead wins. A start with no matching end counts too, since half a window still says
when the entity changes.

**Times published by the integration.** Some entities carry no attributes at all, yet
their integration re-evaluates them at moments it publishes as sensors in their own
right. Where that schedule is knowable, the entity's change is predicted from those
sensors rather than from anything recomputed here — so whatever offsets and options the
integration was configured with are already accounted for.

Where the *direction* of a change is known, `to: "on"` and `to: "off"` resolve to
different moments. Where only the moment is known, an automation watching for **any**
change is predicted while one watching for a **particular** value is not: that would
otherwise be reported every time the value turned over, and a banner you learn to ignore
protects nobody.

`to:` decides which edge is meant; failing that `from:` decides by implication, since a
trigger leaving `off` waits for the same moment as one arriving at `on`. With neither,
any change counts and the sooner edge wins.

> [!NOTE]
> This only ever *adds* warnings. A trigger it can't resolve is treated exactly as it
> was before — invisible — so the worst case is a warning you didn't need rather than a
> run you weren't told about.

Deliberately left alone: `state` with `for:`, because the run happens some time *after*
the change and reporting the change time would be the wrong minute, and `attribute:`
triggers, which watch something other than the state.


---

## What it cannot catch

> [!WARNING]
> Restart Guard predicts **clock-driven** work only. Everything below can fire during
> your restart with no warning at all. Treat the green banner as "no *scheduled* work
> imminent", never as "nothing will happen".

### Impossible in principle

Nothing can predict these, because predicting them means predicting the future. A
`binary_sensor` can flip one second from now:

| Trigger | Example |
|---|---|
| `state` | A door opens, motion is detected, someone comes home |
| `numeric_state` | Power draw crosses a threshold |
| `template` | A template becomes true because any entity in it changed |
| `event`, MQTT, webhook, `tag` | An NFC tag scan, a button press, an inbound message |
| `device` triggers | A remote button, a Zigbee double-tap |
| `zone`, `geo_location` | Someone arrives or leaves |
| `conversation` | A voice command |

If a motion sensor trips two seconds after you press Restart, that automation is lost
and the banner was green. This is not a bug and cannot be fixed.

The exception is a `state` trigger on an entity that *publishes* when it next changes —
those are predictable and are covered. See
[Predictable state changes](#predictable-state-changes).

### Predictable, but not implemented yet

These *do* publish or imply a fire time, so they could be supported. They are not, yet:

| Trigger | Where the time lives | Why it matters |
|---|---|---|
| `schedule.*` helper (core) | The helper's `next_event` attribute | Not the same thing as the Scheduler component |

### Outside Home Assistant entirely

Restart Guard only sees Home Assistant automations, scripts and Scheduler schedules. It
knows nothing about:

- **Node-RED, AppDaemon, pyscript** flows and their own timers
- **Add-ons** doing their own scheduling
- **Another integration's internal timers** — a vacuum's cloud schedule, a thermostat's
  built-in program, an alarm panel's exit delay

Some of those are unaffected by a Home Assistant restart, since they run on the device
or in the cloud. Node-RED and AppDaemon run as separate add-ons and keep going while
Home Assistant restarts, though anything of theirs that *calls* Home Assistant during
the downtime will fail.

### Blind spots in what it does cover

- **Restarts from outside the browser.** The CLI, an SSH `ha core restart`, or the host
  rebooting on its own are not intercepted — nothing in a frontend module can see those.
  Everything that goes *through* the Home Assistant UI is guarded, though; see
  [What else is guarded](#what-else-is-guarded).
- **Beyond the lookahead.** Default 60 minutes. Anything further out is invisible, and
  the sensor reads `9999`.
- **High-frequency `time_pattern`.** Anything repeating more often than the min interval
  (default 60 min) is deliberately ignored, or the banner would be permanently red.
  The rate is measured *after* any `condition: time` window, so a `minutes: "*"` pattern
  fenced into 21 minutes a week still warns — it is only the genuinely constant ones
  that get dropped.
- **Disabled automations and schedules** are ignored, correctly — but if you re-enable
  one, allow up to 30 seconds for the sensor to notice.
- **Condition checks use live state**, except where the entity's next change is
  knowable. A condition on an unpredictable entity that flips between now and the
  trigger is still judged on the old value.
- **Safe mode.** Core serves no extra frontend modules, so no banner appears at all.

---

## Contributing

Issues and pull requests welcome.

## License

MIT — see [LICENSE](LICENSE).

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-link]: https://github.com/hacs/integration
[downloads-badge]: https://img.shields.io/github/downloads/igraph100/ha-restart-guard/total?label=downloads&color=41BDF5&cacheSeconds=1800
[release-badge]: https://img.shields.io/github/v/release/igraph100/ha-restart-guard?label=latest&color=41BDF5
[releases-link]: https://github.com/igraph100/ha-restart-guard/releases
[validate-badge]: https://github.com/igraph100/ha-restart-guard/actions/workflows/validate.yml/badge.svg
[validate-link]: https://github.com/igraph100/ha-restart-guard/actions/workflows/validate.yml
[license-badge]: https://img.shields.io/github/license/igraph100/ha-restart-guard?color=41BDF5

<!-- Absolute raw URLs, not relative paths: HACS renders this README *inside* Home
     Assistant, where a relative path resolves against your own HA hostname and 404s. -->
[img-warning]: https://raw.githubusercontent.com/igraph100/ha-restart-guard/main/docs/images/restart-dialog-warning.png
[img-armed]: https://raw.githubusercontent.com/igraph100/ha-restart-guard/main/docs/images/restart-dialog-armed.png
[img-safe]: https://raw.githubusercontent.com/igraph100/ha-restart-guard/main/docs/images/restart-dialog-safe.png
[img-soon]: https://raw.githubusercontent.com/igraph100/ha-restart-guard/main/docs/images/restart-safe-60min.png
[img-update]: https://raw.githubusercontent.com/igraph100/ha-restart-guard/main/docs/images/update-confirm.png
