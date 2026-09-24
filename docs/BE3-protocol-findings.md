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

## Writing a personality restarts the panel

Confirmed on site (2026-09-21): when a component's configuration is written —
`setBtn`, or the dimmer and shade equivalents — **the panel reboots**. It leaves
the bus for a few seconds and comes back.

Three consequences:

* **The restart is the receipt.** The gateway acknowledges no configuration
  message, so the address disappearing and returning is the only evidence the
  write was accepted. A restart that happens between two heartbeats is invisible,
  so its absence proves nothing.
* **Anything sent during the restart is lost**, including LED writes.
* **Backlights come back off**, so assumed LED state must be discarded rather
  than shown as if it survived.

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

## One panel, several components — and they are not interchangeable

Found on site (2026-09-21) while changing a component's personality.

The SUBLIME Pro on the bench spans **three** CoTP addresses, not two:

| Address | What it is |
|---|---|
| 16 | the button block — reports `btnEvt` |
| 24 | a screen slot |
| 63 | a second screen slot |

The panel's screen shows **a page per configured screen slot**, and its
bottom-left button cycles between them. Configure 24 as a shade and 63 as a
dimmer and the screen offers both, by name. That is what the third address was
for — it produced no traffic in the original capture because nothing had been
configured on it.

**The components are different hardware.** A button block can be a keypad; a
screen slot can be a dimmer or a shade. Nothing in the protocol says which is
which — the address list is just numbers — but each component says so itself by
what it reports.

Telling a screen slot it is a keypad writes a configuration it cannot use, and
its page stops working until the right type is written back. Since the protocol
acknowledges nothing, there is no error to catch: the panel simply goes quiet.
The only guard available is the component's own traffic, which is why
`learning.contradicts` treats that combination as a category error rather than
a change of mind. Swapping a dimmer for a shade is fine — both are screen
slots.

## Pages cannot be removed, and clearing does nothing visible

Tested on the bench (2026-09-22) against firmware 0.10.

A screen showed four pages left over from earlier configurations. Every page
number was cleared with `clrCfg`, for both screen types — eight clears in all,
confirmed sent:

```
Clearing dim configuration on component 24
Clearing cn configuration on component 24
...
```

Afterwards:

* **the pages were still on the screen**, with their old names, and
* **the cleared page kept polling its links**, fourteen times in the following
  minute, quoting the same device and attribute ids as before.

So on this firmware `clrCfg` has no observable effect at all. It does not
remove a page, and it does not detach the page's links.

Two consequences for anything built on this protocol:

* **A page is permanent once created.** Deleting it in a controller can only
  ever mean forgetting it; the panel keeps showing it and keeps asking. An
  integration should say that plainly rather than implying removal.
* **Writing a configuration restarts the panel, but clearing does not.** A
  component polled straight through its neighbours being cleared, so a
  sequence of clears needs no settling time — waiting for a restart that never
  comes cost 45 seconds per clear.

### How Control4 lives with it: by hiding its own fields

Asked directly of the driver, which has exactly seven places where it sends
anything to the gateway — LED, identify, OTA start, set-address, clear, config
save, value response. None of them removes or hides a page.

The property that looks like it should is `Number of Devices`:

```lua
elseif (strProperty == "Number of Devices") then
    InitProperties()   -- only SetPropertyVisiblity(...); nothing is sent
```

Reducing it hides that device's fields in Composer and tells the panel nothing.
So a Control4 installer with too many pages is in the same position as anyone
else: the pages stay on the wall, and the driver simply stops showing the
fields that configure them.

An integration's honest options are therefore to **manage** a page or to
**stop listing** it, which is what deleting one does here. The only lever over
the wall itself is cosmetic: a configuration's `name` becomes the panel's
label (`NM="..."`, ten bytes), so a leftover page can be relabelled to
something that does not promise a function — at the cost of a write, which on
this hardware is the operation to avoid.

### The clear we send is the clear Control4 sends

Worth ruling out before blaming the firmware: our payload is byte-identical to
the Control4 driver's. The driver builds `clrCfg` with `devMold`,
`devCotpAddr`, `devIdx` and `name`, and the gateway turns all of it into
`ID=<idx>;MD=<mold>;` for objmsg `#C4C` — `name` is read for writes and
ignored for clears, and `devIdx` is concatenated, so the driver passing it as a
string and us passing an integer produce the same bytes. Whatever makes CLEAR
work in Control4, it is not the message.

### A clear survives a power cycle of the gateway

The last explanation left was that a clear is stored and applied at a restart,
since writing restarts a panel and clearing does not. Tested on 2026-09-22:
with eight clears already sent to address 24, the installation was powered down
and back up, and all four pages came back with their names.

Read the wire carefully before concluding much from this, though. What the log
shows is the bus going quiet for 42 seconds, our idle watchdog dropping the
socket at 30 seconds, and the gateway reconnecting 1.5 seconds later — which is
the signature of the **gateway** losing power, not the panel: a hard power cut
leaves our side holding a half-open socket, and the gateway gets in the moment
we let go of it. Heartbeats arrive minutes apart, not per poll, so their absence
during the gap says nothing either way.

So what is established is that a clear does not survive a gateway power cycle.
Whether a panel's own reboot applies one is still open, and needs the panel
powered down specifically.

### What the vendor's driver actually does about it

Its CLEAR blanks the link *properties* in Control4 and sends the inert
`clrCfg`. Nothing in the driver removes a page, which is consistent with what
the hardware does: a later save writes the emptied links back out as `LL=;`,
and the firmware accepts them, because in Lua `""` is truthy and the link is
passed through unchecked. Control4 cannot delete a page either — it can only
stop pointing one at anything.

### An empty link is ignored, so blanking is not a workaround either

Tested on 2026-09-22 against address 24 page 1: a dimmer configuration with
`dimBrightnessLink` empty was sent and accepted by the gateway without error,
and the panel did not react at all — no restart, and the page kept its name.
Every configuration that carries a real link restarts the panel, so the panel
is evidently dropping the empty one rather than storing it.

Which leaves exactly one operation that changes a page: **writing a valid
configuration to that page number**. A leftover page can be taken over, renamed
and pointed somewhere harmless, but it cannot be emptied and it cannot be
removed. `build_blank_config` is kept for the record and for other firmware; it
is not a way to clean a panel.

**Questions for LifeSmart:** how is a page removed from a SUBLIME panel, and
what is `clrCfg` (objmsg `#C4C`) meant to do? Their own driver offers CLEAR as
a per-device action, which suggests it is meant to do something.

## Send every field the vendor's driver sends

The gateway branches on a key being *present*, not on its value:

```lua
if r_data.dimColorTemperatureLink then
    msg = msg..';WL='..r_data.dimColorTemperatureLink..';WM='..r_data.dimlimC
end
```

Control4 always includes `dimColorTemperatureLink` and `dimlimC`, empty string
and all, so a panel in a Control4 install always receives the `WL`/`WM` clause.
Leaving the keys out produces a shorter message the hardware never sees from
its own controller — and the one configuration written that way, turning colour
temperature off on 2026-09-22, was silently ignored: the page kept the name and
properties it already had.

So this builds the vendor's shape exactly, including the fields it has nothing
to say about. Two related cautions from the same afternoon:

* **A write is not acknowledged.** No restart is guaranteed either — a panel
  polled straight through one write and rebooted for another — so nothing in
  the protocol confirms that a configuration was applied. The only evidence is
  the panel's own screen.
* **Writes are not free.** Beyond the wedge described above, a panel stopped
  responding altogether about fifty seconds after its third write in half an
  hour, leaving a black screen and dropping off the heartbeat until it was
  power cycled.

## The panel counts taps itself

Under the press type code (`0x4f`) the **value is a tap count**: `1` for a tap,
`2` for a double tap. Recorded on 2026-09-22 with a button tapped once, twice,
three times and held:

| Gesture | Events |
|---|---|
| tap | `0x4f`/0 then `0x4f`/1 |
| double tap | `0x4f`/0 then `0x4f`/**2** |
| hold | `0x4f`/0, `0x4f`/255, then `0x4e`/0 |

This was missed for a day because the capture the protocol layer was written
from contains only values 0, 1 and 255 — nobody double-tapped while it was
recording — so `2` was being discarded as unknown. The visible symptom was
subtle rather than absent: a double tap published `press` with no release, and
the press that opened the *next* gesture looked like its completion.

Two consequences:

* **Nothing needs timing.** The panel decides what a double tap is, and reports
  a hold with its own type code, so a controller does not synthesise gestures
  from press/release intervals. Buttons Machine is told both are native.
* **There is no release after a multi-tap.** The gesture is complete when it is
  counted, so anything expecting a press/release pair has to supply the release
  itself.

A triple tap did not produce `0x4f`/3 in that test — three taps were read as a
slow single instead, so either the panel counts only to two or the taps were not
fast enough. A count of 3 or more is decoded as a triple tap, untested.

## A page polls once it has been shown, and not before

Observed on 2026-09-22 while taking over four pages on address 24. Straight
after a restart the panel polled for one page only — the one on the screen —
and said nothing about the others. Once someone cycled through all four with
the top-left button, **all four polled continuously**, and kept polling while a
different page was displayed. So a page is dormant until it is first shown
after a restart, and awake from then on.

Two consequences. **Silence is not evidence that a page does not exist**: a
page nobody has visited since the last restart is invisible on the wire, which
is what made pages 1, 3 and 4 look absent for most of a day. And **a write is
enough to hide a page again**, because it restarts the panel back to a single
displayed page. Page numbers in use can therefore only be known from what was
written, never from listening.

One more thing the four pages showed: **colour temperature is never polled.**
Every page with a colour link polled brightness (`1001`) and the shade polled
level (`1003`), but `1002` was never asked for, even on pages configured with
it. The panel evidently reads it only while its colour property is on screen.

## Writing a configuration can wedge the gateway

Two of four writes on 2026-09-22 left the gateway in the state described under
the wedge above: within about seven seconds of the write it stopped sending
anything at all — no heartbeats, no bus traffic — while continuing to accept
TCP connections. Both needed a power cycle. The writes themselves were
accepted: the panels restarted and kept their new configuration.

Notably the silence began *before* anything on our side dropped the socket, so
the idle watchdog is a symptom and not the cause. Consequences for anything
built on this protocol:

* **Do not write a configuration that has not changed.** The panel already
  holds it, and the write costs a restart and risks the gateway.
* **Expect to pace writes**, one page at a time, and to tell someone that only
  power recovers a wedged gateway.

## A screen holds many pages, one per slot

A component's screen shows a page for each configuration written to it, and
its top-left button cycles between them. The page is identified by the slot
(`devIdx`, `ID=` in the configuration message), and the panel quotes that slot
back as `idx` in every read and write it makes — so routing a message to the
right page is exact, not guesswork.

That makes a page **(address, slot)**, not an address. One screen can offer a
light on one page and a shade on another, and LifeSmart indicate a panel can
hold ten or more.

Two consequences:

* **Slots are per address.** Two panels may both use slot 1, because the slot
  is only ever quoted alongside the address.
* **Clearing must be per slot.** Wiping "everything this component is not" for
  an address would take its other pages with it, which is why the clear sent
  before writing names the slot.

A page left behind is what produced the original confusion here: a screen
showed a dimmer page and a shade page at once, from two configurations written
to the same slot at different times, because nothing cleared the first.

## Not every address on the bus is a device

The heartbeat's address list contains more than the components an installer
can use. On the bench it reads `16,24,63,61`:

| Address | What it is |
|---|---|
| 16 | the panel's button block |
| 24 | the panel's screen |
| **63** | **not a component** |
| 61 | the gateway's own CoTP address (always last) |

Address 63 has never sent a single message — no button event, no value write,
no poll — in any capture. Identify produces no flash, and configurations
written to it change nothing and restart nothing. LifeSmart say it is the
gateway's own WiFi module; that has not been independently confirmed, but the
behaviour stands on its own whatever the cause.

So excluding the last entry is not enough: **the gateway can appear in its own
address list more than once**, and the extra entry is indistinguishable from a
component until something is asked of it. An integration that treats every
address as a device will show a phantom on every site, and an installer will
reasonably spend time trying to configure it — writing to it looks like it
works, because nothing acknowledges anything.

There is no known way to tell these apart from the protocol, so the practical
answer is to let an installer mark an address as not a device, which hides it
and stops writing to it.

**Question for LifeSmart:** is there a rule for which addresses belong to the
gateway rather than the bus, or a fixed relationship to its own address?

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
