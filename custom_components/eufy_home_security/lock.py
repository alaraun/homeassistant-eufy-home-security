"""Smart locks: one lock entity per device the library grades ``Capability.LOCK``.

The state is the library's lock reading (``Station.lock_state``), followed on the lock's
``LockStateChanged``; nothing is polled for it.

Lock and unlock go through the library (``EufySecurity.async_lock`` / ``async_unlock``)
where its ``lock_control`` says the lock is writable; elsewhere they raise
``errors.lock_not_writable`` with the library's reason and send nothing. A command runs as
a background task of the entry and the service call waits for it: it ends once the lock
reports the asked state, or with the library's error, within the library's default wait
(about 50 s). Meanwhile the lock shows locking or unlocking, and a second lock or unlock
of the same lock is refused before anything is sent. The shown state is always the lock's
own report, never the call's return. The entry's unload cancels a waiting command, as it
does every background task of the entry.

States map as the library's guide suggests: passage mode shows unlocked, privacy mode
locked, a bolt stopped between its positions or a reported fault jammed; initial,
uncalibrated, calibrating and unknown show unknown, as does a lock that has reported
nothing yet. The library's state name and the reported raw value are attributes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, override

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from eufy_home_security import (
    EufySecurityError,
    LockReading,
    LockState,
    LockStateChanged,
    UnsupportedError,
)

from . import detections, errors
from .const import ATTR_LOCK_STATE, ATTR_RAW_VALUE, LOCK_KEY
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# Each lock guards its own command (one at a time per lock).
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

_LOCK: Final = "lock"
_UNLOCK: Final = "unlock"

type _Send = Callable[[str], Coroutine[Any, Any, LockState]]


@dataclass(frozen=True, slots=True)
class _Command:
    """A lock or unlock of one lock: its task (the library's error as its result, None
    once the lock reported the asked state) and its action."""

    task: asyncio.Task[EufySecurityError | None]
    action: str


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
    """A smart lock's state, with lock and unlock where the library can send them."""

    _attr_translation_key = LOCK_KEY

    def __init__(self, coordinator: StationCoordinator, serial: str) -> None:
        station = coordinator.station
        super().__init__(coordinator, None if serial == station.serial else serial, LOCK_KEY)
        self._serial = serial
        self._command: _Command | None = None

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

    def _waiting(self, action: str) -> bool:
        """Whether a command ``action`` of this lock still waits for the lock's report."""
        command = self._command
        return command is not None and not command.task.done() and command.action == action

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
        """The lock reports locking, or a lock command waits for its report."""
        return self._state is LockState.LOCKING or self._waiting(_LOCK)

    @property
    @override
    def is_unlocking(self) -> bool:
        """The lock reports unlocking, or an unlock command waits for its report."""
        return self._state is LockState.UNLOCKING or self._waiting(_UNLOCK)

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
        """Lock; returns once the lock reports locked, else raises the mapped error."""
        del kwargs  # no code: the library sends the account's own command
        eufy = self.coordinator.config_entry.runtime_data.eufy
        await self._async_command(_LOCK, eufy.async_lock)

    @override
    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock; returns once the lock reports unlocked, else raises the mapped error."""
        del kwargs  # no code: the library sends the account's own command
        eufy = self.coordinator.config_entry.runtime_data.eufy
        await self._async_command(_UNLOCK, eufy.async_unlock)

    async def _async_command(self, action: str, send: _Send) -> None:
        """Start ``action`` unless a command of this lock waits or the library cannot send
        it (both refused before anything is sent), then wait for its outcome. Nothing
        awaits between the checks and the start, so two calls at once cannot both start.
        """
        command = self._command
        if command is not None and not command.task.done():
            raise errors.lock_command_waiting()
        entry = self.coordinator.config_entry
        try:
            control = entry.runtime_data.eufy.lock_control(self._serial)
        except UnsupportedError as err:
            raise errors.lock_failed(err) from err
        if not control.writable:
            raise errors.lock_not_writable(control.reason)
        task = entry.async_create_background_task(
            self.hass, self._async_run(send, action), f"{LOCK_KEY} {action}"
        )
        self._command = _Command(task, action)
        task.add_done_callback(self._on_command_done)
        self.async_write_ha_state()
        await asyncio.wait((task,))
        if task.cancelled():
            raise errors.lock_command_cancelled()
        if (failure := task.result()) is not None:
            raise errors.lock_failed(failure) from failure

    async def _async_run(self, send: _Send, action: str) -> EufySecurityError | None:
        """Send the command and wait for the lock; the library's error is returned, so a
        caller that stopped waiting leaves no unretrieved exception."""
        try:
            await send(self._serial)
        except EufySecurityError as err:
            _LOGGER.debug(
                "%s: the %s command failed: %s", self.entity_id, action, type(err).__name__
            )
            return err
        return None

    @callback
    def _on_command_done(self, task: asyncio.Task[EufySecurityError | None]) -> None:
        """End the locking or unlocking shown while the command waited."""
        del task
        self.async_write_ha_state()
