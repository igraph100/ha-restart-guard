"""Constants for Restart Guard."""

DOMAIN = "restart_guard"
VERSION = "0.0.2"

CONF_WARN_WINDOW = "warn_window"
CONF_LOOKAHEAD = "lookahead"
CONF_MIN_INTERVAL = "min_interval"
CONF_STALE_RUN = "stale_run"
CONF_CHECK_CONDITIONS = "check_conditions"
CONF_TRACK_SCHEDULES = "track_schedules"

# evaluate conditions to stay quiet about automations that would do nothing
DEFAULT_CHECK_CONDITIONS = True
# also watch schedules from the Scheduler component, if it is installed
DEFAULT_TRACK_SCHEDULES = True

DEFAULT_WARN_WINDOW = 6      # minutes: "too close to restart"
DEFAULT_LOOKAHEAD = 60       # minutes: how far ahead to look at all
DEFAULT_MIN_INTERVAL = 60    # ignore time_patterns firing more often than this
DEFAULT_STALE_RUN = 60       # minutes: a run older than this is "parked", not urgent

# domains whose in-progress runs we watch (both expose a `current` attribute)
RUN_DOMAINS = ("automation", "script")

# nielsfaber/scheduler-component, watched only if it is installed
SCHEDULER_DOMAIN = "scheduler"

# state used when no automation is due inside the lookahead window
NOTHING_DUE = 9999

FRONTEND_FILE = "restart_guard.js"
FRONTEND_URL = "/restart_guard_static/restart_guard.js"
