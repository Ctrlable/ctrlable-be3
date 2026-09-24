"""What this integration offers other integrations as keypads.

Buttons Machine discovers keypads by reading a registry the source integration
publishes, rather than by guessing from entities. This builds that registry.

Only button blocks appear. A screen page is not a keypad: its buttons belong to
whatever property it is showing, they change meaning as the screen changes, and
the panel reports them as value writes rather than button events. Offering one
would produce a keypad whose buttons mean something different depending on what
the wall happens to be displaying.

No Home Assistant imports, so what we publish can be tested directly.
"""

from __future__ import annotations

from .devices import Component, ComponentKind, keypad_id


def keypad_catalogue(
    mac: str, components: tuple[Component, ...] | list[Component]
) -> dict[str, dict]:
    """The keypads on this gateway, keyed by serial.

    The serial is the page-aware id — gateway, address and page — because a
    physical panel spans several addresses and a screen holds several pages, so
    an address alone does not identify one keypad.
    """
    catalogue: dict[str, dict] = {}
    for component in components:
        if component.kind is not ComponentKind.BUTTON or component.ignored:
            continue
        if component.buttons < 1:
            # Nothing to offer yet: the button count is learned from the panel's
            # own traffic, so a keypad nobody has pressed says nothing about
            # its size.
            continue

        serial = keypad_id(mac, component.address, component.slot)
        catalogue[serial] = {
            "keypad_id": serial,
            "keypad_name": component.name or f"BE3 keypad {component.address}",
            "address": component.address,
            "page": component.slot,
            "panel": component.panel or "",
            "buttons": [
                {"number": number, "name": f"Button {number}"}
                for number in range(1, component.buttons + 1)
            ],
        }
    return catalogue


class KeypadRegistry:
    """The published registry, refreshed whenever components change.

    Kept as an object with a ``keypads`` mapping because that is the shape
    Buttons Machine reads, and because entries are reloaded often enough that
    handing out a snapshot would go stale.

    One registry covers every gateway, since serials carry the MAC and so never
    collide — but each gateway's keypads are held separately, or reloading one
    gateway would take another's keypads out of the list.
    """

    def __init__(self) -> None:
        self.keypads: dict[str, dict] = {}
        self._by_gateway: dict[str, dict[str, dict]] = {}

    def refresh(
        self, mac: str, components: tuple[Component, ...] | list[Component]
    ) -> None:
        self._by_gateway[mac] = keypad_catalogue(mac, components)
        self._rebuild()

    def forget(self, mac: str) -> None:
        """Drop a gateway's keypads, when its entry is unloaded."""
        if self._by_gateway.pop(mac, None) is not None:
            self._rebuild()

    def _rebuild(self) -> None:
        merged: dict[str, dict] = {}
        for keypads in self._by_gateway.values():
            merged.update(keypads)
        self.keypads = merged
