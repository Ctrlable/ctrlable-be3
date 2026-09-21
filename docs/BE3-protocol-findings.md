# BE3 protocol — findings from a live capture

Captured 2026-09-21 against a LifeSmart BE3 (`ESPSUBLIMEBE3`, MAC `58:e6:c5:71:3c:38`,
firmware **0.10**) at 172.16.0.251, with the gateway connected to a laptop acting as the
controller. The laptop only listened; it sent no commands to the gateway.

Reference documents: *BE3 Control Interface Specification* (written against firmware 1.0.5)
and the Control4 SUBLIME driver.

## Corrections to the specification

### 1. Button event values are inverted in the spec

The spec's `ty`/`val` table says `0x4f`/`val=1` means Press and `val=0` means Release.
The captured order is the opposite. Every press begins with `val=0`:

| Gesture | Sequence observed |
|---|---|
| Short tap (~0.4s) | `ty=0x4f val=0` → `ty=0x4f val=1` |
| Medium hold (~1.0s) | `ty=0x4f val=0` → `ty=0x4e val=0` |
| Long hold (~2s) | `ty=0x4f val=0` → `ty=0x4f val=255` → `ty=0x4e val=0` |

Actual meanings:

- `0x4f` / `0` — **button down**, always first
- `0x4f` / `1` — **released quickly** (a click)
- `0x4f` / `255` — **hold threshold reached**, about 1s after button down
- `0x4e` / `0` — **released after a hold**

The Control4 driver is consistent with this: it fires its programming event on `val=0`
(button down) and issues `DO_CLICK` on `val=1`.

### 2. The `addresses` list ends with the gateway's own address

The spec describes a comma-terminated list of device addresses. The gateway actually sends
`"16,24,63,61"` — no trailing comma, and the **last entry is the BE3's own COTP address**
(`md.cotpsrcAdr()` in its firmware), not a device. Treating the whole list as devices
invents a phantom device.

### 3. Heartbeat interval is ~5s, not ~10s

Heartbeats arrived every 5 seconds (firmware sends on a 300-tick timer at 33ms). A 30s
idle timeout remains a reasonable rule.

## Confirmed by the capture

**Brightness encoding.** The spec's formula `(val - 0x300) / 0x10000` is exactly right.
Captured values decode to clean multiples of 5, matching the panel's configured step:

```
4917760 → 75    3934720 → 60    2951680 → 45    1968640 → 30
4590080 → 70    3607040 → 55    2624000 → 40
4262400 → 65    3279360 → 50    2296320 → 35
```

So `val = brightness * 0x10000 + 0x300`.

**On/off.** `ty=0x81` (129) is ON and `ty=0x80` (128) is OFF, as documented.

**Message framing and discovery** behave as specified: `C4Z-SEARCH * PORT:<port> \r\n` on
UDP 12345, gateway connects back, line-delimited JSON with `\r\n`.

## New behavior not in the spec

- **The gateway opens two TCP connections at once** on connect. An integration must
  tolerate duplicates instead of assuming a single socket.
- **A panel configured as a dimmer never sends `btnEvt`.** Its buttons arrive as `setVal`
  commands carrying the linked device's id. Raw button events come only from panels
  configured as button type. Address 16 sent `btnEvt`; address 24, configured as a dimmer
  by the Control4 project, sent `setVal` for the same physical presses.
- **Panels poll for state continuously** with `cmdId=5` (GETVAL) every ~8s per configured
  device, with `ty` varying (`0` then `60`/`0x3c`). An integration must answer these or the
  panel's own indicators never reflect reality.
- **A stale controller holds the gateway indefinitely.** Removing the controller's driver is
  not enough: with no keepalive of its own, the gateway keeps its half-dead socket and
  ignores all discovery. Only a power cycle released it.
- **`devinfo` is sent on the existing connection** whenever any discovery broadcast is heard,
  so it doubles as a signal that another controller is hunting for the gateway.

## Site observations

- The panel on the bus is a **SUBLIME Pro 6**. It occupies **two COTP addresses**:
  **16** is its 6-button component, and **24** is its small-screen component, configured as a
  dimmer linked to Control4 device 797 / attribute 1001. **63** produced no traffic and is
  unidentified. The gateway itself is **61**.
- This matches their Composer guide: a SUBLIME Standard panel counts as one device, a
  SUBLIME Pro (with the small screen) counts as two. **One physical panel can therefore span
  several COTP addresses**, and an integration must group them into a single device rather
  than presenting each address separately.
- A Control4 controller at **172.16.0.201** was still broadcasting `C4Z-SEARCH` every 5
  seconds throughout. It will reclaim the gateway once our listener stops.

## Addressing

LifeSmart confirmed (2026-09-21) that **keypads ship pre-addressed**, and that
re-addressing is only needed when two components on the same bus collide.

Two consequences follow, neither of them obvious:

**A conflict is invisible in the protocol.** The gateway derives its address
list from a bitmap — one bit per address — so two components sharing address 24
set the same bit and appear as a single `24`. No amount of parsing reveals the
duplicate. It has to be found physically: `IND` makes a component flash, and
two panels flashing at once is the only reliable signal. A component count that
falls short of the panels actually installed is the other hint.

**Re-addressing cannot separate a colliding pair.** `setaddr` is delivered *to*
an address, so both components answer it and both move, preserving the conflict
at the new address. Resolving a collision means isolating one component first —
disconnect or power down one, move the other, then restore it. An integration
must say this plainly, because the obvious action makes no visible difference
and looks like the command failed.

Still unconfirmed: whether a factory-addressed panel reports button events
before any `setBtn` provisioning, or whether bindings must be written first.
Pre-addressed means it has an address, not that its buttons are bound to
anything. Address 16 in the capture had been provisioned by Control4, so it
cannot answer this question.

## Implications for the Ctrlable Pro integration

1. Parse button events with the corrected state machine above; expose click, long press,
   and release.
2. Drop the last entry of `addresses` (the gateway's own address), and treat the rest as
   candidate components whose type the installer confirms. Let the installer group several
   addresses into one physical panel, since a SUBLIME Pro spans two.
3. Answer GETVAL polls promptly from Home Assistant state, or panels will show stale status.
4. Accept and reconcile multiple inbound connections from the same gateway.
5. Migration from Control4 requires rewriting each panel's bindings, since panels keep their
   Control4 device ids until reconfigured. Plan a "re-provision panel" step.
6. Document the power-cycle requirement when moving a gateway between controllers.
