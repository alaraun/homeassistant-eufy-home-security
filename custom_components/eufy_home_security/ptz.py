"""Pan/tilt, go-to, zoom and preset-edit calls shared by the entities and the camera actions.

Each call maps the library's errors to the integration's translated ones. The
pan/tilt buttons, the "Save current view" button, the live-view zoom number and the
camera entity's ``pan_tilt``, ``goto_preset``, ``zoom``, ``save_preset`` and
``delete_preset`` actions all go through here, so a card and an entity say the same
thing about a failure. Nothing is sent for a refusal the library raises before
sending (a capture holding the camera, an unset slot, dual view, a full camera).
A ``CommandUnsupportedError`` is the station's own refusal of a command it sent on
(receipt -108): it is caught before ``UnsupportedError`` and ``CommandError``, both of
which it also is, so it is never reported as a refusal before sending.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from eufy_home_security import (
    CommandError,
    CommandNotAppliedError,
    CommandRejectedError,
    CommandUnsupportedError,
    DeviceBusyError,
    EufySecurityError,
    PanTilt,
    PresetSlotsFullError,
    UnsupportedError,
    redact_serial,
)

from . import errors, presets, runtime
from .const import LIVE_ZOOM_KEY, SERVICE_DELETE_PRESET, SERVICE_SAVE_PRESET

if TYPE_CHECKING:
    from .coordinator import StationCoordinator
    from .presets import PresetManager

_LOGGER = logging.getLogger(__name__)

# ``CommandNotAppliedError.command`` of a default-preset write whose read-back failed.
_DEFAULT_COMMAND = 6242
# The camera's "set anyway?" refusal of a preset or default write.
_NEEDS_CONFIRMATION_CODE = -502


def _not_handled(action: str, serial: str, err: CommandUnsupportedError) -> Exception:
    """Log a command the station sent on and then refused (receipt -108); its error."""
    _LOGGER.debug("%s of %s not handled by the station (code %d)", action, serial, err.code)
    return errors.ptz_command_not_handled()


def _target(name: object, fallback: str) -> str:
    """The entity's translated name for an error message, else its key."""
    return name if isinstance(name, str) else fallback


async def async_pan_tilt(
    coordinator: StationCoordinator, device_sn: str, direction: PanTilt, name: object
) -> None:
    """Move ``device_sn`` one step; returns once the camera has moved."""
    station = coordinator.station
    serial = redact_serial(device_sn)
    try:
        await station.async_pan_tilt(device_sn, direction)
    except DeviceBusyError as err:
        _LOGGER.debug(
            "Pan/tilt %s of %s refused: camera busy, nothing sent", direction.name, serial
        )
        raise errors.capture_in_progress() from err
    except CommandUnsupportedError as err:
        raise _not_handled(f"Pan/tilt {direction.name}", serial, err) from err
    except CommandError as err:
        _LOGGER.debug(
            "Pan/tilt %s of %s refused by the camera: %s",
            direction.name,
            serial,
            type(err).__name__,
        )
        raise errors.pan_tilt_not_applied() from err
    except EufySecurityError as err:
        _LOGGER.debug(
            "Pan/tilt %s of %s failed: %s", direction.name, serial, errors.failure_reason(err)
        )
        raise errors.setting_write_failed(
            err,
            _target(name, direction.name.lower()),
            on_demand=station.connects_on_demand,
        ) from err
    _LOGGER.debug("Pan/tilt %s of %s done", direction.name, serial)


async def async_goto_preset(
    coordinator: StationCoordinator, device_sn: str, index: int, name: object
) -> None:
    """Turn ``device_sn`` to slot ``index`` once; returns at the camera's receipt."""
    streams = runtime.streaming(coordinator.config_entry)
    if streams is None:
        return
    station = coordinator.station
    serial = redact_serial(device_sn)
    try:
        watching = await streams.async_goto_preset(device_sn, index)
    except DeviceBusyError as err:
        _LOGGER.debug("Go-to %d of %s refused: camera busy, nothing sent", index, serial)
        raise errors.capture_in_progress() from err
    except CommandUnsupportedError as err:
        raise _not_handled(f"Go-to {index}", serial, err) from err
    except UnsupportedError as err:
        _LOGGER.debug("Go-to %d of %s refused: slot not set, nothing sent", index, serial)
        raise errors.preset_not_set(index) from err
    except CommandError as err:
        _LOGGER.debug("Go-to %d of %s refused by the camera: %s", index, serial, type(err).__name__)
        raise errors.pan_tilt_not_applied() from err
    except EufySecurityError as err:
        _LOGGER.debug("Go-to %d of %s failed: %s", index, serial, errors.failure_reason(err))
        raise errors.setting_write_failed(
            err, _target(name, "goto_preset"), on_demand=station.connects_on_demand
        ) from err
    _LOGGER.debug("Go-to %d of %s sent%s", index, serial, " during a live view" if watching else "")


async def async_set_live_zoom(
    coordinator: StationCoordinator, device_sn: str, zoom: float, name: object
) -> None:
    """Set ``device_sn``'s live-view zoom; with a view running, zoom the camera first."""
    streams = runtime.streaming(coordinator.config_entry)
    if streams is None:
        return
    serial = redact_serial(device_sn)
    try:
        zoomed = await streams.async_set_live_zoom(device_sn, zoom)
    except DeviceBusyError as err:
        _LOGGER.debug("Zoom of %s refused: camera busy, nothing sent", serial)
        raise errors.capture_in_progress() from err
    except CommandUnsupportedError as err:
        raise _not_handled("Zoom", serial, err) from err
    except UnsupportedError as err:
        _LOGGER.debug("Zoom of %s refused: %s", serial, errors.failure_reason(err))
        raise errors.zoom_needs_single_view() from err
    except EufySecurityError as err:
        _LOGGER.debug("Zoom of %s failed: %s", serial, errors.failure_reason(err))
        raise errors.setting_write_failed(
            err,
            _target(name, LIVE_ZOOM_KEY),
            on_demand=coordinator.station.connects_on_demand,
        ) from err
    _LOGGER.debug(
        "Live view zoom of %s is now %g%s",
        serial,
        zoom,
        " (the running view zoomed)" if zoomed else "",
    )


async def async_save_preset(
    coordinator: StationCoordinator,
    device_sn: str,
    manager: PresetManager,
    *,
    preset: int | None = None,
    make_default: bool = False,
    name: object = None,
) -> int:
    """Store what ``device_sn`` shows now as a preset; returns the slot index.

    ``preset=None`` takes the lowest free slot (the library re-reads the slots first);
    a named slot is overwritten. The saved slot's preset image is dropped, since it
    shows the slot's former view (``PresetManager.async_forget_image``).
    """
    station = coordinator.station
    serial = redact_serial(device_sn)
    target = "a free slot" if preset is None else f"slot {preset}"
    known = station.presets(device_sn) is not None
    before = presets.enabled_indexes(station, device_sn)
    try:
        slot = await station.async_save_preset(device_sn, preset=preset, make_default=make_default)
    except DeviceBusyError as err:
        _LOGGER.debug("Save to %s of %s refused: camera busy, nothing sent", target, serial)
        raise errors.capture_in_progress() from err
    except CommandUnsupportedError as err:
        raise _not_handled(f"Save to {target}", serial, err) from err
    except UnsupportedError as err:
        _LOGGER.debug("Save to %s of %s refused: %s", target, serial, type(err).__name__)
        if preset is None:
            raise errors.pan_tilt_unsupported() from err
        raise errors.preset_slot_unknown(preset) from err
    except PresetSlotsFullError as err:
        _LOGGER.debug(
            "Save to %s of %s refused: all %d preset slots in use", target, serial, err.slots
        )
        raise errors.presets_full(err.slots) from err
    except CommandNotAppliedError as err:
        if err.command == _DEFAULT_COMMAND:
            _LOGGER.debug("Save to %s of %s stored, but not made the default", target, serial)
            # The error names no slot: the one saved is the named one, or the one the
            # read-back added (unknowable when the slots were never read before).
            stored = presets.enabled_indexes(station, device_sn) - before if known else set()
            for index in stored | ({preset} if preset is not None else set()):
                manager.async_forget_image(device_sn, index)
            raise errors.preset_saved_not_default() from err
        _LOGGER.debug("Save to %s of %s not read back (command %d)", target, serial, err.command)
        raise errors.preset_not_saved() from err
    except CommandRejectedError as err:
        _LOGGER.debug("Save to %s of %s rejected with code %d", target, serial, err.code)
        if err.code == _NEEDS_CONFIRMATION_CODE:
            raise errors.default_preset_needs_confirmation() from err
        raise errors.preset_not_saved() from err
    except EufySecurityError as err:
        _LOGGER.debug("Save to %s of %s failed: %s", target, serial, errors.failure_reason(err))
        raise errors.setting_write_failed(
            err, _target(name, SERVICE_SAVE_PRESET), on_demand=station.connects_on_demand
        ) from err
    manager.async_forget_image(device_sn, slot.index)
    _LOGGER.debug(
        "Saved the view of %s as preset %d%s",
        serial,
        slot.index,
        " (the default)" if slot.is_default else "",
    )
    return slot.index


async def async_delete_preset(
    coordinator: StationCoordinator,
    device_sn: str,
    manager: PresetManager,
    index: int,
    name: object = None,
) -> None:
    """Clear slot ``index`` of ``device_sn``; returns once the read-back shows it free.

    The slot's entities follow the library's ``PresetsChanged``: they go unavailable
    and the slot leaves the selects' options. Clearing the default slot leaves no
    default, so the default-preset select shows unknown. The slot's image is dropped.
    """
    station = coordinator.station
    serial = redact_serial(device_sn)
    try:
        await station.async_delete_preset(device_sn, index)
    except DeviceBusyError as err:
        _LOGGER.debug("Delete of preset %d of %s refused: camera busy, nothing sent", index, serial)
        raise errors.capture_in_progress() from err
    except CommandUnsupportedError as err:
        raise _not_handled(f"Delete of preset {index}", serial, err) from err
    except UnsupportedError as err:
        _LOGGER.debug("Delete of preset %d of %s refused: %s", index, serial, type(err).__name__)
        raise errors.preset_slot_unknown(index) from err
    except CommandError as err:
        _LOGGER.debug(
            "Delete of preset %d of %s not applied: %s", index, serial, type(err).__name__
        )
        raise errors.preset_not_deleted(index) from err
    except EufySecurityError as err:
        _LOGGER.debug(
            "Delete of preset %d of %s failed: %s", index, serial, errors.failure_reason(err)
        )
        raise errors.setting_write_failed(
            err, _target(name, SERVICE_DELETE_PRESET), on_demand=station.connects_on_demand
        ) from err
    manager.async_forget_image(device_sn, index)
    _LOGGER.debug("Deleted preset %d of %s", index, serial)
