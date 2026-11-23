"""Two native per-camera events derived from reported Eufy activity."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util.signal_type import SignalType

from eufy_home_security import DetectedEventsCounted, RecordedEventsCounted

from .const import (
    DETECTED_EVENTS_COUNTED_EVENT_KEY,
    DOMAIN,
    RECORDED_EVENTS_COUNTED_EVENT_KEY,
)
from .entity import EufyDeviceEntity

if TYPE_CHECKING:
    from .coordinator import StationCoordinator


def activity_signal(
    entry_id: str, device_sn: str
) -> SignalType[DetectedEventsCounted | RecordedEventsCounted]:
    """Private per-camera dispatcher signal."""
    return SignalType(f"{DOMAIN}_activity_{entry_id}_{device_sn}")


class EufyDetectedEventsCountedEvent(EufyDeviceEntity, EventEntity):
    """A detected-events counter increase, not a confirmed detection."""

    entity_description = EventEntityDescription(
        key=DETECTED_EVENTS_COUNTED_EVENT_KEY,
        translation_key=DETECTED_EVENTS_COUNTED_EVENT_KEY,
        event_types=["counted"],
    )

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, DETECTED_EVENTS_COUNTED_EVENT_KEY)
        self._serial = device_sn

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                activity_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._on_activity,
            )
        )

    @callback
    def _on_activity(self, event: DetectedEventsCounted | RecordedEventsCounted) -> None:
        if not isinstance(event, DetectedEventsCounted):
            return
        self._trigger_event(
            "counted",
            {
                "count": event.count,
                "observed_at": event.observed_at.isoformat(),
            },
        )
        self.async_write_ha_state()


class EufyRecordedEventsCountedEvent(EufyDeviceEntity, EventEntity):
    """A rising recorded-events counter; not proof a clip is already finalised."""

    entity_description = EventEntityDescription(
        key=RECORDED_EVENTS_COUNTED_EVENT_KEY,
        translation_key=RECORDED_EVENTS_COUNTED_EVENT_KEY,
        event_types=["counted"],
    )

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, RECORDED_EVENTS_COUNTED_EVENT_KEY)
        self._serial = device_sn

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                activity_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._on_activity,
            )
        )

    @callback
    def _on_activity(self, event: DetectedEventsCounted | RecordedEventsCounted) -> None:
        if not isinstance(event, RecordedEventsCounted):
            return
        self._trigger_event(
            "counted",
            {
                "count": event.count,
                "observed_at": event.observed_at.isoformat(),
            },
        )
        self.async_write_ha_state()
