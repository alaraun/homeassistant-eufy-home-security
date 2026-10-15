# eufy-homebase-card

One card per eufy HomeBase (`eufy_home_security` integration). It shows the security mode and switches it, draws
the HomeBase with its paired devices and what each does in a mode, and has sections for the devices' actions per
mode, the arming groups, the HomeBase's settings and its status.

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
    with the names given in the eufy app), the arming groups, **Restart** and **Refresh**. Picking a mode or a
    group only stages it; Set sends it.
  - **Restart** shows as `Not available yet` until the integration can restart the HomeBase.
  - **While the alarm sounds** a red **Disarm** button sits in the header. It disarms at once (no staging): it is
    the one control on the card that acts without Set.
- **Drawing:** the HomeBase in the middle, its ring in the mode's colour (it breathes while the alarm sounds),
  the paired devices around it. Under each device: what it does in the shown mode (record, camera alarm,
  HomeBase alarm, notification, light alarm, monitoring center, sensor response; the alarm actions in red) and its
  entry delay (`⏱30s`); an offline device is dimmed and reads `Offline`, a battery under 30 % gets a red battery
  icon. While the alarm sounds, the links of the devices that sound the HomeBase alarm carry moving dots (none
  with reduced motion).
  - The **lens** under the drawing shows another mode's behaviour (the mode in force has a dot); it changes
    nothing on the HomeBase. When the mode names do not fit, it becomes `‹ Away ›`. With a schedule, **Schedule**
    in the lens shows the week: one row per day, each mode in its own colour with a legend (read only).
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
  - **Groups:** the arming groups with their members and target modes. **Activate** stages the activation;
    the pencil edits the name and each station's mode in the group; **New group** adds one; the bin deletes it.
    The last activation (who, when) shows in the editor. A member Home Assistant does not know stays in the group
    and is listed greyed.
  - **Settings:** the HomeBase's settings in tabs. Tabs, their names and their order come from the integration's
    settings layout (websocket `eufy_home_security/settings_layout`), so a new setting needs no card change; the
    Modes section is left out (its per-mode entities are what Devices edits). Detection also shows the
    cross-camera tracking cameras and groups, read only, when the HomeBase reports them.
  - **Status:** disk and internal storage use, model and firmware (and `Update x.y` when an update is offered; it
    opens the update entity), every paired device with its model and battery or `Offline`.
- **Support per model:** the integration grades each part per HomeBase model. A part graded unknown is left out
  (its section, toggle or menu items); a declared part shows `Not tested on this model` in its section; a verified
  one shows plainly. A part the integration cannot write yet is shown read only.

## Writes

| what | sent as | when |
|---|---|---|
| security mode | `select.select_option` on the HomeBase's Security mode select (the mode's name) | Set |
| actions and delays of one mode | websocket `eufy_home_security/homebase/mode_table/set`: one call per changed mode with every change of that mode and `expect_revision` | Set |
| a setting | `switch.turn_on`/`turn_off`, `number.set_value`, `select.select_option` on its entity | Set |
| arming groups | websocket `eufy_home_security/arming_groups/create`, `update`, `delete`, `activate` | Set |
| restart | websocket `eufy_home_security/homebase/restart` (once the integration offers it) | Set |
| Disarm | `alarm_control_panel.alarm_disarm` on the HomeBase's alarm panel | at once, only while the alarm sounds |

- Everything else is **staged**: ✕ / Set appear in the header; nothing reaches the HomeBase before Set. Staged
  changes stay while sections are switched.
- **Changes that affect the alarm ask first:** Set lists them over the drawing (`Away · Porch: HomeBase alarm
  on`, `Away · Entry delay 30 s → 0 s`, `Sets the HomeBase to Home now`, `Activates <group>: HomeBase → Away`,
  a setting in the Alarm section, a restart); **Set** there sends, **Back** returns to the changes. Record and
  notification changes alone go without it. Which parts count comes from the integration.
- Set sends one write at a time: the mode tables, the settings, the group edits, the mode, the group
  activations, the restart. A row reads `sending` until the HomeBase reports the value (the integration's live
  updates, websocket `eufy_home_security/homebase/subscribe`), then shows it; `Not applied` after 30 s or when
  the write is refused. A refused write's message shows over the drawing; a closed section with a failed write
  has a red toggle, and the header counts the failed writes.
- **Changed elsewhere:** when the HomeBase's configuration changed since the card read it (the eufy app, another
  dashboard), the integration refuses the mode-table write. The card reads the HomeBase again, stages the
  changes that still make a difference again, drops those that are already in place, and says `Changed
  elsewhere, review`. Set again sends them.

## Data

| command | use |
|---|---|
| `eufy_home_security/homebase` `{device_id}` | the HomeBase: modes with names, support per part, devices, mode tables, schedule, cross-camera, status, revision |
| `eufy_home_security/homebase/subscribe` `{device_id}` | the same payload on every change; the card unsubscribes when it leaves the page |
| `eufy_home_security/settings_layout` `{device_id}` | Settings tabs: section ids, headings, order and entity ids |
| `eufy_home_security/arming_groups/list` / `logs` | the groups, their members and candidates; a group's last activations |

Settings rows read their entities' states in Home Assistant; the card keeps no list of settings, sections, modes or
devices of its own.

## Limits

- Creating, renaming and deleting custom modes, editing the schedule and the cross-camera picks, restarting the
  HomeBase and sounding its alarm are not available yet; the card shows what the HomeBase reports.
- Mode-table changes made in the eufy app reach the card with the HomeBase's next report.
- The card needs an admin user for mode-table writes (the integration's rule).
