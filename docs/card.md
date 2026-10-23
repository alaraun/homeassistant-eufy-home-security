# eufy-camera-card

A card for one or several eufy cameras (`eufy_home_security` integration). It shows the camera's still until live
view is started, pan/tilt/zoom and presets on the picture for cameras that have them, a record button, its saved
history and the recordings on its HomeBase, and the camera's settings. With several cameras a list on the name
switches between them.

## Install

The card comes with the integration; there is nothing to download or copy.

- The integration serves it at `/eufy_home_security/eufy-camera-card.js` and, with dashboard resources in
  storage mode (the default), keeps one resource for it, `…/eufy-camera-card.js?v=<hash>`. The hash is the
  file's own, so an integration update reloads the card in every browser.
- Add it: edit a dashboard → **Add card** → **eufy camera**, or a card with `type: custom:eufy-camera-card`.
- Resources in YAML mode (`lovelace: resource_mode: yaml`): add it once yourself:

  ```yaml
  lovelace:
    resources:
      - url: /eufy_home_security/eufy-camera-card.js
        type: module
  ```

- After an update the HA companion app can keep the old card until its cache is cleared
  (**Settings → Companion app → Debugging → Reset frontend cache**).

## Options

Every option is also in the card editor.

```yaml
type: custom:eufy-camera-card
entity: camera.front_door        # the eufy camera entity
entities:                        # optional: more cameras; the name opens a list to switch between them
  - camera.backyard
name: Front door                 # optional: the header name (default: the camera's name)
picture: auto                    # optional: auto | landscape | portrait
show_header: true                # optional: false hides the header (name, status, camera menu)
show_history: true               # optional: false hides History
show_settings: true              # optional: false hides Settings
live_buttons:                    # optional: buttons on the live view bar (lock, cover, button, script, scene, switch)
  - lock.front_door
live_seconds: 120                # optional: length of a started live view (default 120 s)
auto_live: timed                 # optional: start live view when the dashboard opens: timed | continuous (true = timed)
history_folder: Front_door       # optional: the camera's history folder, when it differs from the device name
layout: auto                     # optional: auto | compact | regular
settings_include:                # optional: more of the camera's entities in Settings (entity ids)
  - switch.front_door_status_led
settings_exclude:                # optional: controls to leave out of Settings (entity ids)
  - switch.front_door_silent_firmware_updates
settings_groups:                 # optional: Settings tabs to show, in this order (default: all, in the integration's order)
  - detection
  - picture
  - more
```

- `auto_live` wakes the camera every time the card is shown (dashboard opened, view entered, tab shown again),
  so it costs battery. Stop holds until the next showing.
- `layout: auto` switches to the compact layout (one column of settings rows) below 400 px card width.
- `history_folder` is needed only when the camera was renamed after its files were saved.
- Colours come from the HA theme, light or dark: tabs, switches, sliders and fields use HA's own control
  colours. The controls drawn over the picture stay white on dark glass.
- `<role>_entity` keys override an entity the card finds on the camera's device (`zoom_entity`,
  `battery_entity`, `panLeft_entity`, …).
- `entities`: the cameras after `entity` (or all of them, without `entity`). With two or more, the name in the
  header is a list of the cameras; the picture, live view, History and Settings follow the chosen one. Choosing
  another camera ends a running live view and drops changes not yet Set; writes already sent (a Set, a saved
  view, a recording) finish on the camera they were for, without marks or messages on the chosen one;
  `auto_live` starts the chosen camera's view. `name`, `history_folder` and the `<role>_entity` keys apply to
  the first camera only; the other cameras use their own entity names, device-name folders and the entities of
  their own devices. Every other option applies to every camera of the card: `picture`, `show_header` /
  `show_history` / `show_settings`, `layout`, `live_seconds`, `auto_live`, `live_buttons` (the same buttons on
  each camera's live view bar) and `settings_include` / `settings_exclude` / `settings_groups` (an entity id
  there only matters on the camera whose device it belongs to). The card starts at the first camera when the
  page loads. In full screen the list is a name chip on the picture, after the `● LIVE` chip, and a running
  live view goes on with the chosen camera; the controls stay up while the list is open.
- `show_header`, `show_history`, `show_settings`: with all three off the card is the picture alone, with the
  play button, the live controls and the chips over it (for a popup or a wall display). Without the header its
  menu (`Record a clip` while idle, `New still`, `Refresh event image`) is not offered; with several cameras the
  camera list moves to a name chip on the picture; ✕ / Set for staged settings sit at the right end of the
  History / Settings tabs. Without Settings the card does not ask the integration for the settings layout.
- `picture`: `landscape` is the fixed 16:9 frame, filled edge to edge. `portrait` shows the whole picture in a
  frame at most 80 % of the screen high, with bars at the sides when the card is wider: a camera whose picture is
  taller than wide gets its own shape (no narrower than 9:16); until the card has seen a picture, and for a
  camera whose picture is not taller than wide, the frame is 3:4 (a wide picture then shows with bars above
  and below). `auto` (default) is portrait for a camera whose picture is taller
  than wide (a doorbell), else landscape; the card reads the shape from the still and the live video and keeps it
  in the browser, so the next visit starts in the right shape.
- `live_buttons`: entities offered on the live view bar, after the view's own buttons, e.g. the door lock beside
  a doorbell. A press asks `Press again to …`; a second press within 4 s calls the action it asked for (when the
  entity's state changed in between and now gives another action, that press asks again with the new one): a
  lock `lock.lock` while unlocked or open, else `lock.open` when it can open its latch, else `lock.unlock`; a lock
  that is jammed, locking, unlocking or opening offers nothing (the button is disabled and names the state); a
  cover `cover.open_cover` / `cover.close_cover`; a button `button.press` (or `input_button.press`); a script or
  scene `turn_on`; a switch `turn_on` / `turn_off`.

## Battery rule

Without `auto_live` nothing on the card opens a stream on its own. The picture is the integration's cached still;
the card never wakes a camera for it. Live view opens only on a press and ends:

- after `live_seconds`, unless continuous (∞) is on;
- on Stop, when the card leaves the page, or when the browser tab is hidden (a continuous view keeps running on a
  wall display);
- when the integration ends it: a streaming-quality write, or on a standalone camera a still or preset capture;
- when no picture arrives within 60 s (`No picture from the camera`).

## What the card shows

- **Header:** the camera button, the name and one status line (`Idle · Person 5 min ago`, `Live`, `Recording`,
  `Unavailable`, …). The button opens a menu: live view (timed or continuous), stop, `Record a clip`, `New still`
  and `Refresh event image`. A doorbell reads `Ringing` (a bell on the button) for a minute after a press, then
  `Ring 5 min ago` while the ring is its newest event.
- **Picture:** a 16:9 frame, or the camera's portrait shape (see `picture`). Top left says what the still is (`Event · 2 h ago`, `Snapshot`, `No still yet`)
  or `● LIVE 1:42` while live; next to it the battery chip (charging and solar charging shown on its icon).
  - Idle: a play button (timed live view) and ∞ (continuous). A camera without live video shows
    `No live view for this model` instead, and its menu has no live view or `Record a clip`.
  - Live: a bar with Stop, ∞, Record, sound (starts muted), presets and full screen.
  - Pan/tilt cameras: a pan/tilt pad with a home button (the default preset), usable once the live picture shows.
    Zoom cameras: a zoom pill (−, the value, +, and a reset to 1×), usable in every state.
  - Presets: the bar's preset button opens a row of the preset slots' pictures; a tap turns the camera there.
    - `Save view`, the row's last tile, stores the current view in the lowest free slot and makes the live
      picture at the press its picture (taken in the browser, at most 1920 px wide). The new tile joins the row
      once the integration has read the slots back. With all 5 slots set the tile reads `Slots full`; delete a
      preset first. Without a picture (video not playing, a browser that refuses the frame) the preset is saved
      without one, and the message says so.
    - Holding a preset tile (about 0.6 s), a right click, or the context-menu key or Shift+F10 on it asks
      `Replace Pn`; confirming stores the current view over that preset, with its picture; ✕ or Esc keeps it.
    - While a save runs the pan/tilt pad and the other tiles wait; a camera with presets but none set shows the
      tile alone.
    - `Save view` and `Replace` need the camera's **Save current view** button: a camera that cannot store a
      preset (T8173), or one whose button is disabled, offers neither.
  - While the live picture plays, its controls, the battery chip and the time fade 4 s after the last touch or click
    (every width, full screen too) and a red dot at the top left stays; a tap or click on the picture brings them
    back, another hides them. Keyboard focus shows them too; a recording's `● REC` chip stays.
- **Record:** the live bar's record button, or `Record a clip` in the menu while idle, records a clip of the
  integration's Recording length into the history (Captures). While any client records, a `● REC 12 s` chip shows,
  and the same button (red, a stop square) or `Stop recording` in the menu ends the clip; the part recorded so far
  is saved.
- **History:** the camera's saved files, newest first, as a row of tiles in sub-tabs: **Events** (detections),
  **Captures** (live captures, New still), **Presets** (only when there are any) and **Station** (the recordings
  on the HomeBase's own storage). Events, Captures and Presets have a Pictures / Videos filter; 10 items show
  first, `Show more` adds 10. A tile opens the picture or plays the video in the card's picture area.
  - The calendar button beside the filter goes to a day (HA's date picker): only that day's files show, newest
    first, on every sub-tab (Station lists that day's recordings from the HomeBase, up to 30 days back); ✕ or the
    picker's Clear goes back to the newest.
  - Station asks the HomeBase a week of days per page, back as far as **Event history** reaches (30 days at
    most); `Show more` goes on from where the page stopped.
  - Station rows have **Play** and **Save**: both copy the recording from the HomeBase into the history folder
    (5–10 s each, one at a time); Play then plays it. A standalone camera has no Station sub-tab.
  - Files come from the integration's history in HA's media folder (`Media › eufy_home_security › <camera name>`).
    A missing folder (history off, or nothing saved yet) reads as empty.
- **Settings:** the camera device's settings (its `select`, `number` and `switch` entities), so a new camera
  model needs no card change. Entities hidden or disabled in HA are left out.
  - Tabs follow the integration's sections: the card asks the integration for the device's settings layout
    (websocket `eufy_home_security/settings_layout`) and shows one tab per section that has settings, with the
    section's name, in the integration's order; rows keep the integration's order within a section (two-line
    rows first, switches after). The layout is asked when the card first draws and again when the device's
    entities change in HA. Until the first answer Settings shows `Loading settings` in the tabs' place; when it
    fails, `Settings did not load` with Retry. A later failure keeps the last tabs.
  - The Modes section (guard-mode settings) belongs to the HomeBase and is left out; `settings_include` adds one
    of its entities back under Modes. A camera's per-mode actions and entry and leaving delays have no entities:
    the [HomeBase card](homebase-card.md) edits them. An entity in no section (a `settings_include` extra, another
    configuration control of the device) is under More, the last tab.
  - On a narrow card only the open tab shows its name; when even that does not fit, the tabs are icons and the
    open tab's name is on a line under them.
  - `settings_groups` lists the tab ids to show, in that order. The ids are the integration's section ids:
    `picture`, `detection`, `recording`, `audio`, `power`, `ptz` (Pan & tilt), `notifications`, `doorbell`
    (Doorbell & chime), `alarm`, `modes`, `storage`, `network`, `system`, `other`, then `more`. Without the key
    every section with settings shows in the integration's order, then More. A section not listed is hidden
    with its rows; an id the camera has no section for is skipped and kept in the list. A section the
    integration adds later stays hidden while the key is set; remove the key to get every section. In the
    editor the tabs are chips (the camera's sections and More): drag to reorder, ✕ to hide, the picker below
    adds one back; removing every chip shows every tab.
  - A setting that applies only in one option of another sits under it, captioned with that option (`Only in
    Custom`), and shows only while that option is chosen or picked (before Set).
  - A choice with up to four short options is a row of segments, numbered options a slider, any other a
    dropdown. Its list opens inside the card with at least five rows (all when fewer, up to eight): downward
    when there is room below, else upward, else the card grows while the list is open. Long labels end with
    `…` (the full text is read out, and shows while the option has keyboard focus). A list of more than twelve
    options starts with a filter field: typing narrows the list and Enter picks the first match. Arrows, Home,
    End, Page Up/Down move, typing jumps to an option starting with the typed text, Escape closes. Picking only
    stages the value, like every setting.
  - A setting the integration makes unavailable is greyed out; one the device never reports reads
    `Not reported` until written from the card.
  - The Power tab adds a line of readings (charging, solar intensity, battery temperature, days since the last
    charge, recorded and detected events); each opens its entity's history.

## Writes

- **Immediate:** pan/tilt, zoom and go-to-preset (`eufy_home_security.pan_tilt`, `.zoom`, `.goto_preset`), Record
  (`eufy_home_security.record`), Stop recording (`eufy_home_security.stop_recording`), Save view and Replace (`eufy_home_security.save_preset`, then the picture over
  the websocket command `eufy_home_security/preset_image`) and the Station Play/Save. Pan/tilt steps run one at a time; up to 3 presses wait
  their turn.
- **Live view buttons** (`live_buttons`): immediate on the second press.
- **Staged:** settings, `New still` and `Refresh event image`. ✕ / Set appear in the header; Set sends one write
  at a time. A row reads `sending` until the entity reports the value, `Not applied` after 30 s or on a refused
  write (the integration's message shows on the picture), and `may still apply` when the device did not confirm
  in time.
- Changing the streaming quality during live view ends the view (the integration restarts the stream at the new
  size); the row says `restarts live` before Set.

## Limits

- Pan/tilt and go-to-preset need a running live view; an idle camera stores zoom and preset for the next view.
- Cameras behind one HomeBase stream side by side only up to the integration's "Sessions per HomeBase" budget;
  past it the view ends with `No picture from the camera` after about 20 s.
- Live view uses HA's WebRTC player where the browser receives the camera's codec over WebRTC: H.264 cameras in
  every browser, HEVC cameras in Chrome, Edge and Safari. Otherwise (an HEVC camera in Firefox) it uses HA's HLS
  player, and the first picture takes longer. A camera whose `video_codec` is not known yet (no picture since Home
  Assistant started) counts as HEVC. See [network.md](network.md) for the browser table.
- A `New still` behind a HomeBase at its session budget waits up to about 20 s for a view to end first.
