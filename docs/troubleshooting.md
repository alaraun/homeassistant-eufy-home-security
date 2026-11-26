# Troubleshooting

## Debug logging

```yaml
logger:
  logs:
    custom_components.eufy_home_security: debug
    eufy_home_security: debug
    firebase_messaging: debug  # cloud push only
```

Or at runtime: **Settings → Devices & services → Anker eufy Home Security → ⋮ →
Enable debug logging**.

Passwords, codes and captcha answers are logged as `***`. Serials are shortened to
their model prefix and last 4 characters, account ids and tokens to their last 4.
Read a log before sharing it.

## Adding the account

"Home Assistant could not reach eufy" covers every failure without its own message
(network, eufy's answer, the device list after the sign-in). The log then has one
warning `eufy sign-in did not complete: <error>` from `custom_components.eufy_home_security`
naming the cause; attach it to an issue. No entry exists yet, so turn on debug logging
in `configuration.yaml` (above) for the full request trail.

## Diagnostics

**⋮ → Download diagnostics** on the integration entry: session health (in each station's
`session`, `connect_path` and `connects_by_path` say whether its links come up on the
LAN, `lan`, or through eufy's servers, `rendezvous`, and `lan_reply_blocked` whether a
station on Home Assistant's network needed eufy's servers because its LAN reply did not
arrive), LAN warnings,
storage figures, event-video copy counters (`recording_sync`), cloud push state
(`push`), whether the security mode in force sends each device's detections
(`detection_delivery`), whether each camera gets a live stream and why (`live_support`:
its grade, `verified`, `declared` or `unknown`, and the library's reason), options,
`models` (how the library knows each product code; see
[devices.md](devices.md#other-models)) and `account_report`: every device eufy's lists
name for the account, whether the integration serves it, its firmware, and whether
eufy's keys for it are usable. Serials are shortened and device names removed; no key
leaves Home Assistant. The account report asks the eufy cloud a few questions on the
existing session (never a sign-in), so the download takes a few seconds. It works for
an account where no device was found too.

## Repair issues

| Issue | Meaning | Fix |
|---|---|---|
| eufy ended Home Assistant's session | another client signed in with the same account | **Fix**, which signs the other client out; see [network.md](network.md#eufy-account) |
| eufy is refusing sign-ins | a sign-in was needed and eufy refused it | wait until the time it names; each early attempt can restart the wait. It clears at that time, or at the next start of the integration that needs no sign-in |
| Home Assistant is holding back eufy sign-ins | the integration's own limit of 3 sign-ins per 6 hours on one eufy cluster is used up; every sign-in country on that cluster, extra countries included, counts against it | wait until the time it names; it clears then, or at the next start that needs no sign-in |
| A station rejects its key | the station refused its key after one refetch | **Fix** allows one more key fetch |
| Fetched the key again | the key was refreshed; information only | dismiss |
| HomeBase stamps its events with another eufy account | commands may be ignored | check that the account is the owner or the owner shared the HomeBase with it |
| eufy has no key for a station | eufy holds no key for the key number the station uses, so it cannot connect; its entities are unavailable | in the eufy app, on the owner's account, check the station is there and shared with this account, then reload; asked again at most once an hour otherwise |
| eufy sends no key for a station | eufy answers for the key number the station uses but without the key, so it cannot connect; its entities are unavailable; signing in does not help | reload; asked again at most once an hour otherwise. If it stays, open an issue with the diagnostics download |
| eufy serves no usable key for a station | eufy serves no key for the station's connection method, so it cannot connect; its entities are unavailable; fetching again or signing in does not help | open an issue with the diagnostics download |
| No eufy devices found | no eufy region lists a device for the account | check the account sees the devices in the eufy app and the **eufy sign-in country** option matches the country the app signs in with, add the country of anyone who shared a home with it as an extra country (experimental; [network.md](network.md#eufy-sign-in-country)), or turn on **Look for devices under every sign-in country on each refresh**, press **Refresh device list**; see [network.md](network.md#eufy-regions). Still none: open an issue with the diagnostics download |
| eufy invitation not accepted | the account has an invitation it has not accepted (a home or device shared with it; with devices listed, read on **Refresh device list**); the repair names the home or device and who sent it | **Fix** shows each open invitation in turn, with a code field when the sender protected it; after the last one Home Assistant reloads and asks eufy once more for the devices. Or accept it in the eufy app, signed in with this account, then **Fix** loads the devices. See [network.md](network.md#eufy-account) |
| eufy station not supported | eufy lists a station whose P2P id local connections do not accept; the eufy app does not connect to it locally either. The repair names it by its model and last four serial characters. It and its paired devices are left out; the account's other stations work | none in Home Assistant; it is checked again on every reload and **Refresh device list** |
| eufy station needs WebRTC | eufy lists a station the eufy app reaches only over its WebRTC connection (decided per station as the eufy app does, e.g. the HomeBase Professional S1 T9000 or the NVR T8N00), which the integration does not support yet. It and its paired devices are left out, named by model and last four serial characters; the account's other stations work | none in Home Assistant; it is checked again on every reload and **Refresh device list** |
| eufy device not supported | eufy lists a device whose serial number is not only letters and digits; it is left out (a station with its paired devices) | none in Home Assistant; if the device shows in the eufy app, open an issue with the diagnostics download |
| Camera event history is not kept | no host directory mounted at `/media` (Container) | see [usage.md](usage.md#event-history) |
| eufy detections need cloud push | cloud push is off, and a station does not send its cameras' detections over the LAN (a standalone camera, a HomeBase 2, a model not yet checked); their detection sensors stay off | **Fix** switches cloud push on and reloads; or ignore it to leave push off; see [usage.md](usage.md#cloud-push) |
| No detections from some cameras in a mode | the security mode in force has the **Notification** action off for the cameras it names, so the station sends none of their detections while that mode is in force; they keep recording. Not raised while disarmed | in the eufy app: Security mode → the mode → each camera → Notification on (also notifies your phone); see [usage.md](usage.md#detections-in-the-security-mode). Clears when a mode that sends them is in force or the setting changes. To keep the mode silent, ignore it: the ignore applies to that mode only, and is lost when the mode leaves and comes back and on every restart or reload (Home Assistant forgets an ignored repair once it is withdrawn), so the repair shows again then. To turn it off for every mode, switch off the option **Repair when a security mode holds detections back** ([usage.md](usage.md#options)) |
| eufy stations on this network connect through eufy's servers | a station on Home Assistant's own network does not get its replies to the local search through, so each link is set up through eufy's servers; commands and video stay on the LAN, but a reconnect needs the internet. The repair lists each station's address | allow all UDP from that address in the firewall on the Home Assistant host, then reload the integration; see [network.md](network.md#firewall). Clears after the next link over the LAN; ignore it to keep the path |
| eufy cloud push is not running | cloud push is on but not receiving; detections of standalone cameras and a HomeBase 2 are late or missing | retries by itself; check internet access and the log, or switch the option off; see [usage.md](usage.md#cloud-push) |

## Setting errors

An error that names a station names the one that answered: the HomeBase for a camera or
sensor behind it, which the error then names before the setting, as Home Assistant names
the entity. "Hallway Hub refused the change to Garage Cam Light" is the HomeBase
Hallway Hub refusing the Light of the camera Garage Cam. A HomeBase's own setting, and
a standalone camera's, names the station once ("Porch Cam refused the change to Light").

| Error | Meaning | Entity shows |
|---|---|---|
| "{station} took the change to {target} but did not apply it." | the station acknowledged the write and did not act on it; check that the device is online and try again | the value last reported |
| "{station} refused the change to {target}." | the station refused the value | the value last reported |
| "{station} answered that the device does not support changing {target}." | the station answered that the device does not support the change | the value last reported |
| setting unconfirmed | no answer in time; the write may still apply | unknown until the next update (a control the device never reports back: until you set it again) |
| does not accept this eufy account | the station refused the request for the account it was sent under (an arm, a setting, a siren, a pan/tilt or preset command, a record action, a HomeBase card change, the camera card's Station list); an entity's or action's error for a device behind a HomeBase names the device too. Trying again changes nothing. In the eufy app, check that the station is shared with this account | a setting entity the value last reported; nothing is recorded or saved |
| "{target} was not switched, and nothing was sent: the eufy app turns the lamp's control off while {setting} is off. Turn {setting} on first, in the eufy app or with its switch here." | a camera's light (`{target}`) was turned on or off while the setting that enables its lamp control (`{setting}`, such as **Lighting**) reads off; see [usage.md](usage.md#device-settings) | the state last reported |

## No detections from a camera

1. Its **Detections in current mode** sensor reads `not_sent_in_mode`: the security mode
   in force holds its detections back; see
   [usage.md](usage.md#detections-in-the-security-mode).
2. A camera or doorbell reached on demand (see
   [usage.md](usage.md#standalone-battery-cameras-and-doorbells)) or a camera on a
   HomeBase 2 needs [Cloud push](usage.md#cloud-push).
3. Check the camera's detection settings in the eufy app (motion detection on, the
   detection types).
4. Still nothing: open an issue with the diagnostics download (it has
   `detection_delivery` per device) and a debug log of a detection.

## A station is unavailable

The log names it: `Station T8030***1234 did not come up (StationUnreachableError)`. A
station on the LAN answers LAN discovery; one on another subnet or at another site is
reached through eufy's servers, which needs a working eufy session (the station's
connection key comes from eufy) and outbound UDP 32100-32102 to eufy's servers; see
[network.md](network.md#lan). The diagnostics' `connect_path` shows which path the last
link took.

## Known limitations

- **HomeBase session limit**: another P2P client or a second Home Assistant instance
  can leave no room for stills or live views; see
  [network.md](network.md#homebase-sessions).
- **Sign-out by another client** is noticed within 6 hours, or at the next command
  that needs eufy.
- **Cameras and doorbells reached on demand and a HomeBase 2** report detections only with
  [Cloud push](usage.md#cloud-push) on.
- **A HomeBase 2 detection's HD image and video** come only once its recording has
  finished (about the clip length plus 30 s), found by camera and time: no recording of
  the camera that started within 10 s of the detection or was running at its time, by 3
  minutes after the detection's time, gives neither. **Refresh event image** and the
  15-minute copy pass of **Save event videos** still pick the recording up. Not tested
  on a HomeBase 2.
- **A camera that changes video codec during live view** (a battery doorbell that opens
  on HEVC and goes on in H.264) restarts the stream at the change on the open camera,
  without a second wake. The [camera card](card.md) shows a pause of a few seconds over
  WebRTC. When the card picks WebRTC (the camera's last picture was H.264) and the camera
  opens on HEVC in a browser that cannot receive HEVC over WebRTC, the first WebRTC
  session gets sound only; the card opens a second one about 3 s later, which plays the
  H.264 part (in our test the first picture came about 4-6 s after the start, plus the
  camera's wake; the camera woke once). Otherwise such a browser plays the camera over
  HLS from the start.
  Over HLS the picture comes back once HA's playlist holds only the new codec: about
  30-40 s after the change in our test. Home Assistant's own camera views do not reopen
  their player and stay frozen until they are opened again. HA's stream component logs
  `Error from stream worker: Error demuxing stream` at each change for an HLS viewer.
- Not supported: firmware installation.
