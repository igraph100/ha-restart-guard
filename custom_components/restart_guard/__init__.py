"""Restart Guard - warn before restarting if an automation is about to run."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_OPEN_ON_TAP,
    CONF_TAP_ANSWERED,
    DISPLAY_OPTIONS,
    DOMAIN,
    FRONTEND_FILE,
    FRONTEND_URL,
    SERVICE_SET_OPEN_ON_TAP,
    SIGNAL_OPTIONS,
    STARTED_AT,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
_STATIC_FLAG = f"{DOMAIN}_static_path_registered"
_URL_KEY = f"{DOMAIN}_module_url"
_STARTED_AT = STARTED_AT


def _module_url(hass: HomeAssistant) -> str:
    """The frontend module URL, cache-busted by the file's own contents.

    Versioning the URL alone is not enough: the version can go *down* (a
    pre-release renumbered before publishing), and then the browser already has
    something different cached under that exact URL and never refetches. A
    digest of the file changes whenever the file does, in either direction.
    """
    cached = hass.data.get(_URL_KEY)
    if cached:
        return cached

    source = Path(__file__).parent / "frontend" / FRONTEND_FILE
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:10]
    except OSError:
        digest = "nohash"
    url = f"{FRONTEND_URL}?v={VERSION}-{digest}"
    hass.data[_URL_KEY] = url
    return url


def _mark_started(hass: HomeAssistant) -> None:
    """Record when Home Assistant finished starting, once per run.

    A `state ... for:` trigger only arms when Home Assistant *observes* the
    change. A state restored at startup was never observed, so no countdown is
    running for it - and predicting one produces a phantom run for the whole
    `for:` duration after every restart.

    Two things this must not be. Not the moment this integration set up:
    changing an option reloads the entry while Home Assistant keeps running,
    and moving the mark forward would suppress a countdown that really is
    armed - a missed warning, which is worse than the phantom. And not the
    moment setup *began* at a cold start either, because entities are still
    being restored then; waiting for the started event puts the mark after the
    last of them.
    """
    if _STARTED_AT in hass.data:
        return  # a reload: the original mark is the one that means anything
    hass.data[_STARTED_AT] = dt_util.now()
    if hass.state is not CoreState.running:
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            lambda _event: hass.data.__setitem__(_STARTED_AT, dt_util.now()),
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Restart Guard from a config entry."""
    _mark_started(hass)
    await _async_register_static_path(hass)
    url = await hass.async_add_executor_job(_module_url, hass)
    _add_module_url(hass, url)
    _register_services(hass, entry)
    # the snapshot the next option change is diffed against
    hass.data[f"{DOMAIN}_options_{entry.entry_id}"] = dict(entry.options)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


@callback
def _register_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Let the banner record the answer to its own prompt.

    The prompt used to write to `localStorage`, which meant answering it once
    per browser, per phone, per person - and no way to change your mind except
    to find the same prompt again. Writing it to the config entry instead makes
    one answer the answer everywhere, and puts it beside the toggle in the
    options form where it can be flipped back.
    """

    async def _set(call: ServiceCall) -> None:
        enabled = bool(call.data["enabled"])
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_OPEN_ON_TAP: enabled,
                CONF_TAP_ANSWERED: True,
            },
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OPEN_ON_TAP,
        _set,
        schema=vol.Schema({vol.Required("enabled"): cv.boolean}),
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    The static route stays registered - aiohttp cannot unregister one - but the
    module is dropped from the frontend so the banner stops appearing.
    """
    _remove_module_url(hass, _module_url(hass))
    # Forget the cached URL, so setting up again re-reads the file and hashes
    # whatever is there now.
    #
    # Without this the digest was computed once per Home Assistant process and
    # kept for its lifetime. Drop a new restart_guard.js in place and reload the
    # integration, and the frontend was still handed the old URL - which the
    # browser already had cached, so it kept running the old file no matter how
    # hard anyone refreshed. The one thing the hash exists to prevent, arrived
    # at from the other side.
    hass.data.pop(_URL_KEY, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the user changes options - unless nothing needs reloading.

    Most options change what gets computed, so the entry has to come back up
    with them. The display ones do not, and reloading for those would be
    actively harmful: it takes the sensor away for a moment, and the moment is
    while somebody is standing in the restart dialog reading it. Worse, the
    prompt that sets one of them lives *in* that dialog, so answering it would
    blank the very thing being answered.
    """
    key = f"{DOMAIN}_options_{entry.entry_id}"
    before = hass.data.get(key) or {}
    after = dict(entry.options)
    hass.data[key] = after

    changed = {
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    }
    # an empty set is a subset too, which is the answer we want: the entry was
    # updated without any option actually changing, so there is nothing to do
    if changed.issubset(DISPLAY_OPTIONS):
        async_dispatcher_send(hass, SIGNAL_OPTIONS)
        return

    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# frontend module: served from inside the integration, registered on every page
# ---------------------------------------------------------------------------
async def _async_register_static_path(hass: HomeAssistant) -> None:
    """Serve the bundled restart_guard.js over HTTP.

    Replaces copying the file into /config/www by hand.
    """
    if hass.data.get(_STATIC_FLAG):
        return

    source = Path(__file__).parent / "frontend" / FRONTEND_FILE
    if not await hass.async_add_executor_job(source.is_file):
        _LOGGER.error("Bundled frontend file missing at %s", source)
        return

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_URL, str(source), False)]
        )
    except ImportError:  # pragma: no cover - very old cores
        hass.http.register_static_path(FRONTEND_URL, str(source), False)
    except Exception:  # noqa: BLE001 - most likely already registered
        _LOGGER.debug("Static path %s already registered", FRONTEND_URL, exc_info=True)

    hass.data[_STATIC_FLAG] = True


def _add_module_url(hass: HomeAssistant, url: str) -> None:
    """Load an ES module on every frontend page.

    Replaces the `frontend: extra_module_url:` entry in configuration.yaml.
    """
    if not _frontend_call("add_extra_js_url", hass, url):
        store = hass.data.get("frontend_extra_module_url")
        if store is None:
            _LOGGER.warning(
                "Could not register %s with the frontend. Add it manually under "
                "frontend: extra_module_url: if the banner never appears.",
                url,
            )
            return
        store.add(url)


def _remove_module_url(hass: HomeAssistant, url: str) -> None:
    """Stop loading the module (on unload)."""
    if not _frontend_call("remove_extra_js_url", hass, url):
        store = hass.data.get("frontend_extra_module_url")
        if store is not None:
            try:
                store.remove(url)
            except (KeyError, ValueError):
                pass


def _frontend_call(name: str, hass: HomeAssistant, url: str) -> bool:
    """Call a frontend helper by name. True if it worked."""
    try:
        from homeassistant.components import frontend

        helper = getattr(frontend, name, None)
        if helper is None:
            return False
        helper(hass, url)
    except Exception:  # noqa: BLE001 - caller falls back to the data store
        _LOGGER.debug("frontend.%s unavailable", name, exc_info=True)
        return False
    return True
