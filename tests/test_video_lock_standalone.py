"""Detections of a standalone video smart lock: woken for images only with a live stream."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final

import pytest
from conftest import entity_id_for, now_ms, record_states, set_up_warm, wait_until
from eufy_home_security import DetectionType, ImageSource, SecurityEvent, Station
from eufy_home_security.devices import DeviceKind, Support, model_for_serial
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.push.decode import decode_push
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.eufy_home_security import detections
from custom_components.eufy_home_security.const import (
    CONF_CAMERA_IMAGE,
    PERSON_DETECTED_KEY,
    STANDALONE_THUMBNAIL_DELAY_SECONDS,
)

# Synthetic serials on real product prefixes: a video lock with no live open on any
# station, and one the library opens live on its own.
NO_LIVE_SN: Final = "T8530P0000000001"
LIVE_SN: Final = "T8531P0000000001"
# The parameter block a standalone device labels with its device type (synthetic).
DEVICE_TYPE: Final = 51


@pytest.fixture
async def fake_station(request: pytest.FixtureRequest) -> AsyncIterator[FakeStation]:
    """A standalone video lock, the serial passed indirectly (default: no live stream)."""
    station = FakeStation(
        serial=getattr(request, "param", NO_LIVE_SN),
        cipher_id=98,
        receipt_len=STANDALONE_RECEIPT_LEN,
        guard_mode=1,
        params={DEVICE_TYPE: {1101: "61"}, 255: {1216: "Door"}},
    )
    await station.start()
    yield station
    station.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    serial = fake_station.serial
    return FakeCloud(
        devices=[
            {
                "device_sn": serial,
                "device_type": DEVICE_TYPE,
                "device_name": "Door",
                "parent_sn": serial,
                "device_channel": DEVICE_TYPE,
                "p2p_did": SYNTHETIC.did,
                "local_ip": SYNTHETIC.station_ip,
                "params": [{"param_type": 1101, "param_value": "61", "update_time": 1.7e9}],
            }
        ],
        owner_ids={serial: fake_station.account_id},
        cipher_keys={serial: fake_station.ecc_private_key_hex},
    )


def _person_push(serial: str, t_ms: int) -> dict[str, Any]:
    return {
        "msg_type": 18,
        "event_type": int(DetectionType.PERSON),
        "device_sn": serial,
        "station_sn": serial,
        "channel": 0,
        "trigger_time": t_ms,
        "unique_id": "a" * 32,
    }


def _deliver(entry: ConfigEntry, inner: dict[str, Any]) -> None:
    """Hand a cloud push to the router as the client does, through its de-duplicator."""
    event: SecurityEvent = decode_push(
        {
            "station_sn": inner["station_sn"],
            "device_sn": inner["device_sn"],
            "event_time": str(inner["trigger_time"] // 1000 + 3),
            "span_id": f"span-{inner['unique_id']}",
            "payload": base64.b64encode(json.dumps(inner).encode()).decode(),
        }
    )
    deduplicator = entry.runtime_data.eufy.deduplicator
    assert deduplicator is not None
    admitted = deduplicator.admit(event)
    assert admitted is not None
    entry.runtime_data.router.handle(admitted)


def _count_image_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The source of every ``Station`` image call: each one wakes the device."""
    calls: list[str] = []
    for name in ("async_camera_image", "async_event_image"):
        orig = getattr(Station, name)

        async def counting(
            self: Station,
            subject: Any,
            source: ImageSource,
            *args: Any,
            _orig: Any = orig,
            **kw: Any,
        ) -> Any:
            calls.append(source.value)
            return await _orig(self, subject, source, *args, **kw)

        monkeypatch.setattr(Station, name, counting)
    return calls


async def _past_thumbnail_delay(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=STANDALONE_THUMBNAIL_DELAY_SECONDS + 1)
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "extra",
    [{}, {"record_id": 7}, {"thumb_path": "/media/mmcblk0p1/thumb/a.jpg"}],
    ids=["no_picture", "record_id", "thumb_path"],
)
async def test_a_standalone_video_lock_without_live_stream_is_never_woken_for_images(
    extra: dict[str, Any],
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache: Callable[..., None],
    built_clients: list[Any],
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its person detection reaches its entities; no live keyframe, no delayed thumbnail,
    no event image, whatever picture the push names."""
    model = model_for_serial(NO_LIVE_SN)
    assert model is not None and model.kind is DeviceKind.LOCK
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "hd"})
    station = entry.runtime_data.coordinators[NO_LIVE_SN].station
    assert station.is_standalone
    assert not detections.has_live_stream(station, NO_LIVE_SN)
    assert not detections.standalone_wake_allowed(station, NO_LIVE_SN)
    person = record_states(
        hass, entity_id_for(hass, "binary_sensor", NO_LIVE_SN, PERSON_DETECTED_KEY)
    )
    calls = _count_image_calls(monkeypatch)

    _deliver(entry, {**_person_push(NO_LIVE_SN, now_ms()), **extra})
    await hass.async_block_till_done()
    await _past_thumbnail_delay(hass)

    assert person[:1] == ["on"]
    assert calls == []
    assert not entry.runtime_data.snapshots.busy
    assert "a standalone device without a live stream is not woken" in caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("fake_station", [LIVE_SN], indirect=True)
async def test_a_standalone_video_lock_with_a_live_stream_gets_its_detection_image(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache: Callable[..., None],
    built_clients: list[Any],
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
) -> None:
    """As a standalone camera: the detection asks a live keyframe at once."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "hd_only"})
    station = entry.runtime_data.coordinators[LIVE_SN].station
    assert detections.has_live_stream(station, LIVE_SN)
    assert detections.standalone_wake_allowed(station, LIVE_SN)
    calls = _count_image_calls(monkeypatch)

    _deliver(entry, _person_push(LIVE_SN, now_ms()))
    await wait_until(lambda: bool(calls), timeout=20)
    await wait_until(lambda: not entry.runtime_data.snapshots.busy, timeout=20)

    assert calls[:1] == [ImageSource.LIVE.value]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_a_camera_kind_may_always_be_woken() -> None:
    """A model of a camera kind never consults its live stream."""

    class NoLive:
        def live_support(self, serial: str) -> Any:
            raise AssertionError("not asked for a camera kind")

    assert detections.standalone_wake_allowed(NoLive(), "T8170P0000000001")  # type: ignore[arg-type]


def test_standalone_image_wanted_refuses_a_device_it_may_not_wake() -> None:
    """The decision itself refuses a video lock without a live stream, as the router does."""

    class Standalone:
        is_standalone = True

        def live_support(self, serial: str) -> Any:
            return SimpleNamespace(support=Support.UNKNOWN)

    event = decode_push(
        {
            "station_sn": NO_LIVE_SN,
            "device_sn": NO_LIVE_SN,
            "event_time": "1700000000",
            "span_id": "span-b",
            "payload": base64.b64encode(
                json.dumps(_person_push(NO_LIVE_SN, 1_700_000_000_000)).encode()
            ).decode(),
        }
    )
    kind = detections.entity_kind(NO_LIVE_SN)
    assert detections.has_detection_entities(kind)
    assert not detections.standalone_image_wanted(Standalone(), kind, event)  # type: ignore[arg-type]
