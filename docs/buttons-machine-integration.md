# Feeding Buttons Machine

Buttons Machine is the destination for this integration: `ctrlable_be3` is the
transport, and a keypad backend inside Buttons Machine turns SUBLIME button
presses into configurable actions.

This is the contract between the two, established by reading
`buttons-machine-src` at version 5.12.46.

## Division of labour

`ctrlable_be3` owns the gateway, the bus, and the hardware truth. It must
expose three things the backend can consume:

1. **A bus event per button action**, carrying the keypad identity, the button
   number and the action.
2. **LED entities with a parseable unique id**, so the backend can map button
   numbers to entities without being told.
3. **A discoverable keypad registry** in `hass.data`, so Buttons Machine's panel
   can offer SUBLIME keypads when someone adds one. The Control4 integration
   does this with `hass.data["ctrlable_control4"]["_keypad_registry"]`.

The backend itself lives in `buttons-machine-src`, not here.

## Gestures: why we do not use the native path

The controller has two modes, selected by two flags on the backend:

```python
if self._backend.native_hold or self._backend.native_double_tap:
    self._handle_native_button(btn_num, btn_cfg, action)
    return
```

Either flag set to true routes **every** action through the native path, where
the controller dispatches only what the backend sends it. A backend that claims
native hold but never emits `double_tap` therefore loses double tap entirely.

With both flags false the controller runs its own timing state machine
(`_HOLD_CONFIRM = 0.30s`, `_PRESS_DEBOUNCE = 0.20s`,
`_DOUBLE_TAP_WINDOW = 0.40s`) and derives hold, double tap and triple tap from
press/release timing. It needs a real release at finger-lift to do so.

**The BE3 provides exactly that**, so this backend sets both flags false:

| BE3 wire event | Sent to the controller |
|---|---|
| `0x4f`/0 (button down) | `press` |
| `0x4f`/1 (quick release) | `release` |
| `0x4e`/0 (release after hold) | `release` |
| `0x4f`/0xff (hold threshold, ~1s) | nothing — see below |

The gateway's own hold signal is deliberately **not** forwarded. The controller
is already timing the press, and a second hold signal would double-fire. It
remains useful on the HA event bus for people automating directly against the
integration.

This matches the Control4 backend, which also sets both flags false for the
same reason.

## The backend contract

From `backends/base.py`:

```python
class KeypadBackend(ABC):
    source_domain: str = ""
    license_product: str = "buttons_machine"
    native_hold: bool = False
    native_double_tap: bool = False

    async def async_initialize(self, hass, controller) -> None: ...   # optional
    @abstractmethod
    def subscribe(self, hass, controller) -> Callable[[], None]: ...
    @abstractmethod
    async def async_write_led(self, hass, led_entity: str, is_on: bool) -> None: ...
    @abstractmethod
    async def async_find_leds(self, hass, config_entry) -> dict[int, str]: ...
```

Notes that matter:

- `subscribe` is **synchronous** and returns an unsubscribe callable. It hands
  each press to `controller.handle_button(int(button), action)`.
- `controller.serial` is `entry.data["device_serial"]` — the backend filters
  events by comparing it to the keypad identity in the event payload. That
  identity is `<mac>-<address>-<page>`: a component can hold several pages,
  so the address alone does not identify one.
- A button that is not configured in Buttons Machine is dropped silently, so a
  keypad that appears to do nothing is usually an unconfigured button.
- `async_find_leds` returns `{button_number: handle}`, and the handle is opaque:
  it is passed back to `async_write_led` verbatim. Most backends use entity ids;
  some use the button number as a string. We use LED switch entity ids, since
  they are real entities users can see.

## Licensing

Each backend names the license product it requires:

```python
license_product = "buttons_machine_be3"
accepted_products = ("buttons_machine_be3", "buttons_machine")
```

The gate is hard: `async_setup_entry` returns False when no accepted product
validates. Three other places in `_impl.py` hardcode product strings and need
the new one — the license modal's labels, `_catalog_accepted`, and the
`_family_licensed` check that decides whether a family appears in keypad
discovery.

## Work items in buttons-machine-src

1. `backends/be3.py` with the class above.
2. `backends/__init__.py`: import, `_BACKENDS["be3"]`, `__all__`.
3. `config_flow.py`: a source probe, a discovery step and a source label;
   plus the matching `config.step.be3` block in `strings.json`.
4. `_impl.py`: a device block in the panel's `discover_keypads`, a
   `serial.startswith("be3_")` branch in `add_keypad`, the `_family_licensed`
   gate, and the license label entries. **Without the serial-prefix branch a
   keypad is silently created as a generic Lutron keypad.**
5. `const.py`: only needed if we ever ship without explicit `button_numbers`.


## How a panel's own screen works

Observed on a SUBLIME Pro's small screen, and confirmed against the vendor's
driver.

A component configured as a dimmer shows a property page. Its top-left button
cycles the property — Power, Brightness, Colour Temperature — and the remaining
buttons act on whichever is selected: on/raise, off/lower. All of that happens
**on the panel**. A controller never sees the selection, only the write it
produces.

What a controller does decide is **which properties exist**, and it does so
purely by how many links it configures:

```
ID=<idx>;MD=dim;LL=<brightness link>;LM=<min,max,step>;WL=<colour link>;WM=<limits>
```

`LL` gives it a brightness page, `WL` a colour-temperature page. Configure one
and the selector offers one property; configure both and it offers both.

The reason this matters for decoding: **brightness and colour temperature are
written with the same type code** (`0xE3`). Only the link quoted back
distinguishes them. So a link is not merely a destination, it is the address of
a *property* — which is why `links.py` maps each link to a role rather than to
an entity alone.

Reads work the same way. The panel polls each link so its screen can show the
real value, and a controller answers per link.

**Open question for LifeSmart:** panels appear to offer full colour as well as
colour temperature, but the protocol documents only these two dimmer links. If
colour is reachable, it needs a link type that is not in the specification.


## Done: the backend (2026-09-22)

`buttons-machine-src/custom_components/buttons_machine/backends/be3.py`, wired
through the registry, the discovery list, the add-keypad branch, the config
flow, the licence tables and the panel badge. What this integration has to keep
stable for it:

* **`ctrlable_be3_keypad_event`** with `keypad_id`, `button`, `action`. The
  backend filters on `keypad_id` matching the keypad's stored serial.
* **The serial**, `<mac>-<address>-<page>`, from `keypad_id()`. It is stored in
  the Buttons Machine entry, so changing its shape breaks keypads already
  configured.
* **LED unique ids** of `be3-<serial>-led<button>`, which is how the backend
  finds a keypad's LED switches from the serial alone.
* **`hass.data["ctrlable_be3"]["_keypad_registry"]`**, an object with a
  `keypads` mapping, one entry per button block, each carrying `keypad_id`,
  `keypad_name` and `buttons`. Screen pages are deliberately absent.

Gestures map `press → press`, `click → release`, `hold_release → release`, and
`hold` is dropped. The panel reports hold natively, but Buttons Machine routes
every event down its native path as soon as a backend claims any native
gesture, and that path only knows what the backend sends it — so claiming hold
would lose double and triple tap, which this hardware cannot report at all.
Forwarding press and release lets the controller derive all three.

One wrinkle worth knowing: a keypad's button count is learned from its own
traffic and starts at one, so a keypad added to Buttons Machine before every
button has been pressed carries fewer buttons than the wall has. Press them
all, then add it.
