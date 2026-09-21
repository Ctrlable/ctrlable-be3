# Resolving an address conflict

Keypads ship pre-addressed, so most sites need no re-addressing at all. When
two components on the same bus happen to arrive with the same address, this is
the procedure — it is physical work, and there is no way around that.

## Why it cannot be done from software

Two limits in the design, both of which make the obvious approach fail
silently.

**A conflict is invisible.** The gateway reports the components it can see as a
bitmap — one bit per address — and sends the set bits as a list. Two components
sharing address 24 set the same bit, so they arrive as a single `24`. Nothing
in the protocol distinguishes one component from two.

**Commands cannot single one out.** Every command is delivered *to* an address.
A `set_address` aimed at 24 reaches both components, and both obey, so the pair
moves together and the conflict survives at the new address. Repeating it just
moves them again.

So the only way to separate a pair is to make the bus contain one of them.

## Symptoms

Suspect a conflict when:

- the gateway reports fewer components than there are panels installed
- **Identify component** makes **more than one panel flash**
- a keypad appears to work, but presses arrive from an address you did not
  touch, or two panels respond to one press

The identify test is the reliable one. The others can have different causes.

## Procedure

1. **Confirm the conflict.** Press the **Identify** button on the component
   and watch the panels. More than one flashing confirms it; note which ones.
   For an address that is not configured yet, use the **Identify component**
   service, which takes any address.

2. **Isolate a single component.** Disconnect every flashing panel except one,
   by unplugging its bus connection or cutting its power. Only one of the
   colliding components may remain on the bus.

3. **Check the gateway agrees.** Identify the address again: exactly one panel
   should flash. If more than one still does, something is still connected.
   This check is worth repeating rather than trusting — re-addressing a pair
   moves both and undoes the isolation.

4. **Move the remaining one.** Call **Change component address**, choosing an
   address that is free. The service refuses a target already in the gateway's
   address list, including the gateway's own address, and confirms the change
   by waiting for the component to reappear at its new address.

   If it reports that the component never reappeared, power-cycle the panel and
   check the address list before retrying — the command may have applied even
   though the confirmation did not arrive.

5. **Reconnect the others**, one at a time. After each, check the gateway's
   address list. Each panel you restore should add its address; if the count
   does not go up, that panel is colliding too, and it goes through the same
   steps.

6. **Re-provision if needed.** A component that was configured as a keypad
   keeps its bindings across a re-address, but the integration's entities are
   tied to the old address. Remove the old component in the integration's
   options and add it again at the new address.

## Avoiding it

- Before installing panels, power them one at a time and note the address each
  reports. Collisions found on a bench cost minutes; the same collision behind
  a wall plate costs an afternoon.
- Keep a record of which address is which panel. The protocol offers no names,
  serial numbers or model strings — an address is all a gateway ever reports.
