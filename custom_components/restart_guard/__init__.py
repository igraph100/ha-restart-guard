"""Restart Guard - warn before restarting if an automation is about to run."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, FRONTEND_FILE, FRONTEND_URL, VERSION

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
_STATIC_FLAG = f"{DOMAIN}_static_path_registered"
_URL_KEY = f"{DOMAIN}_module_url"


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Restart Guard from a config entry."""
    await _async_register_static_path(hass)
    url = await hass.async_add_executor_job(_module_url, hass)
    _add_module_url(hass, url)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    The static route stays registered - aiohttp cannot unregister one - but the
    module is dropped from the frontend so the banner stops appearing.
    """
    _remove_module_url(hass, _module_url(hass))
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the user changes options."""
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
