# eufy-homebase-card

One card per eufy HomeBase (`eufy_home_security` integration). It shows the security mode and switches it, draws
the HomeBase with its paired devices and what each does in a mode, and has sections for the devices' actions per
mode, the schedule, the arming groups, the HomeBase's settings (cross-camera tracking included) and its status. The
custom modes are edited from the header menu.

## Install

The card comes with the integration, next to the camera card ([card.md](card.md) § Install): the integration
serves `/eufy_home_security/eufy-homebase-card.js` and keeps its dashboard resource up to date. Add it: edit a
dashboard → **Add card** → **eufy HomeBase**, or a card with `type: custom:eufy-homebase-card`. Resources in YAML
mode: add `/eufy_home_security/eufy-homebase-card.js` as a `module` resource.

## Options

```yaml
type: custom:eufy-homebase-card
device_id: 0123456789abcdef0123456789abcdef   # the HomeBase device (the editor's HomeBase picker)
name: House                                  # optional: the header name (default: the HomeBase's name)
```

- Instead of `device_id`, `entity: alarm_control_panel.<homebase>` (the HomeBase's alarm panel) works too.
- The editor lists the integration's devices that have an alarm panel. With one HomeBase it is picked when the
  card is added.
- A standalone camera that is its own station works the same way (its own device id).
- Colours come from the HA theme, light or dark: the mode button, the HomeBase ring and the lens dot use HA's
  alarm-panel state colours.

## What the card shows

- **Header:** a round button coloured by the security mode (a calendar badge under Schedule), the name, and one
  status line: `Away · 4 online`, `Schedule · Home · 1 offline` (Schedule and the mode in force now), `Alarm!`.
  - The button opens a menu inside the card: the modes the HomeBase can be set to, by their names (custom modes
    with the names given in the eufy app or on the card), the arming groups, **Edit modes**, **Sound alarm 30 s**
    (**Stop alarm** while it sounds), **Restart** and **Refresh**. Picking a mode, a group, the alarm or Restart
    only stages it; Set sends it.
  - **Edit modes** opens the custom modes over the drawing: each one's name (edit it to rename the mode) and a bin
    to delete it, and **+ Mode** to add one (up to three). The current mode cannot be deleted; when the HomeBase
    refuses a delete (a mode the schedule uses), the row shows its reason and the bin stays off. Names follow the
    HomeBase's rules (up to 30 characters, not used by another mode): a refused name shows the HomeBase's message.
  - **Sound alarm**, **Stop alarm** and **Restart** show as `Not available yet` while the integration cannot send
    them on this HomeBase.
  - **While the alarm sounds** a red **Disarm** button and a **Stop alarm** button (a bell icon on narrow cards)
    sit in the header. Both act at once (no staging): Stop alarm stops the siren, Disarm disarms. They are the
    only controls on the card that act without Set.
- **Drawing:** the HomeBase in the middle, its ring in the mode's colour (it breathes while the alarm sounds),
  the paired devices around it. Under each device: what it does in the shown mode (record, camera alarm,
  HomeBase alarm, notification, light alarm, monitoring center, sensor response; the alarm actions in red) and its
  entry delay (`⏱30s`); an offline device is dimmed and reads `Offline`, a battery under 30 % gets a red battery
  icon. While the alarm sounds, the links of the devices that sound the HomeBase alarm carry moving dots (none
  with reduced motion).
  - The **lens** under the drawing shows another mode's behaviour (the mode in force has a dot); it changes
    nothing on the HomeBase. When the mode names do not fit, it becomes `‹ Away ›`. With a schedule, **Schedule**
    in the lens shows the week: one row per day, each mode in its own colour with a legend (a staged schedule
    shows as staged; edit it in the Schedule section).
  - **Many devices:** up to 6 devices fit around the HomeBase below 460 px card width, 8 from there. More devices
    are on further pages: `‹ ›` and page dots at the top of the drawing (arrow keys work on them). The card
    height does not change between pages.
  - A device opens its actions in Devices; the HomeBase opens Status.
- **Sections** under the drawing (one open at a time; the toggles stay where they are; on narrow cards they are
  icons and the open section's name is its first line):
  - **Devices:** one device at a time (the picker at the top lists every device, so nothing needs paging): its
    actions × the modes that have a table (on narrow cards the lens's mode only), then the entry and leaving
    delay per mode. A cell switches an action; a delay cell chooses the delay shown in the row below, which has
    the device's switch (whether this device waits) and the mode's delay slider. The delay is one value per mode
    for every device that uses it, as in the eufy app.
  - **Schedule:** the week (one row per day, 24 h, each slot a bar in its mode's colour; the mode outside the
    slots fills the rest), **Outside the slots** (the default mode), and one slot form under the week: the slot
    list (**New slot** or an existing slot; pressing a bar picks it too), the days, start, end and mode, and
    **Add** / **Save** / **Delete**. Times follow the HA profile's time format; a slot that runs to midnight ends
    at `24:00` (the last End choice). A slot that overlaps another on
    one of its days, or ends before it starts, is refused in the form (Add stays off and the line under it names
    the slot it overlaps). Each change stages the whole schedule; Set sends it in one write. When the HomeBase
    refuses it, the line under the form shows `Not applied:` with the HomeBase's reason.
  - **Groups:** the arming groups with their members and target modes. **Activate** stages the activation;
    the pencil edits the name and each station's mode in the group; **New group** adds one; the bin deletes it.
    The last activation (who, when) shows in the editor. A member Home Assistant does not know stays in the group
    and is listed greyed.
  - **Settings:** the HomeBase's settings in tabs. Tabs, their names and their order come from the integration's
    settings layout (websocket `eufy_home_security/settings_layout`), so a new setting needs no card change.
    Detection also holds cross-camera tracking when the HomeBase reports it: the devices it follows (chips for
    the devices the integration lists as eligible, cameras and doorbells and other paired devices the eufy app
    offers, never sensors; **All cameras** = every eligible device), the tracking groups (each a row of chips to
    toggle, a bin to remove it, **+ Group** to add one; the list scrolls inside a fixed box) and the tracking time.
    A choice needs at least the integration's minimum (`min_cameras`, 2): at the minimum the chosen chips stay
    chosen and the line under them says `At least 2 cameras`; with fewer eligible devices than that the chips
    and **All cameras** are off and the line says why. A staged choice that falls under the minimum (a device
    left the eligible list) keeps Set off, with the reason on that line and in Set's label.
  - **Status:** disk and internal storage use, model and firmware (and `Update x.y` when an update is offered; it
    opens the update entity), the last alarm (`Last alarm 12.10 21:05`: the alarm's own time, else when the
    integration saw it; in the HA profile's time format), every paired device with its model and battery or
    `Offline`.
- **Support per model:** the integration grades each part per HomeBase model. A part graded unknown is left out
  (its section, toggle or menu items); a declared part shows `Not tested on this model` in its section; a verified
  one shows plainly. A part the integration cannot write is shown read only.
- **Lists** (a setting with many options, the schedule's slots, times and modes, the device picker) open inside
  the card: 5 to 8 rows (all when fewer), downward when they fit below the field, else upward, else the card grows
  while the list is open and shrinks back when it closes. Long labels end in `…` (the full text is in the option's
  label for screen readers and shows on keyboard focus). A list of more than 12 options (the timezone) has a
  filter field at the top: typing narrows the list, Enter picks the first match. Keys: arrows, Home, End,
  Page Up/Down, Enter, Escape, and typing a name's first letters jumps to it. The current value is scrolled into
  view. A pick is staged like every other change.
- **Delays** offer the choices the integration lists (`delay_choices`: 0, 15, 30, 45, 60, 180, 300 s); without
  them the delays show but cannot be changed.

## Writes

| what | sent as | when |
|---|---|---|
| security mode | `select.select_option` on the HomeBase's Security mode select (the option the integration names for the mode) | Set |
| actions and delays of one mode | websocket `eufy_home_security/homebase/mode_table/set`: one call per changed mode with every change of that mode and `expect_revision` | Set |
| a setting | `switch.turn_on`/`turn_off`, `number.set_value`, `select.select_option` on its entity | Set |
| arming groups | websocket `eufy_home_security/arming_groups/create`, `update`, `delete`, `activate` | Set |
| custom modes | websocket `eufy_home_security/homebase/custom_mode/create` `{name}`, `rename` `{mode, name}`, `delete` `{mode}`, each with `expect_revision` | Set |
| schedule | websocket `eufy_home_security/homebase/schedule/set`: the whole schedule (`slots` with weekday 0 = Sunday, `start`, `end` (`24:00` = midnight), `mode`; `default` when changed) and `expect_revision` | Set |
| cross-camera tracking | websocket `eufy_home_security/homebase/cross_camera/set`: `cameras` (device ids from `cross_camera.eligible`, at least `min_cameras`; `null` = every eligible device) and/or `groups`, with `expect_revision`; one call for both | Set |
| sound / stop the alarm | websocket `eufy_home_security/homebase/alarm` `{action: sound, seconds: 30}` / `{action: stop}` | Set |
| restart | websocket `eufy_home_security/homebase/restart` | Set |
| Stop alarm (header) | websocket `eufy_home_security/homebase/alarm` `{action: stop}` | at once, only while the alarm sounds |
| Disarm | `alarm_control_panel.alarm_disarm` on the HomeBase's alarm panel | at once, only while the alarm sounds |

- Everything else is **staged**: ✕ / Set appear in the header; nothing reaches the HomeBase before Set. Staged
  changes stay while sections are switched.
- **Changes that affect the alarm ask first:** Set lists them over the drawing (`Away · Porch: HomeBase alarm
  on`, `Away · Entry delay 30 s → 0 s`, `Sets the HomeBase to Home now`, `Activates <group>: HomeBase → Away`,
  `Schedule: 13 → 14 slots`, `Sounds the HomeBase alarm for 30 s`, `Restarts the HomeBase`, an alarm setting);
  **Set** there sends, **Back** returns to the changes. Which changes count comes from the integration alone: per
  action (`flags.<kind>[].alarm_affecting`), per part (`support.<part>.alarm_affecting`) and per setting (the
  layout's `alarm_affecting` and `alarm_affecting_entity_ids`); with the integration's current marks record and
  notification changes, custom modes and cross-camera tracking go without it.
- Set sends one write at a time: the mode tables, the settings, new and renamed custom modes, the schedule,
  cross-camera tracking, deleted custom modes, the group edits, the mode, the group activations, the alarm, the
  restart. A row reads `sending` until the HomeBase reports the value (the integration's live
  updates, websocket `eufy_home_security/homebase/subscribe`), then shows it; `Not applied` after 30 s or when
  the write is refused. A refused write's message shows over the drawing; a closed section with a failed write
  has a red toggle, and the header counts the failed writes.
- **Changed elsewhere:** when the HomeBase's configuration changed since the card read it (the eufy app, another
  dashboard), the integration refuses the write (mode tables, custom modes, schedule, cross-camera tracking). The
  card reads the HomeBase again, stages the changes not sent yet that still make a difference, drops those that
  are already in place, and says `Changed elsewhere, review`. Set again sends them.

## Data

| command | use |
|---|---|
| `eufy_home_security/homebase` `{device_id}` | the HomeBase: modes with names and select options, support per part (with what affects the alarm), action flags, delay choices, devices, mode tables, schedule, cross-camera tracking (picks, groups, `eligible` devices, `min_cameras`), alarm state and last alarm, status, revision |
| `eufy_home_security/homebase/subscribe` `{device_id}` | the same payload on every change; the card unsubscribes when it leaves the page |
| `eufy_home_security/settings_layout` `{device_id}` | Settings tabs: section ids, headings, order, entity ids, which of them affect the alarm |
| `eufy_home_security/arming_groups/list` / `logs` | the groups, their members and candidates; a group's last activations |

Settings rows read their entities' states in Home Assistant; the card keeps no list of settings, sections, modes or
devices of its own.

## Limits

- Mode-table changes made in the eufy app reach the card with the HomeBase's next report.
- The card needs an admin user for its websocket writes (the integration's rule).
- A new custom mode gets the HomeBase's own starting table (cameras record and notify, sensors notify); change it
  in Devices after Set.
- A schedule slot ends at midnight (`24:00`) at the latest; a slot over midnight is two slots.
- A tracking-group member Home Assistant does not know shows as `Unknown camera`; a group edit sent from the card
  leaves it out.
