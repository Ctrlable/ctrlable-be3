# Ctrlable BE3

Home Assistant integration for **LifeSmart SUBLIME** control panels, reached
over their **BE3 CoTP network adaptor**.

Panels are discovered from the gateway itself: every component on the bus
becomes a device, and each device carries its own settings page and an identify
button so an installer can tell which panel is which.

> **Early release.** The protocol layer is verified against real hardware; the
> Home Assistant layer has had limited field testing. Please report what breaks.

## Install

**HACS** → ⋮ → *Custom repositories* → add `Ctrlable/ctrlable-be3`, category
*Integration* → install → restart Home Assistant.

Then **Settings → Devices & services → Add integration → Ctrlable BE3**.
Gateways on the network are found automatically.

## What you get

Every component the gateway reports becomes a device, whether or not anyone has
said what it is yet. Each device has:

- an **Identify** button, which makes the panel flash — the only way to tell
  which physical panel an address belongs to
- a **Configure** page for its type, button count, name and panel grouping

Components configured as keypads also get:

- an **event entity per button**, firing `press`, `click`, `hold` and
  `hold_release`
- a **backlight switch per button** (disabled by default, assumed state — the
  gateway accepts LED commands but never reports LED state)

Button actions are also published on the event bus as
`ctrlable_be3_keypad_event`, carrying `keypad_id`, `address`, `button` and
`action`.

## Services

| Service | What it does |
|---|---|
| `identify` | Flash a component so it can be located |
| `set_address` | Move a component to a different bus address |
| `configure_keypad` | Provision a component as a keypad |
| `clear_configuration` | Clear a component's stored configuration |
| `start_update` | Start a pending gateway firmware update |

## How it works

The roles are reversed from most integrations: **Home Assistant is the TCP
server**. While no gateway is connected it broadcasts a search datagram on UDP
12345, and the gateway connects back to port 21324.

A gateway accepts **one controller at a time**, and ignores discovery while it
believes it has one. Moving a gateway from another controller therefore needs
that controller removed *and* the gateway power-cycled — it has no keepalive of
its own, so it will otherwise hold a dead connection indefinitely.

## Notes from the hardware

- A SUBLIME Pro uses **two bus addresses** — one for its buttons, one for its
  small screen — so it appears as two devices.
- Keypads ship pre-addressed. If two collide, the conflict is invisible in the
  protocol and cannot be fixed by re-addressing alone: see
  [docs/address-conflicts.md](docs/address-conflicts.md).
- The vendor specification is wrong in three places; the differences are
  documented in [docs/BE3-protocol-findings.md](docs/BE3-protocol-findings.md).

## Support

Issues: https://github.com/Ctrlable/ctrlable-be3/issues
