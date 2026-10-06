"""What a station and its devices report about themselves, as diagnostic sensors.

Every value comes from the typed state the eufy library builds from a parameter
dump, never from a parameter this integration read itself: the library owns what a
number means, and this module owns only which Home Assistant entity shows it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    Platform,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from eufy_home_security import StationState, StorageMedium, SubDeviceState
from eufy_home_security.devices import SettingKind, SettingUnit, model_for_serial

from .const import (
    BATTERY_KEY,
    BATTERY_TEMPERATURE_KEY,
    DETECTED_EVENTS_KEY,
    DISK_FREE_KEY,
    DISK_SIZE_KEY,
    DISK_TEMPERATURE_KEY,
    DISK_USED_KEY,
    DISK_USED_PERCENT_KEY,
    EMMC_FREE_KEY,
    EMMC_SIZE_KEY,
    EMMC_USED_KEY,
    EMMC_USED_SPACE_KEY,
    EMMC_WEAR_KEY,
    FIRMWARE_KEY,
    MODEL_KEY,
    RECORDED_EVENTS_KEY,
    SIGNAL_STRENGTH_KEY,
    SOLAR_INTENSITY_KEY,
    WORKING_DAYS_KEY,
)
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufySettingEntity, async_add_when_reported, device_block
from .settings import SettingEntitySpec, setting_specs
from .storage import EufyStorageEntity, StorageCoordinator, StoragePart, async_add_part_entities

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# Reads come from the coordinator, so HA need not serialise entity updates.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EufyDiagnosticDescription(SensorEntityDescription):
    """One diagnostic reading, and how to take it from the entity that shows it."""

    value_fn: Callable[[EufyDeviceEntity], StateType]


def _firmware(entity: EufyDeviceEntity) -> StateType:
    """The running firmware, with the cloud fallback a sensor's version needs."""
    return entity.firmware_version


def _emmc_used(entity: EufyDeviceEntity) -> StateType:
    """The station's internal storage used, as a percentage."""
    data: StationState | None = entity.coordinator.data
    return None if data is None else data.emmc_used_percent


def _station_model(entity: EufyDeviceEntity) -> StateType:
    """The station's model name, as the library's own catalog names it."""
    model = entity.coordinator.station.model
    return model.name if model else None


def _battery(entity: EufyDeviceEntity) -> StateType:
    """The paired device's battery percentage."""
    device = entity.device_state
    return None if device is None else device.battery


def _signal_strength(entity: EufyDeviceEntity) -> StateType:
    """The device's own radio: Wi-Fi where it reports one, else sub-1 GHz.

    The library chooses per device rather than per parameter, which is what makes
    one entity right for a Wi-Fi camera and for a sub-1 GHz sensor alike.
    """
    device = entity.device_state
    return None if device is None else device.rssi


def _device_model(entity: EufyDeviceEntity) -> StateType:
    """The paired device's model name, from its serial's catalogued model."""
    device = entity.device_state
    if device is None or device.serial is None:
        return None
    model = model_for_serial(device.serial)
    return model.name if model else None


# Every description is DIAGNOSTIC: these are readings about the hardware, not
# controls. Each value function answers None before the first read, so an
# unknown value is shown as unknown and never as a default.
STATION_DIAGNOSTICS: Final = (
    EufyDiagnosticDescription(
        key=FIRMWARE_KEY,
        translation_key=FIRMWARE_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_firmware,
    ),
    EufyDiagnosticDescription(
        key=EMMC_USED_KEY,
        translation_key=EMMC_USED_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_emmc_used,
    ),
    EufyDiagnosticDescription(
        key=MODEL_KEY,
        translation_key=MODEL_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_station_model,
    ),
)

SUB_DEVICE_DIAGNOSTICS: Final = (
    # Battery and signal take their names from their device classes, so they read
    # as "Battery" and "Signal strength" without a translation of their own.
    EufyDiagnosticDescription(
        key=BATTERY_KEY,
        device_class=SensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_battery,
    ),
    EufyDiagnosticDescription(
        key=SIGNAL_STRENGTH_KEY,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        # Home Assistant's own practice for a signal reading: registered, and off
        # until someone asks for it.
        entity_registry_enabled_default=False,
        value_fn=_signal_strength,
    ),
    EufyDiagnosticDescription(
        key=FIRMWARE_KEY,
        translation_key=FIRMWARE_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_firmware,
    ),
    EufyDiagnosticDescription(
        key=MODEL_KEY,
        translation_key=MODEL_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_device_model,
    ),
)


@dataclass(frozen=True, kw_only=True)
class EufyDeviceFieldDescription(SensorEntityDescription):
    """One field of a paired device's state; the entity exists once a state reports it."""

    read: Callable[[SubDeviceState], StateType]


# Power-manager figures of a camera, all DIAGNOSTIC. The code meanings are declared
# by the library (vendor handlers), not compared with the app.
DEVICE_FIELD_SENSORS: Final = (
    EufyDeviceFieldDescription(
        key=SOLAR_INTENSITY_KEY,
        translation_key=SOLAR_INTENSITY_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Raw solar input; its scale is model specific, so no unit or device class.
        state_class=SensorStateClass.MEASUREMENT,
        read=lambda device: device.solar_intensity,
    ),
    EufyDeviceFieldDescription(
        key=BATTERY_TEMPERATURE_KEY,
        translation_key=BATTERY_TEMPERATURE_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        read=lambda device: device.battery_temperature,
    ),
    EufyDeviceFieldDescription(
        key=WORKING_DAYS_KEY,
        translation_key=WORKING_DAYS_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        read=lambda device: device.working_days,
    ),
    # Counted since the last USB charge, which resets them.
    EufyDeviceFieldDescription(
        key=DETECTED_EVENTS_KEY,
        translation_key=DETECTED_EVENTS_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL,
        read=lambda device: device.detected_events,
    ),
    EufyDeviceFieldDescription(
        key=RECORDED_EVENTS_KEY,
        translation_key=RECORDED_EVENTS_KEY,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL,
        read=lambda device: device.recorded_events,
    ),
)


def _device_field_reported(
    device_sn: str, description: EufyDeviceFieldDescription
) -> Callable[[StationState], bool]:
    """Whether a state's block for ``device_sn`` carries the description's field."""

    def reported(data: StationState) -> bool:
        device = device_block(data, device_sn)
        return device is not None and description.read(device) is not None

    return reported


@dataclass(frozen=True, kw_only=True)
class EufyStorageDescription(SensorEntityDescription):
    """One figure of the storage record, and the part of the record it belongs to."""

    part: StoragePart
    value_fn: Callable[[StorageMedium], StateType]


type _MediumRead = Callable[[StorageMedium], StateType]


def _gib_sensor(
    part: StoragePart,
    key: str,
    read: _MediumRead,
    *,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    enabled: bool = True,
) -> EufyStorageDescription:
    """A size in GiB, the unit whose figures the eufy app shows (labelled "GB")."""
    return EufyStorageDescription(
        key=key,
        translation_key=key,
        part=part,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=state_class,
        entity_registry_enabled_default=enabled,
        value_fn=read,
    )


def _used_percent_sensor(part: StoragePart, key: str) -> EufyStorageDescription:
    """The medium's use in percent, one decimal."""
    return EufyStorageDescription(
        key=key,
        translation_key=key,
        part=part,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda medium: medium.used_percent,
    )


def _temperature_sensor(part: StoragePart, key: str) -> EufyStorageDescription:
    """The medium's temperature in °C."""
    return EufyStorageDescription(
        key=key,
        translation_key=key,
        part=part,
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda medium: medium.temperature_c,
    )


def _wear_sensor(part: StoragePart, key: str) -> EufyStorageDescription:
    """The medium's life used, in percent (end of life at 100)."""
    return EufyStorageDescription(
        key=key,
        translation_key=key,
        part=part,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda medium: medium.wear_percent,
    )


# The storage record's figures, all DIAGNOSTIC, from the library's ``StorageMedium``.
# Each part gets only the figures its record carries (HomeBase 3):
# - Disk: used, free, used percentage and temperature on; total size off (it never
#   changes). No wear: ``hdd_info`` has no ``eol_percent``.
# - eMMC: used space, free and wear on, total size off. No used percentage: the
#   station's ``emmc_used`` dump sensor shows it, which is also why used space is
#   keyed ``emmc_used_space``. No temperature: ``emmc_info`` has no ``cur_temperate``.
# - Recordings used/capacity: no app screen shows them; diagnostics download only.
STORAGE_SENSORS: Final = (
    _gib_sensor(StoragePart.DISK, DISK_USED_KEY, lambda medium: medium.used_gib),
    _gib_sensor(StoragePart.DISK, DISK_FREE_KEY, lambda medium: medium.free_gib),
    _gib_sensor(
        StoragePart.DISK,
        DISK_SIZE_KEY,
        lambda medium: medium.size_gib,
        state_class=None,
        enabled=False,
    ),
    _used_percent_sensor(StoragePart.DISK, DISK_USED_PERCENT_KEY),
    _temperature_sensor(StoragePart.DISK, DISK_TEMPERATURE_KEY),
    _gib_sensor(StoragePart.EMMC, EMMC_USED_SPACE_KEY, lambda medium: medium.used_gib),
    _gib_sensor(StoragePart.EMMC, EMMC_FREE_KEY, lambda medium: medium.free_gib),
    _gib_sensor(
        StoragePart.EMMC,
        EMMC_SIZE_KEY,
        lambda medium: medium.size_gib,
        state_class=None,
        enabled=False,
    ),
    _wear_sensor(StoragePart.EMMC, EMMC_WEAR_KEY),
)


@callback
def _async_add_storage_sensors(
    entry: EufyConfigEntry,
    coordinator: StorageCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a station's storage sensors part by part, as its records report each part."""

    @callback
    def _add_part(part: StoragePart) -> None:
        async_add_entities(
            EufyStorageSensor(coordinator, description)
            for description in STORAGE_SENSORS
            if description.part is part
        )

    async_add_part_entities(entry, coordinator, _add_part)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each device's diagnostics, and every readable setting it has no control for."""
    entities: list[SensorEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        entities.extend(
            EufyDiagnosticSensor(coordinator, None, description)
            for description in STATION_DIAGNOSTICS
        )
        for device in coordinator.station.devices:
            device_sn = device.device_sn
            if not device_sn:
                # No serial, no identity, so no device and no entities.
                continue
            # A standalone station's own device view (library guide): its firmware
            # and model already come from the station diagnostics above, under the
            # same unique ids, so only its battery and signal are added here.
            own = device_sn == coordinator.station.serial
            entities.extend(
                EufyDiagnosticSensor(coordinator, device_sn, description)
                for description in SUB_DEVICE_DIAGNOSTICS
                if not (own and description.key in (FIRMWARE_KEY, MODEL_KEY))
            )
        entities.extend(
            EufySettingSensor(coordinator, spec)
            for spec in setting_specs(coordinator.station)
            if spec.platform is Platform.SENSOR
        )
    async_add_entities(entities)
    for coordinator in entry.runtime_data.coordinators.values():
        # A readable setting of the same key already owns that unique id.
        owned = {
            (spec.device_sn, spec.key)
            for spec in setting_specs(coordinator.station)
            if spec.platform is Platform.SENSOR
        }
        async_add_when_reported(
            entry,
            coordinator,
            (
                (
                    _device_field_reported(device.device_sn, description),
                    partial(EufyDeviceFieldSensor, coordinator, device.device_sn, description),
                )
                for device in coordinator.station.devices
                if device.device_sn
                for description in DEVICE_FIELD_SENSORS
                if (device.device_sn, description.key) not in owned
            ),
            async_add_entities,
        )
    for storage in entry.runtime_data.storage.values():
        _async_add_storage_sensors(entry, storage, async_add_entities)


class EufyDiagnosticSensor(EufyDeviceEntity, SensorEntity):
    """One reading a station or a paired device makes about itself."""

    entity_description: EufyDiagnosticDescription

    def __init__(
        self,
        coordinator: StationCoordinator,
        device_sn: str | None,
        description: EufyDiagnosticDescription,
    ) -> None:
        super().__init__(coordinator, device_sn, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """What the description reads off the latest state; None shows as unknown."""
        return self.entity_description.value_fn(self)


class EufyDeviceFieldSensor(EufyDeviceEntity, SensorEntity):
    """One field of a paired device's state."""

    entity_description: EufyDeviceFieldDescription

    def __init__(
        self,
        coordinator: StationCoordinator,
        device_sn: str,
        description: EufyDeviceFieldDescription,
    ) -> None:
        super().__init__(coordinator, device_sn, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """The field off the device's latest block; None shows as unknown."""
        device = self.device_state
        return None if device is None else self.entity_description.read(device)


class EufySettingSensor(EufySettingEntity, SensorEntity):
    """A readable setting with no control: read-only, or a string or code value."""

    def __init__(self, coordinator: StationCoordinator, spec: SettingEntitySpec) -> None:
        super().__init__(coordinator, spec)
        setting = spec.setting
        if setting.kind is SettingKind.RANGE:
            if setting.unit is SettingUnit.SECONDS:
                self._attr_device_class = SensorDeviceClass.DURATION
                self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
                self._attr_suggested_display_precision = 0
            elif setting.unit is not None and setting.unit.value:
                self._attr_native_unit_of_measurement = setting.unit.value
        elif setting.kind is SettingKind.ENUM:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [self.option_label(value) for value in setting.values]

    @property
    @override
    def native_value(self) -> StateType:
        """The decoded value; an enum by its label, None for a value outside its values."""
        value = self.setting_value
        if value is None:
            return None
        setting = self._spec.setting
        if setting.kind is SettingKind.ENUM:
            return self.option_label(value) if value in setting.values else None
        if isinstance(value, bool):
            return str(value).lower()
        return value


class EufyStorageSensor(EufyStorageEntity, SensorEntity):
    """One figure of a station's storage record."""

    entity_description: EufyStorageDescription

    def __init__(
        self, coordinator: StorageCoordinator, description: EufyStorageDescription
    ) -> None:
        super().__init__(coordinator, description.part, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """The figure off the latest record; None shows as unknown."""
        medium = self.entity_description.part.medium(self.storage)
        return None if medium is None else self.entity_description.value_fn(medium)
