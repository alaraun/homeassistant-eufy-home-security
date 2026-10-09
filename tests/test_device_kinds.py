"""Entities per device kind of the library's model list.

Only a camera or a doorbell gets camera, image, detection, button, preset or
pan/tilt entities. Every other paired device (keypad, lock, sensor, any other
product) gets its device entry with the status diagnostics, its firmware entity and
the settings the library lists for its model. A model with the library's generic
profile (every capability unknown) gets no live stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Final

import pytest
from conftest import PUSHED_THUMB_PATH, PUSHED_THUMBNAIL, set_guard_mode, set_up_warm
from eufy_home_security import EufySecurity, entity_unique_id
from eufy_home_security.devices import (
    Capability,
    DeviceKind,
    Support,
    model_for_serial,
    profile_for_serial,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security import runtime
from custom_components.eufy_home_security.const import (
    BATTERY_KEY,
    CAMERA_KEY,
    CAPTURE_LIVE_IMAGE_KEY,
    DEFAULT_PRESET_KEY,
    DETECTION_EVENT_KEY,
    DOMAIN,
    DOORBELL_EVENT_KEY,
    FIRMWARE_KEY,
    LIVE_PRESET_KEY,
    LIVE_ZOOM_KEY,
    MODEL_KEY,
    MOTION_DETECTED_KEY,
    PERSON_DETECTED_KEY,
    PET_DETECTED_KEY,
    REFRESH_IMAGE_KEY,
    SIGNAL_STRENGTH_KEY,
    VEHICLE_DETECTED_KEY,
)

# Synthetic serials on real product prefixes of the library's model list.
KEYPAD_SN: Final = "T8960P0000000001"
LOCK_SN: Final = "T8510P0000000001"
OTHER_SN: Final = "T7401P0000000001"
ENTRY_SENSOR_SN: Final = "T8900P0000000001"
DOORBELL_SN: Final = "T8210P0000000001"
# A camera with the generic camera profile (eufyCam 2C).
GENERIC_CAMERA_SN: Final = "T8113P0000000001"
# An Indoor Cam 2K Pan & Tilt: a live open only as a standalone camera.
INDOOR_PT_SN: Final = "T8410P0000000001"
# A HomeBase 2 with the synthetic station's tail, so its key derivation is the fake's.
HOMEBASE_2_SN: Final = "T8010" + SYNTHETIC.station_sn[5:]

# The entity of every pan/tilt camera feature: none appears on a generic profile.
_PTZ_KEYS: Final = frozenset({DEFAULT_PRESET_KEY, LIVE_PRESET_KEY, LIVE_ZOOM_KEY})

# Every entity a camera or doorbell gets for its detections and stills.
_CAMERA_ENTITIES: Final = frozenset(
    {
        ("camera", CAMERA_KEY),
        ("binary_sensor", MOTION_DETECTED_KEY),
        ("binary_sensor", PERSON_DETECTED_KEY),
        ("binary_sensor", PET_DETECTED_KEY),
        ("binary_sensor", VEHICLE_DETECTED_KEY),
        ("event", DETECTION_EVENT_KEY),
        ("button", CAPTURE_LIVE_IMAGE_KEY),
        ("button", REFRESH_IMAGE_KEY),
    }
)

# The status diagnostics and firmware entity every paired device gets.
_STATUS_ENTITIES: Final = frozenset(
    {
        ("sensor", BATTERY_KEY),
        ("sensor", SIGNAL_STRENGTH_KEY),
        ("sensor", FIRMWARE_KEY),
        ("sensor", MODEL_KEY),
        ("update", FIRMWARE_KEY),
    }
)

# Platforms whose entities on a device are only ever camera ones.
_CAMERA_ONLY_DOMAINS: Final = frozenset({"camera", "image", "event", "button"})


@pytest.fixture
async def fake_station(request: pytest.FixtureRequest) -> AsyncIterator[FakeStation]:
    """The conftest station, with the serial a test passes indirectly (default HomeBase 3)."""
    station = FakeStation(serial=getattr(request, "param", SYNTHETIC.station_sn))
    set_guard_mode(station, station.guard_mode)
    station.images[PUSHED_THUMB_PATH] = PUSHED_THUMBNAIL
    await station.start()
    yield station
    station.stop()


def _pair(station: FakeStation, cloud: FakeCloud, serial: str) -> None:
    """Pair ``serial`` to ``station`` on channel 1, before setup."""
    cloud.devices.append(camera_device(serial, station_sn=station.serial, channel=1, name="Dev"))


def _entities_of(hass: HomeAssistant, serial: str) -> set[tuple[str, str]]:
    """Every registered ``(domain, key)`` on the device ``serial``."""
    prefix = entity_unique_id(serial, "x")[:-1]
    return {
        (entity.domain, entity.unique_id.removeprefix(prefix))
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN and entity.unique_id.startswith(prefix)
    }


@pytest.mark.parametrize(
    ("serial", "kind"),
    [
        (KEYPAD_SN, DeviceKind.KEYPAD),
        (LOCK_SN, DeviceKind.LOCK),
        (OTHER_SN, DeviceKind.OTHER),
        (ENTRY_SENSOR_SN, DeviceKind.SENSOR),
    ],
    ids=["keypad", "lock", "other", "sensor"],
)
async def test_a_device_other_than_a_camera_gets_status_entities_and_nothing_of_a_camera(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    serial: str,
    kind: DeviceKind,
) -> None:
    """A keypad, lock, sensor or other product: a device with its status, no camera.

    Its settings entities are what the library lists for its model; none of them is
    a camera, image, detection, button, preset or zoom entity.
    """
    model = model_for_serial(serial)
    assert model is not None and model.kind is kind
    _pair(fake_station, fake_cloud, serial)
    entry = await set_up_warm(hass, seed_warm_cache)

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    assert device.model == model.name
    entities = _entities_of(hass, serial)
    assert _STATUS_ENTITIES <= entities
    assert not {domain for domain, _ in entities} & _CAMERA_ONLY_DOMAINS
    assert not {key for _, key in entities} & (
        {key for _, key in _CAMERA_ENTITIES} | _PTZ_KEYS | {DOORBELL_EVENT_KEY}
    )


async def test_a_doorbell_gets_the_camera_entities_and_the_ring_event(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A doorbell is a camera that rings: camera, detections, stills and its ring event."""
    _pair(fake_station, fake_cloud, DOORBELL_SN)
    await set_up_warm(hass, seed_warm_cache)

    entities = _entities_of(hass, DOORBELL_SN)
    assert _CAMERA_ENTITIES | _STATUS_ENTITIES | {("event", DOORBELL_EVENT_KEY)} <= entities
    assert not {key for _, key in entities} & _PTZ_KEYS


def _camera_state(hass: HomeAssistant, serial: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        CAMERA_DOMAIN, DOMAIN, entity_unique_id(serial, CAMERA_KEY)
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


@pytest.mark.parametrize("fake_station", [HOMEBASE_2_SN], indirect=True)
async def test_a_generic_camera_behind_a_homebase_2_gets_a_live_stream(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A generic-profile camera whose live open the library sends behind its station.

    The stream feature and the broadcast follow ``Station.live_support``; pan/tilt
    entities still follow the profile, which grades them unknown.
    """
    _pair(fake_station, fake_cloud, GENERIC_CAMERA_SN)
    entry = await set_up_warm(hass, seed_warm_cache)

    entities = _entities_of(hass, GENERIC_CAMERA_SN)
    assert _CAMERA_ENTITIES | _STATUS_ENTITIES <= entities
    assert not {key for _, key in entities} & _PTZ_KEYS
    state = _camera_state(hass, GENERIC_CAMERA_SN)
    assert state.attributes["supported_features"] & CameraEntityFeature.STREAM
    streams = runtime.streaming(entry)
    assert streams is not None
    assert streams.has_camera(GENERIC_CAMERA_SN)
    assert streams.has_camera(SYNTHETIC.camera_sn)


async def test_a_camera_whose_open_the_library_lacks_behind_its_station_gets_no_stream(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The gate is the device on its station, not the model alone.

    A T8410's profile grades live video declared (its standalone open), but the
    library has no open for it behind a HomeBase: camera entity, no stream feature,
    no broadcast.
    """
    model_profile = profile_for_serial(INDOOR_PT_SN)
    assert model_profile is not None
    assert model_profile.support(Capability.LIVE_STREAM) is not Support.UNKNOWN
    _pair(fake_station, fake_cloud, INDOOR_PT_SN)
    entry = await set_up_warm(hass, seed_warm_cache)

    station = entry.runtime_data.coordinators[fake_station.serial].station
    assert station.live_support(INDOOR_PT_SN).support is Support.UNKNOWN
    assert _CAMERA_ENTITIES <= _entities_of(hass, INDOOR_PT_SN)
    state = _camera_state(hass, INDOOR_PT_SN)
    assert not state.attributes["supported_features"] & CameraEntityFeature.STREAM
    streams = runtime.streaming(entry)
    assert streams is not None
    assert not streams.has_camera(INDOOR_PT_SN)
    assert streams.has_camera(SYNTHETIC.camera_sn)
