"""Smart locks: one lock entity per device the library grades ``Capability.LOCK``.

The state is the library's lock reading (``Station.lock_state``), followed on the lock's
``LockStateChanged``; nothing is polled for it. The library sends no lock command yet,
so lock and unlock raise ``errors.lock_not_supported`` and send nothing. The entity's
unique id stays the same when they are added.

States map as the library's guide suggests: passage mode shows unlocked, privacy mode
locked, a bolt stopped between its positions or a reported fault jammed; initial,
uncalibrated, calibrating and unknown show unknown, as does a lock that has reported
nothing yet. The library's state name and the reported raw value are attributes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, override

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from eufy_home_security import LockReading, LockState, LockStateChanged, UnsupportedError

from . import detections, errors
from .const import ATTR_LOCK_STATE, ATTR_RAW_VALUE, LOCK_KEY
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# Nothing is sent from this platform.
PARALLEL_UPDATES = 0

# The library's lock states that show locked or unlocked; any other shows unknown unless
# it is one of the moving or jammed states below.
_LOCKED: Final = {
    LockState.LOCKED: True,
    LockState.PRIVACY_MODE: True,
    LockState.UNLOCKED: False,
    LockState.PASSAGE_MODE: False,
}
_JAMMED: Final = frozenset({LockState.INTERMEDIATE, LockState.JAMMED})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a lock per device of each station that the library grades as a lock."""
    del hass  # the coordinators carry everything this platform needs
    async_add_entities(
        EufyLock(coordinator, serial)
        for coordinator in entry.runtime_data.coordinators.values()
        for serial in detections.paired_device_kinds(coordinator.station)
        if detections.has_lock(serial)
    )


class EufyLock(EufyPushAvailability, EufyDeviceEntity, LockEntity):
    """A smart lock's state, read only: lock and unlock are refused."""

    _attr_translation_key = LOCK_KEY

    def __init__(self, coordinator: StationCoordinator, serial: str) -> None:
        station = coordinator.station
        super().__init__(coordinator, None if serial == station.serial else serial, LOCK_KEY)
        self._serial = serial

    @property
    def _reading(self) -> LockReading | None:
        """The library's last lock reading; None before one, or for a device the latest
        device list no longer pairs here (the reload follows)."""
        try:
            return self.coordinator.station.lock_state(self._serial)
        except UnsupportedError:
            return None

    @property
    def _state(self) -> LockState | None:
        reading = self._reading
        return None if reading is None else reading.state

    @property
    @override
    def is_locked(self) -> bool | None:
        """Locked or privacy mode: True; unlocked or passage mode: False; else None."""
        state = self._state
        return None if state is None else _LOCKED.get(state)

    @property
    @override
    def is_jammed(self) -> bool:
        """The bolt stopped between its positions, or the lock reported a fault."""
        return self._state in _JAMMED

    @property
    @override
    def is_locking(self) -> bool:
        return self._state is LockState.LOCKING

    @property
    @override
    def is_unlocking(self) -> bool:
        return self._state is LockState.UNLOCKING

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The library's state name and the reported raw value, once there is a reading."""
        reading = self._reading
        if reading is None:
            return None
        attributes: dict[str, Any] = {ATTR_LOCK_STATE: str(reading.state)}
        if reading.raw is not None:
            attributes[ATTR_RAW_VALUE] = reading.raw
        return attributes

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the lock's state changes until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.lock_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_lock_state,
            )
        )

    @callback
    def _async_on_lock_state(self, event: LockStateChanged) -> None:
        """Show the change; ``Station.lock_state`` already holds it."""
        del event
        self.async_write_ha_state()

    @override
    async def async_lock(self, **kwargs: Any) -> None:
        """Refused: the library sends no lock command yet."""
        del kwargs  # the refusal does not depend on the call's data
        raise errors.lock_not_supported()

    @override
    async def async_unlock(self, **kwargs: Any) -> None:
        """Refused: the library sends no unlock command yet."""
        del kwargs  # the refusal does not depend on the call's data
        raise errors.lock_not_supported()
