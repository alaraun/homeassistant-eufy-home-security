# Supported devices

Device support comes from the
[`eufy-home-security`](https://pypi.org/project/eufy-home-security/) library
([source](https://github.com/alaraun/python-eufy-home-security)). The
integration adds no per-model code: a model the library knows gets the entities its
capabilities and settings allow.

## Settings

Each device's settings come from the library's settings file for its product code,
generated from eufy's own model data. What the file says decides the entity:

| The library can | Entity |
|---|---|
| write an on/off, a choice, a range or a string | switch, select, number (slider or box) or text, as the library's control says |
| write several choices at once (detection types) | one switch per choice |
| only read it | diagnostic sensor or binary sensor, disabled by default |
| neither | none |

A model without a settings file in the library gets no setting entities. A newer
library release adds models and controls; update the integration to get them.
Details: [usage.md](usage.md#device-settings).

## Tested models

| Model | Device | Tested on hardware |
|---|---|---|
| T8030 | HomeBase 3 (S380) | guard mode, status, storage, detections, event history, stills, settings (clock format; entry and leaving delays of Home, Away and Custom 1) |
| T8160 | eufyCam 3 (S330), on a HomeBase | detections, stills, live video and audio, settings (detection sensitivity, trigger interval, clip length, speaker volume, streaming quality, night vision, working mode) |
| T8170 | Battery SoloCam, standalone | guard mode, status, live video, presets, pan/tilt, zoom, settings (detection sensitivity, motion detection, status LED, night vision, record audio, AI tracking, streaming quality, privacy zones, spotlight, lighting) |
| T8910 | Motion sensor, on a HomeBase | battery and signal only |

## Models from eufy app code

The library knows these models from the eufy app, not from hardware. Their entities
are offered, but none is tested:

| Model | Device | Offered |
|---|---|---|
| T8161 | eufyCam 3C, on a HomeBase | as the T8160: detections, stills, live video, settings |
| T8410 | Indoor Cam 2K Pan & Tilt, standalone | live video, pan/tilt one step; no presets, no zoom |

Report what works and what does not ([below](#reporting-a-new-device)).

## Other models

Every other model the eufy app names is added too, by its kind in the library's
model list (declared from eufy's own model data):

| Kind | Entities |
|---|---|
| Camera, doorbell | camera with stills, detection sensors and event, **Capture live image** and **Refresh event image** buttons; a doorbell also its ring event. No live video, presets or pan/tilt |
| HomeBase, keypad, lock, sensor, other | none of a camera |

Every device also gets:

- status entities (battery, signal, firmware) where the device reports them;
- its settings, as [above](#settings).

Nothing about such a model is tested: an entity may show nothing, or a value with an
unexpected meaning.

**Which data a model uses**: diagnostics, `models`, one row per product code of the
account:

| Field | Meaning |
|---|---|
| `state` | `bundled`: the library ships a settings file. `cloud-listed`: no file; eufy lists the settings, no entities. `unknown`: no file, no listing |
| `newer_vendor_data` | `true`: eufy has newer model data than the library's file, which stays in use |

## Adding a device

1. Add the device to the HomeBase or account in the eufy app.
2. If Home Assistant uses a shared account, share the new device to it as well.
3. In Home Assistant, press **Refresh device list** on the **eufy account** device.
   A device under a sign-in country that listed none before needs the option **Look
   for devices under every sign-in country on each refresh** first; see
   [network.md](network.md#eufy-regions).
4. For a pan/tilt camera, press **Refresh presets** once.

## Removing a device

- Every device of the account that eufy's device list no longer names is removed from
  Home Assistant with its entities, at each start or reload of the integration and at
  **Refresh device list**. A removed extra sign-in country takes its devices this way,
  also when eufy refuses the new list at that reload.
- A paired device goes at the press; a HomeBase or standalone camera that left the
  list goes at the reload the press starts.
- A device eufy still lists is kept, also one Home Assistant builds nothing for (a
  camera whose HomeBase is not on the list). A list with no device at all removes
  nothing.
- A device the list no longer names can also be deleted on its device page; a listed
  device cannot.

## Reporting a new device

Report a model whose `models` row is not `bundled`, has `newer_vendor_data`, or
whose settings misbehave. Open an issue with:

1. **Diagnostics**: **Settings → Devices & services → Anker eufy Home Security → ⋮ →
   Download diagnostics**. Serials are shortened, names removed. The `models` block
   gives the product code and how the library knows it.
2. **Model and firmware** from the device page.
3. **What works and what does not**, per entity.
4. **A parameter dump**, from the library's command line on a host on the same LAN:

   ```bash
   uv tool install eufy-home-security
   eufy-security login
   eufy-security --redact-serials status --raw
   eufy-security --redact-serials coverage
   ```

   The command line signs in to eufy itself: with Home Assistant's account it ends
   Home Assistant's session, so use another account the home is shared with. Its
   cache in `~/.config/eufy-security/` holds the password; delete it afterwards.
5. **For a setting**: the entity's unique id or key (`nightvision_type_new`), `status --raw`
   before and after changing it in the eufy app, plus the app's name for the setting
   and the value chosen.

Never post passwords, full serials, email addresses, IP addresses or recordings.

How the library grades and proves a model is in its own documentation: the support
matrix (`docs/reference/devices.md`) and the guide to adding a device
(`docs/how-to/add-a-device.md`), and how its settings files are regenerated
(`docs/how-to/regenerate-models.md`).
