"""What has been written to each page, kept out of the config entry.

This is bookkeeping, not configuration: nothing an installer sets, and nothing
that should reappear in a form. Keeping it in ``entry.options`` looked harmless
until the cost showed up on real hardware — updating an entry reloads it, a
reload drops the gateway's session, and reconnecting is when this firmware's
bus loop tends to stop, which only a power cycle recovers. So it lives in its
own store, where recording a write costs nothing.
"""

from __future__ import annotations

import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

#: Bumped only if the shape below changes.
STORE_VERSION = 1

#: Long enough to batch a burst of pages, short enough to survive a restart.
SAVE_DELAY = 5.0

#: How long a record is trusted. Nothing in this protocol acknowledges a write:
#: there is no reply, and a restart does not reliably follow one, so "we wrote
#: this" is a belief rather than a fact. A panel that quietly ignored a write
#: would otherwise keep its old configuration for good, because the save that
#: would fix it looks redundant. Trusting the record briefly still spares a
#: panel the repeated writes of one editing session — which is what it was for
#: — and after that a save reaches the hardware again.
TRUSTED_FOR = 900.0


class WriteRecord:
    """The last configuration written to each page of each component."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, dict[str, object]]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.writes"
        )
        self._written: dict[str, dict[str, object]] = {}
        self._dismissed: set[str] = set()

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        if "writes" in stored or "dismissed" in stored:
            writes = stored.get("writes") or {}
            self._dismissed = set(stored.get("dismissed") or [])
        else:
            # The first shape of this store held writes at the top level.
            writes = stored
            self._dismissed = set()
        # Entries written before records carried a time are not trusted: an
        # unconfirmed write from an unknown moment is exactly what this guards
        # against.
        self._written = {
            key: value for key, value in writes.items() if isinstance(value, dict)
        }

    def last_written(self, address: int, slot: int) -> str | None:
        """What was written to this page recently enough to still believe."""
        record = self._written.get(f"{address}:{slot}")
        if not record:
            return None
        written_at = float(record.get("at", 0.0))
        if time.time() - written_at > TRUSTED_FOR:
            return None
        return str(record.get("signature", "")) or None

    def record(self, address: int, slot: int, signature: str) -> None:
        """Remember a write that has gone out, and when."""
        self._written[f"{address}:{slot}"] = {
            "signature": signature,
            "at": time.time(),
        }
        self._save()

    def dismiss(self, address: int, slot: int) -> None:
        """Stop listing a page that was deleted here.

        The panel keeps it — nothing removes a page — so it goes on polling
        and would otherwise be adopted straight back, which makes deleting
        look broken. Dismissed means "we know it is there and we are not
        managing it"; adding the page again picks it back up.
        """
        self._dismissed.add(f"{address}:{slot}")
        self._save()

    def is_dismissed(self, address: int, slot: int) -> bool:
        return f"{address}:{slot}" in self._dismissed

    def restore(self, address: int, slot: int) -> None:
        """Manage this page again, after it was dismissed."""
        key = f"{address}:{slot}"
        if key in self._dismissed:
            self._dismissed.discard(key)
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "writes": dict(self._written),
                "dismissed": sorted(self._dismissed),
            },
            SAVE_DELAY,
        )

    def forget(self, address: int, slot: int) -> None:
        """Drop a page's record, so the next save writes to the panel again.

        Used when a page is deleted here: the panel keeps it, and whoever takes
        it over next needs their configuration to be sent rather than skipped.
        """
        if self._written.pop(f"{address}:{slot}", None) is not None:
            self._save()
