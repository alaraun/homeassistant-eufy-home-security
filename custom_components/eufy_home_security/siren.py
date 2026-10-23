"""Sirens: a camera's own alarm and the HomeBase siren, where the eufy app offers them.

**Camera siren.** One per camera whose model the eufy app offers an alarm
(``Station.device_alarms``), a standalone camera's own serial included. ``tone`` picks
the alarm (``sound``, ``light``, ``sound_and_light``, as offered); without one the app's
preselected alarm sounds. A start sounds for 30 s. The library reports no state for it,
so the entity assumes on once the start is confirmed, and off 30 s later or after a stop.
A stop the device does not confirm (the alarm already over) reads as off.

**HomeBase siren.** One per station whose model the library sounds
(``StationFeature.SOUND_ALARM`` not unknown), never on a standalone camera, whose siren
is its camera siren. On while the station alarms (``Station.alarming``, followed on the
station's alarm signal), whoever started it. ``duration`` is sent as the shortest length
the library allows at or above it.

A failure raises ``errors.siren_failed``; nothing shows on that was not confirmed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.siren import (
    ATTR_DURATION,
    ATTR_TONE,
    SirenEntity,
    SirenEntityFeature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from eufy_home_security import (
    SOUND_ALARM_SECONDS,
    AlarmChanged,
    CommandNotAppliedError,
    DeviceAlarm,
    EufySecurityError,
)

from . import detections, errors
from .const import DEVICE_SIREN_SECONDS, SIREN_KEY, STATION_SIREN_DEFAULT_SECONDS
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability, EufyStationEntity

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# The library serialises the wire per station; sirens of several devices start together.
PARALLEL_UPDATES = 0


def station_alarm_seconds(
    duration: int | None, allowed: Sequence[int] = SOUND_ALARM_SECONDS
) -> int:
    """The shortest ``allowed`` length at or above ``duration`` (default 30 s).

    Raises ``errors.siren_duration_invalid`` above the longest allowed length.
    """
    wanted = STATION_SIREN_DEFAULT_SECONDS if duration is None else duration
    longer = [seconds for seconds in allowed if seconds >= wanted]
    if not longer:
        raise errors.siren_duration_invalid(max(allowed))
    return min(longer)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each station's HomeBase siren and its cameras' sirens, where offered."""
    del hass  # the coordinators carry everything this platform needs
    entities: list[SirenEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        station = coordinator.station
        if detections.has_station_siren(station):
            entities.append(EufyStationSiren(coordinator))
        entities.extend(
            EufyCameraSiren(coordinator, serial, alarms)
            for serial, alarms in detections.device_siren_alarms(station).items()
        )
    async_add_entities(entities)


class _EufySiren(SirenEntity):
    """What both sirens share: the name a failure names, and the error mapping."""

    _attr_translation_key = SIREN_KEY
    coordinator: StationCoordinator

    def _failed(self, err: EufySecurityError) -> HomeAssistantError:
        """The translated error for a failed command, naming this entity by its shown
        name (device and siren), never a serial."""
        state = self.hass.states.get(self.entity_id) if self.entity_id else None
        name = state.name if state is not None else self.name
        return errors.siren_failed(
            err,
            name if isinstance(name, str) else SIREN_KEY,
            on_demand=self.coordinator.station.connects_on_demand,
        )


class EufyCameraSiren(EufyPushAvailability, EufyDeviceEntity, _EufySiren):
    """A camera's own siren and light; its state is assumed (the library reports none).

    Each alarm has its own stop, so a stop names the alarms that may be sounding: those
    started (confirmed or not) and not yet stopped, else the last one started, else, with
    none started since setup, every alarm the camera offers.
    """

    _attr_assumed_state = True
    _attr_supported_features = (
        SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF | SirenEntityFeature.TONES
    )

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, alarms: tuple[DeviceAlarm, ...]
    ) -> None:
        super().__init__(coordinator, device_sn, SIREN_KEY)
        self._serial = device_sn
        self._alarms = alarms
        self._attr_available_tones = [alarm.name.lower() for alarm in alarms]
        self._attr_is_on = False
        # Alarms started and not yet stopped, oldest first.
        self._unstopped: list[DeviceAlarm] = []
        # The alarm last started; None before any start since setup.
        self._last: DeviceAlarm | None = None
        self._removed = False
        self._cancel_off: CALLBACK_TYPE | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Cancel the 30 s off when the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(self._async_removed)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Sound ``tone`` (else the app's default); on for 30 s once confirmed."""
        tone = kwargs.get(ATTR_TONE)
        alarm = (
            DeviceAlarm[str(tone).upper()]
            if tone is not None
            else self.coordinator.station.device_alarm_default(self._serial)
        )
        if alarm is not None:
            # Recorded before the start: an unconfirmed start may still be sounding.
            if alarm in self._unstopped:
                self._unstopped.remove(alarm)
            self._unstopped.append(alarm)
            self._last = alarm
        try:
            await self.coordinator.station.async_trigger_device_alarm(self._serial, alarm)
        except EufySecurityError as err:
            raise self._failed(err) from err
        if self._removed:
            return
        self._async_cancel_off()
        self._cancel_off = async_call_later(self.hass, DEVICE_SIREN_SECONDS, self._async_sounded)
        self._attr_is_on = True
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop each alarm that may be sounding; an unconfirmed stop reads as off.

        Every stop is tried; the first refusal is raised after the last one.
        """
        del kwargs  # turn_off takes no service parameters
        if self._unstopped:
            targets = list(self._unstopped)
        elif self._last is not None:
            targets = [self._last]
        else:
            targets = list(self._alarms)
        failure: EufySecurityError | None = None
        for alarm in targets:
            try:
                await self.coordinator.station.async_stop_device_alarm(self._serial, alarm)
            except CommandNotAppliedError:
                # Acknowledged and not confirmed: the device sends no confirmation once silent.
                _LOGGER.debug("%s: stop not confirmed; shown as off", self.entity_id)
            except EufySecurityError as err:
                failure = failure or err
                continue
            if alarm in self._unstopped:
                self._unstopped.remove(alarm)
        if failure is not None:
            raise self._failed(failure) from failure
        self._async_show_off()

    @callback
    def _async_sounded(self, _now: object) -> None:
        """The 30 s of a start are over (assumed: the alarm stays unstopped)."""
        self._cancel_off = None
        self._async_show_off()

    @callback
    def _async_show_off(self) -> None:
        self._async_cancel_off()
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _async_removed(self) -> None:
        self._removed = True
        self._async_cancel_off()

    @callback
    def _async_cancel_off(self) -> None:
        if self._cancel_off is not None:
            self._cancel_off()
            self._cancel_off = None


class EufyStationSiren(EufyPushAvailability, EufyStationEntity, _EufySiren):
    """The HomeBase's own siren: on while the station alarms."""

    _attr_supported_features = (
        SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF | SirenEntityFeature.DURATION
    )

    def __init__(self, coordinator: StationCoordinator) -> None:
        super().__init__(coordinator, SIREN_KEY)

    @property
    @override
    def is_on(self) -> bool:
        """``Station.alarming``: the station's last alarm transition."""
        return self.coordinator.station.alarming

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the station's alarm signal until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.alarm_signal(
                    self.coordinator.config_entry.entry_id, self.coordinator.station.serial
                ),
                self._async_on_alarm_changed,
            )
        )

    @callback
    def _async_on_alarm_changed(self, event: AlarmChanged) -> None:
        """Show the transition; ``Station.alarming`` already holds it."""
        del event
        self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Sound the station siren for ``duration`` (rounded up to an allowed length).

        The state follows the station's alarm signal, not this call.
        """
        seconds = station_alarm_seconds(kwargs.get(ATTR_DURATION))
        try:
            await self.coordinator.station.async_trigger_alarm(seconds)
        except EufySecurityError as err:
            raise self._failed(err) from err

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the station siren, whoever started it."""
        del kwargs  # turn_off takes no service parameters
        try:
            await self.coordinator.station.async_stop_alarm()
        except EufySecurityError as err:
            raise self._failed(err) from err
