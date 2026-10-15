"""A HomeBase's configuration, per-mode device actions and arming groups, for the HomeBase card.

Websocket commands, registered once per Home Assistant instance. ``device_id`` is the
HA device of a station (a HomeBase, or a standalone camera); no response carries a
serial: devices are named by HA device id and name, a device HA does not know by
``device_id: null``.

- ``eufy_home_security/homebase`` ``{device_id}`` (read): the station's configuration
  (``Station.config``, no I/O) with its revision, defined modes and their names, mode
  tables, schedule and cross-camera tracking; the support of every ``StationFeature``;
  the per-mode action flags with the library's labels; the paired devices; the alarm
  panel, security-mode select, alarm and storage status. See ``payload``.
- ``eufy_home_security/homebase/subscribe`` ``{device_id}`` (read): the same payload as
  an event at once, then after every update of the station's coordinator (which the
  router makes on ``StationConfigChanged``, ``GuardModeChanged`` and every state) and
  every state change of its alarm panel (``AlarmChanged``), at most one per
  ``_DEBOUNCE_S``. Follows the entry across a reload.
- ``eufy_home_security/homebase/mode_table/set`` (admin) ``{device_id, mode, actions,
  entry_s?, entry_devices?, exit_s?, exit_devices?, expect_revision}``: one mode's
  device actions and delays in one write (``Station.async_set_mode_actions``); answers
  the payload after the write.
- ``homebase/custom_mode/create`` ``{name}``, ``rename`` ``{mode, name}``, ``delete``
  ``{mode}``; ``homebase/schedule/set`` ``{slots, default?}``; ``homebase/cross_camera/set``
  ``{cameras?, groups?}`` (admin, each with ``device_id`` and ``expect_revision``): the
  library's configuration writes; answer the payload after the write.
- ``homebase/restart`` and ``homebase/alarm`` ``{action, seconds?}`` (admin,
  ``device_id``): restart the station, sound or stop its alarm; answer the payload.
- ``eufy_home_security/arming_groups/list`` (read), ``create``, ``update``, ``delete``,
  ``activate`` (admin) and ``logs`` (read): the arming groups of the station's house
  (``EufySecurity.async_arming_groups`` and siblings). Every call reads the cloud; the
  library caches nothing. ``activate`` changes the alarm state of every member.

Library errors: ``ConfigChangedError`` -> ``config_changed``, ``ModeInUseError`` ->
``mode_in_use`` (the library's reason), ``CommandNotAppliedError`` -> ``not_applied``,
``ValueError`` -> ``invalid_format``, ``UnsupportedError`` -> ``not_supported``, any
other library error -> ``home_assistant_error``. The devices an error names
(``device_sns``) and the request's station read as their HA names; any other redacted
serial as ``a device``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import time
from typing import Any, Final

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    MODE_ACTION_ALARM_AFFECTING,
    MODE_DELAY_CHOICES,
    ArmingGroupList,
    ArmingGroupPreset,
    CommandNotAppliedError,
    ConfigChangedError,
    EufySecurityError,
    GuardMode,
    ModeInUseError,
    Schedule,
    ScheduleSlot,
    Station,
    StationConfig,
    StationFeature,
    StationState,
    SubDeviceState,
    UnsupportedError,
    guard_mode_label,
    redact_serial,
)
from eufy_home_security.devices import (
    MODE_ACTION_FLAGS,
    mode_action_flags,
    mode_action_label,
    scope_for_kind,
)
from eufy_home_security.station import MIN_TRACKING_CAMERAS

from . import detections
from .const import DOMAIN, FIRMWARE_KEY, GUARD_MODE_KEY, SECURITY_MODE_KEY
from .select import mode_choices
from .settings_layout import (
    ATTR_DEVICE_ID,
    ERR_NOT_FOUND,
    Refused,
    ResolvedDevice,
    device_model,
    device_name,
    registered_entity_id,
    resolve_device,
)
from .storage import StoragePart

_LOGGER = logging.getLogger(__name__)

WS_HOMEBASE: Final = f"{DOMAIN}/homebase"
WS_HOMEBASE_SUBSCRIBE: Final = f"{DOMAIN}/homebase/subscribe"
WS_MODE_TABLE_SET: Final = f"{DOMAIN}/homebase/mode_table/set"
WS_CUSTOM_MODE_CREATE: Final = f"{DOMAIN}/homebase/custom_mode/create"
WS_CUSTOM_MODE_RENAME: Final = f"{DOMAIN}/homebase/custom_mode/rename"
WS_CUSTOM_MODE_DELETE: Final = f"{DOMAIN}/homebase/custom_mode/delete"
WS_SCHEDULE_SET: Final = f"{DOMAIN}/homebase/schedule/set"
WS_CROSS_CAMERA_SET: Final = f"{DOMAIN}/homebase/cross_camera/set"
WS_RESTART: Final = f"{DOMAIN}/homebase/restart"
WS_ALARM: Final = f"{DOMAIN}/homebase/alarm"
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
ATTR_ACTIONS: Final = "actions"
ATTR_ENTRY_S: Final = "entry_s"
ATTR_EXIT_S: Final = "exit_s"
ATTR_ENTRY_DEVICES: Final = "entry_devices"
ATTR_EXIT_DEVICES: Final = "exit_devices"
ATTR_EXPECT_REVISION: Final = "expect_revision"
ATTR_SLOTS: Final = "slots"
ATTR_DEFAULT: Final = "default"
ATTR_CAMERAS: Final = "cameras"
ATTR_GROUPS: Final = "groups"
ATTR_ACTION: Final = "action"
ATTR_SECONDS: Final = "seconds"
ALARM_SOUND: Final = "sound"
ALARM_STOP: Final = "stop"
ERR_CONFIG_CHANGED: Final = "config_changed"
ERR_MODE_IN_USE: Final = "mode_in_use"
ERR_NOT_APPLIED: Final = "not_applied"
_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_homebase")
# At most one subscription event per this many seconds.
_DEBOUNCE_S: Final = 0.5
# A mode id for a guard-mode code ``GuardMode`` does not name: ``code_<n>``.
_CODE_PREFIX: Final = "code_"

# Written by its setting entity when the station has the setting (``StationConfig.setting_keys``);
# every other feature is written by a command of this module, the select or the panel.
_SETTING_FEATURES: Final = {StationFeature.WORKING_MODE: "working_mode"}

# A serial as the library's messages redact it (``T8030***4321``), and what replaces one
# the error's ``device_sn`` does not name.
_REDACTED_SERIAL: Final = re.compile(r"\b[A-Z0-9]{5}\*+[A-Z0-9]{4}\b")
_UNNAMED: Final = "a device"

_MEMBER = vol.Schema({vol.Required(ATTR_DEVICE_ID): str, vol.Required(ATTR_MODE): str})
_SLOT = vol.Schema(
    {
        vol.Required("weekday"): int,
        vol.Required("start"): str,
        vol.Required("end"): str,
        vol.Required(ATTR_MODE): str,
    }
)
_CLOCK = re.compile(r"(\d{2}):(\d{2})")
# A slot's end at midnight, as the card shows and sends it.
_DAY_END: Final = "24:00"


def mode_id(mode: GuardMode | int) -> str:
    """A guard mode's id (``away``, ``custom_1``); ``code_<n>`` for a code ``GuardMode``
    does not name."""
    return mode.name.lower() if isinstance(mode, GuardMode) else f"{_CODE_PREFIX}{int(mode)}"


def parse_mode_id(text: str) -> GuardMode | int:
    """The guard mode of a ``mode_id``; ``ValueError`` for anything else."""
    if text.startswith(_CODE_PREFIX) and text.removeprefix(_CODE_PREFIX).isdigit():
        code = int(text.removeprefix(_CODE_PREFIX))
        try:
            return GuardMode(code)
        except ValueError:
            return code
    try:
        return GuardMode[text.upper()]
    except KeyError:
        raise ValueError(f"unknown mode {text!r}") from None


def _station_target(hass: HomeAssistant, device_id: str) -> ResolvedDevice:
    """The station ``device_id``; ``not_found`` for a paired device or any other device."""
    target = resolve_device(hass, device_id)
    if target.device_sn is not None:
        raise Refused(ERR_NOT_FOUND, "Not a HomeBase or standalone camera of this integration")
    return target


def _device_of(hass: HomeAssistant, entry_id: str, serial: str) -> dr.BaseDeviceEntry | None:
    """The HA device of ``serial`` that entry ``entry_id`` registered, else None."""
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry_id)


def _json_value(value: object) -> bool | int | float | str | None:
    """A setting's public value as JSON; anything else as its text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _clock(value: time) -> str:
    """A time of day as ``HH:MM``."""
    return f"{value.hour:02d}:{value.minute:02d}"


def _parse_clock(text: str, *, end: bool = False) -> time:
    """An ``HH:MM`` time of day; with ``end``, ``24:00`` is midnight (the library's
    ``time(0)`` end). ``ValueError`` for anything else."""
    if end and text == _DAY_END:
        return time(0)
    match = _CLOCK.fullmatch(text)
    if match is None or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"a time must be HH:MM, got {text!r}")
    return time(int(match[1]), int(match[2]))


class _Paired:
    """A station's paired devices for one payload: HA device ids, names, flags, state."""

    def __init__(
        self, hass: HomeAssistant, target: ResolvedDevice, data: StationState | None
    ) -> None:
        self.ids: dict[str, str | None] = {}
        self.flags: dict[str, list[str]] = {}
        self.devices: list[dict[str, Any]] = []
        states: dict[str, SubDeviceState] = {}
        if data is not None:
            states = {d.serial: d for d in data.devices.values() if d.serial is not None}
        station = target.station
        listed: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for sub in station.sub_devices:
            serial = sub.device_sn
            if not serial or serial == station.serial:
                continue
            device = _device_of(hass, target.entry.entry_id, serial)
            kind = detections.device_kind(serial)
            scope = scope_for_kind(kind)
            flags: list[str] = []
            if scope in MODE_ACTION_FLAGS:
                flags = list(mode_action_flags(scope, sub.device_type or None))
            self.ids[serial] = None if device is None else device.id
            self.flags[serial] = flags
            state = states.get(serial)
            name = (device_name(device) if device is not None else None) or sub.name
            entry: dict[str, Any] = {
                ATTR_DEVICE_ID: None if device is None else device.id,
                "name": name,
                "kind": None if kind is None else str(kind),
                "model": None if device is None else device_model(device),
                "online": None if state is None else state.online,
                "battery": None if state is None else state.battery,
            }
            # The kind's list in the payload's ``flags``, if the kind has one.
            kind_flags = next(
                (list(v) for k, v in MODE_ACTION_FLAGS.items() if str(k) == str(kind)), None
            )
            if kind_flags != flags:
                entry["flags"] = flags
            listed.append((((name or "").casefold(), "" if device is None else device.id), entry))
        # By name: the cloud list's order changes between refreshes, the card's drawing must not
        self.devices.extend(entry for _, entry in sorted(listed, key=lambda item: item[0]))

    def device_id(self, serial: str | None) -> str | None:
        """The HA device id of a paired ``serial``; None when HA does not know it."""
        return None if serial is None else self.ids.get(serial)


def _support(station: Station, config: StationConfig | None) -> dict[str, Any]:
    """Each ``StationFeature``'s evidence, and whether the integration writes it today."""
    out: dict[str, Any] = {}
    for feature in StationFeature:
        evidence = station.config_support(feature)
        key = _SETTING_FEATURES.get(feature)
        available = key is None or (config is not None and key in config.setting_keys)
        out[feature.value] = {
            "grade": evidence.support.value,
            "note": evidence.note or None,
            "alarm_affecting": feature.alarm_affecting,
            "available": available,
        }
    return out


def _mode_tables(config: StationConfig, paired: _Paired) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for mode, view in config.mode_tables.items():
        if view is None:
            tables[mode_id(mode)] = None
            continue
        devices: dict[str, Any] = {}
        for item in view.devices:
            device_id = paired.device_id(item.device_sn)
            if device_id is None or item.device_sn is None:
                continue
            on = item.flags
            devices[device_id] = {
                "flags": {flag: flag in on for flag in paired.flags[item.device_sn]},
                "entry": item.alarm_delay,
                "leaving": item.leaving_delay,
            }
        tables[mode_id(mode)] = {"devices": devices}
    return tables


def _schedule(config: StationConfig) -> dict[str, Any] | None:
    if config.schedule is None and config.schedule_default is None:
        return None
    slots = () if config.schedule is None else config.schedule.slots
    return {
        "slots": [
            {
                "weekday": int(slot.weekday),
                "start": _clock(slot.start),
                "end": _DAY_END if slot.ends_at_midnight else _clock(slot.end),
                "mode": mode_id(slot.mode),
            }
            for slot in slots
        ],
        "default": None if config.schedule_default is None else mode_id(config.schedule_default),
    }


def _cross_camera(config: StationConfig, paired: _Paired) -> dict[str, Any] | None:
    cross = config.cross_camera
    if cross is None:
        return None
    return {
        "enabled": cross.enabled,
        "assistance": cross.assistance,
        "continuous_time": _json_value(cross.continuous_time),
        "continuous_time_s": cross.continuous_time_s,
        "cameras": (
            None if cross.cameras is None else [paired.device_id(sn) for sn in cross.cameras]
        ),
        "groups": [[paired.device_id(sn) for sn in group] for group in cross.groups],
        "eligible": [d for d in map(paired.device_id, cross.eligible) if d is not None],
        "min_cameras": MIN_TRACKING_CAMERAS,
    }


def _alarm(station: Station) -> dict[str, Any]:
    """Whether the alarm sounds; ``last``: when the last transition was seen and its own
    time (null when it carries none), absent before any."""
    out: dict[str, Any] = {"triggered": station.alarming}
    last = station.last_alarm
    if last is not None:
        out["last"] = {
            "at": last.at.isoformat(),
            "event_time": None if last.event_time is None else last.event_time.isoformat(),
        }
    return out


def _status(hass: HomeAssistant, target: ResolvedDevice) -> dict[str, Any]:
    serial = target.station.serial
    device = target.device
    storage = target.entry.runtime_data.storage.get(serial)
    info = None if storage is None else storage.data
    media: dict[str, Any] = {}
    for part in StoragePart:
        medium = part.medium(info)
        media[str(part)] = (
            None
            if medium is None
            else {
                "used_gib": medium.used_gib,
                "size_gib": medium.size_gib,
                "used_percent": medium.used_percent,
            }
        )
    return {
        "firmware": device.sw_version if isinstance(device, dr.DeviceEntry) else None,
        "update": registered_entity_id(hass, Platform.UPDATE, serial, FIRMWARE_KEY),
        "storage": media,
    }


def payload(hass: HomeAssistant, target: ResolvedDevice) -> dict[str, Any]:
    """The ``homebase`` answer for the station ``target``, from ``Station.config`` (no I/O).

    ``guard_mode`` / ``effective_mode`` follow the coordinator's state, which takes a
    ``GuardModeChanged`` at once; the configuration's copy follows with the next dump.
    """
    station = target.station
    config = station.config
    data: StationState | None = target.coordinator.data
    paired = _Paired(hass, target, data)
    guard = data.guard_mode if data is not None else None
    effective = data.active_mode if data is not None else None
    if data is None and config is not None:
        guard, effective = config.guard_mode, config.effective_mode
    settable = () if config is None else config.settable_modes
    # The security-mode select's option per mode, as it builds them.
    current = None if data is None else data.guard_mode
    options = {code: name for name, code in mode_choices(config, current).items()}
    return {
        ATTR_DEVICE_ID: target.device.id,
        "name": device_name(target.device),
        "model": device_model(target.device),
        "revision": None if config is None else config.revision,
        "alarm_panel": registered_entity_id(hass, ALARM_DOMAIN, station.serial, GUARD_MODE_KEY),
        "security_mode": registered_entity_id(
            hass, Platform.SELECT, station.serial, SECURITY_MODE_KEY
        ),
        "connected": station.connected,
        "alarm": _alarm(station),
        "guard_mode": None if guard is None else mode_id(guard),
        "effective_mode": None if effective is None else mode_id(effective),
        "modes": [
            {
                "id": mode_id(mode.code),
                "name": mode.name,
                "custom": mode.custom,
                "settable": mode.code in settable,
                "option": options.get(mode.code),
            }
            for mode in (() if config is None else config.modes)
        ],
        "support": _support(station, config),
        "flags": {
            str(scope): [
                {
                    "id": flag,
                    "label": mode_action_label(flag),
                    "alarm_affecting": flag in MODE_ACTION_ALARM_AFFECTING,
                }
                for flag in flags
            ]
            for scope, flags in MODE_ACTION_FLAGS.items()
        },
        "delay_choices": list(MODE_DELAY_CHOICES),
        "devices": paired.devices,
        "mode_tables": {} if config is None else _mode_tables(config, paired),
        "schedule": None if config is None else _schedule(config),
        "cross_camera": None if config is None else _cross_camera(config, paired),
        "status": _status(hass, target),
        "sources": {} if config is None else {k: str(v) for k, v in config.sources.items()},
        "unreadable": {} if config is None else dict(config.unreadable),
    }


class _Serials:
    """Serial <-> HA device for one request; names devices in library messages."""

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

    def paired_serial(self, device_id: str) -> str:
        """The serial of a device paired to the target station; ``invalid_format`` otherwise."""
        station = self._target.station
        for sub in station.sub_devices:
            if not sub.device_sn or sub.device_sn == station.serial:
                continue
            device = self.device(sub.device_sn)
            if device is not None and device.id == device_id:
                return sub.device_sn
        raise Refused(
            websocket_api.ERR_INVALID_FORMAT, f"{device_id} is not a device of this HomeBase"
        )

    def message(self, text: str, device_sns: Iterable[str] = ()) -> str:
        """``text`` with ``device_sns`` (the devices a library error names) and the
        target station, plain or redacted, as their HA names; any other redacted serial
        as ``a device``."""
        known = dict.fromkeys((*device_sns, self._target.station.serial))
        for serial in known:
            device = self.device(serial)
            name = (device_name(device) if device is not None else None) or _UNNAMED
            text = text.replace(serial, name).replace(redact_serial(serial), name)
        return _REDACTED_SERIAL.sub(_UNNAMED, text)


def _station_config(target: ResolvedDevice, serial: str) -> StationConfig | None:
    """The configuration of the entry's station ``serial``; None for another station."""
    coordinator = target.entry.runtime_data.coordinators.get(serial)
    return None if coordinator is None else coordinator.station_config


def _candidates(serials: _Serials, target: ResolvedDevice) -> list[Any]:
    out: list[dict[str, Any]] = []
    for serial in target.entry.runtime_data.coordinators:
        device = serials.device(serial)
        if device is None:
            continue
        config = _station_config(target, serial)
        modes = [] if config is None else [(m, config.mode_name(m)) for m in config.settable_modes]
        out.append(
            {
                ATTR_DEVICE_ID: device.id,
                "name": device_name(device),
                "modes": [{"id": mode_id(mode), "label": label} for mode, label in modes],
            }
        )
    return out


def _member_label(target: ResolvedDevice, serial: str, mode: GuardMode | int, title: str) -> str:
    """A member's mode label: its station's mode name; else the app's stored title."""
    config = _station_config(target, serial)
    if config is not None:
        return config.mode_name(mode)
    return title or guard_mode_label(mode)


def _groups(
    listing: ArmingGroupList, serials: _Serials, target: ResolvedDevice
) -> list[dict[str, Any]]:
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
                    "label": _member_label(target, member.device_sn, member.mode, member.title),
                }
            )
        preset = group.preset
        groups.append(
            {
                ATTR_GROUP_ID: group.group_id,
                "name": group.name,
                "active": group.active,
                # A code the library does not name is shown as a group of the user's.
                "preset": preset.name.lower() if isinstance(preset, ArmingGroupPreset) else "user",
                ATTR_MEMBERS: members,
            }
        )
    return groups


async def _listing(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
    """The ``list`` answer: the house's groups and this account's stations."""
    listing = await target.entry.runtime_data.eufy.async_arming_groups()
    return {
        "groups": _groups(listing, serials, target),
        "candidates": _candidates(serials, target),
    }


def _members(serials: _Serials, members: Iterable[Mapping[str, str]]) -> dict[str, str]:
    return {serials.station_serial(m[ATTR_DEVICE_ID]): m[ATTR_MODE] for m in members}


type _Handler = Callable[[ResolvedDevice, _Serials], Awaitable[dict[str, Any]]]


def _error(err: EufySecurityError, serials: _Serials) -> tuple[str, str]:
    """The websocket error code and message of a library error."""
    text = serials.message(str(err), err.device_sns)
    if isinstance(err, ConfigChangedError):
        return ERR_CONFIG_CHANGED, text
    if isinstance(err, ModeInUseError):  # before UnsupportedError, which it is
        return ERR_MODE_IN_USE, text  # the message is the library's reason
    if isinstance(err, UnsupportedError):
        return websocket_api.ERR_NOT_SUPPORTED, text
    if isinstance(err, CommandNotAppliedError):
        return ERR_NOT_APPLIED, text
    _LOGGER.debug("HomeBase request failed: %s", type(err).__name__)
    return websocket_api.ERR_HOME_ASSISTANT_ERROR, text


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
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, serials.message(str(err))
        )
        return
    except EufySecurityError as err:
        connection.send_error(msg["id"], *_error(err, serials))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): WS_HOMEBASE, vol.Required(ATTR_DEVICE_ID): str}
)
@callback
def _ws_homebase(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the station's configuration payload."""
    try:
        target = _station_target(hass, msg[ATTR_DEVICE_ID])
    except Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], payload(hass, target))


class _Subscription:
    """One ``homebase/subscribe``: sends the payload on every change of its station.

    Attached to the loaded entry's coordinator and alarm panel; detached while the entry
    is not loaded and attached again when it is.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg_id: int,
        target: ResolvedDevice,
    ) -> None:
        self._hass = hass
        self._connection = connection
        self._msg_id = msg_id
        self._device_id = target.device.id
        self._entry = target.entry
        self._detach: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None
        self._unwatch_entry = target.entry.async_on_state_change(self._on_entry_state)
        self._attach(target)

    @callback
    def _attach(self, target: ResolvedDevice) -> None:
        self._detach.append(target.coordinator.async_add_listener(self._schedule))
        panel = registered_entity_id(
            self._hass, ALARM_DOMAIN, target.station.serial, GUARD_MODE_KEY
        )
        if panel is not None:
            self._detach.append(async_track_state_change_event(self._hass, [panel], self._on_panel))

    @callback
    def _release(self) -> None:
        while self._detach:
            self._detach.pop()()
        if self._pending is not None:
            self._pending()
            self._pending = None

    @callback
    def close(self) -> None:
        """End the subscription (the client unsubscribed or disconnected)."""
        self._release()
        self._unwatch_entry()

    @callback
    def _on_entry_state(self) -> None:
        if self._entry.state is not ConfigEntryState.LOADED:
            self._release()
            return
        if self._detach:
            return
        try:
            target = _station_target(self._hass, self._device_id)
        except Refused:
            return
        self._attach(target)
        self.send()

    @callback
    def _on_panel(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule()

    @callback
    def _schedule(self) -> None:
        if self._pending is None:
            self._pending = async_call_later(self._hass, _DEBOUNCE_S, self._fire)

    @callback
    def _fire(self, _now: Any) -> None:
        self._pending = None
        self.send()

    @callback
    def send(self) -> None:
        """Send the current payload; nothing while the station cannot be resolved."""
        try:
            target = _station_target(self._hass, self._device_id)
        except Refused:
            return
        self._connection.send_message(
            websocket_api.event_message(self._msg_id, payload(self._hass, target))
        )


@websocket_api.websocket_command(
    {vol.Required("type"): WS_HOMEBASE_SUBSCRIBE, vol.Required(ATTR_DEVICE_ID): str}
)
@callback
def _ws_homebase_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe to the station's payload: one event now, then one per change."""
    try:
        target = _station_target(hass, msg[ATTR_DEVICE_ID])
    except Refused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    subscription = _Subscription(hass, connection, msg["id"], target)
    connection.subscriptions[msg["id"]] = subscription.close
    connection.send_result(msg["id"])
    subscription.send()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_MODE_TABLE_SET,
        vol.Required(ATTR_DEVICE_ID): str,
        vol.Required(ATTR_MODE): str,
        vol.Required(ATTR_ACTIONS): {str: {str: bool}},
        vol.Optional(ATTR_ENTRY_S): int,
        vol.Optional(ATTR_ENTRY_DEVICES): {str: bool},
        vol.Optional(ATTR_EXIT_S): int,
        vol.Optional(ATTR_EXIT_DEVICES): {str: bool},
        vol.Required(ATTR_EXPECT_REVISION): int,
    }
)
@websocket_api.async_response
async def _ws_mode_table_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Write one mode's device actions and delays in one table write; answer the payload."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        mode = parse_mode_id(msg[ATTR_MODE])

        def by_serial[T](values: Mapping[str, T] | None) -> dict[str, T] | None:
            if values is None:
                return None
            return {serials.paired_serial(device_id): v for device_id, v in values.items()}

        await target.station.async_set_mode_actions(
            mode,
            by_serial(msg[ATTR_ACTIONS]) or {},
            entry_s=msg.get(ATTR_ENTRY_S),
            exit_s=msg.get(ATTR_EXIT_S),
            entry_devices=by_serial(msg.get(ATTR_ENTRY_DEVICES)),
            exit_devices=by_serial(msg.get(ATTR_EXIT_DEVICES)),
            expect_revision=msg[ATTR_EXPECT_REVISION],
        )
        return payload(hass, target)

    await _answer(hass, connection, msg, handler)


def _write_command(
    command: str, fields: Mapping[Any, Any], *, revision: bool = True
) -> Callable[
    [Callable[[HomeAssistant, dict[str, Any], ResolvedDevice, _Serials], Awaitable[None]]],
    websocket_api.WebSocketCommandHandler,
]:
    """An admin command of ``fields`` (plus ``device_id`` and, with ``revision``, a
    required ``expect_revision``) that runs a write and answers the payload after it."""
    schema: dict[Any, Any] = {vol.Required("type"): command, vol.Required(ATTR_DEVICE_ID): str}
    schema.update(fields)
    if revision:
        schema[vol.Required(ATTR_EXPECT_REVISION)] = int

    def wrap(
        write: Callable[[HomeAssistant, dict[str, Any], ResolvedDevice, _Serials], Awaitable[None]],
    ) -> websocket_api.WebSocketCommandHandler:
        async def handle(
            hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
        ) -> None:
            async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
                await write(hass, msg, target, serials)
                return payload(hass, target)

            await _answer(hass, connection, msg, handler)

        handle.__doc__ = write.__doc__
        return websocket_api.require_admin(
            websocket_api.websocket_command(schema)(websocket_api.async_response(handle))
        )

    return wrap


@_write_command(WS_CUSTOM_MODE_CREATE, {vol.Required(ATTR_NAME): str})
async def _ws_custom_mode_create(
    _hass: HomeAssistant, msg: dict[str, Any], target: ResolvedDevice, _serials: _Serials
) -> None:
    """Add a custom mode named ``name`` (the station seeds its device table)."""
    await target.station.async_create_custom_mode(
        msg[ATTR_NAME], expect_revision=msg[ATTR_EXPECT_REVISION]
    )


@_write_command(WS_CUSTOM_MODE_RENAME, {vol.Required(ATTR_MODE): str, vol.Required(ATTR_NAME): str})
async def _ws_custom_mode_rename(
    _hass: HomeAssistant, msg: dict[str, Any], target: ResolvedDevice, _serials: _Serials
) -> None:
    """Rename the custom mode ``mode``."""
    await target.station.async_rename_custom_mode(
        parse_mode_id(msg[ATTR_MODE]), msg[ATTR_NAME], expect_revision=msg[ATTR_EXPECT_REVISION]
    )


@_write_command(WS_CUSTOM_MODE_DELETE, {vol.Required(ATTR_MODE): str})
async def _ws_custom_mode_delete(
    _hass: HomeAssistant, msg: dict[str, Any], target: ResolvedDevice, _serials: _Serials
) -> None:
    """Delete the custom mode ``mode``; a mode in use is ``mode_in_use``."""
    await target.station.async_delete_custom_mode(
        parse_mode_id(msg[ATTR_MODE]), expect_revision=msg[ATTR_EXPECT_REVISION]
    )


def _slot(slot: Mapping[str, Any]) -> ScheduleSlot:
    """A ``schedule/set`` slot; ``ValueError`` (the library's text where it has one)."""
    return ScheduleSlot(
        slot["weekday"],
        _parse_clock(slot["start"]),
        _parse_clock(slot["end"], end=True),
        parse_mode_id(slot[ATTR_MODE]),  # type: ignore[arg-type]  # the slot refuses a bare code
    )


@_write_command(
    WS_SCHEDULE_SET,
    {vol.Required(ATTR_SLOTS): [_SLOT], vol.Optional(ATTR_DEFAULT): vol.Any(None, str)},
)
async def _ws_schedule_set(
    _hass: HomeAssistant, msg: dict[str, Any], target: ResolvedDevice, _serials: _Serials
) -> None:
    """Replace the weekly schedule (an end of ``24:00`` runs a slot to midnight);
    ``default`` (a mode id) sets the mode outside its slots, absent or null leaves it."""
    schedule = Schedule(account_id=None, slots=tuple(_slot(s) for s in msg[ATTR_SLOTS]))
    default = msg.get(ATTR_DEFAULT)
    await target.station.async_set_schedule(
        schedule,
        default_mode=None if default is None else parse_mode_id(default),
        expect_revision=msg[ATTR_EXPECT_REVISION],
    )


@_write_command(
    WS_CROSS_CAMERA_SET,
    {
        vol.Optional(ATTR_CAMERAS): vol.Any(None, [str]),
        vol.Optional(ATTR_GROUPS): [[str]],
    },
)
async def _ws_cross_camera_set(
    _hass: HomeAssistant, msg: dict[str, Any], target: ResolvedDevice, serials: _Serials
) -> None:
    """Choose the cross-camera tracking cameras (null: every camera) and/or replace its
    groups; a key left out is unchanged."""
    if ATTR_CAMERAS not in msg and ATTR_GROUPS not in msg:
        raise Refused(websocket_api.ERR_INVALID_FORMAT, "Give cameras, groups or both")
    station = target.station
    revision: int | None = msg[ATTR_EXPECT_REVISION]
    cameras = msg.get(ATTR_CAMERAS)
    groups = [[serials.paired_serial(d) for d in group] for group in msg.get(ATTR_GROUPS, ())]
    if ATTR_CAMERAS in msg:
        chosen = None if cameras is None else [serials.paired_serial(d) for d in cameras]
        await station.async_set_cross_camera_cameras(chosen, expect_revision=revision)
        # The cameras write changed the revision the request names.
        revision = None
    if ATTR_GROUPS in msg:
        await station.async_set_cross_camera_groups(groups, expect_revision=revision)


@_write_command(WS_RESTART, {}, revision=False)
async def _ws_restart(
    _hass: HomeAssistant, _msg: dict[str, Any], target: ResolvedDevice, _serials: _Serials
) -> None:
    """Restart the station; it is unavailable until its session reconnects."""
    await target.station.async_restart()


@_write_command(
    WS_ALARM,
    {
        vol.Required(ATTR_ACTION): vol.In([ALARM_SOUND, ALARM_STOP]),
        vol.Optional(ATTR_SECONDS): int,
    },
    revision=False,
)
async def _ws_alarm(
    _hass: HomeAssistant, msg: dict[str, Any], target: ResolvedDevice, _serials: _Serials
) -> None:
    """Sound the station's alarm (``seconds``, else the library's default) or stop it."""
    station = target.station
    if msg[ATTR_ACTION] == ALARM_STOP:
        await station.async_stop_alarm()
    elif ATTR_SECONDS in msg:
        await station.async_trigger_alarm(msg[ATTR_SECONDS])
    else:
        await station.async_trigger_alarm()


@websocket_api.websocket_command(
    {vol.Required("type"): WS_GROUPS_LIST, vol.Required(ATTR_DEVICE_ID): str}
)
@websocket_api.async_response
async def _ws_groups_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List the house's arming groups and the stations a group can hold."""

    async def handler(target: ResolvedDevice, serials: _Serials) -> dict[str, Any]:
        return await _listing(target, serials)

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
        return await _listing(target, serials)

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
        return await _listing(target, serials)

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
        return await _listing(target, serials)

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
                    "error": (
                        None if error is None else serials.message(str(error), error.device_sns)
                    ),
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
                    "time": log.created_at.isoformat(),
                }
                for log in logs
            ]
        }

    await _answer(hass, connection, msg, handler)


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the commands once per Home Assistant instance."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for command in (
        _ws_homebase,
        _ws_homebase_subscribe,
        _ws_mode_table_set,
        _ws_custom_mode_create,
        _ws_custom_mode_rename,
        _ws_custom_mode_delete,
        _ws_schedule_set,
        _ws_cross_camera_set,
        _ws_restart,
        _ws_alarm,
        _ws_groups_list,
        _ws_groups_create,
        _ws_groups_update,
        _ws_groups_delete,
        _ws_groups_activate,
        _ws_groups_logs,
    ):
        websocket_api.async_register_command(hass, command)
