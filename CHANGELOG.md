# Changelog

All notable changes to this project. The project follows
[Semantic Versioning](https://semver.org/); while it is in beta (before 1.0), a minor
release may change entities, options or actions. From 0.1.0 on, release-please writes the
entries from the conventional commits.

## [0.1.6](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.5...v0.1.6) (2026-10-07)


### Bug Fixes

* a station key eufy serves unusable gets its own repair ([#24](https://github.com/alaraun/homeassistant-eufy-home-security/issues/24)) ([627729c](https://github.com/alaraun/homeassistant-eufy-home-security/commit/627729c5fa14f21081d5320f2a5ce00f3180162e))

## [0.1.5](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.4...v0.1.5) (2026-10-07)


### Bug Fixes

* ask for eufy's two-step verification code when signing in ([#22](https://github.com/alaraun/homeassistant-eufy-home-security/issues/22)) ([1f282cc](https://github.com/alaraun/homeassistant-eufy-home-security/commit/1f282cc25b039a8295d46cb2b1d233f24c9fa62b)), closes [#19](https://github.com/alaraun/homeassistant-eufy-home-security/issues/19)

## [0.1.4](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.3...v0.1.4) (2026-10-07)


### Features

* support the T8410 and the T8161 with eufy-home-security 0.2.2 ([#20](https://github.com/alaraun/homeassistant-eufy-home-security/issues/20)) ([b57f2b2](https://github.com/alaraun/homeassistant-eufy-home-security/commit/b57f2b2e258a1762999fe19c2af1bd8dab9523ee))

## [0.1.3](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.2...v0.1.3) (2026-10-07)


### Features

* look for devices in every eufy region only when the option is on ([#17](https://github.com/alaraun/homeassistant-eufy-home-security/issues/17)) ([1e9f99e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/1e9f99e56623f02135ed553e0e5b06dd43607a75))

## [0.1.2](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.1...v0.1.2) (2026-10-06)


### Features

* History day picker, HA editor selector and theme colours in the camera card ([#12](https://github.com/alaraun/homeassistant-eufy-home-security/issues/12)) ([443d232](https://github.com/alaraun/homeassistant-eufy-home-security/commit/443d232608df469cdfad7dc7225ed6f33b3966fc))

## [0.1.1](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.0...v0.1.1) (2026-10-06)


### Bug Fixes

* adopt eufy-home-security 0.1.2; repair issue when eufy has no station key ([#7](https://github.com/alaraun/homeassistant-eufy-home-security/issues/7)) ([680d151](https://github.com/alaraun/homeassistant-eufy-home-security/commit/680d151dfae8e379222d2dfa76df913cbe2e5dca)), closes [#6](https://github.com/alaraun/homeassistant-eufy-home-security/issues/6)

## 0.1.0

The first public release: a Home Assistant integration for eufy Security HomeBase systems
and standalone eufy battery cameras, local over the LAN, with the eufy cloud used for
sign-in, the device list, device keys and optional push. Built on the
[`eufy-home-security`](https://pypi.org/project/eufy-home-security/) library 0.1.1.

### Setup and account

- Config flow with the eufy account's e-mail and password; the session is kept across
  restarts, and Home Assistant never signs in by itself.
- **Reconfigure** to sign in again, with a prompt before signing out another client that
  holds the session; an optional 6-hourly session check.
- Repair issues for an ended session, eufy's sign-in limit, a rejected HomeBase key, an
  account mismatch, a missing `/media` mount and cloud push that is not running.
- **Refresh device list** button for devices added to the account later.

### Alarm and detections

- Alarm panel per HomeBase and standalone camera: arm Home, arm Away and disarm over the
  LAN, confirmed by the HomeBase; Schedule and Custom modes are shown.
- Motion, person, pet and vehicle sensors, and detection, alarm, arming and doorbell
  events, by local push from the HomeBase.
- Optional cloud push for the detections of standalone cameras.

### Cameras

- Camera entity with the latest detection's image (thumbnail, HD frame, or both), plus
  **Refresh event image** and **Capture live image** buttons.
- Live video, passed through without transcoding, shared by every viewer of a camera, with
  a configurable number of sessions per HomeBase.
- `eufy_home_security.record` action: records a clip of the live stream to the media
  folder.
- Event history in the media folder (images, optional event videos copied from the
  HomeBase), kept for a configurable number of days.
- Pan/tilt cameras: presets with captured images, pan, tilt and zoom buttons and actions,
  default and live-view presets.

### Settings and status

- Device settings from the library's per-model data as switches, selects, numbers and
  text, read-only ones as disabled diagnostic sensors; per-mode alarm actions and entry
  and leaving delays.
- Battery, signal, firmware, storage, charging and solar entities where the device
  reports them; firmware update entities.

### Camera card

- A dashboard card for each camera ships with the integration and registers itself: live
  view, pan/tilt and presets, record, saved history, recordings on the HomeBase, settings.

### Diagnostics

- Diagnostics download with serials shortened and names removed; per-model data source in
  `models`.
