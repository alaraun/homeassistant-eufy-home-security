# Changelog

All notable changes to this project. The project follows
[Semantic Versioning](https://semver.org/); while it is in beta (before 1.0), a minor
release may change entities, options or actions. From 0.1.0 on, release-please writes the
entries from the conventional commits.

## [0.4.7](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.6...v0.4.7) (2026-10-23)


### Features

* 49 more models get their settings, as the eufy app lists them (library 0.4.7) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))
* a camera's on/off switch where the eufy app offers it (library 0.4.7) ([issue 117](https://github.com/alaraun/homeassistant-eufy-home-security/issues/117)) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))
* a siren per camera the eufy app offers an alarm for, on and off as the camera reports it, and the HomeBase's own siren ([issue 117](https://github.com/alaraun/homeassistant-eufy-home-security/issues/117)) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))


### Bug Fixes

* 84 enum settings on 56 models show their value after a restart (library 0.4.7) ([issue 114](https://github.com/alaraun/homeassistant-eufy-home-security/issues/114)) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))
* a camera whose model cannot store a preset gets no Save current view button or card Save view tile, and one that cannot change its default preset refuses the change (library 0.4.7) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))
* a standalone camera such as the T8414 is armed as the eufy app does and shows its mode after a restart (library 0.4.7) ([issue 114](https://github.com/alaraun/homeassistant-eufy-home-security/issues/114)) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))
* a standalone video smart lock without a live stream is not woken for detection images ([130c0fa](https://github.com/alaraun/homeassistant-eufy-home-security/commit/130c0fae9efd0b26f6d1b16d8df5d489dba31452))
* an arm or setting change a station refuses names that station and says why ([issue 114](https://github.com/alaraun/homeassistant-eufy-home-security/issues/114)) ([cf46647](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf466477721aee0f55da47d731525705d4382849))
* **card:** a live view button's second press sends the action the first press named (a lock that locked itself in between was unlocked) ([130c0fa](https://github.com/alaraun/homeassistant-eufy-home-security/commit/130c0fae9efd0b26f6d1b16d8df5d489dba31452))
* **card:** a lock that is jammed, moving or in an unknown state offers no action; a lock that can open its latch offers Lock while unlocked or open ([130c0fa](https://github.com/alaraun/homeassistant-eufy-home-security/commit/130c0fae9efd0b26f6d1b16d8df5d489dba31452))
* **card:** after a camera switch, an answer for the previous camera acts on that camera only (a preset picture was stored for the camera now shown) ([130c0fa](https://github.com/alaraun/homeassistant-eufy-home-security/commit/130c0fae9efd0b26f6d1b16d8df5d489dba31452))
* **card:** the header's camera list returns to the card after full screen ([130c0fa](https://github.com/alaraun/homeassistant-eufy-home-security/commit/130c0fae9efd0b26f6d1b16d8df5d489dba31452))

## [0.4.6](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.5...v0.4.6) (2026-10-23)


### Features

* video smart locks (T8530, T8531, T8532, T853A, T85M0, T85M1, T85M2, T85V0) get the camera, detection and still entities, as the eufy app shows them as cameras; not tested on a video smart lock ([9effc3f](https://github.com/alaraun/homeassistant-eufy-home-security/commit/9effc3fde921ad0564313a2c18a003dda40efab2))


### Bug Fixes

* live view over the local connection is decided per device as the eufy app decides it: a Video Doorbell C30 behind a HomeBase 3 opens on the firmware the app streams it locally; a camera the app reaches only over WebRTC (such as some standalone T8170 from firmware 3.4.0.0) shows "No live view for this model", and the T7000 and T8040 stations raise the station-needs-WebRTC repair (library 0.4.6) ([issue 108](https://github.com/alaraun/homeassistant-eufy-home-security/issues/108)) ([7735c91](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7735c91c61d3928177b2bb156bc4036124a8b998))
* setting and guard-mode changes on a camera that answers with a plain reply code (seen on a T8414) confirm instead of reporting "did not confirm"; not tested on a T8414 (library 0.4.6) ([issue 114](https://github.com/alaraun/homeassistant-eufy-home-security/issues/114)) ([7735c91](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7735c91c61d3928177b2bb156bc4036124a8b998))
* the devices of a home shared with Share Home are found, listed per home as in the eufy app; not yet tested on a Share Home account (library 0.4.6) ([issue 45](https://github.com/alaraun/homeassistant-eufy-home-security/issues/45)) ([7735c91](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7735c91c61d3928177b2bb156bc4036124a8b998))
* the eufyCam S3 Pro (T8162) and the T8144, T8452, T8453 get camera and detection entities, the T8215 and T8217 count as doorbells with a ring event, and the baby cameras T8352, T8354 and the T8790 count as cameras, as in the eufy app (library 0.4.6) ([issue 123](https://github.com/alaraun/homeassistant-eufy-home-security/issues/123)) ([7735c91](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7735c91c61d3928177b2bb156bc4036124a8b998))

## [0.4.5](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.4...v0.4.5) (2026-10-22)


### Features

* **card:** `picture: auto | landscape | portrait`; a doorbell's tall picture shows whole ([issue 104](https://github.com/alaraun/homeassistant-eufy-home-security/issues/104)) ([5219ebe](https://github.com/alaraun/homeassistant-eufy-home-security/commit/5219ebee5e1605224aa34628a321becafa8e0593))
* **card:** `show_header`, `show_history` and `show_settings` options; with all three off the card is the picture alone ([issue 106](https://github.com/alaraun/homeassistant-eufy-home-security/issues/106)) ([5219ebe](https://github.com/alaraun/homeassistant-eufy-home-security/commit/5219ebee5e1605224aa34628a321becafa8e0593))
* **card:** a doorbell ring badge and `live_buttons` on the live view bar (a door lock, a gate) ([5219ebe](https://github.com/alaraun/homeassistant-eufy-home-security/commit/5219ebee5e1605224aa34628a321becafa8e0593))
* **card:** several cameras in one card with a camera list ([issue 86](https://github.com/alaraun/homeassistant-eufy-home-security/issues/86)) ([5219ebe](https://github.com/alaraun/homeassistant-eufy-home-security/commit/5219ebee5e1605224aa34628a321becafa8e0593))


### Bug Fixes

* setting and guard-mode changes on a camera that answers under eufy's fixed key (seen on a T8414) confirm instead of reporting "did not confirm"; not tested on a T8414 (library 0.4.5) ([issue 114](https://github.com/alaraun/homeassistant-eufy-home-security/issues/114)) ([9924875](https://github.com/alaraun/homeassistant-eufy-home-security/commit/99248759a7bb12667e6c04394544eabad2754dfc))

## [0.4.4](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.3...v0.4.4) (2026-10-21)


### Bug Fixes

* a camera that sends its settings and guard mode under eufy's fixed key or unencrypted (seen on a T8414) connects and shows them instead of staying unavailable; not tested on a T8414 (library 0.4.4) ([issue 114](https://github.com/alaraun/homeassistant-eufy-home-security/issues/114)) ([c782804](https://github.com/alaraun/homeassistant-eufy-home-security/commit/c782804c22c21a5e2b9aa73c58b31fa643ef9824))

## [0.4.3](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.2...v0.4.3) (2026-10-21)


### Bug Fixes

* a HomeBase 2 detection picture in eufy's encoded formats is shown; one protected by Video Encryption is skipped (library 0.4.3) ([ffe6f19](https://github.com/alaraun/homeassistant-eufy-home-security/commit/ffe6f194d50c8424a20480e8e3ac76adaed60ab6))
* a verification code eufy refuses shows its own message and asks to sign in again for a new code, instead of saying the email or password is wrong ([ffe6f19](https://github.com/alaraun/homeassistant-eufy-home-security/commit/ffe6f194d50c8424a20480e8e3ac76adaed60ab6))
* signing in with two-step verification answers the code the way the eufy app does, so an e-mailed code is accepted (declared, not tested on a live account; library 0.4.3) ([ffe6f19](https://github.com/alaraun/homeassistant-eufy-home-security/commit/ffe6f194d50c8424a20480e8e3ac76adaed60ab6))

## [0.4.2](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.1...v0.4.2) (2026-10-19)


### Features

* a HomeBase 2 detection shows the picture its cloud push names, a recognised face or an event picture, as the camera's event image (declared, not tested) ([0a4cecd](https://github.com/alaraun/homeassistant-eufy-home-security/commit/0a4cecd45cd4014197d19175f18fb444f2f906ad))
* a later push that names a more specific kind of a detection turns that kind's sensor on and fires the detection event again ([0a4cecd](https://github.com/alaraun/homeassistant-eufy-home-security/commit/0a4cecd45cd4014197d19175f18fb444f2f906ad))


### Bug Fixes

* a HomeBase 2 detection that eufy reports as a person, pet or vehicle shows as that kind instead of motion (library 0.4.2) ([0a4cecd](https://github.com/alaraun/homeassistant-eufy-home-security/commit/0a4cecd45cd4014197d19175f18fb444f2f906ad))
* several cloud pushes of one HomeBase 2 detection count as one detection (library 0.4.2) ([0a4cecd](https://github.com/alaraun/homeassistant-eufy-home-security/commit/0a4cecd45cd4014197d19175f18fb444f2f906ad))

## [0.4.1](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.4.0...v0.4.1) (2026-10-19)


### Features

* a repair names the stations whose detections need cloud push (a HomeBase 2, a standalone camera) while it is off; its fix switches cloud push on ([7fbc6e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7fbc6e8527117257dfa9a283f8b064b1a0d8947c))
* diagnostics show whether each station sends its detections over the LAN ([7fbc6e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7fbc6e8527117257dfa9a283f8b064b1a0d8947c))


### Bug Fixes

* a camera's state is Streaming while a live view of it plays ([f2ec2f3](https://github.com/alaraun/homeassistant-eufy-home-security/commit/f2ec2f3609e428a13738caf75a01cde8b512977d))
* a live view the camera refuses fails at once instead of waiting for the first frame (library 0.4.1) ([7fbc6e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7fbc6e8527117257dfa9a283f8b064b1a0d8947c))
* live view for cameras paired to a HomeBase 2 or 3 that the eufy app opens through the HomeBase, such as the T8600 (declared, not tested; library 0.4.1) ([7fbc6e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7fbc6e8527117257dfa9a283f8b064b1a0d8947c))
* the cloud push option is named "Cloud push for detections" and no longer says a HomeBase 2's detections arrive locally ([7fbc6e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7fbc6e8527117257dfa9a283f8b064b1a0d8947c))
* the T8210C, T8510P and T8520P get their settings and live view (library 0.4.1) ([7fbc6e8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/7fbc6e8527117257dfa9a283f8b064b1a0d8947c))

## [0.4.0](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.11...v0.4.0) (2026-10-15)


### ⚠ BREAKING CHANGES

* per-mode device action switches, entry and leaving delay numbers and delay sensors are removed and their registry entries deleted; the HomeBase card edits each device's actions and delays per mode
* the HomeBase's alarm type control is removed; Sound alarm and Stop alarm are in the HomeBase card (library 0.4.0)
* the keypad password status is a read-only binary sensor, disabled by default (library 0.4.0)

### Features

* a HomeBase card: the station and its devices per security mode, custom modes, the weekly schedule, arming groups, cross-camera tracking, the station's settings by section and its status; every change is staged behind one Set, with a confirmation for changes that affect the alarm ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* a security mode select per HomeBase lists its modes by name, custom modes included ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* HomeBase settings the station does not report are read from the eufy cloud's device list, refreshed hourly (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* HomeBases with pro alarm (T8020, T8023, T8024, T8026) get per-mode device tables in the HomeBase card, graded from the eufy app, not tested on hardware (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* per-mode device action switches, entry and leaving delay numbers and delay sensors are removed and their registry entries deleted; the HomeBase card edits each device's actions and delays per mode ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* setting entities carry their section and value source (station, cloud, app default) as attributes, kept out of the recorder; settings the eufy app hides behind a warning start disabled ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* the camera card's settings tabs follow the library's setting sections; settings_groups takes section ids ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* websocket commands for the HomeBase configuration (modes, mode tables, schedule, cross-camera tracking, restart, sound/stop alarm) and arming groups; writes need an admin and are refused when the station changed since it was read ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))


### Bug Fixes

* a guard mode change on a station model the library has no data for (T8N00, T9000) is refused with an error instead of being sent (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* a motion sensor's React to motion switch shows and sets the state the eufy app shows; it was inverted (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* light switches carry the eufy app's names, so a camera no longer shows two Spotlight switches (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* per-mode entry and leaving delays offer only the eufy app's choices: 0, 15, 30, 45, 60, 180 and 300 s (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* registry entries of settings the library no longer lists are removed at setup ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* setting names are in sentence case like the eufy app's; entity ids do not change (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* the camera card's settings dropdowns show five to eight options, open upward when there is no room below, and get a filter field for long lists ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* the HomeBase's alarm type control is removed; Sound alarm and Stop alarm are in the HomeBase card (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))
* the keypad password status is a read-only binary sensor, disabled by default (library 0.4.0) ([53c6e9e](https://github.com/alaraun/homeassistant-eufy-home-security/commit/53c6e9e4ab1c186e055b513b2fbdefb80f3d3642))

## [0.3.11](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.10...v0.3.11) (2026-10-14)


### Bug Fixes

* live view of a HomeBase 2 camera no longer freezes when a keyframe arrives in parts (library 0.3.17) ([fe913c2](https://github.com/alaraun/homeassistant-eufy-home-security/commit/fe913c26eb51264fb00a87e989970f4d6686102e))

## [0.3.10](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.9...v0.3.10) (2026-10-14)


### Features

* pan/tilt, presets and zoom for every camera model whose eufy app declares them (library 0.3.16) ([b337fd0](https://github.com/alaraun/homeassistant-eufy-home-security/commit/b337fd0ff07440cadcf9c9caaa98e74d496432f1))


### Bug Fixes

* Home Assistant 2026.10.0 or newer is required ([e0164d0](https://github.com/alaraun/homeassistant-eufy-home-security/commit/e0164d0cf13096ba13626d9f17778edab9432ba4))

## [0.3.9](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.8...v0.3.9) (2026-10-14)


### Bug Fixes

* a HomeBase camera's live view opens without broken first frames (library 0.3.15) ([db02bc1](https://github.com/alaraun/homeassistant-eufy-home-security/commit/db02bc14afd50eeb3e1656ad93c349caf2c01b51))

## [0.3.8](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.7...v0.3.8) (2026-10-13)


### Features

* a Last event image entity per camera ([#83](https://github.com/alaraun/homeassistant-eufy-home-security/issues/83)) ([653f3c8](https://github.com/alaraun/homeassistant-eufy-home-security/commit/653f3c82d67760f9f9e020769cb37d9e629f7657))


### Bug Fixes

* no battery or charging entities for a camera model without a battery ([#84](https://github.com/alaraun/homeassistant-eufy-home-security/issues/84)) ([45e6f80](https://github.com/alaraun/homeassistant-eufy-home-security/commit/45e6f8093193152ac2bb4a77eecdbf81d90a55da))

## [0.3.7](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.6...v0.3.7) (2026-10-13)


### Bug Fixes

* storage entities of figures a station does not report are removed ([#80](https://github.com/alaraun/homeassistant-eufy-home-security/issues/80)) ([72e89ef](https://github.com/alaraun/homeassistant-eufy-home-security/commit/72e89efd5619b55be9ab59ada208e7c9059d6844))
* the camera card shows Custom-only settings only while Custom is chosen ([#82](https://github.com/alaraun/homeassistant-eufy-home-security/issues/82)) ([44e0e85](https://github.com/alaraun/homeassistant-eufy-home-security/commit/44e0e854106a6a10e1ff03e86c079bdfcaa0fe88))

## [0.3.6](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.5...v0.3.6) (2026-10-13)


### Bug Fixes

* HomeBase 2 storage sizes, a repair for WebRTC-only stations, standalone T8442/T8200 live view (library 0.3.14) ([#78](https://github.com/alaraun/homeassistant-eufy-home-security/issues/78)) ([6dd16d3](https://github.com/alaraun/homeassistant-eufy-home-security/commit/6dd16d305a8e395ad74f0f61039e3108aa50b910))

## [0.3.5](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.4...v0.3.5) (2026-10-12)


### Bug Fixes

* the S100 (T84A1) connects and a refused storage query is not repeated (library 0.3.12) ([#76](https://github.com/alaraun/homeassistant-eufy-home-security/issues/76)) ([bb8b631](https://github.com/alaraun/homeassistant-eufy-home-security/commit/bb8b631216c11c06f8db571080fa25dbc6f98871))

## [0.3.4](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.3...v0.3.4) (2026-10-12)


### Bug Fixes

* H.264 clips are stored, and the S100 (T84A1) and Doorbell 2K (T8200) connect (library 0.3.10) ([#72](https://github.com/alaraun/homeassistant-eufy-home-security/issues/72)) ([cf9c4b9](https://github.com/alaraun/homeassistant-eufy-home-security/commit/cf9c4b9f852bc5765cb57e49fe79e86caa2c9edb))
* stills from H.264 cameras decode, and the card plays them over WebRTC in every browser (library 0.3.11) ([#75](https://github.com/alaraun/homeassistant-eufy-home-security/issues/75)) ([1ed52a4](https://github.com/alaraun/homeassistant-eufy-home-security/commit/1ed52a4a040b733a905562ee4b18baf950c377e0))

## [0.3.3](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.2...v0.3.3) (2026-10-11)


### Bug Fixes

* HomeBase 2 cameras' H.264 live video plays and a standalone T8442 comes up (library 0.3.9) ([#70](https://github.com/alaraun/homeassistant-eufy-home-security/issues/70)) ([f2fa338](https://github.com/alaraun/homeassistant-eufy-home-security/commit/f2fa338e905bada158a17573dac536ca7f0d1fa4))

## [0.3.2](https://github.com/alaraun/homeassistant-eufy-home-security/compare/v0.3.1...v0.3.2) (2026-10-10)


### Features

* a second press on the camera card's record button stops the recording (library 0.3.8) ([#69](https://github.com/alaraun/homeassistant-eufy-home-security/issues/69)) ([0e6d914](https://github.com/alaraun/homeassistant-eufy-home-security/commit/0e6d9147c9e04c3c56b0bea70aef2534157e0e5a))


### Bug Fixes

* a station with a P2P id local connections do not accept is skipped with a repair, the account's other stations load (library 0.3.7) ([#68](https://github.com/alaraun/homeassistant-eufy-home-security/issues/68)) ([4a1f977](https://github.com/alaraun/homeassistant-eufy-home-security/commit/4a1f9773d8970d467c9f7aeb66dfd18b3f04171d))
* the camera card centres the icons of its picture controls ([#66](https://github.com/alaraun/homeassistant-eufy-home-security/issues/66)) ([08e14e7](https://github.com/alaraun/homeassistant-eufy-home-security/commit/08e14e712cfabc1cd908df24f07a0dec42f5cd9b))

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
