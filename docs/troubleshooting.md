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

## Diagnostics

**⋮ → Download diagnostics** on the integration entry: session health, LAN warnings,
storage figures, event-video copy counters (`recording_sync`), cloud push state
(`push`), options, `models` (how the library knows each product code; see
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
| eufy is refusing sign-ins | a sign-in was needed and refused: by eufy, or held back by the integration itself (3 sign-ins per 6 hours on each eufy region) | wait until the time it names; each early attempt can restart the wait. It clears at that time, or at the next start of the integration that needs no sign-in |
| A station rejects its key | the station refused its key after one refetch | **Fix** allows one more key fetch |
| Fetched the key again | the key was refreshed; information only | dismiss |
| HomeBase stamps its events with another eufy account | commands may be ignored | check that the account is the owner or the owner shared the HomeBase with it |
| eufy has no key for a station | eufy holds no key for the key number the station uses, so it cannot connect; its entities are unavailable | in the eufy app, on the owner's account, check the station is there and shared with this account, then reload; asked again at most once an hour otherwise |
| eufy serves an unusable key for a station | the station uses an older connection method whose key eufy serves unreadable; its entities are unavailable; fetching again or signing in does not help | install a firmware update for the device if the eufy app offers one, then reload |
| No eufy devices found | no eufy region lists a device for the account | check the account sees the devices in the eufy app and the **eufy sign-in country** option matches the country the app signs in with, add the country of anyone who shared a home with it as an extra country (experimental; [network.md](network.md#eufy-sign-in-country)), or turn on **Look for devices under every sign-in country on each refresh**, press **Refresh device list**; see [network.md](network.md#eufy-regions). Still none: open an issue with the diagnostics download |
| eufy invitation not accepted | the account lists no devices and has an invitation it has not accepted; the repair names the home and who sent it | accept it in the eufy app, signed in with this account, then **Fix**: Home Assistant reloads and asks eufy once more for the devices |
| Camera event history is not kept | no host directory mounted at `/media` (Container) | see [usage.md](usage.md#event-history) |
| eufy cloud push is not running | cloud push is on but not receiving; standalone cameras' detections are late or missing | retries by itself; check internet access and the log, or switch the option off; see [usage.md](usage.md#cloud-push) |

## Setting errors

| Error | Meaning | Entity shows |
|---|---|---|
| setting not applied | the device rejected the write | the value last reported |
| setting unconfirmed | no answer in time; the write may still apply | unknown until the next update |

## Known limitations

- **HomeBase session limit**: another P2P client or a second Home Assistant instance
  can leave no room for stills or live views; see
  [network.md](network.md#homebase-sessions).
- **Sign-out by another client** is noticed within 6 hours, or at the next command
  that needs eufy.
- **Standalone battery cameras** report detections only with
  [Cloud push](usage.md#cloud-push) on.
- Not supported: firmware installation.
