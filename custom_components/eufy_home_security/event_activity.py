"""Two native per-camera events derived from reported Eufy activity."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util.signal_type import SignalType

from eufy_home_security import MotionDetected, MotionRecorded

from .const import DOMAIN, MOTION_DETECTED_EVENT_KEY, MOTION_RECORDED_EVENT_KEY
from .entity import EufyDeviceEntity

if TYPE_CHECKING:
    from .coordinator import StationCoordinator


def activity_signal(entry_id: str, device_sn: str) -> SignalType[MotionDetected | MotionRecorded]:
    """Private per-camera dispatcher signal."""
    return SignalType(f"{DOMAIN}_activity_{entry_id}_{device_sn}")


class EufyMotionDetectedEvent(EufyDeviceEntity, EventEntity):
    """A rising detection counter, or push fallback if that counter is unavailable."""

    entity_description = EventEntityDescription(
        key=MOTION_DETECTED_EVENT_KEY,
        translation_key=MOTION_DETECTED_EVENT_KEY,
        event_types=["motion"],
    )

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, MOTION_DETECTED_EVENT_KEY)
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
    def _on_activity(self, event: MotionDetected | MotionRecorded) -> None:
        if not isinstance(event, MotionDetected):
            return
        self._trigger_event(
            "motion",
            {
                "source": event.source,
                "count": event.count,
                "observed_at": event.observed_at.isoformat(),
            },
        )
        self.async_write_ha_state()


class EufyMotionRecordedEvent(EufyDeviceEntity, EventEntity):
    """A rising recorded-events counter; not proof a clip is already finalised."""

    entity_description = EventEntityDescription(
        key=MOTION_RECORDED_EVENT_KEY,
        translation_key=MOTION_RECORDED_EVENT_KEY,
        event_types=["recorded"],
    )

    def __init__(self, coordinator: StationCoordinator, device_sn: str) -> None:
        super().__init__(coordinator, device_sn, MOTION_RECORDED_EVENT_KEY)
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
    def _on_activity(self, event: MotionDetected | MotionRecorded) -> None:
        if not isinstance(event, MotionRecorded):
            return
        self._trigger_event(
            "recorded",
            {
                "source": "counter",
                "count": event.count,
                "observed_at": event.observed_at.isoformat(),
            },
        )
        self.async_write_ha_state()
