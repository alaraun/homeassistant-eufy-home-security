# Usage

## Options

**Settings → Devices & services → Anker eufy Home Security → Configure**. The options
sit in six collapsible sections.

| Section | Option | Default | Description |
|---|---|---|---|
| Detections and alarm | Detection hold (seconds) | 10 | 5–300. How long a detection sensor stays on; the HomeBase never reports a detection's end |
| | Alarm safety-net timeout (minutes) | 10 | 1–60. Ends Triggered or Pending if the HomeBase never reports the alarm's end |
| | Repair when a security mode holds detections back | On | The repair naming cameras whose detections the security mode in force holds back. Off removes it and raises none; each camera's **Detections in current mode** sensor still shows it. See [Detections in the security mode](#detections-in-the-security-mode) |
| Camera images | Event image | Thumbnail, then HD | What a detection and **Refresh event image** show; see [Camera images](#camera-images) |
| | Refresh event image keyframe | 1 | 1–5. Selects the keyframe for Refresh event image unless Event image is Thumbnail only. Later keyframes take longer and can delay image fetches for other cameras on the same HomeBase. Short recordings use their last keyframe; detection images are unchanged |
| | Live snapshot for cameras without a detection image | Off | A camera with live video and without a detection image takes one live still, at most every 5 min. Wakes battery cameras |
| | Capture live image at full resolution | Off | **Capture live image** waits until the stream reaches the camera's full resolution and shows that picture, not the first, smaller one a woken camera sends (a T8170 starts at 1280×720). Takes longer, at most 30 s more, and keeps a battery camera awake that long. Image fetches for other cameras on the same HomeBase wait meanwhile |
| Live view and recording | Sessions per HomeBase | 6 | 2–9. Connections Home Assistant holds to each HomeBase; each live view beyond the first needs one |
| | Recording length (seconds) | 30 | 5–300. Length of a **Record clip** action that names no duration |
| Event history | Event history (days) | 7 | 0–365. Days of camera images and videos kept in the media folder; 0 keeps none |
| | Save event videos | Off | Copies each HomeBase recording from then on into the event history; see [Event videos](#event-videos) |
| eufy account | eufy sign-in country | Home Assistant's country | The country your eufy app signs in with; eufy lists a device only under the country it was set up or shared under. A change asks eufy once more for the devices. See [network.md](network.md#eufy-sign-in-country) |
| | Check the eufy session every 6 hours | On | One cloud read (not a sign-in) that notices when another client ended Home Assistant's session |
| | Cloud push for detections | Off | Receives eufy's push messages; needed for detections of a standalone camera or a HomeBase 2. See [Cloud push](#cloud-push) |
| Extra countries (experimental) | Extra eufy sign-in countries | None | The country of each account that shared a home with this one, when it differs; one more sign-in each. Experimental: can end Home Assistant's or the eufy app's session. See [network.md](network.md#eufy-sign-in-country) |
| | Look for devices under every sign-in country on each refresh | Off | Every device list asks every sign-in country, also one that listed no devices; may cost a sign-in. See [network.md](network.md#eufy-regions) |

**Recording length** and **Sessions per HomeBase** apply at once. Any other change
reloads the integration.

## Alarm panel

- Arm Home, arm Away and disarm go to the HomeBase directly, over its P2P session; the
  panel shows the new mode once the HomeBase confirms it.
- The panel shows Schedule and the Custom modes but does not set them.
- The **Security mode** select of a HomeBase or standalone camera sets every mode it has
  except Off and Geofencing, Schedule and the Custom modes included, under their names
  (in an automation: `select.select_option`). It shows the selected mode (Schedule while
  a schedule runs); the panel shows the mode in force.
- A mode changed in the eufy app shows within 45 s.
- **Entry delay:** when an armed HomeBase starts its alarm delay for a trigger (an
  entry sensor's door opened in a mode with a delay), the panel shows **Pending**
  with the attributes `delay_seconds` (the delay) and `delay_until` (when it runs
  out, UTC). Then:
  - the alarm sounds: **Triggered**, as for any alarm;
  - the HomeBase ends the delay without an alarm: the mode again;
  - a disarm in Home Assistant, on a keypad or in the eufy app: **Disarmed** as soon
    as the HomeBase reports it (its own report comes at once; otherwise the 45 s
    refresh shows it);
  - another trigger during the delay: Pending runs to whichever end is later;
  - nothing more is heard: Pending ends on its own 2 s after `delay_until` (the
    alarm a delay runs into comes a little after it), showing the mode again.

  The panel does not say which sensor started the delay; the sensor's door entity
  turns on at the same moment.

## Sirens

- **Siren** of a camera: where the eufy app offers the camera's model an alarm, a
  standalone camera included. Its `available_tones` are the alarms the app offers that
  model on its station: `sound`, `light`, `sound_and_light`. No `tone`: the app's
  preselected alarm.
  - It sounds for 30 s; no duration or volume.
  - On while the camera reports its alarm on, whoever started it (this siren, the eufy
    app, a detection); off when it reports the alarm over, or once its time has run out
    without that report. Unknown after a restart until the camera's first report. A
    camera whose alarm the station plays as its alarm tone shows the HomeBase's alarm
    state instead. Turning it off after it went quiet is no error.
  - Turning it off stops each alarm that may be sounding: the one the camera reports and
    the ones started and not yet stopped; for an alarm the report does not name (a
    detection's siren) and after a restart, every alarm the camera offers.
  - Tested on a T8170 behind a HomeBase 3 (sound: start and stop confirmed); the state
    and the other models follow the eufy app, not tested here.
- **Siren** of a HomeBase: the HomeBase's own siren, where the library sounds its model
  (tested on a T8030).
  - On while the HomeBase alarms, whoever started the alarm. It is not read again after
    a reconnect or a restart: an alarm that ends meanwhile can still show on, and one
    sounding at a restart shows off until its next change.
  - The HomeBase reports its siren as its alarm: while it sounds, the alarm panel shows
    **Triggered** and the alarm event fires, so automations on a triggered alarm run. A
    camera siren the station plays as its alarm tone does the same.
  - `duration` is rounded up to a length the library sends (30 s); a longer one is
    refused and nothing is sent.
- A start the device refuses or does not confirm raises an error, and nothing shows on
  that the device did not report. A refused stop raises an error too.

## Locks

- **Lock** of a smart lock, a video smart lock included, where the library reads the
  lock's state.
- Lock and unlock where the library sends the lock's command over the route the eufy app
  uses for it: through the lock's HomeBase or its own station session (T8501, T8502,
  T8503, T8506, T8520, T8530, and T8531, T8532, T85M0, T85M1, T85V0 behind a HomeBase),
  or over eufy's MQTT broker (a T8531, T8532, T85M0, T85M1 or T85V0 that is its own
  station, and a Wi-Fi lock that gets an entity). Declared from the eufy app, not tested
  on hardware.
- The MQTT broker connection is opened only for an account with such a lock: one per
  account entry, following each **Refresh device list**, closed when the entry unloads.
  It needs the library's `mqtt` extra (`eufy-home-security[mqtt]`); without it the log
  says so once at setup, and those locks show their state but refuse lock and unlock.
  After a command over the broker, the lock's own report there sets the state until a
  push or a report from the lock reaches its station, also when it repeats the earlier
  state: the newest report shows. A station status update that repeats its last value
  does not count, so the broker's report stays shown then.
- A command counts only once the lock reports the asked state: the call waits for that
  report, up to 75 s in all (connecting and sending included), and the lock shows
  **Locking** or **Unlocking** meanwhile. As in the eufy app, a lock that does not answer
  fails after 20 s, and one that answers but does not report the asked state after another
  30 s. An automation's lock or unlock step waits up to 75 s; a step stopped meanwhile
  leaves the command running. A lock that refuses
  the command, reports a jammed bolt or reports nothing in time ends the call with an
  error, and the lock shows the state it last reported again. The shown state is always
  the lock's own report.
- Refused before anything is sent, with a message that says why:
  - a lock whose command the library does not send (T8500, T8510: Bluetooth frames
    through the station; T8504: no command route), with the library's reason;
  - an account that is a guest of the lock (the eufy app gives guests no lock control),
    or whose device list names no member record for the lock;
  - a lock that takes its commands over eufy's MQTT broker while the account has no
    connection to it (the library's `mqtt` extra is missing; the log says so);
  - a second lock or unlock while a command of the same lock still waits.
- Unloading or reloading the account entry stops a waiting command; the call then ends
  with an error, and whether the lock moved shows with its next report.
- Locked, unlocked, locking, unlocking and jammed as the lock reports them; passage mode
  shows unlocked, privacy mode locked, a bolt stopped between its positions jammed.
  Uncalibrated, calibrating and nothing reported yet show unknown. The attributes
  `lock_state` (the library's state, e.g. `passage_mode`) and `raw_value` (the value the
  lock reported; none for a state from a push) tell them apart.
- The state follows the lock's reports and pushes; nothing is polled. A jammed push shows
  until the lock's next report.
- A Wi-Fi lock the library reaches through no station gets no entity yet.
- Based on the eufy app's behaviour, not tested on a real lock.

## Garage doors

- **Garage door** cover of a garage-door camera (T8452, T8453), one per door the camera
  reports a motor or a sensor bound to; a door bound later appears with the report that
  binds it. A camera with two doors names them **Garage door A** and **Garage door B**,
  as the eufy app letters them. A door bound while the other was the camera's only one
  is **Garage door B** (or **A**) at once; the first door keeps **Garage door** until the
  integration reloads (a restart, or **Reload** in its menu), then reads **Garage door
  A** (or **B**) with the same entity id.
- Open and close where the door has a motor bound; a door with only a sensor shows its
  state only.
- Open, closed, opening, closing or unknown as the camera reports. Open and close return
  at once: the door shows opening or closing until the camera's next report for it (the
  camera reports no movement in between), for at most 5 minutes. A door that then
  reports another state, a refused command, or no report logs one warning, and the door
  shows the state it last reported.
- The camera takes one door command at a time. While one waits for its report, the same
  command of the same door again does nothing; the opposite command is refused with "The
  garage door is still moving. Try again when it has stopped.", and a command of the
  camera's other door with "The other garage door on this camera is still moving. Try
  again when it has stopped."; nothing is sent. Reloading the integration ends the wait.
- A door whose position is unknown refuses open and close with "The garage door's
  position is unknown. Use the eufy app to move it." and sends nothing. The eufy app's
  operate command, which runs the motor whichever way, is not offered.
- Based on the eufy app's behaviour, not tested on a real garage door.

## Detections

A motion sensor has a **Motion** binary sensor and a **Detection** event entity
for motion only. Motion turns off after the configured Detection hold because
the sensor does not report a motion-cleared event.

Each camera has a Motion, Person, Pet and Vehicle sensor and a **Detection** event
entity. A detection turns on only the sensor of its own kind (a person detection
leaves Motion off) for the **Detection hold** seconds; the event entity fires for every
kind, so an automation for "anything detected" triggers on it.

For a camera whose mode sends no detections (below), the **Recorded events** counter
still rises with each recording: see [Automation triggers](#automation-triggers).

### Detections in the security mode

In the eufy app each camera has a **Notification** switch per security mode (Security
mode → the mode → the camera). With it off, the station sends none of that camera's
detections while the mode is in force: its detection sensors and **Detection** event stay
unchanged, and the camera still records. Turning Notification on also notifies your phone.
Seen on HomeBase 3 stations; other stations follow the eufy app's mode settings, not tested.

Each camera and doorbell has a **Detections in current mode** diagnostic sensor:

| State | Meaning |
|---|---|
| `sent` | Notification is on for the device in the mode in force |
| `not_sent_in_mode` | Notification is off: no detections from it while this mode is in force |
| `unknown` | not known: no mode settings reported yet, the mode in force unknown, or a mode without per-device settings (Geofencing) |

Attributes: `mode` (the mode in force) and `sending_modes` (the armed modes that send
its detections; Disarmed is not listed). In an armed mode, `not_sent_in_mode` also
raises a repair issue, unless the option **Repair when a security mode holds detections
back** is off; see
[troubleshooting.md](troubleshooting.md#no-detections-from-a-camera). Motion sensors get no
such sensor: whether their Notification switch holds detections back is not known.

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

A HomeBase 2 detection arrives by [Cloud push](#cloud-push), which names no recording.
Home Assistant then looks for the camera's recording that started within 10 s of the
detection (else the one running at its time) and, once that recording has finished,
shows its first frame as the HD image (Thumbnail, then HD and HD only, with no
thumbnail before it; Thumbnail only shows none). The recording is found about the
camera's clip length plus 30 s after the detection's time, and the search ends 3
minutes after that time; fetching the frame adds a few seconds. With no such recording
by then the image stays. Two detections inside one recording get its frame once: it
replaces whatever the camera showed since the first of them (an event picture, a button
pressed between them), but not a button pressed after the last one. A later copy of
the same detection's push does not search again. This works by matching the recording
by camera and time; not tested on a HomeBase 2.

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
| **Capture live image** | what the camera sees now; only a camera with live video ([devices.md](devices.md#other-models)) | yes |

The **Refresh event image keyframe** option selects which keyframe of the stored
recording **Refresh event image** shows (unless Event image is Thumbnail only): 1, the
default, is the first. Keyframe spacing depends on camera and recording, so a numbered
keyframe is not a fixed second. Each later keyframe plays more of the recording: the
refresh takes longer (about 2.4 s more for keyframe 5 on a HomeBase 2), and image
fetches for other cameras on the same HomeBase wait meanwhile. A recording with fewer
keyframes uses its last one. Detection images always use the first keyframe.

A detection after a button press replaces the button's image; a press after a
detection keeps its image when that detection's HD image comes later (a HomeBase 2's,
above). For a still at a fixed
interval, an automation with a time pattern trigger can press **Capture live image**;
each press wakes a battery camera. For the camera's full picture size, turn on the option
**Capture live image at full resolution**.

**Attributes:** `image_source`, `triggered_at` (the event's time) and
`image_updated` (changes with every new image, so a card can refetch).
`image_source` is one of:

| Value | Image | `triggered_at` |
|---|---|---|
| `thumbnail` | a detection's event thumbnail | the detection's time |
| `trigger_frame` | the first frame of a HomeBase detection's recording (HD) | the detection's time |
| `detection_live` | a live picture a camera without a HomeBase took for a detection (HD) | the detection's time |
| `push_picture` | the picture a detection's cloud push names: a recognised face or an event picture (see [Cloud push](#cloud-push)) | the detection's time |
| `live` | a **Capture live image** press, or the live image of a camera with none | none |

A detection's HD image is `trigger_frame` or `detection_live` with `triggered_at` set
to the detection's time. A camera has no image, and no `image_source`, until its first
detection or button press. The last image of each camera and preset survives a
restart.

**Last event** image entity: the same picture as `image.<camera>_last_event`, for
`picture-entity` cards and automations. Its state is the time the image was stored, its
attributes `image_source` and `triggered_at` as above; unknown until the first image.
Showing it never wakes a camera.

`video_codec` is `hevc` or `h264`: the codec of the camera's latest picture since Home
Assistant started (most cameras send HEVC; cameras on a HomeBase 2 and some doorbells and
Solo cameras send H.264). Absent until the camera has sent a picture. Stills, recordings
and live view work in both codecs.

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
  the latest within 15 minutes. A HomeBase 2 detection's recording is copied once it
  has been found and has finished (see [Camera images](#camera-images): about the clip
  length plus 30 s after the detection), after its HD image (at most about a minute;
  when that image was not shown and other detections of the camera are still being
  matched, once those are done, so a later detection that shows it names the video too)
  and after a copy pass already running; the 15-minute pass copies the rest.
- Named like the event's image (same date, time and kind), so the two sit side by
  side; a recording whose image was not saved is named by its start, kind `event`.
- The HomeBase plays the recording off its own storage: no camera is woken. Each copy
  uses one session of **Sessions per HomeBase** while it runs.
- Copied once: a restart or a deleted file does not copy it again. A copy that fails
  is tried again on later passes, three times in all; then one warning is logged and
  the recording stays on the HomeBase only. A recording whose video codec changes
  partway is cut at the change on every copy, so it is not tried again; play it in the
  eufy app. A recording end-to-end encrypted under the owner's cloud key whose key could
  not be fetched or used counts as a failed copy and is tried again the same way.
- A recording protected by the eufy app's **Local Advanced Encryption** cannot be
  copied: its key is protected by the PIN of that encryption, which Home Assistant
  neither asks for nor stores. It is not tried again and logs no warning; a repair
  names the camera. To have new recordings copied, turn Local Advanced Encryption off
  in the eufy app (**Settings > Security > Media Content Encryption**). Recordings made
  while it was on stay encrypted and play only in the eufy app. The always-on Local
  Storage Encryption does not matter. Reported with a HomeBase 2 and a Video Doorbell
  S330 (T8213); recordings end-to-end encrypted under the owner's cloud key are
  decoded (based on the eufy app's behaviour, not tested on hardware). The repair goes
  once a later recording of that camera is copied (the encryption is off again), or with
  the next reload or restart, and comes back only for a new such recording.
- The HomeBase is asked for one day of recordings at a time, oldest first. A day it
  does not list within a minute is asked again on the next pass; the days before it
  stay copied.
- The video is the camera's own (HEVC, or H.264 where the camera sends it) and AAC in an
  MP4, never re-encoded. It plays in
  browsers that play the live view (see [network.md](network.md#browsers)).
- Needs **Event history** above 0 days. A standalone camera keeps no recordings on a
  HomeBase, so it has no event videos.
- A 4K recording is several MB per 10 s; plan the media folder's space accordingly.

### Recordings on the HomeBase

Whatever the option, the recordings a HomeBase keeps for a camera are listed on
request (the camera card's **Station** list, over the websocket commands
`eufy_home_security/recordings`, `eufy_home_security/recordings/play` and
`eufy_home_security/recordings/fetch`). Each can be played or saved:

- **Play** streams the recording to the viewer and saves nothing. Home Assistant
  repackages the recording's video and sound into HLS in memory (no re-encoding,
  nothing written to disk), which the card plays with Home Assistant's own player and
  Safari plays natively. The recording arrives at its own pace; the picture starts
  once its first piece (two to four seconds of video, up to the next keyframe) has
  arrived, from the beginning, and runs to the last frame. Seeking works within what
  has arrived; after the end the recording stays available for seeking back while
  the player stays open, for at most 30 minutes. A play keeps at most 96 MB of video
  in memory; past that the oldest part can no longer be sought back to. Each viewer
  holds one HomeBase session while the HomeBase sends the recording (usually faster
  than it plays): plays, live views and live stills share **Sessions per HomeBase**,
  and a play past it waits for a session, then fails with "every session of the
  HomeBase is in use". Closing the
  player stops the HomeBase sending at once. A recording whose video codec changes
  partway plays up to the change. A recording protected by Local Advanced Encryption
  (see [Event videos](#event-videos)) does not play; the card says how to turn the
  encryption off. Tested with the fake station and with recorded
  HomeBase video in Firefox and Chrome; not yet checked with a HomeBase.
- **Save** copies it into the event history exactly as an event video: same name,
  noted as copied (the option's sync skips it), kept and deleted with the history. A
  recording whose video codec changes partway cannot be saved: the card says so, and
  later requests for it (or one the event-videos sync gave up for this) are refused
  without downloading it again; play it in the eufy app. One protected by Local
  Advanced Encryption is refused: the card shows "Protected by Local Advanced
  Encryption" and, under that recording's row, how to turn it off. Needs **Event
  history** above 0 days.

A recording still running can be played or saved once it has finished. A HomeBase 2's
recordings are listed, played and saved the same way (the library's HomeBase 2 listing, playback and download checked on a
HomeBase 2 by a contributor; the Station list with a HomeBase 2 tested on the fake
station only).

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

- `eufy_home_security.stop_recording` on the same camera ends a running clip now: the part
  recorded so far is saved as a complete clip (`complete: true`) and the record action
  returns. Stopped before the camera sent its first picture, nothing is saved and the record
  action raises an error. With no clip running it is refused.
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
- The camera entity's state is `streaming` from a view's first picture until the last
  viewer leaves (`recording` while it records a clip), else `idle`.

Browser support: [network.md](network.md#browsers). Adding devices:
[devices.md](devices.md#adding-a-device).

## Pan/tilt cameras

Which cameras get these: [devices.md](devices.md#pan-tilt-presets-and-zoom).

| Entity or action | Effect |
|---|---|
| **Refresh presets** button | Reads the camera's presets and captures each one (only reads them on a camera without live video). Press it on a fresh install and after changing presets in the eufy app |
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
| `eufy_home_security/preset_image` (websocket) | `entity_id`, `preset`, `image` (base64 JPEG, at most 2 MB): keeps the picture as that set slot's **Preset n image**, as a capture does; no camera I/O. The camera card's **Save view** sends its frame of the live video here |

- Every press and action wakes the camera.
- A saved view's slot shows no image until it is captured, unless the camera card
  saved it: the card sends the frame of the live view it saved.
- A preset deleted in the eufy app makes its entities unavailable after **Refresh
  presets**.
- While a capture runs, other captures and preset edits are refused and nothing is
  sent.
- The actions target the camera entity, for camera cards with PTZ controls.
- A camera without presets (T8410) gets only the pan/tilt buttons and `pan_tilt`; the
  preset actions are refused, and `zoom` on a camera without zoom too. Nothing is sent.
- A camera with presets but no live video gets no **Capture preset n** buttons, its
  **Preset n image** entities stay empty, and it refuses `capture_preset`; nothing is
  sent.
- A camera that cannot save a view (T8173) gets no **Save current view** button (nor
  the card's Save view tile) and refuses `save_preset`. One that cannot change its
  default preset (T8419, T8423, T8W11) shows its **Default preset** but refuses a
  change and `make_default`. Nothing is sent.

## Standalone battery cameras and doorbells

- A camera or doorbell without a HomeBase is its own device with an alarm panel,
  battery, signal, camera and buttons.
- It is reached on demand when its model is on the eufy app's on-demand list (such as
  the T8170, the SoloCam C20 or a 4G Starlight Camera) or is marked as a battery device
  (such as the SoloCam E50, the Solar Wall Light Cam S120 or most battery doorbells).
  Other models keep a connection: models not marked as battery devices, such as the
  wired doorbells, the Wall Light Cam S100 or the eufyCam C31, and battery models
  without bundled settings, such as the Battery Doorbell 8212 or the eufyCam E52. The
  points below are for one reached on demand.
- Home Assistant keeps no connection to it, so it sleeps. Guard mode, battery and
  signal come from eufy's cloud and can be up to an hour old.
- Arming, every button, every capture and each detection's picture wake it (about
  10 s) and cost battery.
- Its local storage is not read, since each read would wake it: no storage entities.
  Their rows from an earlier release are removed at the next start.
- Its detections reach Home Assistant only with
  [Cloud push](#cloud-push) on; without it, its detection sensors stay off.

## Cloud push

**Cloud push for detections** option, off by default.

Motion detections from a T8910 sensor behind a HomeBase 2 arrive through cloud
push. Whether a HomeBase 3 also delivers these events over LAN is unverified.

- Home Assistant listens for eufy's push messages, as the eufy app on a phone does.
  The app keeps working.
- Needed for the detections (sensors, detection event, event image) of every station
  that does not send them over the LAN: a standalone camera, a HomeBase 2 and its
  cameras, or a station model not yet checked. A HomeBase 3 reports its cameras'
  detections over the LAN without it.
- While it is off and such a station is set up, the repair **eufy detections need
  cloud push** names the stations; its **Fix** switches the option on and reloads.
- A detection that arrives both over the LAN and from the cloud fires once. When a
  later push of the same detection names a more specific kind (motion, then a
  person, pet, vehicle or identified person), that kind's sensor turns on and the
  detection event fires again with the new kind.
- A HomeBase 2 detection push carries no picture path of the station; when it names a
  picture of its own (the face eufy recognised, or an event picture), that picture is
  the camera's event image (`image_source` `push_picture`), fetched from eufy's cloud
  without waking the camera. eufy's encoded pictures are decoded; a picture protected
  by the camera's Video Encryption setting is not shown. Not with the Event image
  choice HD only. Declared from the eufy app's behaviour, not tested on a HomeBase 2.
- A HomeBase 2 detection's recording is found by camera and time once it has finished:
  then its first frame replaces the event image and, with **Save event videos**, its
  video is copied ([Camera images](#camera-images)). Not tested on a HomeBase 2.
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

A device whose model has no battery level in the eufy app gets no **Battery**,
**Charging**, **Solar charging**, **Solar intensity**, **Battery temperature** or
**Working days** entity. Such devices include a mains-powered camera or most wired
doorbells (the HomeBase still reports a charging field for them), the Siren (T8970; the
E20 sirens keep theirs), the Keypad (T8960), the Entry Sensor (T8900; the E20 keeps its),
the Water and Freeze Sensor (T8920), the Smoke Alarm Listener (T8931) and the Doorbell
Chime mini (T8028). A **Battery low** binary sensor, where the device reports one, stays.
Rows an earlier release created for such a device are removed at setup; their history
stays in the recorder, and automations on those entity ids stop working.

| Device | Entity | Type | Shows |
|---|---|---|---|
| Camera | **Charging** | binary sensor (battery charging) | Charging from any source. Attributes `solar_charging`, `power_source` (raw source code: 0/2 none, 1 USB, 3 AC, 4 built-in solar, 5 USB + built-in solar, 6/8 external panel, 7/12 external + built-in, 20 connected panel; other codes as reported) |
| Camera | **Solar charging** | binary sensor (battery charging) | Charging from a solar source |
| Camera | **Solar intensity** | diagnostic sensor | Raw solar input, 0 at night or without a panel; scale differs by model |
| Camera | **Battery temperature** | diagnostic sensor, °C | |
| Camera | **Working days** | diagnostic sensor, days | Days since the last USB charge |
| Camera | **Detected events**, **Recorded events** | diagnostic sensors, total increasing | Counted since the last USB charge; as automation triggers see [Automation triggers](#automation-triggers) |
| Motion or entry sensor | **Battery low** | diagnostic binary sensor (battery) | The sensor's own low-battery flag |
| HomeBase, or a camera that is its own station | **Storage problem** | diagnostic binary sensor (problem) | On when the storage status is not one the app shows as normal for that model. Attribute `storage_status` (raw code) |

**Disk and eMMC** entities (used, free, size, used percentage, temperature, wear,
problem, formatting) come from the HomeBase's storage record, read every 30 minutes.
Each HomeBase gets only the figures its record carries: a HomeBase 2 from firmware
3.3.7.7 reports sizes only (below that, see the next paragraph), a HomeBase mini its
eMMC only. A HomeBase that refuses the storage query gets none.
An entity an earlier release created for a figure the HomeBase does not report is removed.

A camera without a HomeBase (not one reached on demand, see
[above](#standalone-battery-cameras-and-doorbells)), a HomeBase 2 on firmware below
3.3.7.7 and the models T8001, T8002, T8020 and T8023 report
their local medium, a microSD card or a built-in eMMC, as a status with a size. They
get **eMMC used space**, **eMMC free** and **eMMC size** (off by default), named eMMC
whatever the medium, **Local storage usable** (diagnostic binary sensor) and **Local
storage** (diagnostic sensor: Normal, No card, Corrupted, Other problem, or Not usable
for a normal status without a size). The state is the eufy app's meaning of the status
for that model. While the device reports no size (no card) the sizes show unknown; a
medium that is not usable but reports a size keeps its figures. They fill in without a
reload once a card is in place.

**Firmware update** entities also carry, when reported: `subsystem_firmware` on the
HomeBase (version strings by parameter id; the subsystems are unnamed) and
`siren_actions` on a camera (raw siren action per guard mode, keyed `away`, `home`,
`custom_1`…).

## Automation triggers

One trigger per subsection; replace the entity ids with your own. To add one, open a pull
request that edits this section.

### A recording was counted

The camera's **Recorded events** sensor rises when the camera records. It works in a
security mode whose Notification switch is off, where the **Detection** event does not
fire ([Detections in the security mode](#detections-in-the-security-mode)).

```yaml
triggers:
  - trigger: state
    entity_id: sensor.front_door_recorded_events
    not_from: [unavailable, unknown]
conditions:
  - condition: template
    value_template: >-
      {{ trigger.from_state is not none and trigger.to_state is not none
         and trigger.to_state.state | int(0) > trigger.from_state.state | int(0) }}
actions:
  - action: notify.notify
    data:
      message: >-
        Front door recorded
        {{ trigger.to_state.state | int(0) - trigger.from_state.state | int(0) }} event(s)
```

- `not_from` and the condition skip a restart or reload (the change to and from
  unavailable, the entity added or removed) and the counter's reset after a USB charge.
- The station counts a recording some seconds after it starts, and the sensor shows the
  count at the next refresh (45 s), so the trigger fires up to about a minute after the
  recording started. Several recordings between two refreshes fire once; `to - from` is
  their number.
- It carries no time, kind, picture or clip. Where the **Detection** event fires, an
  automation on both notifies twice.

### A detection was counted

The same trigger on **Detected events** (`sensor.front_door_detected_events`) fires when
the camera's detection count rises. It can rise without a recording; how often depends on
the camera and its settings. The limits above apply.

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
  or text. A camera's lamp is a light (below).
  - A numbered scale is a number, not a choice: detection sensitivity 1–7 is a slider.
  - A setting with several choices on at once is one switch per choice, named
    "Detection types: Pet". A toggle changes that one choice; the others stay. This
    covers detection types and the HomeBase's mode-switch notifications.
  - Some on/off settings share one device value with others (the HomeBase's
    notification switches). A toggle changes only its own part. It is refused, with
    nothing sent, while the device does not report that value.
- The **Camera** switch turns a camera on or off; a camera turned off stops detecting
  and recording.
- A setting's device class and unit follow what the library says its value means,
  never its identifier:
  - a length of time is a duration in the library's unit (seconds on every bundled model);
  - a percentage is in %;
  - a temperature has no unit and no device class: the model data does not say
    Celsius or Fahrenheit;
  - a point in time (a glass-break sensor's last break) is a timestamp, unknown while
    none is recorded;
  - a time of day is text (`20:00`);
  - an on/off status is a binary sensor of class door (on = open), tamper, battery
    (on = low), moisture, cold, sound (a siren ringing), smoke or problem (a fault, or a
    device at the end of its service life);
  - a status made of on/off parts (a smoke sensor's) is one binary sensor per meaning
    of its parts, on while any part of that meaning is on: **Smoke** (smoke class; an
    alarm or a high alarm), **Problem**, **End of life** (problem class), **Low battery**
    (battery class), and **Alarm muted** and **Testing** without a class (Home Assistant
    has none for them). Unknown until the device reports its status. Declared from the
    eufy app, not tested on hardware: whether a test press also turns **Smoke** on is
    not known. A meaning a later release no longer gives loses its binary sensor at the
    next start.

  A setting sensor has no state class, so Home Assistant keeps no long-term
  statistics for it and a unit change never raises a statistics issue.
- A camera's lamp is a **light**, named as the eufy app names its switch (**Light**,
  **Spotlight**; a track light's second lamp **Zoom spotlight**), wherever the library
  can switch it in the camera's setup. It turns the lamp on and off and, where the
  camera's brightness setting is writable, sets the brightness: a brightness with levels
  (Low, Medium, High = 40, 70, 100 % on a eufyCam 3) takes the nearest level, a range
  the percent within its bounds, and 1 % or less turns the lamp off, as the eufy app
  does. A turn-on with a brightness writes the brightness first, then switches the lamp
  on if it is off. On and off are verified on a eufyCam 3 behind a HomeBase 3 and a
  standalone SoloCam S340, the brightness on the eufyCam 3; other models are declared
  from the eufy app.
  - The light replaces the lamp's switch of earlier releases: `switch.<camera>_light`
    becomes `light.<camera>_light` (`switch.<camera>_spotlight` becomes
    `light.<camera>_spotlight`), and the switch's registry entry is removed at the next
    start. What you set on the switch carries over to the light: its entity id, also one
    you changed (`switch.garden_lamp` becomes `light.garden_lamp`; Home Assistant adds
    `_2` when another light already has that id, and the log names the id the light
    got), its name, icon, area, labels and voice assistant aliases, which voice
    assistants it is exposed to, and whether you hid it. An enabled switch gives an
    enabled light, also with **Enable newly added entities** turned off in the
    integration's system options; a switch you disabled gives a disabled light. Unlike
    the switch, a configuration entity, the light has no category, so it shows in
    auto-generated dashboards; it keeps the voice assistant exposure stored for the
    switch, and where none was stored each assistant treats it as any new light.
    Automations, scripts and dashboards that used the switch still need the light entity
    and `light.turn_on` / `light.turn_off`.
  - A light you made from the lamp's switch with **Switch as X** holds the switch's
    voice assistant exposure (the helper moves it from the switch to its own light), and
    the integration's light gets that exposure instead. Home Assistant removes a helper
    that is loaded (or being set up) when the switch is removed, and the integration's
    light then replaces the helper's light: it takes its entity id
    (`light.<camera>_light`, or the one you gave it, also when you change it before the
    helper is removed) and what you set on it (name, icon, area, labels, aliases,
    whether you hid or disabled it), so automations that use the helper's light keep
    working. A helper that is not loaded stays, with its light unavailable: the
    integration's light then takes the next free id (`light.<camera>_light_2`) and the
    switch's settings. You can delete the helper and change the integration's light's
    entity id to `light.<camera>_light`. When several helpers wrap the switch, the first
    one making it a light counts. A **Switch as X** helper that makes the switch
    anything other than a light (a fan, a lock) is removed by Home Assistant with the
    switch, and nothing takes its place.
  - The lamp's brightness setting stays an entity, disabled on a new install (the
    light sets it); one you already have stays as it is. The lamp's colour temperature
    and its other settings (**Lighting**, motion-activated lighting, schedules) stay
    their own entities; the light offers no colour temperature.
  - Where the eufy app disables the lamp's control while a setting such as
    **Lighting** is off, the light refuses to switch, with nothing sent, until that
    setting is on. While that setting is not reported, the light switches.
  - A lamp whose switch is not read from the camera's lamp report (the T8442 and
    T8L60) is marked as assumed state. One the camera never reports (the T870G) is too,
    and shows the value set from Home Assistant, as any control the device never reports
    back.
  - A lamp the camera can only report in its setup (a Wall Light Cam S100 behind a
    HomeBase) gets no light: its read-only sensor stays.
- A status it can only read is enabled: one the eufy app shows on the device card
  (an entry sensor's door, tamper or low battery) is a sensor or binary sensor, with
  the device class above where its value has a meaning; one the app shows on a
  settings page is listed under Diagnostic. A
  status the device has not reported shows unknown. The door (and tamper) state
  follows each open and close as the HomeBase reports it, without waiting for the
  45 s refresh.
- A setting it can only read but the app offers as a control is a diagnostic sensor
  or binary sensor, disabled until you enable it. A value the app does not show, one
  another entity already gives (battery, firmware update in progress), a status made
  of several fields, a code the device card shows only as an icon (connection
  type, Wi-Fi level), and a setting it can neither read nor write have no entity.
  Their rows from an earlier release are removed at the next start.
- A setting value made of several fields is never shown as the state: the state is
  unknown and the attribute `value` holds the fields, or the value as the device
  reported it when it does not parse. Fields that locate your home (`location`,
  `latitude`, `longitude` and their short forms `lat`, `lng`, `lon`) and address fields
  (a name whose last word is `address` or `addr`: `home_address`, `routerAddr`, so a
  network or e-mail address too) are left out, at any depth: a light's motion-activated,
  ambient-light and dusk-to-dawn settings can carry them. A text longer than 255
  characters is cut to 255. Such a value gets no control even when writable (a light's
  schedule or motion-activated settings): a sensor when readable, else no entity; the
  text entity of an earlier release is removed at the next start.
- A read-only status an earlier release created disabled is enabled at the next start
  (Home Assistant reloads the integration once), unless you disabled it yourself or
  "Enable newly added entities" is off.
- **Per-mode actions and delays** (what each camera, doorbell and sensor does in Home,
  Away and Custom 1–3, and each mode's entry and leaving delays) have no entities: the
  [HomeBase card](homebase-card.md) edits them. Their switches, numbers and sensors
  from an older release are removed at the next start. A doorbell behind a HomeBase
  offers the actions the eufy app shows for its model (declared from the eufy app, not
  tested).
- A write shows the written value at once.
  - The station refuses it or does not apply it: one of the setting errors in
    [troubleshooting.md](troubleshooting.md#setting-errors); the entity keeps the value
    last reported. The error names the station that answered and, for a device behind
    a HomeBase, the device before the setting.
  - No answer in time: error *setting unconfirmed*; the entity shows unknown until
    the next update shows the value in force.
  - A control the device never reports back is marked as assumed state: unknown until
    you set it from Home Assistant, then the value you set; unknown again after a
    restart or reload; after a write with no answer in time, unknown until you set it
    again.
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
password, keys and cached images are deleted; with the last account also the dashboard
resources of the bundled cards (remove those cards from dashboards first). The sign-in
hold-off is kept, so removing and re-adding cannot trigger eufy's lockout. Event
history files stay.
