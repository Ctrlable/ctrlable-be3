"""Constants shared by the Home Assistant layer."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "ctrlable_be3"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SWITCH,
]

# Config entry keys.
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAC = "mac"
CONF_MODEL = "model"

#: Pages we have written to panels, so one whose component is deleted can
#: be cleared from the hardware. Home Assistant offers no callback when a
#: subentry is removed, so the record has to outlive it.
CONF_WRITTEN_PAGES = "written_pages"

# Each bus component is a subentry, which gives it its own device page.
SUBENTRY_COMPONENT = "component"

#: Bus event fired for every button action, consumed by the Buttons Machine
#: backend and available to anyone writing automations directly.
EVENT_KEYPAD = "ctrlable_be3_keypad_event"

#: Bus event for components that report value writes rather than buttons:
#: dimmers and shade controllers, including raise and lower.
EVENT_VALUE = "ctrlable_be3_value_event"

# Service names.
SERVICE_IDENTIFY = "identify"
SERVICE_SET_ADDRESS = "set_address"
SERVICE_CONFIGURE_KEYPAD = "configure_keypad"
SERVICE_CLEAR_CONFIGURATION = "clear_configuration"
SERVICE_START_UPDATE = "start_update"
SERVICE_CLEAR_PANEL = "clear_panel"
SERVICE_BLANK_PAGE = "blank_page"
SERVICE_CONFIGURE_PAGE = "configure_page"

ATTR_ENTRY_ID = "config_entry_id"
ATTR_ADDRESS = "address"
ATTR_NEW_ADDRESS = "new_address"
ATTR_BUTTONS = "buttons"
ATTR_SLOT = "slot"
ATTR_SLOTS = "slots"
ATTR_COLOUR_TEMPERATURE = "colour_temperature"
ATTR_DIRECTION = "direction"
ATTR_NAME = "name"
ATTR_PAGES = "pages"

#: A breath between clears. They do not restart the panel, but firing
#: dozens back to back gives its bus no room to keep up.
CLEAR_INTERVAL = 0.5
ATTR_KIND = "kind"

MANUFACTURER = "LifeSmart"
GATEWAY_MODEL = "BE3 network adaptor"
PANEL_MODEL = "SUBLIME panel"

#: Sent when a component's own traffic has taught us something — a keypad's
#: button count, or what an unidentified component is. Platforms listen so new
#: entities appear without reloading the entry, because reloading drops the
#: gateway's session and this firmware stops relaying the bus when that happens.
SIGNAL_COMPONENTS_LEARNED = "ctrlable_be3_components_learned_{entry_id}"
