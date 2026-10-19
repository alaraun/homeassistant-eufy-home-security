"""Cloud detection pushes: a later push naming a more specific kind, and push pictures.

A push without a ``unique_id`` (a HomeBase 2's) is keyed by its ``session_id``; the
library's de-duplicator admits a later push of the same detection that names a more
specific kind as an enrichment. The router dispatches that one again. A push that names
a picture (a face id, a ``pic_url``) and no still to look up shows that picture,
fetched through ``EufySecurity.async_push_image``, which is replaced here: the fake
cloud serves no pictures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

import pytest
from conftest import (
    SYNTHETIC,
    detection_event,
    entity_id_for,
    now_ms,
    record_states,
    set_up_warm,
    wait_until,
)
from eufy_home_security import (
    DetectionType,
    EmptyResponseError,
    EufySecurity,
    FrameCipher,
    PushImage,
    PushMessageType,
    PushPictureSource,
    SecurityEvent,
)
from eufy_home_security.testing import FakeStation
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.eufy_home_security.const import (
    CONF_CAMERA_IMAGE,
    DETECTION_EVENT_KEY,
    EVENT_EUFY_HOME_SECURITY,
    MOTION_DETECTED_KEY,
    PERSON_DETECTED_KEY,
)
from custom_components.eufy_home_security.snapshots import SnapshotManager

FACE_JPEG: Final = b"\xff\xd8FACE-CROP\xff\xd9"
SCENE_JPEG: Final = b"\xff\xd8PUSH-PICTURE\xff\xd9"
PIC_URL: Final = "https://picture.example/event.jpg"
FACE_FETCH_ID: Final = "1a2b3c"


def _push(
    detection: DetectionType,
    t_ms: int,
    *,
    cipher: FrameCipher | None = None,
    enriches: bool = False,
    session_id: str = "session-1",
    **fields: Any,
) -> SecurityEvent:
    """A HomeBase 2 style detection push: msg_type 1, no ``unique_id``, one session."""
    return detection_event(
        detection,
        t_ms=t_ms,
        msg_type=PushMessageType.SECURITY,
        cipher=cipher,
        session_id=session_id,
        enriches=enriches,
        **fields,
    )


def _admit(eufy: EufySecurity, entry: MockConfigEntry, event: SecurityEvent) -> bool:
    """Deliver ``event`` as the client does: through its de-duplicator to the router."""
    deduplicator = eufy.deduplicator
    assert deduplicator is not None
    admitted = deduplicator.admit(event)
    if admitted is not None:
        entry.runtime_data.router.handle(admitted)
    return admitted is not None


def _fired(states: list[str]) -> list[str]:
    """The detection event timestamps among ``states``, unavailable ones left out."""
    return [state for state in states if state not in ("unavailable", "unknown")]


@pytest.fixture
def pictures(monkeypatch: pytest.MonkeyPatch) -> list[PushPictureSource]:
    """Replace the client's push picture fetch; returns the sources asked for, in order.

    A face fetch answers ``FACE_JPEG``, a pic_url fetch ``SCENE_JPEG``. Patched on the
    class before setup, so the snapshot manager's bound fetch is this one.
    """
    asked: list[PushPictureSource] = []

    async def fetch(
        self: EufySecurity, event: SecurityEvent, source: PushPictureSource | None = None
    ) -> PushImage:
        assert source is not None
        asked.append(source)
        data = FACE_JPEG if source is PushPictureSource.FACE else SCENE_JPEG
        return PushImage(
            source=source, device_sn=event.device_sn, data=data, content_type="image/jpeg"
        )

    monkeypatch.setattr(EufySecurity, "async_push_image", fetch)
    return asked


def _manager(entry: MockConfigEntry) -> SnapshotManager:
    manager = entry.runtime_data.snapshots
    assert manager is not None
    return manager


# ── a more specific kind of a delivered detection ─────────────────────────────


async def test_a_later_push_naming_a_person_fires_the_person_event_and_sensor(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Motion first, then the same session with a face id: the person sensor turns on and
    the detection event fires ``person``; a later motion copy fires nothing."""
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = built_clients[-1]
    event_id = entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DETECTION_EVENT_KEY)
    detection_states = record_states(hass, event_id)
    motion_states = record_states(
        hass, entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.camera_sn, MOTION_DETECTED_KEY)
    )
    person_states = record_states(
        hass, entity_id_for(hass, BINARY_SENSOR_DOMAIN, SYNTHETIC.camera_sn, PERSON_DETECTED_KEY)
    )
    fallback = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)
    t = now_ms()

    assert _admit(eufy, entry, _push(DetectionType.MOTION, t))
    await hass.async_block_till_done()
    assert len(_fired(detection_states)) == 1
    state = hass.states.get(event_id)
    assert state is not None
    assert state.attributes["event_type"] == "motion"

    upgraded = _push(DetectionType.MOTION, t, fetch_id=FACE_FETCH_ID)
    assert upgraded.detection is DetectionType.PERSON
    assert _admit(eufy, entry, upgraded), "the library did not admit the person copy"
    await hass.async_block_till_done()

    assert len(_fired(detection_states)) == 2
    state = hass.states.get(event_id)
    assert state is not None
    assert state.attributes["event_type"] == "person"
    assert person_states == [STATE_ON]
    assert motion_states == [STATE_ON]

    # The router drops an enrichment the library admits for a new path whose kind is
    # less specific, or as specific as the one dispatched.
    for kind in (DetectionType.MOTION, DetectionType.PERSON):
        entry.runtime_data.router.handle(_push(kind, t, crop_path="/zx/crop.jpg", enriches=True))
    await hass.async_block_till_done()
    assert len(_fired(detection_states)) == 2
    assert fallback == []


async def test_an_upgrade_outside_the_consuming_window_fires_no_fallback(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A more specific copy that no entity receives is no new detection: no bus event."""
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    fallback = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)
    t = now_ms()
    router.handle(_push(DetectionType.MOTION, t))
    await hass.async_block_till_done()

    router.async_stop_consuming()
    router.handle(_push(DetectionType.MOTION, t, fetch_id=FACE_FETCH_ID, enriches=True))
    await hass.async_block_till_done()

    assert fallback == []


async def test_an_upgrade_of_an_occurrence_never_dispatched_fires_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An enriching person copy whose first copy no entity received is no detection."""
    entry = await set_up_warm(hass, seed_warm_cache)
    detection_states = record_states(
        hass, entity_id_for(hass, EVENT_DOMAIN, SYNTHETIC.camera_sn, DETECTION_EVENT_KEY)
    )
    fallback = async_capture_events(hass, EVENT_EUFY_HOME_SECURITY)

    entry.runtime_data.router.handle(
        _push(DetectionType.MOTION, now_ms(), fetch_id=FACE_FETCH_ID, enriches=True)
    )
    await hass.async_block_till_done()

    assert detection_states == []
    assert fallback == []


# ── the push's own picture ─────────────────────────────────────────────────────


async def test_a_push_naming_a_face_shows_its_face_crop(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pictures: list[PushPictureSource],
) -> None:
    """No still to look up, a face id: the face crop is the camera's image, no station query."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)
    queries = len(fake_station.history_queries)

    entry.runtime_data.router.handle(_push(DetectionType.MOTION, now_ms(), fetch_id=FACE_FETCH_ID))
    await wait_until(lambda: manager.source_for(SYNTHETIC.camera_sn) == "push_picture")

    assert manager.image_for(SYNTHETIC.camera_sn) == FACE_JPEG
    assert pictures == [PushPictureSource.FACE]
    assert len(fake_station.history_queries) == queries


async def test_an_enriching_copy_fetches_only_the_source_it_adds(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pictures: list[PushPictureSource],
) -> None:
    """Face first, then the same detection with a pic_url: the pic_url alone is fetched."""
    entry = await set_up_warm(hass, seed_warm_cache)
    eufy = built_clients[-1]
    manager = _manager(entry)
    t = now_ms()

    assert _admit(eufy, entry, _push(DetectionType.MOTION, t, fetch_id=FACE_FETCH_ID))
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == FACE_JPEG)
    assert _admit(
        eufy, entry, _push(DetectionType.MOTION, t, fetch_id=FACE_FETCH_ID, pic_url=PIC_URL)
    )
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == SCENE_JPEG)

    assert pictures == [PushPictureSource.FACE, PushPictureSource.PIC_URL]


async def test_a_failed_source_falls_to_the_next_one(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pictures: list[PushPictureSource],
) -> None:
    """A face the cloud keeps no picture of, and a pic_url that is no JPEG: nothing shown;
    the next detection's JPEG pic_url is shown."""
    asked: list[PushPictureSource] = []
    scene: list[tuple[bytes, str]] = [(b"GIF89a", "image/gif")]

    async def fetch(
        self: EufySecurity, event: SecurityEvent, source: PushPictureSource | None = None
    ) -> PushImage:
        assert source is not None
        asked.append(source)
        if source is PushPictureSource.FACE:
            raise EmptyResponseError(0, "no face picture")
        data, content_type = scene[0]
        return PushImage(
            source=source, device_sn=event.device_sn, data=data, content_type=content_type
        )

    monkeypatch.setattr(EufySecurity, "async_push_image", fetch)
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        _push(DetectionType.MOTION, now_ms(), fetch_id=FACE_FETCH_ID, pic_url=PIC_URL)
    )
    await wait_until(lambda: len(asked) == 2 and not manager.busy)
    assert manager.image_for(SYNTHETIC.camera_sn) is None

    scene[0] = (SCENE_JPEG, "image/jpeg")
    entry.runtime_data.router.handle(
        _push(DetectionType.MOTION, now_ms() + 1000, session_id="session-2", pic_url=PIC_URL)
    )
    await wait_until(lambda: manager.image_for(SYNTHETIC.camera_sn) == SCENE_JPEG)
    assert manager.source_for(SYNTHETIC.camera_sn) == "push_picture"


async def test_an_unauthenticated_push_fetches_no_picture(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pictures: list[PushPictureSource],
) -> None:
    """An ECB push's pic_url is anyone's on the LAN: never fetched."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = _manager(entry)

    entry.runtime_data.router.handle(
        _push(DetectionType.PERSON, now_ms(), cipher=FrameCipher.ECB, pic_url=PIC_URL)
    )
    await hass.async_block_till_done()
    await wait_until(lambda: not manager.busy)

    assert pictures == []
    assert manager.image_for(SYNTHETIC.camera_sn) is None


@pytest.mark.parametrize(("mode", "shown"), [("thumbnail", True), ("hd_only", False)])
async def test_the_camera_image_option_decides_whether_a_push_picture_shows(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pictures: list[PushPictureSource],
    mode: str,
    shown: bool,
) -> None:
    """Thumbnail only shows it; HD only does not: a push picture is no trigger frame."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: mode})
    manager = _manager(entry)

    entry.runtime_data.router.handle(_push(DetectionType.MOTION, now_ms(), fetch_id=FACE_FETCH_ID))
    await hass.async_block_till_done()
    await wait_until(lambda: not manager.busy)

    assert (manager.image_for(SYNTHETIC.camera_sn) == FACE_JPEG) is shown
    assert pictures == ([PushPictureSource.FACE] if shown else [])
