# Changelog

All notable changes to this project. The project follows
[Semantic Versioning](https://semver.org/); while it is in beta (before 1.0), a minor
release may change entities, options or actions. From 0.1.0 on, release-please writes the
entries from the conventional commits.

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
