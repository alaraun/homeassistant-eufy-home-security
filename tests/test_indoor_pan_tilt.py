"""Tests for a standalone T8410 (indoor pan/tilt camera): the RSA session, one-step
pan/tilt, live video, and no presets or zoom."""

from typing import Any

import pytest
from conftest import entity_id_for, set_up_warm
from eufy_home_security import Station
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security import detections
from custom_components.eufy_home_security.const import DOMAIN

SN = "T8410P0000000001"
CHANNEL = 0


def indoor_entry() -> dict[str, Any]:
    return {
        "device_sn": SN,
        "device_type": 31,
        "device_name": "Indoor",
        "parent_sn": SN,
        "device_channel": CHANNEL,
        "p2p_did": SYNTHETIC.did,
        "local_ip": SYNTHETIC.station_ip,
        "params": [{"param_type": 1216, "param_value": "Indoor", "update_time": 1.7e9}],
    }


@pytest.fixture
async def fake_station():
    s = FakeStation(
        serial=SN,
        receipt_len=STANDALONE_RECEIPT_LEN,
        conn_init_version=1,
        sd_info=(0, 32000, 16000),
        params={CHANNEL: {1142: "-40"}, 255: {1216: "Indoor"}},
    )
    await s.start()
    yield s
    s.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    return FakeCloud(
        devices=[indoor_entry()],
        owner_ids={SN: fake_station.account_id},
        rsa_cipher_keys={SN: fake_station.rsa_private_key_pem},
    )


@pytest.fixture
def fast_pan_tilt(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = Station.async_pan_tilt
    monkeypatch.setattr(
        Station,
        "async_pan_tilt",
        lambda self, sn, direction, **kw: orig(self, sn, direction, settle=0),
    )


def _unique_keys(hass: HomeAssistant) -> set[str]:
    return {
        entity.unique_id.split("_", 1)[1]
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN and entity.unique_id.startswith(f"{SN}_")
    }


def test_the_t8410_has_live_video_and_pan_tilt_but_no_presets_or_zoom() -> None:
    assert detections.has_live_stream(SN) is True
    assert detections.has_pan_tilt_control(SN) is True
    assert detections.has_preset_entities(SN) is False
    assert detections.has_zoom(SN) is False


async def test_a_t8410_gets_a_stream_and_step_buttons_and_no_preset_or_zoom_entities(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    camera_id = entity_id_for(hass, "camera", SN, "camera")
    assert camera_id is not None
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes["supported_features"] & CameraEntityFeature.STREAM

    keys = _unique_keys(hass)
    assert {"pan_left", "pan_right", "tilt_up", "tilt_down"} <= keys
    assert not {"refresh_presets", "save_view", "live_zoom", "default_preset", "live_preset"} & keys
    assert not any("_preset_" in key or key.startswith("preset_") for key in keys)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_t8410_pan_tilt_step_reaches_the_camera_over_the_rsa_session(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_pan_tilt
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    button_id = entity_id_for(hass, "button", SN, "pan_left")
    assert button_id is not None

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    assert fake_station.rsa_session
    assert fake_station.pan_tilts == [1]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("goto_preset", {"preset": 0}),
        ("save_preset", {}),
        ("delete_preset", {"preset": 0}),
        ("capture_preset", {"preset": 0}),
    ],
)
async def test_a_t8410_refuses_every_preset_action_and_sends_nothing(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    action: str,
    data: dict[str, Any],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    camera_id = entity_id_for(hass, "camera", SN, "camera")

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, action, {"entity_id": camera_id, **data}, blocking=True
        )

    assert exc.value.translation_key == "presets_unsupported"
    assert fake_station.doorbell_payloads == []
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_t8410_refuses_the_zoom_action_as_no_zoom(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    camera_id = entity_id_for(hass, "camera", SN, "camera")

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, "zoom", {"entity_id": camera_id, "direction": "in"}, blocking=True
        )

    assert exc.value.translation_key == "zoom_unsupported"
    assert fake_station.zoom_writes == []
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
