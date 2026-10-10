# eufy-camera-card

One card per eufy camera (`eufy_home_security` integration). It shows the camera's still until live view is
started, pan/tilt/zoom and presets on the picture for cameras that have them, a record button, its saved history
and the recordings on its HomeBase, and the camera's settings.

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
name: Front door                 # optional: the header name (default: the camera's name)
live_seconds: 120                # optional: length of a started live view (default 120 s)
auto_live: timed                 # optional: start live view when the dashboard opens: timed | continuous (true = timed)
history_folder: Front_door       # optional: the camera's history folder, when it differs from the device name
layout: auto                     # optional: auto | compact | regular
settings_include:                # optional: more of the camera's controls in Settings (entity ids)
  - switch.front_door_away_record
settings_exclude:                # optional: controls to leave out of Settings (entity ids)
  - switch.front_door_silent_firmware_updates
settings_groups:                 # optional: Settings tabs to show, in this order (default: all, in the card's order)
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
  and `Refresh event image`.
- **Picture:** a fixed 16:9 frame. Top left says what the still is (`Event · 2 h ago`, `Snapshot`, `No still yet`)
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
- **Settings:** built from the camera device's own controls (every `select`, `number` and `switch` with entity
  category *config*, plus the live view preset), so a new camera model needs no card change. Entities hidden or
  disabled in HA are left out.
  - Tabs: Picture, Detection, Recording, Power, Pan & tilt, Other, More (only those with rows). The common
    settings sit in the named tabs; every other setting is under More; `settings_include` adds a row to Other.
  - `settings_groups` lists the tab ids to show, in that order: `picture`, `detection`, `recording`, `power`,
    `ptz` (Pan & tilt), `other`, `more`. Without the key every group shows in the order above. A group not
    listed is hidden with its rows; an unknown id is ignored. A group a later card version adds stays hidden
    while the key is set; remove the key to get every group. In the editor the groups are chips: drag to
    reorder, ✕ to hide, the picker below adds one back; removing every chip shows every group.
  - A setting that applies only in one option of another sits under it, captioned with that option (`Only in
    Custom`), greyed while that option is not chosen.
  - A setting the integration makes unavailable is greyed out; one the device never reports reads
    `Not reported` until written from the card.
  - The Power tab adds a line of readings (charging, solar intensity, battery temperature, days since the last
    charge, recorded and detected events); each opens its entity's history.

## Writes

- **Immediate:** pan/tilt, zoom and go-to-preset (`eufy_home_security.pan_tilt`, `.zoom`, `.goto_preset`), Record
  (`eufy_home_security.record`), Stop recording (`eufy_home_security.stop_recording`), Save view and Replace (`eufy_home_security.save_preset`, then the picture over
  the websocket command `eufy_home_security/preset_image`) and the Station Play/Save. Pan/tilt steps run one at a time; up to 3 presses wait
  their turn.
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
- Live view uses HA's WebRTC player where the browser receives H.265 over WebRTC (Chrome, Edge, Safari) and HA's
  HLS player otherwise (Firefox; the first picture takes longer). See [network.md](network.md) for the
  browser table.
- A `New still` behind a HomeBase at its session budget waits up to about 20 s for a view to end first.
