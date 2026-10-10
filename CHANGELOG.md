# Changelog

All notable changes to this project. The project follows
[Semantic Versioning](https://semver.org/); while it is in beta (before 1.0), a minor
release may change entities, options or actions. From 0.1.0 on, release-please writes the
entries from the conventional commits.

## [0.3.1](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.0...v0.3.1) (2026-10-10)


### Bug Fixes

* a camera behind a HomeBase shows its detection sensitivity, recording quality, notification type, view mode and snooze (library 0.3.6) ([#64](https://github.com/alaraun/homeassistant-eufy-home-security/issues/64)) ([f3391e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/f3391e8b9464774918d10a13a25569172a72982d))

## [0.3.0](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.2.2...v0.3.0) (2026-10-10)


### Features

* settings a camera reports behind its HomeBase (library 0.3.5) ([#59](https://github.com/alaraun/homeassistant-eufy-home-security/issues/59)) ([1e45e54](https://github.com/alaraun/homeassistant-eufy-home-security/commit/1e45e546f971b36f9311d1e3e5e1c61a29f89493))


### Miscellaneous Chores

* release 0.3.0 ([#60](https://github.com/alaraun/homeassistant-eufy-home-security/issues/60)) ([a08d278](https://github.com/alaraun/homeassistant-eufy-home-security/commit/a08d27860c17a1db872db117d524bc93194f885c))

## [0.2.2](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.2.1...v0.2.2) (2026-10-09)


### Features

* live video for every camera the library can open on its station (library 0.3.4) ([#57](https://github.com/alaraun/homeassistant-eufy-home-security/issues/57)) ([9dfbf06](https://github.com/alaraun/homeassistant-eufy-home-security/commit/9dfbf06f3bb1d9d1ed58523a41b171a8ad80342e))

## [0.2.1](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.2.0...v0.2.1) (2026-10-09)


### Features

* the camera card saves the current view as a preset, with the live picture ([#53](https://github.com/alaraun/homeassistant-eufy-home-security/issues/53)) ([6ed247a](https://github.com/alaraun/homeassistant-eufy-home-security/commit/6ed247ae3062ac3cff6dd3d25e8040f64603f220))


### Bug Fixes

* a sign-in that ends in "could not reach eufy" logs its cause ([#54](https://github.com/alaraun/homeassistant-eufy-home-security/issues/54)) ([aae4234](https://github.com/alaraun/homeassistant-eufy-home-security/commit/aae4234503c5150af66d5f447bdcdb572c7ba61b))
* the camera card says when a recorded clip ended early ([#52](https://github.com/alaraun/homeassistant-eufy-home-security/issues/52)) ([68a2f63](https://github.com/alaraun/homeassistant-eufy-home-security/commit/68a2f639cd0e2b9eb06585388eb136d156ba2ea9))
* the stale-device check reads the device's config entry, not the deprecated list ([#49](https://github.com/alaraun/homeassistant-eufy-home-security/issues/49)) ([e47aff6](https://github.com/alaraun/homeassistant-eufy-home-security/commit/e47aff6b2f08292d6110195eb24532bf6dc36b99))

## [0.2.0](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.10...v0.2.0) (2026-10-09)


### Features

* a repair for the integration's own sign-in budget, and removed countries' devices go even when eufy refuses the list (library 0.3.1) ([#46](https://github.com/alaraun/homeassistant-eufy-home-security/issues/46)) ([6ee5aee](https://github.com/alaraun/homeassistant-eufy-home-security/commit/6ee5aee2481a68bb8a11d8f931b0bf67d25ad76b))
* the camera card hides its controls during live video ([#42](https://github.com/alaraun/homeassistant-eufy-home-security/issues/42)) ([54cfc6b](https://github.com/alaraun/homeassistant-eufy-home-security/commit/54cfc6b7908fc94d473b9c1a79371c779371187b))


### Bug Fixes

* remove devices the eufy device list no longer names ([#41](https://github.com/alaraun/homeassistant-eufy-home-security/issues/41)) ([6696ec9](https://github.com/alaraun/homeassistant-eufy-home-security/commit/6696ec93fdf0182e1fc0f56fc995cf297810f4ca))
* the station list asks a week of days per page, so a camera with few recordings answers at once ([#43](https://github.com/alaraun/homeassistant-eufy-home-security/issues/43)) ([56d6d24](https://github.com/alaraun/homeassistant-eufy-home-security/commit/56d6d24a3e87e52d29c20bd0e052bb16600f227a))


### Miscellaneous Chores

* release 0.2.0 ([#47](https://github.com/alaraun/homeassistant-eufy-home-security/issues/47)) ([185292e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/185292ef07fc311385c95ec39d200814a691f5c1))

## [0.1.10](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.9...v0.1.10) (2026-10-09)


### Features

* options in collapsible sections; extra countries marked experimental ([#38](https://github.com/alaraun/homeassistant-eufy-home-security/issues/38)) ([a953beb](https://github.com/alaraun/homeassistant-eufy-home-security/commit/a953bebe6ad01ae9aa45f97c8f3695931b2118aa))


### Bug Fixes

* cameras and doorbells of every model the eufy app names get their entities (library 0.3.0) ([#37](https://github.com/alaraun/homeassistant-eufy-home-security/issues/37)) ([4dc359c](https://github.com/alaraun/homeassistant-eufy-home-security/commit/4dc359c95ff19391d0e1147159f6c2ce01bb2535))
* the options sections show the saved values ([#40](https://github.com/alaraun/homeassistant-eufy-home-security/issues/40)) ([fcdcc00](https://github.com/alaraun/homeassistant-eufy-home-security/commit/fcdcc0002d19c117f6c3c48e3b1c64f145905350))
* the sign-in limit repair names a clock time and goes when it passes ([#36](https://github.com/alaraun/homeassistant-eufy-home-security/issues/36)) ([90d59ca](https://github.com/alaraun/homeassistant-eufy-home-security/commit/90d59ca2363a314991b17856c159210a3de13daa))

## [0.1.9](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.8...v0.1.9) (2026-10-08)


### Features

* extra eufy sign-in countries for homes shared from another country ([#34](https://github.com/alaraun/homeassistant-eufy-home-security/issues/34)) ([b9d8274](https://github.com/alaraun/homeassistant-eufy-home-security/commit/b9d827421dac2b8b8e1cb9d4c788961515275248))
* sign in with the eufy app's country; name unaccepted invitations ([#33](https://github.com/alaraun/homeassistant-eufy-home-security/issues/33)) ([b7c4c96](https://github.com/alaraun/homeassistant-eufy-home-security/commit/b7c4c96d42b43465fa2c17e43a393fde4f50aa28))

## [0.1.8](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.7...v0.1.8) (2026-10-08)


### Bug Fixes

* a station that sends a non-printable session key connects ([#29](https://github.com/alaraun/homeassistant-eufy-home-security/issues/29)) ([2a4c7ba](https://github.com/alaraun/homeassistant-eufy-home-security/commit/2a4c7bafc5381a55a58658478f0583fe2b12f854))

## [0.1.7](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.1.6...v0.1.7) (2026-10-08)


### Features

* the diagnostics download carries the library's account report ([#27](https://github.com/alaraun/homeassistant-eufy-home-security/issues/27)) ([c9d7ec7](https://github.com/alaraun/homeassistant-eufy-home-security/commit/c9d7ec7d9f88c2b8aeda9c611c865a8f2ad94c9d))

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
