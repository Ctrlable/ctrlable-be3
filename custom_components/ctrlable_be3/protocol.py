"""Wire protocol for the LifeSmart BE3 CoTP network adaptor.

Nothing here imports Home Assistant: the module is pure Python so it can be
exercised against a recorded gateway session (see tests/fixtures).

The gateway is the TCP *client*. A controller broadcasts a search datagram on
UDP 12345, the gateway connects back to the advertised TCP port, and both sides
then exchange JSON objects delimited by CRLF.

Where this module disagrees with the vendor specification it follows a capture
taken from firmware 0.10 on 2026-09-21; those cases are called out in comments
and in docs/BE3-protocol-findings.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DISCOVERY_PORT = 12345
DEFAULT_TCP_PORT = 21324

#: Sent while no gateway is connected. The space before CRLF is required: the
#: firmware matches on "PORT:(%d+) \r\n".
SEARCH_TEMPLATE = "C4Z-SEARCH * PORT:{port} \r\n"

#: Legacy probe. The gateway answers with a plaintext key/value block without
#: connecting back, which makes it a safe liveness check.
LEGACY_SEARCH = b"Z-SEARCH * \r\n"

LINE_SEPARATOR = b"\r\n"

#: Any inbound traffic counts as proof of life. Heartbeats were observed every
#: ~5s on firmware 0.10 (the specification claims 10s).
IDLE_TIMEOUT = 30.0

#: Guards against a peer that never sends a delimiter.
MAX_BUFFER = 65536

# Button event type codes.
TY_PRESS = 0x4F
TY_RELEASE_AFTER_HOLD = 0x4E
# Legacy codes named by the specification.
TY_SINGLE_CLICK = 0x01
TY_RELEASE = 0x00

# Button event values under TY_PRESS. The specification has these inverted:
# in practice every gesture starts with VAL_DOWN.
VAL_DOWN = 0
VAL_CLICK = 1
VAL_HOLD = 0xFF

# Value type codes used by setVal.
TYPE_OFF = 0x80
TYPE_ON = 0x81
TYPE_BRIGHTNESS = 0xE3
TYPE_SHADE_LEVEL = 0xCF
TYPE_SHADE_STOP = 0xCE

# Brightness is carried in the high bits with a constant offset.
BRIGHTNESS_SCALE = 0x10000
BRIGHTNESS_OFFSET = 0x300

# setVal command ids.
CMD_GET_VALUE = 5
CMD_SET_VALUE = 6

#: Module identifiers used in configuration messages.
MOLD_BUTTON = "btn"
MOLD_DIMMER = "dim"
MOLD_SHADE = "cn"

# Bus limits, kept here because they are properties of the wire format rather
# than of any one layer above it.
MIN_ADDRESS = 0
MAX_ADDRESS = 255

#: Buttons per keypad component, per the vendor's configuration guide.
MAX_BUTTONS = 6

#: Logical device slots. The vendor's own driver exposes four; raising this is
#: untested rather than known to be wrong.
MAX_DEVICE_INDEX = 4

#: The gateway silently truncates labels at ten bytes.
MAX_NAME_BYTES = 10


class ButtonAction(str, Enum):
    """Gestures derived from the raw button reports.

    The names are Buttons Machine's vocabulary rather than our own, so nothing
    has to translate between the two. They also describe the hardware more
    precisely than a generic "long press" would: the gateway distinguishes a
    release that ends a hold from one that ends a tap.
    """

    PRESS = "press"
    CLICK = "click"
    HOLD = "hold"
    HOLD_RELEASE = "hold_release"


@dataclass(frozen=True)
class Heartbeat:
    version: str | None
    ota_pending: bool
    ota_state: str | None
    #: Addresses of the components on the bus, gateway excluded.
    device_addresses: tuple[int, ...]
    #: The gateway's own COTP address, which it appends to the same list.
    gateway_address: int | None


@dataclass(frozen=True)
class DeviceInfo:
    model: str | None
    version: str | None


@dataclass(frozen=True)
class ButtonReport:
    """A raw btnEvt. Feed these to :class:`ButtonTracker` for gestures."""

    address: int
    button: int
    type_code: int
    value: int


@dataclass(frozen=True)
class ValueRequest:
    """A setVal message: the panel reading or writing a linked device."""

    address: int
    index: int
    device_id: int
    attribute_id: int
    type_code: int
    value: int
    command_id: int

    @property
    def is_read(self) -> bool:
        return self.command_id == CMD_GET_VALUE

    @property
    def is_write(self) -> bool:
        return self.command_id == CMD_SET_VALUE

    @property
    def turns_on(self) -> bool:
        return self.is_write and self.type_code == TYPE_ON

    @property
    def turns_off(self) -> bool:
        return self.is_write and self.type_code == TYPE_OFF

    @property
    def brightness(self) -> int | None:
        """Requested brightness percentage, or None for other writes."""
        if self.is_write and self.type_code == TYPE_BRIGHTNESS:
            return decode_brightness(self.value)
        return None


@dataclass(frozen=True)
class Unknown:
    """Anything this module does not model yet, kept for logging."""

    payload: dict[str, Any] = field(default_factory=dict)


Message = Heartbeat | DeviceInfo | ButtonReport | ValueRequest | Unknown


def decode_brightness(value: int) -> int:
    """Decode the encoded brightness carried by a 0xE3 write."""
    return (value - BRIGHTNESS_OFFSET) // BRIGHTNESS_SCALE


def encode_brightness(percent: int) -> int:
    """Inverse of :func:`decode_brightness`."""
    return percent * BRIGHTNESS_SCALE + BRIGHTNESS_OFFSET


def parse_addresses(raw: str | None) -> tuple[tuple[int, ...], int | None]:
    """Split a heartbeat address list into components and the gateway.

    The firmware emits one comma-terminated entry per component and then
    appends its own address, so ``"16,24,63,61"`` means components 16, 24 and
    63 behind gateway 61. A list that still ends in a comma carries no gateway
    address, which is how the specification's example is written.
    """
    if not raw:
        return (), None

    trailing = raw.endswith(",")
    parts = [part for part in raw.split(",") if part.strip()]
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return (), None

    if not values:
        return (), None
    if trailing:
        return tuple(values), None
    return tuple(values[:-1]), values[-1]


def parse_button_index(vidx: str | None) -> int | None:
    """Extract the button number from a ``vidx`` field.

    ``vidx`` is four bytes, ``'B'`` then two NULs then the button number. Any
    other prefix belongs to a different component and must be ignored.
    """
    if not vidx or len(vidx) < 4:
        return None
    if vidx[0] != "B" or vidx[1] != "\x00" or vidx[2] != "\x00":
        return None
    return ord(vidx[3])


def parse_message(payload: dict[str, Any]) -> Message:
    """Turn one decoded JSON object into a message.

    Several messages carry no ``cmd`` key, so the discriminating keys are
    checked in the order the specification prescribes.
    """
    if not isinstance(payload, dict):
        return Unknown({})

    command = payload.get("cmd")

    if command == "heartbeat":
        devices, gateway = parse_addresses(payload.get("addresses"))
        return Heartbeat(
            version=payload.get("version"),
            ota_pending=bool(payload.get("otawait")),
            ota_state=payload.get("otastate"),
            device_addresses=devices,
            gateway_address=gateway,
        )

    if command == "devinfo":
        return DeviceInfo(model=payload.get("model"), version=payload.get("version"))

    if command == "btnEvt":
        button = parse_button_index(payload.get("vidx"))
        address = payload.get("addr")
        type_code = payload.get("ty")
        value = payload.get("val")
        if button is None or not isinstance(address, int):
            return Unknown(payload)
        if not isinstance(type_code, int) or not isinstance(value, int):
            return Unknown(payload)
        return ButtonReport(
            address=address, button=button, type_code=type_code, value=value
        )

    if command == "setVal":
        required = ("addr", "idx", "devId", "devAtrId", "ty", "val", "cmdId")
        if any(payload.get(key) is None for key in required):
            return Unknown(payload)
        return ValueRequest(
            address=payload["addr"],
            index=payload["idx"],
            device_id=payload["devId"],
            attribute_id=payload["devAtrId"],
            type_code=payload["ty"],
            value=payload["val"],
            command_id=payload["cmdId"],
        )

    return Unknown(payload)


class LineReader:
    """Reassembles CRLF-delimited JSON objects from a TCP stream.

    One read may hold several objects, or half of one, so the remainder is
    carried over. Undecodable lines are dropped rather than raising: a single
    malformed frame should not tear down a working connection.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.dropped = 0

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        if len(self._buffer) > MAX_BUFFER:
            # A peer that never delimits is broken; keep only the tail so a
            # later valid frame can still be recovered.
            del self._buffer[:-MAX_BUFFER]
            self.dropped += 1

        messages: list[dict[str, Any]] = []
        while True:
            index = self._buffer.find(LINE_SEPARATOR)
            if index < 0:
                break
            line = bytes(self._buffer[:index])
            del self._buffer[: index + len(LINE_SEPARATOR)]
            if not line.strip():
                continue
            try:
                payload = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self.dropped += 1
                continue
            if isinstance(payload, dict):
                messages.append(payload)
            else:
                self.dropped += 1
        return messages

    def reset(self) -> None:
        self._buffer.clear()


class ButtonTracker:
    """Derives gestures from raw button reports.

    Observed on firmware 0.10, where every gesture opens with ``0x4f``/0:

    * tap          ``0x4f``/0 then ``0x4f``/1
    * medium hold  ``0x4f``/0 then ``0x4e``/0
    * long hold    ``0x4f``/0, ``0x4f``/255 (~1s in), then ``0x4e``/0

    So ``0x4e`` means "released after a hold" only once the hold was reported;
    otherwise it is just a slow click. The reference Control4 driver draws the
    same distinction using a pending-timer flag.
    """

    def __init__(self) -> None:
        self._holding: set[tuple[int, int]] = set()

    def feed(self, report: ButtonReport) -> list[ButtonAction]:
        key = (report.address, report.button)

        if report.type_code == TY_PRESS:
            if report.value == VAL_DOWN:
                self._holding.discard(key)
                return [ButtonAction.PRESS]
            if report.value == VAL_CLICK:
                self._holding.discard(key)
                return [ButtonAction.CLICK]
            if report.value == VAL_HOLD:
                self._holding.add(key)
                return [ButtonAction.HOLD]
            return []

        if report.type_code in (TY_RELEASE_AFTER_HOLD, TY_RELEASE):
            if key in self._holding:
                self._holding.discard(key)
                return [ButtonAction.HOLD_RELEASE]
            return [ButtonAction.CLICK]

        if report.type_code == TY_SINGLE_CLICK:
            self._holding.discard(key)
            return [ButtonAction.CLICK]

        return []

    def reset(self, address: int | None = None) -> None:
        """Forget pending holds, for a single address or all of them."""
        if address is None:
            self._holding.clear()
            return
        self._holding = {key for key in self._holding if key[0] != address}


def encode(payload: dict[str, Any]) -> bytes:
    """Serialise one outbound message, delimiter included."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return body.encode("utf-8") + LINE_SEPARATOR


def build_search(port: int) -> bytes:
    """The UDP datagram that invites a gateway to connect back."""
    return SEARCH_TEMPLATE.format(port=port).encode("utf-8")


def build_identify(address: int) -> dict[str, Any]:
    """Make the component at ``address`` flash, to locate it physically."""
    return {"cmd": "IND", "addr": address}


def build_set_address(old: int, new: int) -> dict[str, Any]:
    """Re-address a component on the bus."""
    return {"cmd": "setaddr", "from": old, "to": new}


def build_configure_buttons(
    address: int, index: int, count: int, name: str | None = None
) -> dict[str, Any]:
    """Provision a component as a keypad so it reports button events.

    ``btnBum`` is the vendor's spelling and must be kept. Names longer than ten
    bytes are truncated by the gateway.
    """
    payload: dict[str, Any] = {
        "cmd": "setBtn",
        "devMold": MOLD_BUTTON,
        "devCotpAddr": address,
        "devIdx": index,
        "btnBum": count,
    }
    if name:
        payload["name"] = name
    return payload


def build_clear_config(
    address: int, index: int, mold: str, name: str | None = None
) -> dict[str, Any]:
    """Clear a component's configuration."""
    payload: dict[str, Any] = {
        "cmd": "clrCfg",
        "devMold": mold,
        "devCotpAddr": address,
        "devIdx": index,
    }
    if name:
        payload["name"] = name
    return payload


def build_led(address: int, button: int, on: bool) -> dict[str, Any]:
    """Set a keypad backlight.

    There is no feedback for this, so callers must treat LED state as assumed.
    Note the capitalised keys: this message predates the lowercase convention.
    """
    return {"LedAddr": address, "LedNumber": button, "Val": 1 if on else 0}


def build_value_response(
    address: int,
    index: int,
    device_id: int,
    attribute_id: int,
    value: int,
    value_type: int | None = None,
) -> dict[str, Any]:
    """Answer a read request so the panel can show real state."""
    payload: dict[str, Any] = {
        "rsp": True,
        "devCotpAddr": address,
        "devIdx": index,
        "devId": device_id,
        "devAtrId": attribute_id,
        "ioval": value,
    }
    if value_type is not None:
        payload["ioty"] = value_type
    return payload


def build_ota_start() -> dict[str, Any]:
    """Begin a firmware update. Only valid while a heartbeat reports otawait."""
    return {"cmd": "ota", "start": True}


def parse_discovery_reply(data: bytes) -> dict[str, str]:
    """Parse the plaintext answer to :data:`LEGACY_SEARCH`.

    Yields keys such as MOD, SN, NAME, SUBKEY and VER.
    """
    result: dict[str, str] = {}
    for line in data.decode("utf-8", "replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key.strip()] = value.strip()
    return result
