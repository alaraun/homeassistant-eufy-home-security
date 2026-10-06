"""Event entities: what a camera detected, fired as it happens.

One detection event per camera (and per doorbell, which is also a camera that
detects). It fires when the library delivers a motion, person, identified person,
stranger, pet or vehicle detection for that device, carrying the detection's own
time rather than the moment Home Assistant heard about it.

The router dispatches to the entity only what ``detections.device_event_consumed``
says it consumes, and the library has already de-duplicated every event across
reconnects and channels, so each occurrence fires once. Nothing here polls, and no
push reaches the coordinator.

**Why no device class.** ``EventDeviceClass.MOTION`` would name every one of these
entities "Motion", and a person or vehicle detection is not a motion event. The
translation key names it instead.

**What the attributes do not carry.** A push holds media paths, serials, the device
nickname and the raw record. None of those belongs in state the recorder keeps, so
the entity publishes the allow-list ``detections.detection_attributes`` builds.

**The station's own events.** Every station has an alarm event (triggered, stopped,
delay) and an arming event (the guard mode it changed to), fed on the station's
signal with what ``detections.station_event_consumed`` accepts. The alarm event fires
triggered and stopped from the library's ``AlarmChanged`` on the station's alarm
signal, once per transition and without the push opt-in; from a push it fires only
an entry delay, and a trigger that was not authenticated, marked so. The arming
event fires only from an authenticated push, naming the mode in force.

**The doorbell ring.** A device the library catalogues as a doorbell also gets a
ring event, fired from its doorbell press, beside its detection event. The catalog
has no doorbell model, so a camera's press goes to the fallback bus event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.components.event.const import DoorbellEventType
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from eufy_home_security import (
    AlarmChanged,
    AlarmPhase,
    DetectionType,
    PushMessageType,
    SecurityEvent,
)

from . import detections
from .const import (
    ALARM_EVENT_KEY,
    ARMING_EVENT_KEY,
    ATTR_AUTHENTICATED,
    ATTR_CHANGED_BY,
    ATTR_STOP_SOURCE,
    ATTR_TRIGGERED_AT,
    DETECTION_EVENT_KEY,
    DOORBELL_EVENT_KEY,
)
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability, EufyStationEntity

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

# Events arrive through the dispatcher and nothing here polls, so HA need not
# serialise entity updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add each station's alarm and arming events, then its devices' detection and ring events."""
    del hass  # the coordinators carry everything this platform needs
    entities: list[EventEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        entities.append(EufyAlarmEvent(coordinator))
        entities.append(EufyArmingEvent(coordinator))
        for device_sn, kind in detections.paired_device_kinds(coordinator.station).items():
            if detections.has_detection_entities(kind):
                entities.append(EufyDetectionEvent(coordinator, device_sn))
            if detections.has_doorbell_entity(kind):
                entities.append(EufyDoorbellEvent(coordinator, device_sn))
    async_add_entities(entities)


class EufyDetectionEvent(EufyPushAvailability, EufyDeviceEntity, EventEntity):
    """What one camera detected, fired once per occurrence."""

    entity_description = EventEntityDescription(
        key=DETECTION_EVENT_KEY,
        translation_key=DETECTION_EVENT_KEY,
        event_types=list(detections.DETECTION_EVENT_TYPE_NAMES),
    )

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, DETECTION_EVENT_KEY)
        self._serial = device_sn

    @override
    async def async_added_to_hass(self) -> None:
        """Listen on this device's signal until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.device_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_security_event,
            )
        )

    @callback
    def _async_on_security_event(self, event: SecurityEvent) -> None:
        """Fire the detection ``event`` names, with its allow-listed attributes."""
        event_type = detections.detection_event_type(event)
        if event_type is None:
            # Entities sharing a device signal ignore what they do not declare:
            # ``_trigger_event`` raises on an undeclared type.
            return
        self._trigger_event(event_type, detections.detection_attributes(event))
        self.async_write_ha_state()


class EufyDoorbellEvent(EufyPushAvailability, EufyDeviceEntity, EventEntity):
    """A catalogued doorbell's ring, fired once per press."""

    entity_description = EventEntityDescription(
        key=DOORBELL_EVENT_KEY,
        translation_key=DOORBELL_EVENT_KEY,
        device_class=EventDeviceClass.DOORBELL,
        event_types=[DoorbellEventType.RING],
    )

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, DOORBELL_EVENT_KEY)
        self._serial = device_sn

    @override
    async def async_added_to_hass(self) -> None:
        """Listen on this device's signal until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.device_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_security_event,
            )
        )

    @callback
    def _async_on_security_event(self, event: SecurityEvent) -> None:
        """Ring on a doorbell press; the device's detections are its other entities' job."""
        if event.detection is not DetectionType.DOORBELL_PRESS:
            return
        attributes: dict[str, str] = {}
        when = detections.triggered_at(event)
        if when is not None:
            attributes[ATTR_TRIGGERED_AT] = when
        self._trigger_event(DoorbellEventType.RING, attributes)
        self.async_write_ha_state()


class _EufyStationEvent(EufyPushAvailability, EufyStationEntity, EventEntity):
    """A station event entity, fed on its station's signal."""

    @override
    async def async_added_to_hass(self) -> None:
        """Listen on this station's signal until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.station_signal(
                    self.coordinator.config_entry.entry_id, self.coordinator.station.serial
                ),
                self._async_on_station_event,
            )
        )

    @callback
    def _async_on_station_event(self, event: SecurityEvent) -> None:
        """Fire what ``event`` means to this entity, if anything."""
        raise NotImplementedError


class EufyAlarmEvent(_EufyStationEvent):
    """The station's alarm lifecycle: triggered, stopped, entry delay started."""

    entity_description = EventEntityDescription(
        key=ALARM_EVENT_KEY,
        translation_key=ALARM_EVENT_KEY,
        event_types=list(detections.ALARM_EVENT_TYPE_NAMES),
    )

    def __init__(self, coordinator: StationCoordinator) -> None:
        super().__init__(coordinator, ALARM_EVENT_KEY)

    @override
    async def async_added_to_hass(self) -> None:
        """Also listen on this station's alarm signal until the entity is removed."""
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
        """Fire triggered or stopped for one library alarm transition.

        ``AlarmChanged`` is authenticated and carries no event time, so the time is
        the host's, as the library stamps the P2P frame. ``stop_source`` is the
        library's stop code name (``app``), only when a stop named one.
        """
        attributes: dict[str, str | bool] = {
            ATTR_TRIGGERED_AT: dt_util.utcnow().isoformat(timespec="milliseconds"),
            ATTR_AUTHENTICATED: True,
        }
        if event.alarming:
            event_type = detections.ALARM_EVENT_TYPES[AlarmPhase.TRIGGERED]
        else:
            event_type = detections.ALARM_EVENT_TYPES[AlarmPhase.STOPPED]
            if event.stop_source is not None:
                attributes[ATTR_STOP_SOURCE] = event.stop_source.name.lower()
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    @override
    @callback
    def _async_on_station_event(self, event: SecurityEvent) -> None:
        """Fire an entry delay, or an unauthenticated trigger, marked as such.

        A delay fires under any cipher, and so does a trigger that was not
        authenticated. An authenticated trigger and every stop fire from the
        ``AlarmChanged`` the library makes of them instead, so an alarm seen on both
        channels fires once; the library already withholds an unauthenticated stop.
        """
        phase = event.alarm_phase
        if phase is None or phase is AlarmPhase.STOPPED:
            return
        if phase is AlarmPhase.TRIGGERED and event.authenticated:
            return
        attributes: dict[str, str | bool] = {}
        when = detections.triggered_at(event)
        if when is not None:
            attributes[ATTR_TRIGGERED_AT] = when
        attributes[ATTR_AUTHENTICATED] = event.authenticated
        self._trigger_event(detections.ALARM_EVENT_TYPES[phase], attributes)
        self.async_write_ha_state()


class EufyArmingEvent(_EufyStationEvent):
    """The guard mode the station changed to, from an authenticated push only."""

    entity_description = EventEntityDescription(
        key=ARMING_EVENT_KEY,
        translation_key=ARMING_EVENT_KEY,
        event_types=list(detections.ARMING_EVENT_TYPE_NAMES),
    )

    def __init__(self, coordinator: StationCoordinator) -> None:
        super().__init__(coordinator, ARMING_EVENT_KEY)

    @override
    @callback
    def _async_on_station_event(self, event: SecurityEvent) -> None:
        """Fire the guard mode an authenticated arming push reports, and who changed it.

        An unauthenticated push is ignored; the router already sends it to the
        fallback bus event. ``changed_by`` is the source label, never the push's
        ``user_name``, which any client can set.
        """
        if event.message_type is not PushMessageType.ARMING or not event.authenticated:
            return
        mode = detections.effective_guard_mode(event)
        if mode is None:
            return
        state = detections.ha_state(mode)
        if state is None:  # a known GuardMode always maps; kept for the type checker
            return
        attributes: dict[str, str] = {}
        when = detections.triggered_at(event)
        if when is not None:
            attributes[ATTR_TRIGGERED_AT] = when
        source = event.arming_source
        if source is not None:
            attributes[ATTR_CHANGED_BY] = detections.ARMING_SOURCE_LABELS[source]
        self._trigger_event(detections.ARMING_EVENT_TYPES[state], attributes)
        self.async_write_ha_state()
