"""The base entities: everything that belongs to a station, a device, or a setting."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, override

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from eufy_home_security import (
    CommandError,
    DeviceTimeoutError,
    EufySecurityError,
    StationState,
    SubDeviceState,
    UnsupportedError,
    entity_unique_id,
)

from . import errors, runtime
from .const import (
    ATTR_APPLIES_WHEN,
    ATTR_APPLIES_WHEN_LABEL,
    ATTR_CONTROLS,
    DOMAIN,
    PICTURE_CHANGING_SETTINGS,
)
from .coordinator import StationCoordinator
from .settings import SettingEntitySpec, setting_platform

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

type SettingValue = bool | int | float | str
"""A setting's public value (``Setting.validate``)."""


def device_block(data: StationState | None, device_sn: str) -> SubDeviceState | None:
    """The block of the paired device ``device_sn`` in ``data``, looked up by serial."""
    if data is None:
        return None
    return next((device for device in data.devices.values() if device.serial == device_sn), None)


type ReportedCheck = Callable[[StationState], bool]
"""Whether a state reports the field an entity shows."""


@callback
def async_add_when_reported(
    entry: EufyConfigEntry,
    coordinator: StationCoordinator,
    candidates: Iterable[tuple[ReportedCheck, Callable[[], Entity]]],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each candidate entity once a state of ``coordinator`` reports its field.

    Checked now and on every state after, so a station or device that was down at
    setup gains its entities with its first report. An entity once added stays; a
    field that stops being reported shows as unknown.
    """
    pending = list(candidates)

    @callback
    def _check() -> None:
        data: StationState | None = coordinator.data
        if data is None or not pending:
            return
        ready = [candidate for candidate in pending if candidate[0](data)]
        if not ready:
            return
        for candidate in ready:
            pending.remove(candidate)
        async_add_entities(build() for _, build in ready)

    _check()
    if pending:
        entry.async_on_unload(coordinator.async_add_listener(_check))


class EufyStationEntity(CoordinatorEntity[StationCoordinator]):
    """An entity of the station itself, identified by the station's own serial."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StationCoordinator, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.station.serial
        self._attr_unique_id = entity_unique_id(serial, key)
        # Identifiers only. Every other field of the station's device row is
        # written once by `async_setup_entry` before the platforms are forwarded;
        # repeating them here would make two writers for one row.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, serial)})

    @property
    @override
    def available(self) -> bool:
        """Available only once a poll has produced state.

        ``last_update_success`` starts True, so a station whose first connection
        failed, and was therefore never refreshed, would otherwise look available
        with no data behind it.
        """
        return super().available and self.coordinator.data is not None


class EufyDeviceEntity(CoordinatorEntity[StationCoordinator]):
    """An entity of the station or of one paired device, keyed by that device's serial."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StationCoordinator, device_sn: str | None, key: str) -> None:
        super().__init__(coordinator)
        # ``None`` is the station itself; anything else is one of its paired devices.
        self._device_sn = device_sn
        serial = device_sn if device_sn is not None else coordinator.station.serial
        self._attr_unique_id = entity_unique_id(serial, key)
        # Identifiers only, as ``EufyStationEntity``.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, serial)})

    @property
    def device_state(self) -> SubDeviceState | None:
        """The paired device's latest block; None for the station or before a read.

        Looked up by serial on every read and never cached by channel: a device
        moved to another slot keeps its serial and changes its channel.
        """
        device_sn = self._device_sn
        if device_sn is None:
            return None
        return device_block(self.coordinator.data, device_sn)

    @property
    def firmware_version(self) -> str | None:
        """The firmware this device runs: the live dump's, else the cloud's.

        The station's own version falls back to the cloud inside the library; a paired
        device's does not, and the T8910 motion sensor has only the cloud's.
        """
        data: StationState | None = self.coordinator.data
        if data is None:
            return None
        device_sn = self._device_sn
        if device_sn is None:
            return data.firmware
        device = self.device_state
        if device is None:
            return None
        if device.firmware is not None:
            return device.firmware
        # Between a newer cloud list (``Station.update_sub_devices``) and its reload,
        # the serial can be gone from the paired list while the snapshot still has its
        # block: unknown, rather than a lost update and a traceback per fan-out.
        try:
            return self.coordinator.station.sub_device(device_sn).main_sw_version
        except UnsupportedError:
            return None

    @property
    @override
    def available(self) -> bool:
        """Available once a poll produced state, and while the device is still reporting.

        In the library guide's order: the coordinator's success, the device's block in
        the dump (a full read drops a device that did not answer), then
        ``SubDeviceState.online``: False is unavailable, None (not reported) is not
        offline. The station's own entities only wait for state; its reachability is
        the session's (``ConnectionChanged``).
        """
        if not super().available or self.coordinator.data is None:
            return False
        if self._device_sn is None:
            return True
        device = self.device_state
        return device is not None and device.online is not False


class EufyPushAvailability(CoordinatorEntity[StationCoordinator]):
    """Availability for an entity fed by pushes: the station's session, not the poll.

    Listed before the entity base, so this ``available`` wins. A poll timeout or a
    dump that leaves out a camera must not write a real detection as ``unavailable``;
    with the session down no push can arrive, so the entity is unavailable once the
    router's loss grace has passed (``EventRouter.loss_pending``).

    A station that connects on demand holds no session between commands, so for it
    this defers to the next class in the MRO (``EufyDeviceEntity`` or
    ``EufyStationEntity``).
    """

    @property
    @override
    def available(self) -> bool:
        """Available while the station's P2P session is up; on demand, by its state."""
        station = self.coordinator.station
        if station.connects_on_demand:
            return super().available
        if station.connected:
            return True
        router = getattr(self.coordinator.config_entry.runtime_data, "router", None)
        return router is not None and router.loss_pending(station.serial)


class EufySettingEntity(EufyDeviceEntity):
    """One setting of a device: read from the station's state, written through the library.

    The library decides which settings a device has (``Station.settings_for``), how a
    value decodes and whether it is writable.
    """

    # Constant for the entity's life: kept out of the recorder.
    _unrecorded_attributes = frozenset({ATTR_APPLIES_WHEN, ATTR_APPLIES_WHEN_LABEL, ATTR_CONTROLS})

    def __init__(self, coordinator: StationCoordinator, spec: SettingEntitySpec) -> None:
        super().__init__(coordinator, spec.device_sn, spec.key)
        self._spec = spec
        if spec.translated:
            self._attr_translation_key = spec.key
        else:
            # Vendor identifiers carry no translation of ours; the library names them.
            self._attr_name = spec.setting.name
        self._attr_assumed_state = not spec.setting.readable
        self._attr_entity_category = (
            EntityCategory.CONFIG if spec.is_control else EntityCategory.DIAGNOSTIC
        )
        self._attr_entity_registry_enabled_default = spec.enabled_by_default
        # The last value written, shown for a setting the dump never reports.
        self._written: SettingValue | None = None
        # Set by a write that timed out: unknown until the next state arrives.
        self._unsettled = False
        # The same device's settings that apply only at one of this setting's values.
        self._dependents: tuple[tuple[str, SettingValue], ...] = tuple(
            (s.key, s.applies_when[1])
            for s in coordinator.station.settings_for(spec.device_sn)
            if s.applies_when is not None
            and s.applies_when[0] == spec.setting.key
            and setting_platform(s) is not None
        )

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """``applies_when`` with its label on a dependent; ``controls`` on the setting it depends on."""
        attributes: dict[str, Any] = {}
        if (condition := self._spec.setting.applies_when) is not None:
            key, wanted = condition
            attributes[ATTR_APPLIES_WHEN] = f"{key}={wanted}"
            station = self.coordinator.station
            gate = next(
                (s for s in station.settings_for(self._spec.device_sn) if s.key == key), None
            )
            if gate is not None and (label := gate.label(wanted)) is not None:
                attributes[ATTR_APPLIES_WHEN_LABEL] = label
        if controls := self._controls():
            attributes[ATTR_CONTROLS] = controls
        return attributes or None

    def _controls(self) -> dict[str, str]:
        """Dependent setting key -> the option label it applies at.

        Published on the controlling entity, which stays available while its dependents
        do not (HA drops an unavailable entity's attributes). Keys, not entity ids: a
        dependent's unique id is ``<serial>_<key>``.
        """
        return {key: self.option_label(wanted) for key, wanted in self._dependents}

    @property
    @override
    def available(self) -> bool:
        """Unavailable, besides the device's own reasons, while the setting does not apply."""
        return super().available and self.setting_applies

    @property
    def setting_applies(self) -> bool:
        """False only while the setting's ``applies_when`` is known not to hold.

        ``applies_when`` is ``(key, value)``: the setting takes effect only while the
        same device's setting ``key`` holds ``value`` (clip length, retrigger interval
        and end-clip-early only in the custom power mode). An unreported value, or a
        device without that setting, never takes the entity away.
        """
        condition = self._spec.setting.applies_when
        data: StationState | None = self.coordinator.data
        if condition is None or data is None:
            return True
        key, wanted = condition
        try:
            current = data.setting(key, device_sn=self._spec.device_sn)
        except UnsupportedError:
            return True
        return current is None or current == wanted

    @property
    def setting_value(self) -> SettingValue | None:
        """The setting's decoded value, else None for unknown.

        A setting the dump never reports shows the last value written from here.
        """
        if self._unsettled:
            return None
        setting = self._spec.setting
        if not setting.readable:
            return self._written
        data: StationState | None = self.coordinator.data
        if data is None:
            return None
        try:
            return data.setting(setting.key, device_sn=self._spec.device_sn)
        except UnsupportedError:
            # A device the latest cloud list does not pair here; the reload follows.
            return None

    def option_label(self, value: SettingValue) -> str:
        """How an enum value is shown: the library's label, else the value itself."""
        return self._spec.setting.label(value) or str(value)

    def option_value(self, label: str) -> SettingValue:
        """The enum value a shown label stands for; the label itself when none matches."""
        for value in self._spec.setting.values:
            if self.option_label(value) == label:
                return value
        return label

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """A new state settles a timed-out write."""
        self._unsettled = False
        super()._handle_coordinator_update()

    async def async_write_setting(self, value: SettingValue) -> None:
        """Write the setting; the entity shows the written value until the next dump.

        A rejected or undeliverable write raises a translated error naming this entity,
        and the entity keeps its last value; so does a value the library refuses
        (``ValueError``), as a validation error. A write that timed out may still apply
        later: the entity shows unknown until the next state and the error says so.
        """
        self._require_addressable()
        try:
            await self.coordinator.station.async_set_setting(
                self._spec.setting.key, value, device_sn=self._spec.device_sn
            )
        except ValueError as err:
            raise errors.setting_value_invalid(self._write_target()) from err
        except CommandError as err:
            # Before UnsupportedError: a -108 receipt is both.
            raise errors.setting_write_failed(
                err,
                self._write_target(),
                on_demand=self.coordinator.station.connects_on_demand,
            ) from err
        except UnsupportedError as err:
            if self._spec.writes_mode_table:
                raise errors.setting_mode_table_refused(self._write_target()) from err
            raise
        except DeviceTimeoutError as err:
            self._unsettled = True
            self.async_write_ha_state()
            raise errors.setting_write_unconfirmed(self._write_target()) from err
        except EufySecurityError as err:
            raise errors.setting_write_failed(
                err,
                self._write_target(),
                on_demand=self.coordinator.station.connects_on_demand,
            ) from err
        self._async_show_written(value)
        await self._async_restart_stream_if_picture_changed()

    async def async_write_mode_action(self, on: bool) -> None:
        """Turn this entity's action bit on or off for its guard mode; other bits are kept.

        The library reads the current mask, changes the one bit and confirms the table
        by read-back. A timed-out write shows unknown until the next state, as in
        ``async_write_setting``.
        """
        action, flag, device_sn = self._spec.action, self._spec.flag, self._spec.device_sn
        if action is None or flag is None or device_sn is None:
            raise UnsupportedError(f"{self._spec.key} is not a per-mode action bit")
        self._require_addressable()
        try:
            await self.coordinator.station.async_set_mode_action(
                action.mode, flag, on, device_sn=device_sn
            )
        except CommandError as err:
            raise errors.setting_write_failed(
                err,
                self._write_target(),
                on_demand=self.coordinator.station.connects_on_demand,
            ) from err
        except UnsupportedError as err:
            raise errors.setting_mode_table_refused(self._write_target()) from err
        except DeviceTimeoutError as err:
            self._unsettled = True
            self.async_write_ha_state()
            raise errors.setting_write_unconfirmed(self._write_target()) from err
        except EufySecurityError as err:
            raise errors.setting_write_failed(
                err,
                self._write_target(),
                on_demand=self.coordinator.station.connects_on_demand,
            ) from err
        self._async_show_written(None)

    async def async_write_flag(self, on: bool) -> None:
        """Turn this entity's member of a ``FLAGS`` setting on or off; other bits are kept.

        The library reads the current mask fresh, moves the member's bits and writes the
        mask; it refuses (nothing sent) when the station does not report the mask.
        """
        member = self._spec.member
        if member is None:
            raise UnsupportedError(f"{self._spec.key} is not a flags member")
        self._require_addressable()
        try:
            await self.coordinator.station.async_set_flag(
                self._spec.setting.key, member, on, device_sn=self._spec.device_sn
            )
        except DeviceTimeoutError as err:
            self._unsettled = True
            self.async_write_ha_state()
            raise errors.setting_write_unconfirmed(self._write_target()) from err
        except EufySecurityError as err:
            if isinstance(err, UnsupportedError) and not isinstance(err, CommandError):
                raise
            raise errors.setting_write_failed(
                err,
                self._write_target(),
                on_demand=self.coordinator.station.connects_on_demand,
            ) from err
        self._async_show_written(None)

    async def _async_restart_stream_if_picture_changed(self) -> None:
        """End this camera's live view when the write changed the picture size it sends.

        An MPEG-TS stream cannot carry a picture size that changes underneath it, so the
        view is ended and the next open starts at the new size. A no-op when nobody is
        watching. The camera is the setting's device, or the station itself for a
        standalone camera, whose broadcast is keyed by the station serial.
        """
        if self._spec.setting.key not in PICTURE_CHANGING_SETTINGS:
            return
        camera_sn = self._spec.device_sn or self.coordinator.station.serial
        if (streaming := runtime.streaming(self.coordinator.config_entry)) is not None:
            await streaming.async_restart_camera(camera_sn)

    def _require_addressable(self) -> None:
        """Refuse a write for a paired device the station cannot address; nothing is sent.

        ``Station.channel_for`` raises ``UnsupportedError`` when the latest cloud list
        does not pair the device or its record names no channel: a runtime state, said
        as a sentence. Any later ``UnsupportedError`` is a mode-table refusal or a
        programming error.
        """
        device_sn = self._spec.device_sn
        if device_sn is None:
            return
        try:
            self.coordinator.station.channel_for(device_sn)
        except UnsupportedError as err:
            raise errors.setting_device_unavailable(self._write_target()) from err

    def _write_target(self) -> str:
        """How a failed write names this setting: the entity's name, else its key; never a serial."""
        name = self.name
        return name if isinstance(name, str) else self._spec.setting.key

    @callback
    def _async_show_written(self, value: SettingValue | None) -> None:
        """Show the state the library holds after a write, without moving the poll timer.

        The library merges a written value into its state until the next dump; a
        setting the dump never reports keeps ``value`` here instead.
        """
        self._unsettled = False
        if not self._spec.setting.readable and value is not None:
            self._written = value
        self.coordinator.async_apply_state(self.coordinator.station.state)
        self.async_write_ha_state()
