"""Constants shared by the Home Assistant layer."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "ctrlable_be3"

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.EVENT, Platform.SWITCH]

# Config entry keys.
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAC = "mac"
CONF_MODEL = "model"

# Each bus component is a subentry, which gives it its own device page.
SUBENTRY_COMPONENT = "component"

#: Bus event fired for every button action, consumed by the Buttons Machine
#: backend and available to anyone writing automations directly.
EVENT_KEYPAD = "ctrlable_be3_keypad_event"

# Service names.
SERVICE_IDENTIFY = "identify"
SERVICE_SET_ADDRESS = "set_address"
SERVICE_CONFIGURE_KEYPAD = "configure_keypad"
SERVICE_CLEAR_CONFIGURATION = "clear_configuration"
SERVICE_START_UPDATE = "start_update"

ATTR_ENTRY_ID = "config_entry_id"
ATTR_ADDRESS = "address"
ATTR_NEW_ADDRESS = "new_address"
ATTR_BUTTONS = "buttons"
ATTR_SLOT = "slot"
ATTR_NAME = "name"
ATTR_KIND = "kind"

MANUFACTURER = "LifeSmart"
GATEWAY_MODEL = "BE3 network adaptor"
PANEL_MODEL = "SUBLIME panel"
