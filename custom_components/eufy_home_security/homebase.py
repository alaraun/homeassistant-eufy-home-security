"""A HomeBase's security modes, per-mode device actions and arming groups, for the HomeBase card.

Websocket commands, registered once per Home Assistant instance. ``device_id`` is the
HA device of a station (a HomeBase, or a standalone camera); no response carries a
serial: devices are named by HA device id and name.

- ``eufy_home_security/homebase`` ``{device_id}`` (read): the station's name, model, alarm
  panel and security-mode select, selected and in-force mode, session state, the modes
  with a per-mode table (``modes``, with ``reported`` when a paired device reports a
  value for that table), the per-mode action flags by scope (``flags``) and each paired
  device's per-mode action switches and entry/leaving delay numbers (``devices``).
- ``eufy_home_security/arming_groups/list`` (read), ``create``, ``update``, ``delete``,
  ``activate`` (admin) and ``logs`` (read): the arming groups of the station's house
  (``EufySecurity.async_arming_groups`` and siblings). Every call reads the cloud; the
  library caches nothing. ``activate`` changes the alarm state of every member.

Mode and flag labels are the integration's translations (``selector.guard_mode`` and
``selector.mode_action_flag``) in Home Assistant's language. Library errors: ``ValueError``
-> ``invalid_format`` with the library's message, ``UnsupportedError`` ->
``not_supported``, any other library error -> ``home_assistant_error``; a serial in a
message is replaced by the device's name.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from enum import IntEnum
from typing import Any, Final

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    EufySecurityError,
    GuardMode,
    StationState,
    UnsupportedError,
    redact_serial,
)
from eufy_home_security.devices import MODE_ACTION_FLAGS, Scope, mode_action_key

from . import detections
from .const import (
    DOMAIN,
    GUARD_MODE_KEY,
    GUARD_MODE_LABELS_KEY,
    MODE_ACTION_FLAG_LABELS_KEY,
    SECURITY_MODE_KEY,
)
from .select import SETTABLE_MODES
from .settings import mode_delay
from .settings_layout import (
    ATTR_DEVICE_ID,
    ERR_NOT_FOUND,
    Refused,
    ResolvedDevice,
    device_model,
    device_name,
    device_specs,
    registered_entity_id,
    resolve_device,
)

_LOGGER = logging.getLogger(__name__)

WS_HOMEBASE: Final = f"{DOMAIN}/homebase"
WS_GROUPS_LIST: Final = f"{DOMAIN}/arming_groups/list"
WS_GROUPS_CREATE: Final = f"{DOMAIN}/arming_groups/create"
WS_GROUPS_UPDATE: Final = f"{DOMAIN}/arming_groups/update"
WS_GROUPS_DELETE: Final = f"{DOMAIN}/arming_groups/delete"
WS_GROUPS_ACTIVATE: Final = f"{DOMAIN}/arming_groups/activate"
WS_GROUPS_LOGS: Final = f"{DOMAIN}/arming_groups/logs"
ATTR_GROUP_ID: Final = "group_id"
ATTR_NAME: Final = "name"
ATTR_MEMBERS: Final = "members"
ATTR_MODE: Final = "mode"
_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_homebase")
# An activation record's time above this is in milliseconds (the cloud's unit is not fixed).
_EPOCH_MS_THRESHOLD: Final = 10**11

# A serial as the library's messages redact it (``T8030***4321``), and what replaces one.
_REDACTED_SERIAL: Final = re.compile(r"\b[A-Z0-9]{5}\*+[A-Z0-9]{4}\b")
_UNNAMED: Final = "a device"

_MEMBER = vol.Schema({vol.Required(ATTR_DEVICE_ID): str, vol.Required(ATTR_MODE): str})


def table_modes() -> tuple[GuardMode, ...]:
    """The guard modes with a per-mode action table, in ``GuardMode`` order."""
    modes: list[GuardMode] = []
    for mode in GuardMode:
        try:
            mode_action_key(mode, Scope.CAMERA)
        except UnsupportedError:
            continue
        modes.append(mode)
    return tuple(modes)


def mode_id(mode: GuardMode | int) -> str:
    """A guard mode's id (``away``, ``custom_1``); the code as text for an unknown one."""
    return mode.name.lower() if isinstance(mode, GuardMode) else str(int(mode))


class _Labels:
    """The mode and flag labels in Home Assistant's language; the id where none is known."""

    def __init__(self, strings: Mapping[str, str]) -> None:
        self._strings = strings

    @classmethod
    async def async_load(cls, hass: HomeAssistant) -> _Labels:
        return cls(await async_get_translations(hass, hass.config.language, "selector", {DOMAIN}))

    def _label(self, section: str, key: str) -> str:
        return self._strings.get(f"component.{DOMAIN}.selector.{section}.options.{key}", key)

    def mode(self, mode: str) -> str:
        return self._label(GUARD_MODE_LABELS_KEY, mode)

    def flag(self, flag: str) -> str:
        return self._label(MODE_ACTION_FLAG_LABELS_KEY, flag)


def _station_target(hass: HomeAssistant, device_id: str) -> ResolvedDevice:
    """The station ``device_id``; ``not_found`` for a paired device or any other device."""
    target = resolve_device(hass, device_id)
    if target.device_sn is not None:
        raise Refused(ERR_NOT_FOUND, "Not a HomeBase or standalone camera of this integration")
    return target


def _device_of(hass: HomeAssistant, entry_id: str, serial: str) -> dr.BaseDeviceEntry | None:
    """The HA device of ``serial`` that entry ``entry_id`` registered, else None."""
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry_id)


def _reports(data: StationState | None, key: str, device_sn: str) -> bool:
    """Whether ``data`` holds a value of the paired device's setting ``key``."""
    if data is None:
        return False
    try:
        return data.setting(key, device_sn=device_sn) is not None
    except UnsupportedError:
        return False


def homebase(hass: HomeAssistant, target: ResolvedDevice, labels: _Labels) -> dict[str, Any]:
    """The ``homebase`` answer for the station ``target``."""
    station = target.station
    data: StationState | None = target.coordinator.data
    modes = table_modes()
    reported: set[GuardMode] = set()
    devices: list[dict[str, Any]] = []
    for sub in station.sub_devices:
        serial = sub.device_sn
        if not serial or serial == station.serial:
            continue
        if (device := _device_of(hass, target.entry.entry_id, serial)) is None:
            continue
        paired = ResolvedDevice(device, target.entry, target.coordinator, serial)
        actions: dict[str, dict[str, str]] = {}
        delays: dict[str, dict[str, str]] = {}
        for spec in device_specs(paired):
            if spec.action is not None and spec.flag is not None:
                mode, slot, field = spec.action.mode, actions, spec.flag
            elif (delay := mode_delay(spec.setting)) is not None:
                field, mode = delay
                slot = delays
            else:
                continue
            if mode in modes and _reports(data, spec.setting.key, serial):
                reported.add(mode)
            entity_id = registered_entity_id(hass, spec.platform, serial, spec.key)
            if entity_id is not None:
                slot.setdefault(mode_id(mode), {})[field] = entity_id
        kind = detections.device_kind(serial)
        devices.append(
            {
                ATTR_DEVICE_ID: device.id,
                "name": device_name(device),
                "kind": None if kind is None else str(kind),
                "model": device_model(device),
                "actions": actions,
                "delays": delays,
            }
        )
    guard = None if data is None else data.guard_mode
    active = None if data is None else data.active_mode
    return {
        ATTR_DEVICE_ID: target.device.id,
        "name": device_name(target.device),
        "model": device_model(target.device),
        "alarm_panel": registered_entity_id(hass, ALARM_DOMAIN, station.serial, GUARD_MODE_KEY),
        "security_mode": registered_entity_id(
            hass, Platform.SELECT, station.serial, SECURITY_MODE_KEY
        ),
        "guard_mode": detections.mode_label(guard),
        "active_mode": detections.mode_label(active),
        "connected": station.connected,
        "modes": [
            {"id": mode_id(mode), "label": labels.mode(mode_id(mode)), "reported": mode in reported}
            for mode in modes
        ],
        "flags": {
            str(scope): [{"id": flag, "label": labels.flag(flag)} for flag in flags]
            for scope, flags in MODE_ACTION_FLAGS.items()
        },
        "devices": devices,
        "arming_groups": True,
    }


class _Serials:
    """Serial <-> HA device for one request; scrubs serials out of library messages."""

    def __init__(self, hass: HomeAssistant, target: ResolvedDevice) -> None:
        self._hass = hass
        self._target = target

    def device(self, serial: str) -> dr.BaseDeviceEntry | None:
        """The target entry's HA device of ``serial``, else None."""
        return _device_of(self._hass, self._target.entry.entry_id, serial)

    def station_serial(self, device_id: str) -> str:
        """The serial of a station of the target's entry; ``invalid_format`` otherwise."""
        for serial, coordinator in self._target.entry.runtime_data.coordinators.items():
            device = self.device(serial)
            if device is not None and device.id == device_id:
                return coordinator.station.serial
        raise Refused(
            websocket_api.ERR_INVALID_FORMAT,
            f"{device_id} is not a HomeBase or standalone camera of this account",
        )

    def scrub(self, text: str) -> str:
        """``text`` with every known serial, plain or redacted, replaced by its device's name,
        and any other redacted serial by ``a device``."""
        known: set[str] = set(self._target.entry.runtime_data.coordinators)
        for coordinator in self._target.entry.runtime_data.coordinators.values():
            known.update(s.device_sn for s in coordinator.station.sub_devices if s.device_sn)
        for serial in known:
            device = self.device(serial)
            name = (device_name(device) if device is not None else None) or _UNNAMED
            text = text.replace(serial, name).replace(redact_serial(serial), name)
        return _REDACTED_SERIAL.sub(_UNNAMED, text)


def _candidates(serials: _Serials, target: ResolvedDevice, labels: _Labels) -> list[Any]:
    out: list[dict[str, Any]] = []
    for serial in target.entry.runtime_data.coordinators:
        device = serials.device(serial)
        if device is None:
            continue
        out.append(
            {
                ATTR_DEVICE_ID: device.id,
                "name": device_name(device),
                "modes": [
                    {"id": mode_id(mode), "label": labels.mode(mode_id(mode))}
                    for mode in SETTABLE_MODES
                ],
            }
        )
    return out


def _member_label(mode: GuardMode | int, title: str, labels: _Labels) -> str:
    """A member's mode label: the translation, the app's stored name for a custom mode."""
    custom = isinstance(mode, GuardMode) and mode.name.startswith("CUSTOM_")
    if title and (custom or not isinstance(mode, GuardMode)):
        return title
    return labels.mode(mode_id(mode))


def _groups(listing: Any, serials: _Serials, labels: _Labels) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group in listing.groups:
        members: list[dict[str, Any]] = []
        for member in group.members:
            device = serials.device(member.device_sn)
            members.append(
                {
                    ATTR_DEVICE_ID: None if device is None else device.id,
                    "name": None if device is None else device_name(device),
                    "mode": mode_id(member.mode),
                    "label": _member_label(member.mode, member.title, labels),
                }
            )
        preset = group.preset
        groups.append(
            {
                ATTR_GROUP_ID: group.group_id,
                "name": group.name,
                "active": group.active,
                # A code the library does not name is shown as a group of the user's.
                "preset": preset.name.lower() if isinstance(preset, IntEnum) else "user",
                ATTR_MEMBERS: members,
            }
        )
    return groups


async def _listing(
    hass: HomeAssistant, target: ResolvedDevice, serials: _Serials
) -> dict[str, Any]:
    """The ``list`` answer: the house's groups and this account's stations."""
    labels = await _Labels.async_load(hass)
    listing = await target.entry.runtime_data.eufy.async_arming_groups()
    return {
        "groups": _groups(listing, serials, labels),
        "candidates": _candidates(serials, target, labels),
    }


def _members(serials: _Serials, members: Iterable[Mapping[str, str]]) -> dict[str, str]:
    return {serials.station_serial(m[ATTR_DEVICE_ID]): m[ATTR_MODE] for m in members}


type _Handler = Callable[[ResolvedDevice, _Serials], Awaitable[dict[str, Any]]]


async def _answer(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    handler: _Handler,
) -> None:
    """Run ``handler`` for the station of ``msg`` and send its answer or the mapped error."""
    try:
        target = _station_target(hass, msg[ATTR_DEVICE_ID])
    except Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    serials = _Serials(hass, target)
    try:
        result = await handler(target, serials)
    except Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, serials.scrub(str(err)))
        return
    except UnsupportedError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_SUPPORTED, serials.scrub(str(err)))
        return
    except EufySecurityError as err:
        _LOGGER.debug("Arming-group request failed: %s", type(err).__name__)
        connection.send_error(
            msg["id"], websocket_api.ERR_HOME_ASSISTANT_ERROR, serials.scrub(str(err))
        )
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): WS_HOMEBASE, vol.Required(ATTR_DEVICE_ID): str}
)
@websocket_api.async_response
async def _ws_homebase(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the station's modes, flags and per-mode device entities."""
    try:
        target = _station_target(hass, msg[ATTR_DEVICE_ID])
    except Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], homebase(hass, target, await _Labels.async_load(hass)))


@websocket_api.websocket_command(
    {vol.Required("type"): WS_GROUPS_LIST, vol.Required(ATTR_DEVICE_ID): str}
)
@websocket_api.async_response
async def _ws_groups_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List the house's arming groups and the stations a group can hold."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        return await _listing(hass, target, serials)

    await _answer(hass, connection, msg, handler)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_GROUPS_CREATE,
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_NAME): str,
        vol.Required(ATTR_MEMBERS): [_MEMBER],
    }
)
@websocket_api.async_response
async def _ws_groups_create(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Create a group; answer the new listing."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        members = _members(serials, msg[ATTR_MEMBERS])
        await target.entry.runtime_data.eufy.async_create_arming_group(msg[ATTR_NAME], members)
        return await _listing(hass, target, serials)

    await _answer(hass, connection, msg, handler)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_GROUPS_UPDATE,
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_GROUP_ID): str,
        vol.Optional(ATTR_NAME): str,
        vol.Optional(ATTR_MEMBERS): [_MEMBER],
    }
)
@websocket_api.async_response
async def _ws_groups_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Rename a group and/or replace its members; answer the new listing."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        members = None if ATTR_MEMBERS not in msg else _members(serials, msg[ATTR_MEMBERS])
        await target.entry.runtime_data.eufy.async_update_arming_group(
            msg[ATTR_GROUP_ID], name=msg.get(ATTR_NAME), members=members
        )
        return await _listing(hass, target, serials)

    await _answer(hass, connection, msg, handler)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_GROUPS_DELETE,
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_GROUP_ID): str,
    }
)
@websocket_api.async_response
async def _ws_groups_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Delete a group; answer the new listing."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        await target.entry.runtime_data.eufy.async_delete_arming_group(msg[ATTR_GROUP_ID])
        return await _listing(hass, target, serials)

    await _answer(hass, connection, msg, handler)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_GROUPS_ACTIVATE,
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_GROUP_ID): str,
    }
)
@websocket_api.async_response
async def _ws_groups_activate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Activate a group (changes the alarm state); answer each member's outcome."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        eufy = target.entry.runtime_data.eufy
        activation = await eufy.async_activate_arming_group(msg[ATTR_GROUP_ID])
        results: list[dict[str, Any]] = []
        for result in activation.results:
            device = serials.device(result.device_sn)
            error = result.error
            results.append(
                {
                    ATTR_DEVICE_ID: None if device is None else device.id,
                    "name": None if device is None else device_name(device),
                    "mode": mode_id(result.mode),
                    "outcome": str(result.outcome),
                    "error": None if error is None else serials.scrub(str(error)),
                }
            )
        return {"results": results}

    await _answer(hass, connection, msg, handler)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_GROUPS_LOGS,
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_GROUP_ID): str,
    }
)
@websocket_api.async_response
async def _ws_groups_logs(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer a group's activation records, in the cloud's order."""

    async def handler(target: ResolvedDevice, _serials: _Serials) -> dict[str, Any]:
        logs = await target.entry.runtime_data.eufy.async_arming_group_logs(msg[ATTR_GROUP_ID])
        return {
            "logs": [
                {
                    "user_name": log.user_name,
                    "group_name": log.group_name,
                    "time": _log_time(log.create_time),
                }
                for log in logs
            ]
        }

    await _answer(hass, connection, msg, handler)


def _log_time(stamp: int) -> str:
    """An activation record's epoch time (seconds or milliseconds) as ISO 8601 UTC."""
    seconds = stamp / 1000 if stamp > _EPOCH_MS_THRESHOLD else stamp
    return dt_util.utc_from_timestamp(seconds).isoformat()


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the commands once per Home Assistant instance."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for command in (
        _ws_homebase,
        _ws_groups_list,
        _ws_groups_create,
        _ws_groups_update,
        _ws_groups_delete,
        _ws_groups_activate,
        _ws_groups_logs,
    ):
        websocket_api.async_register_command(hass, command)
