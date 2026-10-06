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
(`push`), options and `models` (how the library knows each product code; see
[devices.md](devices.md#other-models)). Serials are shortened and device names removed.

## Repair issues

| Issue | Meaning | Fix |
|---|---|---|
| eufy ended Home Assistant's session | another client signed in with the same account | **Fix**, which signs the other client out; see [network.md](network.md#eufy-account) |
| eufy is refusing sign-ins | eufy's sign-in limit | wait; each early attempt can restart the wait |
| HomeBase rejects its key | the HomeBase refused its key after one refetch | **Fix** allows one more key fetch |
| Fetched the key again | the key was refreshed; information only | dismiss |
| HomeBase stamps its events with another eufy account | commands may be ignored | check that the account is the owner or an admin share |
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
