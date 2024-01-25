"""Custom integration to integrate BatteryNotes with Home Assistant.

For more details about this integration, please refer to
https://github.com/andrew-codechimp/ha-battery-notes
"""
from __future__ import annotations

import logging
from datetime import datetime

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
import re

from awesomeversion.awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import __version__ as HA_VERSION  # noqa: N812
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .config_flow import CONFIG_VERSION

from .discovery import DiscoveryManager
from .library_updater import (
    LibraryUpdater,
)
from .coordinator import BatteryNotesCoordinator
from .store import (
    async_get_registry,
)

from .const import (
    DOMAIN,
    DOMAIN_CONFIG,
    PLATFORMS,
    CONF_ENABLE_AUTODISCOVERY,
    CONF_USER_LIBRARY,
    DATA_LIBRARY_UPDATER,
    CONF_SHOW_ALL_DEVICES,
    CONF_ENABLE_REPLACED,
    SERVICE_BATTERY_REPLACED,
    SERVICE_BATTERY_REPLACED_SCHEMA,
    DATA_COORDINATOR,
    ATTR_REMOVE,
    ATTR_DEVICE_ID,
    ATTR_DATE_TIME_REPLACED,
    CONF_BATTERY_TYPE,
    CONF_BATTERY_QUANTITY,
)

MIN_HA_VERSION = "2023.7"

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            vol.Schema(
                {
                    vol.Optional(CONF_ENABLE_AUTODISCOVERY, default=True): cv.boolean,
                    vol.Optional(CONF_USER_LIBRARY, default=""): cv.string,
                    vol.Optional(CONF_SHOW_ALL_DEVICES, default=False): cv.boolean,
                    vol.Optional(CONF_ENABLE_REPLACED, default=True): cv.boolean,
                },
            ),
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Integration setup."""

    if AwesomeVersion(HA_VERSION) < AwesomeVersion(MIN_HA_VERSION):  # pragma: no cover
        msg = (
            "This integration requires at least HomeAssistant version "
            f" {MIN_HA_VERSION}, you are running version {HA_VERSION}."
            " Please upgrade HomeAssistant to continue use this integration."
        )
        _LOGGER.critical(msg)
        return False

    domain_config: ConfigType = config.get(DOMAIN) or {
        CONF_ENABLE_AUTODISCOVERY: True,
        CONF_SHOW_ALL_DEVICES: False,
        CONF_ENABLE_REPLACED: True,
    }

    hass.data[DOMAIN] = {
        DOMAIN_CONFIG: domain_config,
    }

    store = await async_get_registry(hass)

    coordinator = BatteryNotesCoordinator(hass, store)
    hass.data[DOMAIN][DATA_COORDINATOR] = coordinator

    library_updater = LibraryUpdater(hass)

    await library_updater.get_library_updates(dt_util.utcnow())

    hass.data[DOMAIN][DATA_LIBRARY_UPDATER] = library_updater

    await coordinator.async_refresh()

    if domain_config.get(CONF_ENABLE_AUTODISCOVERY):
        discovery_manager = DiscoveryManager(hass, config)
        await discovery_manager.start_discovery()
    else:
        _LOGGER.debug("Auto discovery disabled")

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # Register custom services
    register_services(hass)

    return True


async def async_remove_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Device removed, tidy up store."""

    if "device_id" not in config_entry.data:
        return

    device_id = config_entry.data["device_id"]

    coordinator: BatteryNotesCoordinator = hass.data[DOMAIN][DATA_COORDINATOR]
    data = {ATTR_REMOVE: True}

    coordinator.async_update_device_config(device_id=device_id, data=data)

    _LOGGER.debug("Removed Device %s", device_id)


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old config."""
    new_version = CONFIG_VERSION

    if config_entry.version == 1:
        # Version 1 had a single config for qty & type, split them
        _LOGGER.debug("Migrating config entry from version %s", config_entry.version)

        matches: re.Match = re.search(
            r"^(\d+)(?=x)(?:x\s)(\w+$)|([\s\S]+)", config_entry.data[CONF_BATTERY_TYPE]
        )
        if matches:
            _qty = matches.group(1) if matches.group(1) is not None else "1"
            _type = (
                matches.group(2) if matches.group(2) is not None else matches.group(3)
            )
        else:
            _qty = 1
            _type = config_entry.data[CONF_BATTERY_TYPE]

        new_data = {**config_entry.data}
        new_data[CONF_BATTERY_TYPE] = _type
        new_data[CONF_BATTERY_QUANTITY] = _qty

        config_entry.version = new_version

        hass.config_entries.async_update_entry(
            config_entry, title=config_entry.title, data=new_data
        )

        _LOGGER.info(
            "Entry %s successfully migrated to version %s.",
            config_entry.entry_id,
            new_version,
        )

    return True

@callback
async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


@callback
def register_services(hass):
    """Register services used by battery notes component."""

    async def handle_battery_replaced(call):
        """Handle the service call."""
        device_id = call.data.get(ATTR_DEVICE_ID, "")
        datetime_replaced_entry = call.data.get(ATTR_DATE_TIME_REPLACED)

        if datetime_replaced_entry:
            datetime_replaced = dt_util.as_utc(datetime_replaced_entry).replace(tzinfo=None)
        else:
            datetime_replaced = datetime.utcnow()

        device_registry = dr.async_get(hass)

        device_entry = device_registry.async_get(device_id)
        if not device_entry:
            return

        for entry_id in device_entry.config_entries:
            if (
                entry := hass.config_entries.async_get_entry(entry_id)
            ) and entry.domain == DOMAIN:

                coordinator: BatteryNotesCoordinator = hass.data[DOMAIN][DATA_COORDINATOR]
                device_entry = {"battery_last_replaced": datetime_replaced}

                coordinator.async_update_device_config(
                    device_id=device_id, data=device_entry
                )

                await coordinator.async_request_refresh()

                _LOGGER.debug(
                    "Device %s battery replaced on %s", device_id, str(datetime_replaced)
                )

    hass.services.async_register(
        DOMAIN,
        SERVICE_BATTERY_REPLACED,
        handle_battery_replaced,
        schema=SERVICE_BATTERY_REPLACED_SCHEMA,
    )