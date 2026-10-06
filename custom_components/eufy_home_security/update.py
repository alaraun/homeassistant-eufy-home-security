"""Each device's firmware, as an update entity that never claims to be up to date.

The station reports only the firmware it runs, never what eufy has published, so
``latest_version`` stays None (state unknown) and no install is offered. The version
is the live dump's, with the cloud's record as the fallback a T8910 sensor needs.

Raw device reports with no entity of their own ride along as attributes, each only
while reported: the station's ``subsystem_firmware`` (version by param id) and a
camera's ``siren_actions`` (raw action code by lower-case guard mode).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from eufy_home_security import StationState

from .const import ATTR_SIREN_ACTIONS, ATTR_SUBSYSTEM_FIRMWARE, FIRMWARE_KEY
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# Reads come from the coordinator, so HA need not serialise entity updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one firmware entity for each station and each of its paired devices."""
    del hass  # the coordinators carry everything this platform needs
    entities: list[UpdateEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        entities.append(EufyFirmwareUpdate(coordinator, None))
        entities.extend(
            EufyFirmwareUpdate(coordinator, sub.device_sn)
            for sub in coordinator.station.sub_devices
            # No serial, no identity, so no device and no entities.
            if sub.device_sn
        )
    async_add_entities(entities)


class EufyFirmwareUpdate(EufyDeviceEntity, UpdateEntity):
    """The firmware a station or one of its paired devices is running.

    Named "Firmware update" by its own translation key: the device-class name,
    "Firmware", is the diagnostic firmware sensor's name on the same device. The
    unique id is ``entity_unique_id(serial, FIRMWARE_KEY)``, shared with that sensor
    across the two domains.
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_translation_key = "firmware_update"
    # No install, which also makes Home Assistant give the entity the diagnostic category.
    _attr_supported_features = UpdateEntityFeature(0)
    _unrecorded_attributes = frozenset({ATTR_SUBSYSTEM_FIRMWARE, ATTR_SIREN_ACTIONS})

    def __init__(self, coordinator: StationCoordinator, device_sn: str | None) -> None:
        super().__init__(coordinator, device_sn, FIRMWARE_KEY)

    @property
    @override
    def installed_version(self) -> str | None:
        """The version this device reports, with the cloud's record as fallback.

        None where neither has one, which shows as unknown.
        """
        return self.firmware_version

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The station's subsystem versions, or a device's siren actions, when reported."""
        attributes: dict[str, Any] = {}
        if self._device_sn is None:
            data: StationState | None = self.coordinator.data
            if data is not None and data.subsystem_firmware:
                attributes[ATTR_SUBSYSTEM_FIRMWARE] = {
                    str(param): version for param, version in data.subsystem_firmware.items()
                }
        elif (device := self.device_state) is not None and device.siren_actions:
            attributes[ATTR_SIREN_ACTIONS] = {
                mode.name.lower(): action for mode, action in device.siren_actions.items()
            }
        return attributes or None

    @property
    @override
    def latest_version(self) -> str | None:
        """Always None: nothing here knows which firmware eufy has published.

        Home Assistant shows an update entity with no latest version as unknown;
        returning the installed version would claim "up to date".
        """
        return None
