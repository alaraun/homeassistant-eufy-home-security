"""Which pushes an entity consumes, decided once for the router and the platforms.

Pure decisions over the library's own enums and state: ``SecurityEvent`` properties
(``detection``, ``message_type``, ``alarm_phase``, ``enriches``), the paired device
list and the library's serial-to-model lookup. Nothing here reads a payload key or
``raw``, and nothing here holds state.

The router asks :func:`device_event_consumed` whether an entity listens for an event,
and the platforms build their entities from the same :func:`paired_device_kinds` and
:func:`has_detection_entities`. Because both sides read one decision, the router
never has to ask the dispatcher whether a listener exists: an event that no entity
consumes is known to be one, and goes to the fallback bus event.

Station pushes (arming, alarm, alarm delay) are decided by
:func:`station_event_consumed` and travel on :func:`station_signal`, one per station.
The alarm's own state, the library's ``AlarmChanged``, travels on
:func:`alarm_signal`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from homeassistant.components.alarm_control_panel.const import AlarmControlPanelState
from homeassistant.util import dt as dt_util
from homeassistant.util.signal_type import SignalType

from eufy_home_security import (
    AlarmChanged,
    AlarmPhase,
    ArmingSource,
    DetectionType,
    GuardMode,
    PushMessageType,
    SecurityEvent,
    Station,
)
from eufy_home_security.devices import (
    Capability,
    DeviceKind,
    Support,
    model_for_serial,
    profile_for_serial,
)

from .const import (
    ATTR_PERSON_NAME,
    ATTR_TRIGGERED_AT,
    DOMAIN,
    MOTION_DETECTED_KEY,
    PERSON_DETECTED_KEY,
    PET_DETECTED_KEY,
    VEHICLE_DETECTED_KEY,
)

# The device kinds that get detection entities. A doorbell is also a camera that
# detects, so it gets them too.
DETECTION_ENTITY_KINDS: Final[frozenset[DeviceKind]] = frozenset(
    {DeviceKind.CAMERA, DeviceKind.DOORBELL}
)

# Each detection the event entity fires, and the event type it fires. The dog
# detections fire ``pet``, so no detection that lights the pet sensor falls to the
# fallback. Anything missing here (a doorbell press, crying, a sound) is not a
# detection event.
DETECTION_EVENT_TYPES: Final[Mapping[DetectionType, str]] = MappingProxyType(
    {
        DetectionType.MOTION: "motion",
        DetectionType.PERSON: "person",
        DetectionType.IDENTITY_PERSON: "identified_person",
        DetectionType.STRANGER_PERSON: "stranger",
        DetectionType.PET: "pet",
        DetectionType.DOG: "pet",
        DetectionType.DOG_LICK: "pet",
        DetectionType.DOG_POOP: "pet",
        DetectionType.VEHICLE: "vehicle",
    }
)

# Each detection binary sensor and the detections that turn it on. A person
# detection lights only the person sensor, never motion as well. The order is the
# order the sensors are added, and with it their entity ids.
DETECTION_CLASSES: Final[Mapping[str, frozenset[DetectionType]]] = MappingProxyType(
    {
        MOTION_DETECTED_KEY: frozenset({DetectionType.MOTION}),
        PERSON_DETECTED_KEY: frozenset(
            {
                DetectionType.PERSON,
                DetectionType.IDENTITY_PERSON,
                DetectionType.STRANGER_PERSON,
            }
        ),
        PET_DETECTED_KEY: frozenset(
            {
                DetectionType.PET,
                DetectionType.DOG,
                DetectionType.DOG_LICK,
                DetectionType.DOG_POOP,
            }
        ),
        VEHICLE_DETECTED_KEY: frozenset({DetectionType.VEHICLE}),
    }
)

# The detection event entity's declared event types, in a fixed order. Derived
# from the mapping, so a type it fires is always declared: ``_trigger_event`` raises
# on an undeclared one.
DETECTION_EVENT_TYPE_NAMES: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(DETECTION_EVENT_TYPES.values())
)


# Each alarm phase the station alarm event fires, and its event type. Every phase is
# fired whatever the cipher; the event says whether its push was authenticated.
ALARM_EVENT_TYPES: Final[Mapping[AlarmPhase, str]] = MappingProxyType(
    {
        AlarmPhase.TRIGGERED: "alarm_triggered",
        AlarmPhase.STOPPED: "alarm_stopped",
        AlarmPhase.DELAY: "alarm_delay",
    }
)

# The alarm event entity's declared event types, derived as above.
ALARM_EVENT_TYPE_NAMES: Final[tuple[str, ...]] = tuple(dict.fromkeys(ALARM_EVENT_TYPES.values()))

# The arming event type of each panel state. The guard mode goes through
# ``ha_state``, shared with the panel, so the arming event and the panel can never
# disagree.
ARMING_EVENT_TYPES: Final[Mapping[AlarmControlPanelState, str]] = MappingProxyType(
    {
        AlarmControlPanelState.ARMED_AWAY: "armed_away",
        AlarmControlPanelState.ARMED_HOME: "armed_home",
        AlarmControlPanelState.DISARMED: "disarmed",
        AlarmControlPanelState.ARMED_CUSTOM_BYPASS: "armed_custom",
    }
)

# The arming event entity's declared event types, derived as above.
ARMING_EVENT_TYPE_NAMES: Final[tuple[str, ...]] = tuple(dict.fromkeys(ARMING_EVENT_TYPES.values()))

# Who changed the guard mode, as the arming event's ``changed_by``. A label for
# the library's source code, never the push's ``user_name``: any client on the LAN or
# account can send any name.
ARMING_SOURCE_LABELS: Final[Mapping[ArmingSource, str]] = MappingProxyType(
    {
        ArmingSource.KEYPAD: "Keypad",
        ArmingSource.KEY_FOB: "Key fob",
        ArmingSource.APP: "App",
    }
)


def ha_state(mode: GuardMode | int | None) -> AlarmControlPanelState | None:
    """Map a station guard mode to the panel state.

    Shared by the alarm panel and the arming event, and kept here so neither
    platform module imports the other.

    ``None`` (not read yet) and an unknown code (a plain ``int``) show as unknown,
    never as disarmed: "disarmed" is the one wrong answer that tells a user the
    house is unprotected when nobody knows. ``is_disarmed`` covers both DISARMED
    and OFF. Schedule, the custom modes and geofence have no native HA state and
    show as custom bypass.
    """
    if mode is None or not isinstance(mode, GuardMode):
        return None
    if mode.is_disarmed:
        return AlarmControlPanelState.DISARMED
    if mode is GuardMode.AWAY:
        return AlarmControlPanelState.ARMED_AWAY
    if mode is GuardMode.HOME:
        return AlarmControlPanelState.ARMED_HOME
    return AlarmControlPanelState.ARMED_CUSTOM_BYPASS


def device_kind(serial: str) -> DeviceKind | None:
    """What the library knows the device ``serial`` to be; None for an unknown model."""
    model = model_for_serial(serial)
    return model.kind if model is not None else None


def paired_device_kinds(station: Station) -> dict[str, DeviceKind | None]:
    """Every device of ``station`` that has a serial, in ``Station.devices`` order, by kind.

    ``Station.devices`` is the paired list, preceded, for a standalone station (a
    battery camera without a HomeBase), by the station's own device view, whose
    serial is the station's. Such a station is its own camera, so it gets the
    camera, button and detection entities, and a push naming its own serial routes
    to them. A device with no serial has no identity, so no entity and no route.
    """
    return {
        device.device_sn: device_kind(device.device_sn)
        for device in station.devices
        if device.device_sn
    }


def has_detection_entities(kind: DeviceKind | None) -> bool:
    """Whether a device of ``kind`` gets detection entities."""
    return kind in DETECTION_ENTITY_KINDS


def has_preset_entities(serial: str) -> bool:
    """Whether the camera ``serial`` gets the pan/tilt preset entities.

    The same test the library's own preset channel lookup applies: the model's
    profile supports ``Capability.PTZ_PRESETS`` other than ``UNKNOWN``, so the
    integration offers exactly what the library accepts. Never a serial prefix.
    """
    profile = profile_for_serial(serial)
    return profile is not None and profile.support(Capability.PTZ_PRESETS) is not Support.UNKNOWN


def has_pan_tilt_control(serial: str) -> bool:
    """Whether the camera ``serial`` gets the pan/tilt step buttons.

    The library's own test for ``Station.async_pan_tilt``: the model's profile
    supports ``Capability.PTZ_CONTROL`` other than ``UNKNOWN``.
    """
    profile = profile_for_serial(serial)
    return profile is not None and profile.support(Capability.PTZ_CONTROL) is not Support.UNKNOWN


def has_zoom(serial: str) -> bool:
    """Whether the camera ``serial`` gets the live-view zoom.

    The library's own test for ``Station.async_set_zoom``: the model's profile
    supports ``Capability.PTZ_ZOOM`` other than ``UNKNOWN``.
    """
    profile = profile_for_serial(serial)
    return profile is not None and profile.support(Capability.PTZ_ZOOM) is not Support.UNKNOWN


def zoom_signal(entry_id: str, device_sn: str) -> SignalType[()]:
    """The dispatcher signal telling a camera's zoom entity its live-view zoom changed.

    The name is never logged or stored.
    """
    return SignalType(f"{DOMAIN}_zoom_{entry_id}_{device_sn}")


def has_live_stream(serial: str) -> bool:
    """Whether the camera ``serial`` gets a live video stream.

    The same test the library's own live open applies: the model's profile supports
    ``Capability.LIVE_STREAM`` other than ``UNKNOWN``, so the integration offers
    exactly what the library accepts. Never a serial prefix.
    """
    profile = profile_for_serial(serial)
    return profile is not None and profile.support(Capability.LIVE_STREAM) is not Support.UNKNOWN


def has_doorbell_entity(kind: DeviceKind | None) -> bool:
    """Whether a device of ``kind`` gets the doorbell ring event.

    Only the library catalog's DOORBELL, never a serial-prefix list. The catalog
    has no doorbell model, so no device gets one.
    """
    return kind is DeviceKind.DOORBELL


def detection_event_type(event: SecurityEvent) -> str | None:
    """The detection event type ``event`` fires; None when it is not a detection event."""
    detection = event.detection
    if detection is None:
        return None
    return DETECTION_EVENT_TYPES.get(detection)


def device_event_consumed(kind: DeviceKind | None, event: SecurityEvent) -> bool:
    """Whether an entity of a paired device of ``kind`` consumes the device event ``event``.

    Consumed: a catalogued detection on a device with detection entities, and a
    doorbell press on a doorbell. A press from anything else goes to the
    fallback. The caller has already checked that the device is paired to the station
    that delivered the event.
    """
    if has_doorbell_entity(kind) and event.detection is DetectionType.DOORBELL_PRESS:
        return True
    return has_detection_entities(kind) and detection_event_type(event) is not None


def snapshot_wanted(kind: DeviceKind | None, event: SecurityEvent) -> bool:
    """Whether a device event asks for its camera's still.

    A catalogued detection of a device with detection entities (the camera entity is
    built for the same devices) that the library can find a still for: a ``record_id``
    alone lets the library find the thumbnail in the event's history row, and a bound
    thumbnail or recording (its trigger frame) names one directly. A crop is not a
    thumbnail source. An enriching copy counts: it is the same detection with what an
    earlier copy lacked. This reads library attributes only, no payload keys.
    The caller has already checked that the device is paired to the station that
    delivered the event.
    """
    if not has_detection_entities(kind) or detection_event_type(event) is None:
        return False
    return (
        event.record_id is not None or event.thumb_path is not None or event.video_path is not None
    )


def standalone_image_wanted(
    station: Station, kind: DeviceKind | None, event: SecurityEvent
) -> bool:
    """Whether a device event asks a standalone camera for its detection images.

    A catalogued detection of a device with detection entities on a standalone station
    (a battery camera without a HomeBase) with no thumbnail to look up (no
    ``record_id``, no ``thumb_path``). A recording path does not count: such a station
    lists no recordings, and the push pair's second copy, which names one, is the
    same detection. Its images wake the camera.
    """
    if not station.is_standalone:
        return False
    if not has_detection_entities(kind) or detection_event_type(event) is None:
        return False
    return event.record_id is None and event.thumb_path is None


def known_guard_mode(event: SecurityEvent) -> GuardMode | None:
    """The guard mode ``event`` reports; None when it names none or a code eufy never defined."""
    if event.guard_mode is None:
        return None
    try:
        return GuardMode(event.guard_mode)
    except ValueError:
        return None


def effective_guard_mode(event: SecurityEvent) -> GuardMode | None:
    """The guard mode in force an arming push reports; None when it names no known mode.

    The push's ``guard_mode`` is the selected mode, ``SCHEDULE`` while a schedule
    drives the station, and its ``mode`` the slot's mode in force. The panel shows
    the mode in force, so the arming event names that one too and the two never
    disagree. A push under Schedule without a known ``mode`` names
    Schedule itself.
    """
    selected = known_guard_mode(event)
    if selected is not GuardMode.SCHEDULE or event.mode is None:
        return selected
    try:
        return GuardMode(event.mode)
    except ValueError:
        return selected


def mode_label(mode: GuardMode | int | None) -> str | None:
    """A guard mode as a state-attribute value (``schedule``); None for an unknown code."""
    return mode.name.lower() if isinstance(mode, GuardMode) else None


def station_event_consumed(event: SecurityEvent) -> bool:
    """Whether a station entity consumes the station push ``event``.

    Any alarm phase is consumed, whatever the cipher: an ECB trigger or delay
    fires the alarm event marked unauthenticated, and the panel ignores it on its own
    ``authenticated`` check. An unauthenticated stop never gets here: the
    library reads it as no phase, and :func:`dropped_without_trace` removed it.
    An arming push is consumed only when authenticated and naming a known guard mode:
    a forgeable or modeless one goes to the fallback bus event instead.
    """
    if event.alarm_phase is not None:
        return True
    return (
        event.authenticated
        and event.message_type is PushMessageType.ARMING
        and known_guard_mode(event) is not None
    )


def dropped_without_trace(event: SecurityEvent) -> bool:
    """Whether ``event`` fires nothing at all: no entity event and no fallback bus event.

    An enriching copy is an occurrence already delivered, arriving again with media
    paths. An alarm push whose phase the library withholds is an
    unauthenticated stop, which a forged ECB frame could send.
    """
    if event.enriches:
        return True
    return event.message_type is PushMessageType.ALARM and event.alarm_phase is None


def hold_remaining_seconds(event_time_ms: int | None, hold_seconds: float, now_ms: float) -> float:
    """How much of a detection's hold is left at ``now_ms``; zero or less when it ran out.

    The hold runs from the detection's own time, not its arrival, so a
    detection delivered late is held for less, and one delivered after its hold is
    not held at all. A time the library rejected or never had (None) is held from
    arrival. A time ahead of the clock ages nothing, so it never extends the
    hold past ``hold_seconds``, whatever time a push claims.
    """
    if event_time_ms is None:
        return hold_seconds
    return hold_seconds - max(0.0, now_ms - event_time_ms) / 1000


def triggered_at(event: SecurityEvent) -> str | None:
    """When the device raised ``event``, as an ISO timestamp; never its arrival time."""
    if event.event_time_ms is None:
        return None
    return dt_util.utc_from_timestamp(event.event_time_ms / 1000).isoformat(timespec="milliseconds")


def detection_attributes(event: SecurityEvent) -> dict[str, str]:
    """The only attributes a detection event carries, built fresh from an allow-list.

    The detection's own time, and the recognised name of an identified person.
    Never a serial, a media path, the device name, ``user_name``, an account id or
    anything raw.
    """
    attributes: dict[str, str] = {}
    when = triggered_at(event)
    if when is not None:
        attributes[ATTR_TRIGGERED_AT] = when
    if event.detection is DetectionType.IDENTITY_PERSON and event.person_name:
        attributes[ATTR_PERSON_NAME] = event.person_name
    return attributes


def device_signal(entry_id: str, serial: str) -> SignalType[SecurityEvent]:
    """The dispatcher signal of one paired device's events in one entry.

    Keyed by serial, never by channel: a re-paired device keeps its serial and
    changes its channel. The name is never logged or stored.
    """
    return SignalType(f"{DOMAIN}_security_event_{entry_id}_{serial}")


def station_signal(entry_id: str, station_sn: str) -> SignalType[SecurityEvent]:
    """The dispatcher signal of one station's own pushes (arming, alarm) in one entry.

    The name is never logged or stored.
    """
    return SignalType(f"{DOMAIN}_station_event_{entry_id}_{station_sn}")


def alarm_signal(entry_id: str, station_sn: str) -> SignalType[AlarmChanged]:
    """The dispatcher signal of one station's alarm transitions in one entry.

    The name is never logged or stored.
    """
    return SignalType(f"{DOMAIN}_alarm_changed_{entry_id}_{station_sn}")
