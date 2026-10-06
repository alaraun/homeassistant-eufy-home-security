<p align="center">
  <img src="https://raw.githubusercontent.com/alaraun/homeassistant-eufy-home-security/main/custom_components/eufy_home_security/brand/logo.png" alt="eufy" height="80">
</p>

# Anker eufy Home Security for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Home Assistant](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Falaraun%2Fhomeassistant-eufy-home-security%2Fmain%2Fhacs.json&query=%24.homeassistant&label=Home%20Assistant&suffix=%2B&color=41BDF5&cacheSeconds=3600)](https://www.home-assistant.io/)
[![Release](https://img.shields.io/github/v/release/alaraun/homeassistant-eufy-home-security)](https://github.com/alaraun/homeassistant-eufy-home-security/releases)
[![CI](https://github.com/alaraun/homeassistant-eufy-home-security/actions/workflows/ci.yml/badge.svg)](https://github.com/alaraun/homeassistant-eufy-home-security/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/alaraun/homeassistant-eufy-home-security)](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/LICENSE)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-support-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/alaraun)

Local control of eufy Security HomeBase systems and standalone eufy battery cameras:
alarm panel, detections, camera stills, live video, pan/tilt presets and device
settings. The HomeBase is reached over its local P2P protocol; the eufy cloud is
used for sign-in, the device list and device keys, and, when switched on, for the
cloud push that standalone cameras' detections need. The protocol work is done by
the [`eufy-home-security`](https://pypi.org/project/eufy-home-security/) library
([source](https://github.com/alaraun/python-eufy-home-security)), which Home
Assistant installs automatically. Library bugs that show without Home Assistant go to
[its issue tracker](https://github.com/alaraun/python-eufy-home-security/issues).

> [!WARNING]
> Beta. Only the devices in
> [Supported devices](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/devices.md)
> are tested, and a minor release may change entities or options. Keep the eufy app
> installed. Not affiliated with or endorsed by Anker or eufy.

<p align="center">
  <img src="https://raw.githubusercontent.com/alaraun/homeassistant-eufy-home-security/main/docs/images/camera-card.png" alt="A eufy camera with live view and setting controls on a dashboard" width="600">
  <br><sub>The camera card that comes with the integration: live view, pan/tilt, presets, history and settings.</sub>
</p>

## Requirements

- Home Assistant **2026.9.0** or newer, with ffmpeg (included in Home Assistant OS
  and the official container) for 4K stills.
- Home Assistant on the **same LAN segment** as the HomeBase (host networking in
  Docker). Firewall, reverse proxy and browsers:
  [Network and security](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/network.md).
- Live view reads each camera from Home Assistant's own port on `127.0.0.1`: TLS on
  that port works, an `http: server_host` bound only to a LAN address does not
  ([stream access](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/network.md#stream-access)).
- A eufy account that sees the HomeBase: the owner's, or one it is shared with. Use a
  separate account for Home Assistant: a sign-in elsewhere with the same account ends
  Home Assistant's session.

## Installation

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alaraun&repository=homeassistant-eufy-home-security&category=integration)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/alaraun/homeassistant-eufy-home-security`, type **Integration**
   (or use the button above).
2. Download **Anker eufy Home Security** and restart Home Assistant.

Manual: download the source archive of the latest
[release](https://github.com/alaraun/homeassistant-eufy-home-security/releases), copy its
`custom_components/eufy_home_security` into your configuration's `custom_components`
directory and restart.

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=eufy_home_security)

1. **Settings → Devices & services → Add integration → Anker eufy Home Security**.
2. Enter the eufy account's e-mail and password. This is one eufy sign-in; the
   session is kept, so restarts do not sign in again.
3. Every HomeBase, its paired devices and every standalone camera appear as devices.

eufy allows only a few sign-ins per account per day
([details](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/network.md#eufy-account));
Home Assistant never signs in by itself.

Devices added to the eufy account later: press **Refresh device list** on the
**eufy account** device.

The camera card comes with the integration: edit a dashboard → **Add card** →
**eufy camera**
([Camera card](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/card.md)).

## Documentation

| | |
|---|---|
| [Usage](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/usage.md) | options, alarm panel, camera images, event history and videos, live video, pan/tilt, settings, signing in again, removal |
| [Camera card](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/card.md) | the dashboard card that comes with the integration: install, options, layout, writes |
| [Supported devices](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/devices.md) | tested models, settings, adding a device, reporting a new device |
| [Network and security](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/network.md) | LAN, firewall, reverse proxy, browsers, accounts, stored data |
| [Troubleshooting](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/troubleshooting.md) | debug logging, diagnostics, repair issues, known limitations |
| [Changelog](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/CHANGELOG.md) | changes per release |

Bugs and feature requests:
[issues](https://github.com/alaraun/homeassistant-eufy-home-security/issues), with the
integration's diagnostics download attached.

## Support

This project is built in spare time and is free to use. If it is useful to you,
donations are welcome. They are voluntary, buy no support or priority, and are not
tax-deductible.

- [Ko-fi](https://ko-fi.com/alaraun)

Donated eufy hardware helps develop future features; please get in touch first.

## License

[MIT](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/LICENSE). eufy
and Anker are trademarks of Anker Innovations; the eufy logo identifies the devices this
project works with.
