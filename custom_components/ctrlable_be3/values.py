"""Converting between what a panel speaks and what Home Assistant holds.

Panels work in whole percentages, because that is what their screens show and
what their configuration declares (``"0,100,5"``). Home Assistant holds
brightness as 0-255 and colour temperature in kelvin. Everything here is that
translation, kept apart from the wiring so the arithmetic can be tested.

Rounding matters more than it looks. A panel that asks for 65% and is then told
it has 64% will redraw its screen a step lower than the installer just chose,
so conversions round rather than truncate, and round-tripping a percentage must
land back on the same percentage.
"""

from __future__ import annotations

#: Home Assistant's brightness scale.
HA_BRIGHTNESS_MAX = 255

#: Fallback kelvin range, used only when a light does not publish its own.
DEFAULT_MIN_KELVIN = 2000
DEFAULT_MAX_KELVIN = 6500


def clamp_percent(value: float) -> int:
    """Round to a whole percentage inside 0-100."""
    return max(0, min(100, round(value)))


def brightness_to_percent(brightness: int | None) -> int:
    """Home Assistant brightness (0-255) as a percentage."""
    if not brightness:
        return 0
    return clamp_percent(brightness * 100 / HA_BRIGHTNESS_MAX)


def percent_to_brightness(percent: int) -> int:
    """A percentage as Home Assistant brightness (0-255).

    100% must reach exactly 255: a light left one step below full because of
    rounding is a visible fault.
    """
    percent = clamp_percent(percent)
    if percent >= 100:
        return HA_BRIGHTNESS_MAX
    return round(percent * HA_BRIGHTNESS_MAX / 100)


def kelvin_to_percent(
    kelvin: int | None,
    min_kelvin: int | None = None,
    max_kelvin: int | None = None,
) -> int:
    """Colour temperature as a percentage of the light's own range.

    Panels have no concept of kelvin; they move a bar from 0 to 100. The range
    is the light's own, so the ends of the bar mean the warmest and coolest
    that particular fixture can actually do.
    """
    low = min_kelvin or DEFAULT_MIN_KELVIN
    high = max_kelvin or DEFAULT_MAX_KELVIN
    if kelvin is None or high <= low:
        return 0
    return clamp_percent((kelvin - low) * 100 / (high - low))


def percent_to_kelvin(
    percent: int,
    min_kelvin: int | None = None,
    max_kelvin: int | None = None,
) -> int:
    """A percentage as kelvin within the light's own range."""
    low = min_kelvin or DEFAULT_MIN_KELVIN
    high = max_kelvin or DEFAULT_MAX_KELVIN
    if high <= low:
        return low
    return round(low + (high - low) * clamp_percent(percent) / 100)


def position_to_percent(position: int | None) -> int:
    """A cover position, which Home Assistant already keeps as 0-100."""
    if position is None:
        return 0
    return clamp_percent(position)
