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
    Station,
    StationFeature,
)
from eufy_home_security.devices import Support

from . import detections, errors
from .const import DEVICE_SIREN_SECONDS, SIREN_KEY, STATION_SIREN_DEFAULT_SECONDS
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability, EufyStationEntity

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# The library serialises the wire per station; sirens of several devices start together.
PARALLEL_UPDATES = 0


def device_siren_alarms(station: Station) -> dict[str, tuple[DeviceAlarm, ...]]:
    """Each device of ``station`` that gets a camera siren -> the alarms it offers.

    Camera kinds only (``detections.has_detection_entities``) with at least one alarm.
    A station's own serial only when it is standalone: the library sends a HomeBase's
    own device alarm as its station siren, which is the HomeBase siren's.
    """
    offered: dict[str, tuple[DeviceAlarm, ...]] = {}
    for serial, kind in detections.paired_device_kinds(station).items():
        if serial == station.serial and not station.is_standalone:
            continue
        if not detections.has_detection_entities(kind):
            continue
        if alarms := station.device_alarms(serial):
            offered[serial] = alarms
    return offered


def has_station_siren(station: Station) -> bool:
    """Whether ``station`` gets the HomeBase siren: not standalone, and its model's
    ``StationFeature.SOUND_ALARM`` graded other than unknown."""
    if station.is_standalone:
        return False
    return station.config_support(StationFeature.SOUND_ALARM).support is not Support.UNKNOWN


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
        if has_station_siren(station):
            entities.append(EufyStationSiren(coordinator))
        entities.extend(
            EufyCameraSiren(coordinator, serial, alarms)
            for serial, alarms in device_siren_alarms(station).items()
        )
    async_add_entities(entities)


class _EufySiren(SirenEntity):
    """What both sirens share: the name a failure names, and the error mapping."""

    _attr_translation_key = SIREN_KEY
    coordinator: StationCoordinator

    def _failed(self, err: EufySecurityError) -> HomeAssistantError:
        """The translated error for a failed command, naming this entity, never a serial."""
        name = self.name
        return errors.siren_failed(
            err,
            name if isinstance(name, str) else SIREN_KEY,
            on_demand=self.coordinator.station.connects_on_demand,
        )


class EufyCameraSiren(EufyPushAvailability, EufyDeviceEntity, _EufySiren):
    """A camera's own siren and light; its state is assumed (the library reports none)."""

    _attr_assumed_state = True
    _attr_supported_features = (
        SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF | SirenEntityFeature.TONES
    )

    def __init__(
        self, coordinator: StationCoordinator, device_sn: str, alarms: tuple[DeviceAlarm, ...]
    ) -> None:
        super().__init__(coordinator, device_sn, SIREN_KEY)
        self._serial = device_sn
        self._attr_available_tones = [alarm.name.lower() for alarm in alarms]
        self._attr_is_on = False
        # The alarm the last confirmed start sounded (None: the app's default), for its stop.
        self._sounding: DeviceAlarm | None = None
        self._cancel_off: CALLBACK_TYPE | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Cancel the 30 s off when the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(self._async_cancel_off)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Sound ``tone`` (else the app's default); on for 30 s once confirmed."""
        tone = kwargs.get(ATTR_TONE)
        alarm = None if tone is None else DeviceAlarm[str(tone).upper()]
        try:
            await self.coordinator.station.async_trigger_device_alarm(self._serial, alarm)
        except EufySecurityError as err:
            raise self._failed(err) from err
        self._async_cancel_off()
        self._cancel_off = async_call_later(self.hass, DEVICE_SIREN_SECONDS, self._async_sounded)
        self._sounding = alarm
        self._attr_is_on = True
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the alarm last started; an unconfirmed stop reads as off."""
        del kwargs  # turn_off takes no service parameters
        try:
            await self.coordinator.station.async_stop_device_alarm(self._serial, self._sounding)
        except CommandNotAppliedError:
            # Acknowledged and not confirmed: the device sends no confirmation once silent.
            _LOGGER.debug("%s: stop not confirmed; shown as off", self.entity_id)
        except EufySecurityError as err:
            raise self._failed(err) from err
        self._async_show_off()

    @callback
    def _async_sounded(self, _now: object) -> None:
        """The 30 s of a start are over."""
        self._cancel_off = None
        self._async_show_off()

    @callback
    def _async_show_off(self) -> None:
        self._async_cancel_off()
        self._sounding = None
        self._attr_is_on = False
        self.async_write_ha_state()

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
        """Sound the station siren for ``duration`` (rounded up to an allowed length)."""
        seconds = station_alarm_seconds(kwargs.get(ATTR_DURATION))
        try:
            await self.coordinator.station.async_trigger_alarm(seconds)
        except EufySecurityError as err:
            raise self._failed(err) from err
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the station siren, whoever started it."""
        del kwargs  # turn_off takes no service parameters
        try:
            await self.coordinator.station.async_stop_alarm()
        except EufySecurityError as err:
            raise self._failed(err) from err
        self.async_write_ha_state()
