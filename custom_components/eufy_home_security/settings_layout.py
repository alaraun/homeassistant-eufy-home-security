"""A device's setting entities by section, for the dashboard cards.

One websocket command, registered once per Home Assistant instance:

- ``eufy_home_security/settings_layout`` ``{device_id}`` (a station, a paired camera or
  sensor, a standalone camera) answers ``{device_id, sections}``: each section with at
  least one registered entity, as ``{id, heading, order, alarm_affecting, entity_ids,
  alarm_affecting_entity_ids}``. Sections, headings, order and ``alarm_affecting`` are the
  library's (``Setting.section_info``, ``SETTING_SECTIONS``); entity ids follow
  ``Station.settings_for()``, a ``toggles`` setting's members in the library's order where
  the setting sits; ``alarm_affecting_entity_ids`` are those of settings whose write
  changes how the station alarms (``Setting.alarm_affecting``). Disabled registry entries
  are listed. The integration's own pan/tilt selects (default and live-view preset) are added
  to ``ptz``. Entities without a section (camera, battery, buttons) are not listed.

Errors: ``not_found`` for a device that is not a station or device of this integration,
``unavailable`` while its account is not loaded.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    SETTING_SECTIONS,
    SettingSection,
    Station,
    entity_unique_id,
    setting_section,
)

from . import detections
from .const import DEFAULT_PRESET_KEY, DOMAIN, LIVE_PRESET_KEY
from .settings import SettingEntitySpec, setting_specs

if TYPE_CHECKING:
    from .coordinator import StationCoordinator
    from .runtime import EufyConfigEntry

WS_SETTINGS_LAYOUT: Final = f"{DOMAIN}/settings_layout"
ATTR_DEVICE_ID: Final = "device_id"
ERR_NOT_FOUND: Final = websocket_api.ERR_NOT_FOUND
ERR_UNAVAILABLE: Final = "unavailable"
# The section of the integration's own pan/tilt selects.
_PTZ_SECTION: Final = "ptz"
_PTZ_KEYS: Final = (DEFAULT_PRESET_KEY, LIVE_PRESET_KEY)
_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_settings_layout")


class Refused(Exception):
    """A request answered with an error code and a plain message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedDevice:
    """An HA device of a loaded entry: its station, and its serial when it is a paired one."""

    device: dr.BaseDeviceEntry
    entry: EufyConfigEntry
    coordinator: StationCoordinator
    device_sn: str | None
    """A paired device's serial; None is the station itself."""

    @property
    def station(self) -> Station:
        """The station that serves the device."""
        return self.coordinator.station

    @property
    def serial(self) -> str:
        """The device's own serial: the station's for the station itself."""
        return self.device_sn if self.device_sn is not None else self.station.serial


def device_serial(device: dr.BaseDeviceEntry) -> str | None:
    """The serial an HA device of this integration is identified by, else None."""
    return next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)


def device_name(device: dr.BaseDeviceEntry) -> str | None:
    """The name HA shows for ``device``: the user's, else the integration's."""
    return device.name_by_user or device.name


def device_model(device: dr.BaseDeviceEntry) -> str | None:
    """The model the registry holds for ``device``."""
    return device.model if isinstance(device, dr.DeviceEntry) else None


def resolve_device(hass: HomeAssistant, device_id: str) -> ResolvedDevice:
    """The station or paired device ``device_id`` of a loaded entry of this integration.

    ``not_found`` for any other device (the account's service device included),
    ``unavailable`` while its entry is not loaded.
    """
    device = dr.async_get(hass).async_get(device_id)
    serial = None if device is None else device_serial(device)
    if device is None or serial is None:
        raise Refused(ERR_NOT_FOUND, "Not a device of this integration")
    entry: EufyConfigEntry | None = hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise Refused(ERR_NOT_FOUND, "Not a device of this integration")
    if entry.state is not ConfigEntryState.LOADED:
        raise Refused(ERR_UNAVAILABLE, "The device's account is not loaded")
    coordinators = entry.runtime_data.coordinators
    if (coordinator := coordinators.get(serial)) is not None:
        return ResolvedDevice(device, entry, coordinator, None)
    for coordinator in coordinators.values():
        if any(sub.device_sn == serial for sub in coordinator.station.sub_devices):
            return ResolvedDevice(device, entry, coordinator, serial)
    raise Refused(ERR_NOT_FOUND, "Not a device of this integration")


def registered_entity_id(hass: HomeAssistant, domain: str, serial: str, key: str) -> str | None:
    """The registered entity of ``key`` on device ``serial`` and platform ``domain``."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, entity_unique_id(serial, key))


def device_specs(target: ResolvedDevice) -> list[SettingEntitySpec]:
    """The setting entities of ``target`` in the library's order."""
    return [spec for spec in setting_specs(target.station) if spec.device_sn == target.device_sn]


def _section_order(section: SettingSection) -> tuple[int, int, str]:
    """By ``order``; a known section keeps its ``SETTING_SECTIONS`` place on a tie."""
    known = next((i for i, s in enumerate(SETTING_SECTIONS) if s.id == section.id), None)
    return section.order, len(SETTING_SECTIONS) if known is None else known, section.id


def layout(hass: HomeAssistant, target: ResolvedDevice) -> list[dict[str, Any]]:
    """The sections of ``target``'s registered setting entities, in display order."""
    sections: dict[str, tuple[SettingSection, list[str], list[str]]] = {}

    def add(section: SettingSection, entity_ids: Iterable[str], *, alarm: bool = False) -> None:
        _, ids, alarm_ids = sections.setdefault(section.id, (section, [], []))
        ids.extend(entity_ids)
        if alarm:
            alarm_ids.extend(entity_ids)

    serial = target.serial
    for spec in device_specs(target):
        entity_id = registered_entity_id(hass, spec.platform, serial, spec.key)
        if entity_id is not None:
            add(spec.setting.section_info, [entity_id], alarm=spec.setting.alarm_affecting)
    if detections.has_preset_entities(target.station, serial):
        own = [
            entity_id
            for key in _PTZ_KEYS
            if (entity_id := registered_entity_id(hass, Platform.SELECT, serial, key)) is not None
        ]
        if own:
            add(setting_section(_PTZ_SECTION), own)
    return [
        {
            "id": section.id,
            "heading": section.heading,
            "order": section.order,
            "alarm_affecting": section.alarm_affecting,
            "entity_ids": ids,
            "alarm_affecting_entity_ids": alarm_ids,
        }
        for section, ids, alarm_ids in sorted(
            sections.values(), key=lambda item: _section_order(item[0])
        )
    ]


@websocket_api.websocket_command(
    {vol.Required("type"): WS_SETTINGS_LAYOUT, vol.Required(ATTR_DEVICE_ID): str}
)
@callback
def _ws_settings_layout(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer a device's setting entities by section."""
    try:
        target = resolve_device(hass, msg[ATTR_DEVICE_ID])
    except Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(
        msg["id"], {ATTR_DEVICE_ID: target.device.id, "sections": layout(hass, target)}
    )


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the command once per Home Assistant instance."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    websocket_api.async_register_command(hass, _ws_settings_layout)
