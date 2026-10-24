"""Model-scoped PIR support does not affect camera or contact-sensor routing."""

from eufy_home_security.devices import DeviceKind
from eufy_home_security.events import DetectionType, EventSource, SecurityEvent

from custom_components.eufy_home_security import detections


def test_t8910_gets_motion_entities_not_camera_entities():
    assert detections.has_motion_sensor_entities("T8910")
    assert not detections.has_detection_entities(DeviceKind.SENSOR)


def test_other_sensor_does_not_get_pir_entities():
    assert not detections.has_motion_sensor_entities("UNKNOWN")


def test_t8910_motion_routes_without_camera_snapshot():
    event = SecurityEvent(
        source=EventSource.CLOUD,
        device_sn="T8910EXAMPLE",
        msg_type=14,
        event_type=DetectionType.MOTION,
    )
    assert detections.device_event_consumed(DeviceKind.SENSOR, event)
    assert not detections.snapshot_wanted(DeviceKind.SENSOR, event)
