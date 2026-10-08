# Usage

## Options

**Settings → Devices & services → Anker eufy Home Security → Configure**

| Option | Default | Description |
|---|---|---|
| Detection hold (seconds) | 10 | 5–300. How long a detection sensor stays on; the HomeBase never reports a detection's end |
| Alarm safety-net timeout (minutes) | 10 | 1–60. Ends Triggered or Pending if the HomeBase never reports the alarm's end |
| Event image | Thumbnail, then HD | What a detection and **Refresh event image** show; see [Camera images](#camera-images) |
| Live snapshot for cameras without a detection image | Off | A camera without a detection image takes one live still, at most every 5 min. Wakes battery cameras |
| Check the eufy session every 6 hours | On | One cloud read (not a sign-in) that notices when another client ended Home Assistant's session |
| Cloud push for cameras without a HomeBase | Off | Receives eufy's push messages; needed for detections of a standalone camera. See [Cloud push](#cloud-push) |
| eufy sign-in country | Home Assistant's country | The country your eufy app signs in with; eufy lists a device only under the country it was set up or shared under. A change asks eufy once more for the devices. See [network.md](network.md#eufy-sign-in-country) |
| Look for devices in every eufy region | Off | Every device list asks both eufy regions, also one that listed no devices; may cost a sign-in. See [network.md](network.md#eufy-regions) |
| Event history (days) | 7 | 0–365. Days of camera images and videos kept in the media folder; 0 keeps none |
| Save event videos | Off | Copies each HomeBase recording from then on into the event history; see [Event videos](#event-videos) |
| Recording length (seconds) | 30 | 5–300. Length of a **Record clip** action that names no duration |
| Sessions per HomeBase | 6 | 2–9. Connections Home Assistant holds to each HomeBase; each live view beyond the first needs one |

**Recording length** and **Sessions per HomeBase** apply at once. Any other change
reloads the integration.

## Alarm panel

- Arm Home, arm Away and disarm go to the HomeBase over the LAN; the panel shows the
  new mode once the HomeBase confirms it.
- Schedule and the Custom modes are shown, never set.
- A mode changed in the eufy app shows within 45 s.

## Camera images

The camera entity shows its latest detection's image.

**Event image** option:

| Choice | A detection shows |
|---|---|
| Thumbnail, then HD | the 640×360 thumbnail, then the first frame of the event's recording, at the recording's resolution |
| Thumbnail only | the thumbnail |
| HD only | the recording's frame only; nothing for an event without a recording |

On a HomeBase the images come from the HomeBase's storage and no choice wakes a
camera.

A camera without a HomeBase has no recordings to read. Each detection wakes it (each
wake costs battery), and the choice works like this:

| Choice | A detection shows |
|---|---|
| Thumbnail, then HD | a live picture taken at once, while the camera is awake; its event thumbnail about 15 s later goes to the event history only |
| Thumbnail only | its event thumbnail, about 15 s after the detection; no live picture |
| HD only | the live picture only |

- The live picture is what the camera sees after the detection, not the moment that
  triggered it. Right after waking, a camera streams below its full resolution (a
  T8170 starts at 1280×720 and reaches 2880×1616 over about 8 s); Home Assistant waits
  for the full size, so the picture arrives about 17 s after the detection on a
  T8170, from one wake.
- The event thumbnail is shown only when it belongs to that detection; if the camera
  has not written it yet, it is fetched once more about 20 s later, else the previous
  image stays.
- If the live picture fails, the event thumbnail is shown instead (Thumbnail, then HD).
- The two push messages of one detection take one live picture and one thumbnail.

**Buttons:**

| Button | Shows | Wakes the camera |
|---|---|---|
| **Refresh event image** | HomeBase camera: the newest event the HomeBase recorded for that camera in the last 7 days (the HomeBase is not searched further back), per the Event image choice; with no event in that window the image stays. Standalone camera: its newest event thumbnail | HomeBase camera: no. Standalone camera: yes |
| **Capture live image** | what the camera sees now | yes |

A detection after a button press replaces the button's image.

**Attributes:** `image_source`, `triggered_at` (the event's time) and
`image_updated` (changes with every new image, so a card can refetch).
`image_source` is one of:

| Value | Image | `triggered_at` |
|---|---|---|
| `thumbnail` | a detection's event thumbnail | the detection's time |
| `trigger_frame` | the first frame of a HomeBase detection's recording (HD) | the detection's time |
| `detection_live` | a live picture a camera without a HomeBase took for a detection (HD) | the detection's time |
| `live` | a **Capture live image** press, or the live image of a camera with none | none |

A detection's HD image is `trigger_frame` or `detection_live` with `triggered_at` set
to the detection's time. A camera has no image, and no `image_source`, until its first
detection or button press. The last image of each camera and preset survives a
restart.

## Event history

Every camera and preset image is also saved to the media folder, under **Media → My
media → eufy_home_security**, and so are event videos and recorded clips:

```
<media>/eufy_home_security/<camera>/<date>_<time>_<camera>_<kind>.jpg
<media>/eufy_home_security/<camera>/<date>_<time>_<camera>_<kind>.mp4
```

- `<camera>`: the camera's name in Home Assistant, made file-safe. Two cameras whose
  names come out the same each get `_` and the last 4 characters of their serial
  (`Front_1234`); a camera card for one of them needs that folder as `history_folder`.
- `<kind>`: `motion`, `person`, `identified_person`, `stranger`, `pet`, `vehicle`,
  `event`, `live` or `preset_<n>`.
- A detection keeps one image, its best: the HD image replaces the thumbnail. A camera
  without a HomeBase also keeps its event thumbnail beside the live picture, as
  `<kind>_thumbnail` (for example `person_thumbnail`): the two show different moments.
- About 1 MB per image; 100 detections a day for 7 days is about 700 MB per camera.
- Files older than the kept days are deleted once a day, images and videos alike.
- **Home Assistant Container** keeps `/media` only when a host directory is mounted
  there. Without one a repair issue says so. Mount one and recreate the container:

  ```yaml
  services:
    homeassistant:
      volumes:
        - /srv/homeassistant/config:/config
        - /srv/homeassistant/media:/media
  ```

### Event videos

With **Save event videos** on, each recording a HomeBase makes for one of its cameras
from then on is copied into the event history as an MP4:

- Only recordings that start after the option was switched on. Older ones stay on the
  HomeBase; play or save one from the camera card's **Station** list under History.
  Switching the option off and on again starts from the new moment.
- After the recording has finished: within about a minute of the detection, and at
  the latest within 15 minutes.
- Named like the event's image (same date, time and kind), so the two sit side by
  side; a recording whose image was not saved is named by its start, kind `event`.
- The HomeBase plays the recording off its own storage: no camera is woken. Each copy
  uses one session of **Sessions per HomeBase** while it runs.
- Copied once: a restart or a deleted file does not copy it again. A copy that fails
  is tried again on later passes, three times in all; then one warning is logged and
  the recording stays on the HomeBase only.
- The video is the camera's own HEVC and AAC in an MP4, never re-encoded. It plays in
  browsers that play the live view (see [network.md](network.md#browsers)).
- Needs **Event history** above 0 days. A standalone camera keeps no recordings on a
  HomeBase, so it has no event videos.
- A 4K recording is several MB per 10 s; plan the media folder's space accordingly.

### Recordings on the HomeBase

Whatever the option, the recordings a HomeBase keeps for a camera are listed on
request (the camera card's **Station** list, over the websocket commands
`eufy_home_security/recordings` and `eufy_home_security/recordings/fetch`). Playing
or saving one copies it into the event history exactly as an event video: same name,
noted as copied (the option's sync skips it), kept and deleted with the history. A
recording still running cannot be copied until it has finished. Needs **Event
history** above 0 days.

### Recording a clip

The `eufy_home_security.record` action on a camera entity records its live stream into
the event history:

| Field | Default | Description |
|---|---|---|
| `duration` | **Recording length** option | 5–300 seconds |

- The file is `<date>_<time>_<camera>_live.mp4`, beside the camera's images.
- Wakes a battery camera for the clip. A running live view keeps running and is shared;
  without one the camera opens for the clip and closes after it.
- The camera entity's state is `recording` while it runs. A second call for the same
  camera meanwhile is refused.
- With a response asked for, it returns:

  ```yaml
  media_content_id: media-source://media_source/local/eufy_home_security/Front_door/2026-10-02_08-35-59_Front_door_live.mp4
  duration: 30.0   # seconds, by the camera's clock
  complete: true   # false when the stream ended before the duration
  ```

  A clip whose stream ended early is kept with `complete: false`.
- Refused with **Event history** at 0 days, past **Sessions per HomeBase**, and when
  the camera does not wake or sends no picture. Home Assistant's own `camera.record`
  is unchanged.

## Live video

- Press play on a camera. Nothing streams until someone watches.
- First picture after about 3 s on a HomeBase camera and about 5 s on a standalone
  battery camera.
- Video is passed through, never transcoded; audio is converted only for WebRTC.
- Viewers of one camera share one stream. Each further camera of one HomeBase needs
  one session (**Sessions per HomeBase**, see
  [network.md](network.md#homebase-sessions)). A view past the limit waits about
  20 s, then fails with HTTP 503.
- A standalone battery camera streams one view at a time; a live still or preset
  capture ends its view.
- Changing **Streaming quality** (`live_streaming_resolution`) ends a running view.

Browser support: [network.md](network.md#browsers). Adding devices:
[devices.md](devices.md#adding-a-device).

## Pan/tilt cameras

| Entity or action | Effect |
|---|---|
| **Refresh presets** button | Reads the camera's presets and captures each one. Press it on a fresh install and after changing presets in the eufy app |
| **Capture preset n** button, **Preset n image** | Turn to preset n and take a still (about 10 s) |
| **Pan left / right**, **Tilt up / down** buttons | Move one step (about 1.5 s). The camera returns to its default preset when idle |
| **Save current view** button | Store the current view in the lowest free slot (5 slots) |
| **Default preset** select | The preset the camera returns to when idle |
| **Live view preset** select | The preset every live view opens at |
| **Live view zoom** number | 1×–12×, applied whenever a view opens |
| `eufy_home_security.capture_preset` | `preset` (0-based) |
| `eufy_home_security.goto_preset` | one-off turn to `preset` |
| `eufy_home_security.pan_tilt` | `direction`: `left`, `right`, `up`, `down` |
| `eufy_home_security.zoom` | `direction`: `in`, `out` |
| `eufy_home_security.save_preset` | optional `preset` (overwrite a slot) and `make_default`; returns `{"preset": n}` |
| `eufy_home_security.delete_preset` | clear slot `preset` |

- Every press and action wakes the camera.
- A preset deleted in the eufy app makes its entities unavailable after **Refresh
  presets**.
- While a capture runs, other captures and preset edits are refused and nothing is
  sent.
- The actions target the camera entity, for camera cards with PTZ controls.
- A camera without presets (T8410) gets only the pan/tilt buttons and `pan_tilt`; the
  preset actions are refused, and `zoom` on a camera without zoom too. Nothing is sent.

## Standalone battery cameras

- A camera without a HomeBase (for example the T8170) is its own device with an
  alarm panel, battery, signal, camera and buttons.
- Home Assistant keeps no connection to it, so it sleeps. Guard mode, battery and
  signal come from eufy's cloud and can be up to an hour old.
- Arming, every button, every capture and each detection's picture wake it (about
  10 s) and cost battery.
- Its detections reach Home Assistant only with
  [Cloud push](#cloud-push) on; without it, its detection sensors stay off.

## Cloud push

**Cloud push for cameras without a HomeBase** option, off by default.

- Home Assistant listens for eufy's push messages, as the eufy app on a phone does.
  The app keeps working.
- Needed for a standalone camera's detections (sensors, detection event, event
  image). A HomeBase reports its detections over the LAN without it.
- A detection that arrives both over the LAN and from the cloud fires once.
- A standalone camera's detection image follows [Camera images](#camera-images):
  each detection wakes the camera.
- Push messages pass through eufy's and Google's servers (see
  [network.md](network.md#firewall)). While that connection is down, they arrive
  late or not at all.
- Starts in the background after the integration has loaded; a failed start is
  retried, from 1 minute up to every 30 minutes.
- While push is not running, the repair **eufy cloud push is not running** is shown;
  it clears by itself when push runs again.

## Power and storage state

Read from the HomeBase's parameter report, refreshed every 45 s; nothing wakes a
camera. Each entity appears once its device reports the value; a value its device
stops reporting shows unknown. The code meanings are declared from eufy's app code,
not checked against the app's display.

| Device | Entity | Type | Shows |
|---|---|---|---|
| Camera | **Charging** | binary sensor (battery charging) | Charging from any source. Attributes `solar_charging`, `power_source` (raw source code: 0/2 none, 1 USB, 3 AC, 4 built-in solar, 5 USB + built-in solar, 6/8 external panel, 7/12 external + built-in, 20 connected panel; other codes as reported) |
| Camera | **Solar charging** | binary sensor (battery charging) | Charging from a solar source |
| Camera | **Solar intensity** | diagnostic sensor | Raw solar input, 0 at night or without a panel; scale differs by model |
| Camera | **Battery temperature** | diagnostic sensor, °C | |
| Camera | **Working days** | diagnostic sensor, days | Days since the last USB charge |
| Camera | **Detected events**, **Recorded events** | diagnostic sensors, total | Counted since the last USB charge |
| Motion sensor | **Battery low** | diagnostic binary sensor (battery) | The sensor's own low-battery flag |
| HomeBase | **Storage problem** | diagnostic binary sensor (problem) | On when the storage status is not one the app shows as normal. Attribute `storage_status` (raw code) |

**Firmware update** entities also carry, when reported: `subsystem_firmware` on the
HomeBase (version strings by parameter id; the subsystems are unnamed) and
`siren_actions` on a camera (raw siren action per guard mode, keyed `away`, `home`,
`custom_1`…).

## Device settings

- A device's settings come from the library's data for its model (product code).
  Each is keyed by eufy's own setting identifier (`nightvision_type_new`,
  `power_manager_mode`); names, units and option labels are the library's, in
  English, with the eufy app's titles where the app has one ("Working Mode",
  "Clip length").
- Some models list one setting under two identifiers; the one the app does not use
  (for example `nightvision_type` beside `nightvision_type_new`) is registered
  disabled.
- A setting the library can write is a control of the kind the library names for it:
  switch (on/off), select (a choice), number (a slider, or a box for a long range),
  or text.
  - A numbered scale is a number, not a choice: detection sensitivity 1–7 is a slider.
  - A setting with several choices on at once is one switch per choice, named
    "Detection types: Pet". A toggle changes that one choice; the others stay. This
    covers detection types and the HomeBase's mode-switch notifications.
  - Some on/off settings share one device value with others (the HomeBase's
    notification switches). A toggle changes only its own part. It is refused, with
    nothing sent, while the device does not report that value.
- A setting it can only read is a diagnostic sensor or binary sensor, disabled until
  you enable it. A setting it can neither read nor write has no entity.
- **Per-mode actions**: for each camera and sensor, one switch per action and guard
  mode (Home, Away, Custom 1–3), for example `camera_action_away_record`, disabled
  until you enable it. A toggle changes that one action; the mode's other actions
  stay.
- A write shows the written value at once.
  - The device rejects it: error *setting not applied*; the entity keeps the value
    last reported.
  - No answer in time: error *setting unconfirmed*; the entity shows unknown until
    the next update shows the value in force.
- `applies_when` attribute (for example `power_manager_mode=3`, with
  `applies_when_label` "Custom recording"): the setting applies only in that state and
  is unavailable otherwise. On a T8160, clip length, trigger interval and end clip
  early apply only in custom recording. The controlling select names them in its
  `controls` attribute.

## Signing in again

- **Reconfigure** on the entry's ⋮ menu: after the "eufy ended Home Assistant's
  session" repair, a password change, or when eufy looks unreachable.
- Leave the password empty to use the saved one. Each submit is one eufy sign-in.
- If another client holds the session, Home Assistant asks before signing it out.

## Removal

**Settings → Devices & services → Anker eufy Home Security → ⋮ → Delete**. The saved
password, keys and cached images are deleted; with the last account also the camera
card's dashboard resource (remove its cards from dashboards first). The sign-in
hold-off is kept, so removing and re-adding cannot trigger eufy's lockout. Event
history files stay.
