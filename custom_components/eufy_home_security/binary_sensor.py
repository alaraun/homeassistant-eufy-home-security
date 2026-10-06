"""Read-only bool settings, and what each camera is detecting right now.

**Bool settings.** A readable bool setting with no control shows as a binary sensor.

**Detection sensors.** Each camera (and doorbell) has a motion, person, pet and
vehicle sensor. A detection turns on only the sensor of its own class. The
station announces a detection but never its end, so a sensor turns itself off on a
timer, a configurable hold after the detection's own time rather than its arrival.
A detection that arrives after its hold ran out leaves the sensor off; the
detection event entity still fires for it. The hold is read once at platform setup,
and the options flow's reload rebuilds the sensors with a new one.

**Storage sensors.** From the station's storage record (``storage.py``): a problem
sensor per medium (disk and eMMC) that is on while the library reports that medium not
healthy, and, for the disk only, a formatting sensor that is on while a format runs.
Each appears once a record reports its medium.

**Power and station storage state.** From the parameter dump's typed state: a
camera's charging and solar charging, a motion sensor's own low-battery flag, and the
station's storage status. Each appears once a state reports its field.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from eufy_home_security import SecurityEvent, StationState, StorageMedium, SubDeviceState

from . import detections
from .const import (
    ATTR_POWER_SOURCE,
    ATTR_SOLAR_CHARGING,
    ATTR_STORAGE_STATUS,
    BATTERY_LOW_KEY,
    CHARGING_KEY,
    CONF_DETECTION_HOLD,
    DEFAULT_DETECTION_HOLD_SECONDS,
    DISK_FORMATTING_KEY,
    DISK_PROBLEM_KEY,
    EMMC_PROBLEM_KEY,
    MOTION_DETECTED_KEY,
    SOLAR_CHARGING_KEY,
    STORAGE_PROBLEM_KEY,
)
from .coordinator import StationCoordinator
from .entity import (
    EufyDeviceEntity,
    EufyPushAvailability,
    EufySettingEntity,
    async_add_when_reported,
    device_block,
)
from .settings import setting_specs
from .storage import EufyStorageEntity, StorageCoordinator, StoragePart, async_add_part_entities

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# Reads come from the coordinator, so HA need not serialise entity updates.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EufyStorageBinaryDescription(BinarySensorEntityDescription):
    """One yes-or-no fact of a storage medium, and its part; None reads as unknown."""

    part: StoragePart
    value_fn: Callable[[StorageMedium], bool | None]


def _problem(medium: StorageMedium) -> bool | None:
    """On while the library reports the medium not healthy; unknown without a health code."""
    healthy = medium.healthy
    return None if healthy is None else not healthy


def _formatting(medium: StorageMedium) -> bool | None:
    """On while the medium is being formatted."""
    return medium.formatting


def _problem_sensor(part: StoragePart, key: str) -> EufyStorageBinaryDescription:
    """On by default: a failing medium is what this row is for."""
    return EufyStorageBinaryDescription(
        key=key,
        translation_key=key,
        part=part,
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_problem,
    )


def _formatting_sensor(part: StoragePart, key: str) -> EufyStorageBinaryDescription:
    """Off by default: a format runs about a minute, only when started from the app."""
    return EufyStorageBinaryDescription(
        key=key,
        translation_key=key,
        part=part,
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_formatting,
    )


# All DIAGNOSTIC, built per part. The eMMC has no formatting sensor: its record
# (HomeBase 3) carries no ``parted_status``.
STORAGE_BINARY_SENSORS: Final = (
    _problem_sensor(StoragePart.DISK, DISK_PROBLEM_KEY),
    _formatting_sensor(StoragePart.DISK, DISK_FORMATTING_KEY),
    _problem_sensor(StoragePart.EMMC, EMMC_PROBLEM_KEY),
)


@dataclass(frozen=True, kw_only=True)
class EufyDeviceFieldBinaryDescription(BinarySensorEntityDescription):
    """One yes-or-no field of a paired device's state, and its attributes."""

    read: Callable[[SubDeviceState], bool | None]
    attributes_fn: Callable[[SubDeviceState], dict[str, Any]] | None = None


def _charging_attributes(device: SubDeviceState) -> dict[str, Any]:
    """The solar flag and the raw charging-source code, unmapped."""
    return {
        ATTR_SOLAR_CHARGING: device.solar_charging,
        ATTR_POWER_SOURCE: device.power_source,
    }


# The charging-source meanings are the library's (declared from vendor handlers); a
# code it does not know is shown raw in ``power_source``.
DEVICE_FIELD_BINARY_SENSORS: Final = (
    EufyDeviceFieldBinaryDescription(
        key=CHARGING_KEY,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        read=lambda device: device.charging,
        attributes_fn=_charging_attributes,
    ),
    EufyDeviceFieldBinaryDescription(
        key=SOLAR_CHARGING_KEY,
        translation_key=SOLAR_CHARGING_KEY,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        read=lambda device: device.solar_charging,
    ),
    # The motion sensor's own flag, beside its battery percentage.
    EufyDeviceFieldBinaryDescription(
        key=BATTERY_LOW_KEY,
        translation_key=BATTERY_LOW_KEY,
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        read=lambda device: device.low_battery,
    ),
)


def _device_field_reported(
    device_sn: str, description: EufyDeviceFieldBinaryDescription
) -> Callable[[StationState], bool]:
    """Whether a state's block for ``device_sn`` carries the description's field."""

    def reported(data: StationState) -> bool:
        device = device_block(data, device_sn)
        return device is not None and description.read(device) is not None

    return reported


def _storage_reported(data: StationState) -> bool:
    """Whether the station's state carries its storage status."""
    return data.storage_status is not None


@callback
def _async_add_state_binary_sensors(
    entry: EufyConfigEntry,
    coordinator: StationCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the station's storage sensor and each device's power fields, once reported."""
    candidates: list[tuple[Callable[[StationState], bool], Callable[[], BinarySensorEntity]]] = [
        (_storage_reported, partial(EufyStationStorageBinarySensor, coordinator))
    ]
    candidates.extend(
        (
            _device_field_reported(device.device_sn, description),
            partial(EufyDeviceFieldBinarySensor, coordinator, device.device_sn, description),
        )
        for device in coordinator.station.devices
        if device.device_sn
        for description in DEVICE_FIELD_BINARY_SENSORS
    )
    async_add_when_reported(entry, coordinator, candidates, async_add_entities)


@callback
def _async_add_storage_binary_sensors(
    entry: EufyConfigEntry,
    coordinator: StorageCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a station's storage sensors part by part, as its records report each part."""

    @callback
    def _add_part(part: StoragePart) -> None:
        async_add_entities(
            EufyStorageBinarySensor(coordinator, description)
            for description in STORAGE_BINARY_SENSORS
            if description.part is part
        )

    async_add_part_entities(entry, coordinator, _add_part)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every read-only bool setting, then each camera's detection sensors."""
    del hass  # the coordinators carry everything this platform needs
    hold = int(entry.options.get(CONF_DETECTION_HOLD, DEFAULT_DETECTION_HOLD_SECONDS))
    entities: list[BinarySensorEntity] = [
        EufySettingBinarySensor(coordinator, spec)
        for coordinator in entry.runtime_data.coordinators.values()
        for spec in setting_specs(coordinator.station)
        if spec.platform is Platform.BINARY_SENSOR
    ]
    for coordinator in entry.runtime_data.coordinators.values():
        entities.extend(
            EufyDetectionBinarySensor(coordinator, device_sn, key, hold)
            for device_sn, kind in detections.paired_device_kinds(coordinator.station).items()
            if detections.has_detection_entities(kind)
            for key in detections.DETECTION_CLASSES
        )
    async_add_entities(entities)
    for coordinator in entry.runtime_data.coordinators.values():
        _async_add_state_binary_sensors(entry, coordinator, async_add_entities)
    for storage in entry.runtime_data.storage.values():
        _async_add_storage_binary_sensors(entry, storage, async_add_entities)


class EufySettingBinarySensor(EufySettingEntity, BinarySensorEntity):
    """A read-only bool setting."""

    @property
    @override
    def is_on(self) -> bool | None:
        """The decoded value; None until the station reports it."""
        value = self.setting_value
        return value if isinstance(value, bool) else None


class EufyDetectionBinarySensor(EufyPushAvailability, EufyDeviceEntity, BinarySensorEntity):
    """Whether one camera detected one class of thing within the hold.

    Off until a detection of its class; no state is restored across a restart, so a
    camera that has had no detection shows off, never unknown. The off edge is a
    timer computed once at arrival, and it only ever moves later: an older detection
    arriving after a newer one never shortens the running hold.
    """

    _attr_is_on = False

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, key: str, hold: int
    ) -> None:
        super().__init__(coordinator, device_sn, key)
        self._attr_translation_key = key
        # Only the motion sensor is motion. A person, pet or vehicle sensor has no
        # device class, as UniFi Protect's smart detections: its translation key names
        # it and its Detected/Clear states, icons.json its icon, and area summaries
        # and voice assistants do not count a vehicle as motion.
        self._attr_device_class = (
            BinarySensorDeviceClass.MOTION if key == MOTION_DETECTED_KEY else None
        )
        self._serial = device_sn
        self._classes = detections.DETECTION_CLASSES[key]
        self._hold = hold
        self._off_at: datetime | None = None
        self._cancel_off: CALLBACK_TYPE | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Listen on this device's signal, and cancel any pending off edge on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.device_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_security_event,
            )
        )
        self.async_on_remove(self._async_cancel_off)

    @callback
    def _async_on_security_event(self, event: SecurityEvent) -> None:
        """Turn on for a detection of this sensor's class, until its hold runs out."""
        if event.detection not in self._classes:
            return
        now = dt_util.utcnow()
        remaining = detections.hold_remaining_seconds(
            event.event_time_ms, self._hold, now.timestamp() * 1000
        )
        if remaining <= 0:
            # Late: the hold ran out before the detection arrived. The detection
            # event entity has fired for it; this sensor is left as it is.
            return
        off_at = now + timedelta(seconds=remaining)
        if self._off_at is None or off_at > self._off_at:
            self._async_cancel_off()
            self._off_at = off_at
            self._cancel_off = async_call_later(self.hass, remaining, self._async_hold_ended)
        self._attr_is_on = True
        self.async_write_ha_state()

    @callback
    def _async_hold_ended(self, _now: datetime) -> None:
        """Turn off: the latest detection's hold has run out."""
        self._cancel_off = None
        self._off_at = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _async_cancel_off(self) -> None:
        """Cancel a pending off edge, if there is one."""
        if self._cancel_off is not None:
            self._cancel_off()
            self._cancel_off = None


class EufyStorageBinarySensor(EufyStorageEntity, BinarySensorEntity):
    """A yes-or-no fact about one of a station's storage media, from its record."""

    entity_description: EufyStorageBinaryDescription

    def __init__(
        self, coordinator: StorageCoordinator, description: EufyStorageBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.part, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        """The fact off the latest record; None shows as unknown."""
        medium = self.entity_description.part.medium(self.storage)
        return None if medium is None else self.entity_description.value_fn(medium)


class EufyDeviceFieldBinarySensor(EufyDeviceEntity, BinarySensorEntity):
    """One yes-or-no field of a paired device's state."""

    entity_description: EufyDeviceFieldBinaryDescription

    def __init__(
        self,
        coordinator: StationCoordinator,
        device_sn: str,
        description: EufyDeviceFieldBinaryDescription,
    ) -> None:
        super().__init__(coordinator, device_sn, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool | None:
        """The field off the device's latest block; None shows as unknown."""
        device = self.device_state
        return None if device is None else self.entity_description.read(device)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The description's attributes off the latest block, if it has any."""
        attributes_fn = self.entity_description.attributes_fn
        device = self.device_state
        if attributes_fn is None or device is None:
            return None
        return attributes_fn(device)


class EufyStationStorageBinarySensor(EufyDeviceEntity, BinarySensorEntity):
    """On while the station reports a storage status the app does not show as normal."""

    _attr_translation_key = STORAGE_PROBLEM_KEY
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: StationCoordinator) -> None:
        super().__init__(coordinator, None, STORAGE_PROBLEM_KEY)

    @property
    def _state(self) -> StationState | None:
        data: StationState | None = self.coordinator.data
        return data

    @property
    @override
    def is_on(self) -> bool | None:
        """``storage_ok`` inverted; None shows as unknown."""
        data = self._state
        ok = None if data is None else data.storage_ok
        return None if ok is None else not ok

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The raw storage status code."""
        data = self._state
        return None if data is None else {ATTR_STORAGE_STATUS: data.storage_status}
