"""Garage doors: one cover per door of a garage-door camera (``Station.garage_doors``).

A door is added once its camera reports a motor or a sensor bound to it (checked at
setup and on every state of its station after). A camera with one door names it
"Garage door"; one with two names them "Garage door A" and "Garage door B" (doors 1 and 2,
as the eufy app letters them). Open and close are offered only while the door reports a
bound motor; without one the cover shows the door's state only.

The state is the library's door state, followed on the door's ``GarageDoorChanged``;
nothing is polled for it. Open and close start the library's door command as a
background task of the entry and return at once: the door shows opening or closing from
the library's events until the door's next report. The camera takes one door command at
a time: while one waits, the same command of the same door does nothing, and any other
command of either door is refused before anything is sent; only an unload ends the wait.
A command that ends without the asked state (the door reported another state, no report
came, or the camera refused it) logs one warning, and the door shows the state it
reports. A door whose position is unknown refuses open and close before anything is
sent: the eufy app's "operate" command, which runs the motor whichever way, is not
offered.

A door's name is fixed when it is added: a door bound while the camera's other door is
the only one keeps "Garage door" until the entry reloads (the model data gives no door
count).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, override

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from eufy_home_security import (
    CommandRejectedError,
    DeviceTimeoutError,
    DoorNotMovedError,
    EufySecurityError,
    GarageDoor,
    GarageDoorChanged,
    GarageDoorState,
    Station,
    UnsupportedError,
)

from . import detections, errors
from .const import GARAGE_DOOR_KEY, GARAGE_DOOR_LETTERED_KEY
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# Commands run as background tasks; the call itself only starts one.
PARALLEL_UPDATES = 0

# The eufy app's letter for each door.
_DOOR_LETTERS: Final = {1: "A", 2: "B"}


@dataclass
class _CommandSlot:
    """The door command of one camera still waiting for its report (``task`` None or done
    when none), with its door and action."""

    task: asyncio.Task[None] | None = None
    door: int = 0
    action: str = ""


def garage_doors(station: Station, serial: str) -> tuple[GarageDoor, ...]:
    """The doors ``serial`` on ``station`` reports; none for a device the latest device
    list no longer pairs here (the reload follows)."""
    try:
        return station.garage_doors(serial)
    except UnsupportedError:
        return ()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a cover per garage door, each once its camera reports it."""
    del hass  # the coordinators carry everything this platform needs
    for coordinator in entry.runtime_data.coordinators.values():
        serials = [
            serial
            for serial in detections.paired_device_kinds(coordinator.station)
            if detections.has_garage_doors(serial)
        ]
        if serials:
            _async_add_doors_when_reported(entry, coordinator, serials, async_add_entities)


@callback
def _async_add_doors_when_reported(
    entry: EufyConfigEntry,
    coordinator: StationCoordinator,
    serials: list[str],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each door of ``serials`` now and on every later state that first reports it."""
    added: set[tuple[str, int]] = set()
    slots = {serial: _CommandSlot() for serial in serials}

    @callback
    def _check() -> None:
        new: list[EufyGarageDoor] = []
        for serial in serials:
            doors = garage_doors(coordinator.station, serial)
            new.extend(
                EufyGarageDoor(
                    coordinator, serial, door.door, slots[serial], lettered=len(doors) > 1
                )
                for door in doors
                if (serial, door.door) not in added
            )
        if new:
            added.update((entity.serial, entity.door) for entity in new)
            async_add_entities(new)

    _check()
    entry.async_on_unload(coordinator.async_add_listener(_check))


class EufyGarageDoor(EufyPushAvailability, EufyDeviceEntity, CoverEntity):
    """One garage door: its reported state, and open and close where a motor is bound."""

    _attr_device_class = CoverDeviceClass.GARAGE

    def __init__(
        self,
        coordinator: StationCoordinator,
        serial: str,
        door: int,
        slot: _CommandSlot,
        *,
        lettered: bool,
    ) -> None:
        station = coordinator.station
        super().__init__(
            coordinator,
            None if serial == station.serial else serial,
            f"{GARAGE_DOOR_KEY}_{door}",
        )
        self.serial = serial
        self.door = door
        if lettered:
            self._attr_translation_key = GARAGE_DOOR_LETTERED_KEY
            self._attr_translation_placeholders = {"door": _DOOR_LETTERS[door]}
        else:
            self._attr_translation_key = GARAGE_DOOR_KEY
        # Shared by the camera's doors.
        self._slot = slot

    @property
    def _door(self) -> GarageDoor | None:
        """The library's view of this door; None while its camera does not report it."""
        return next(
            (d for d in garage_doors(self.coordinator.station, self.serial) if d.door == self.door),
            None,
        )

    @property
    def _state(self) -> GarageDoorState:
        door = self._door
        return GarageDoorState.UNKNOWN if door is None else door.state

    @property
    @override
    def supported_features(self) -> CoverEntityFeature:
        """Open and close while the door reports a bound motor, else none."""
        door = self._door
        if door is not None and door.motor_bound:
            return CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        return CoverEntityFeature(0)

    @property
    @override
    def is_closed(self) -> bool | None:
        """Closed: True; open: False; moving or unknown: None."""
        state = self._state
        if state is GarageDoorState.CLOSED:
            return True
        if state is GarageDoorState.OPEN:
            return False
        return None

    @property
    @override
    def is_opening(self) -> bool:
        return self._state is GarageDoorState.OPENING

    @property
    @override
    def is_closing(self) -> bool:
        return self._state is GarageDoorState.CLOSING

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the door's state changes until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.garage_door_signal(
                    self.coordinator.config_entry.entry_id, self.serial, self.door
                ),
                self._async_on_door_changed,
            )
        )

    @callback
    def _async_on_door_changed(self, event: GarageDoorChanged) -> None:
        """Show the change; ``Station.garage_doors`` already holds it."""
        del event
        self.async_write_ha_state()

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Start opening the door; the state follows the library's events."""
        del kwargs  # open takes no service parameters
        await self._async_start(self.coordinator.station.async_open_garage_door, "open")

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Start closing the door; the state follows the library's events."""
        del kwargs  # close takes no service parameters
        await self._async_start(self.coordinator.station.async_close_garage_door, "close")

    async def _async_start(
        self, move: Callable[[str, int], Coroutine[Any, Any, GarageDoorState]], action: str
    ) -> None:
        """Start this command unless one waits on the camera: the same one is kept, any
        other refused; a door of unknown position is refused. Nothing awaits between the
        check and the start, so two calls at once cannot both start."""
        slot = self._slot
        if slot.task is not None and not slot.task.done():
            if slot.door != self.door:
                raise errors.garage_other_door_moving()
            if slot.action != action:
                raise errors.garage_door_moving()
            return
        if self._state is GarageDoorState.UNKNOWN:
            raise errors.garage_door_position_unknown()
        slot.door, slot.action = self.door, action
        slot.task = self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_run(move, action),
            f"{GARAGE_DOOR_KEY} {action}",
        )

    async def _async_run(
        self, move: Callable[[str, int], Coroutine[Any, Any, GarageDoorState]], action: str
    ) -> None:
        """Run one door command; a failure is logged once, the state follows the reports."""
        try:
            await move(self.serial, self.door)
        except DoorNotMovedError as err:
            _LOGGER.warning(
                "%s: the %s command ended with the door reporting %s",
                self.entity_id,
                action,
                err.state,
            )
        except DeviceTimeoutError:
            _LOGGER.warning(
                "%s: the door reported no new position after the %s command; it shows its "
                "last reported position",
                self.entity_id,
                action,
            )
        except CommandRejectedError:
            _LOGGER.warning("%s: the camera refused the %s command", self.entity_id, action)
        except EufySecurityError as err:
            _LOGGER.warning(
                "%s: the %s command failed (%s)", self.entity_id, action, type(err).__name__
            )
