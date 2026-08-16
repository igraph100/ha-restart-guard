"""Constants for Restart Guard."""

DOMAIN = "restart_guard"
VERSION = "0.0.8"

CONF_WARN_WINDOW = "warn_window"
CONF_LOOKAHEAD = "lookahead"
CONF_MIN_INTERVAL = "min_interval"
CONF_CHECK_CONDITIONS = "check_conditions"
CONF_TRACK_SCHEDULES = "track_schedules"
CONF_OPEN_ON_TAP = "open_on_tap"
# whether anyone has answered the "open it when you tap?" prompt yet. Stored
# with the options rather than in the browser, so answering once answers it for
# every device - but hidden from the options form, which has the real toggle.
CONF_TAP_ANSWERED = "tap_answered"
CONF_SCHEDULER_PATH = "scheduler_path"

# evaluate conditions to stay quiet about automations that would do nothing
DEFAULT_CHECK_CONDITIONS = True
# also watch schedules from the Scheduler component, if it is installed
DEFAULT_TRACK_SCHEDULES = True
# tapping a row in the banner opens whatever that row is about
DEFAULT_OPEN_ON_TAP = True
DEFAULT_TAP_ANSWERED = False
# A schedule has no page of its own: the Scheduler component is backend-only
# and its UI is a card the user places on a dashboard we cannot discover. Empty
# means "no idea", and those rows open the entity's more-info dialog instead.
DEFAULT_SCHEDULER_PATH = ""

# Options that only change what the banner does, not what it computes. Changing
# one must not reload the config entry: a reload takes the sensor away for a
# moment, and the moment in question is while somebody has the restart dialog
# open. The sensor reads its options live, so a state write is enough.
DISPLAY_OPTIONS = (CONF_OPEN_ON_TAP, CONF_TAP_ANSWERED, CONF_SCHEDULER_PATH)

# sets open_on_tap from the banner's own yes/no prompt
SERVICE_SET_OPEN_ON_TAP = "set_open_on_tap"

# told to the sensor when a display option changes, so the new value reaches
# the banner in a second rather than at the next 30-second poll
SIGNAL_OPTIONS = f"{DOMAIN}_options_changed"

# How far ahead a live entity reading is still a fair guess at what will be
# true when the automation fires. Conditions are only evaluated for runs inside
# this; past it we assume the run happens, because reading a switch now says
# very little about a run tomorrow evening.
#
# This used to be "is it still today", which is the wrong measurement in both
# directions. 23:15 to 00:00 is forty-five minutes and two different days, so
# an hourly automation with a failing condition warned every night in that
# hour. 00:05 to 23:55 is twenty-three hours and one day, and got ruled out on
# a reading that was most of a day stale. A duration doesn't care where
# midnight falls.
#
# Six hours: long enough for "later tonight", short enough that nobody's
# helpers and switches are assumed to hold overnight.
CONDITION_HORIZON = 360      # minutes

DEFAULT_WARN_WINDOW = 6      # minutes: "too close to restart"
DEFAULT_LOOKAHEAD = 60       # minutes: how far ahead to look at all
DEFAULT_MIN_INTERVAL = 60    # ignore time_patterns firing more often than this

# domains whose in-progress runs we watch (both expose a `current` attribute)
RUN_DOMAINS = ("automation", "script")

# nielsfaber/scheduler-component, watched only if it is installed
SCHEDULER_DOMAIN = "scheduler"

# state used when no automation is due inside the lookahead window
NOTHING_DUE = 9999

# hass.data key: when Home Assistant finished starting, so a state restored
# at startup is not mistaken for one whose `for:` countdown is running
STARTED_AT = f"{DOMAIN}_started_at"

FRONTEND_FILE = "restart_guard.js"
FRONTEND_URL = "/restart_guard_static/restart_guard.js"
