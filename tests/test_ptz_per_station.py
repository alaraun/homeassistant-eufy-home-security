"""Pan/tilt, preset and zoom entities follow ``Station.ptz_support``: the camera on its
station, not the model alone."""

from typing import Any, Final

import pytest
from conftest import set_up_warm
from eufy_home_security.devices import Capability, Support, profile_for_serial
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security import detections
from custom_components.eufy_home_security.const import (
    DEFAULT_PRESET_KEY,
    DOMAIN,
    LIVE_PRESET_KEY,
    LIVE_ZOOM_KEY,
    PAN_LEFT_KEY,
    REFRESH_PRESETS_KEY,
)

# A Battery Solo Cam 8172: its handler pans, tilts and zooms only behind a HomeBase.
SN: Final = "T8172P0000000001"
CHANNEL: Final = 0
_PTZ_CAPABILITIES: Final = (Capability.PTZ_CONTROL, Capability.PTZ_PRESETS, Capability.PTZ_ZOOM)


def standalone_entry() -> dict[str, Any]:
    return {
        "device_sn": SN,
        "device_type": 89,
        "device_name": "Solo",
        "parent_sn": SN,
        "device_channel": CHANNEL,
        "p2p_did": SYNTHETIC.did,
        "local_ip": SYNTHETIC.station_ip,
        "params": [{"param_type": 1216, "param_value": "Solo", "update_time": 1.7e9}],
    }


@pytest.fixture
async def fake_station():
    s = FakeStation(
        serial=SN,
        receipt_len=STANDALONE_RECEIPT_LEN,
        conn_init_version=1,
        sd_info=(0, 32000, 16000),
        params={CHANNEL: {1142: "-40"}, 255: {1216: "Solo"}},
    )
    await s.start()
    yield s
    s.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    return FakeCloud(
        devices=[standalone_entry()],
        owner_ids={SN: fake_station.account_id},
        rsa_cipher_keys={SN: fake_station.rsa_private_key_pem},
    )


async def test_a_standalone_camera_gets_no_ptz_entities_its_model_declares_behind_a_homebase(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
) -> None:
    """The model's profile declares all three; on its own the library sends none of them."""
    profile = profile_for_serial(SN)
    assert profile is not None
    assert all(profile.support(c) is Support.DECLARED for c in _PTZ_CAPABILITIES)
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SN].station

    assert all(station.ptz_support(SN, c).support is Support.UNKNOWN for c in _PTZ_CAPABILITIES)
    assert detections.has_pan_tilt_control(station, SN) is False
    assert detections.has_preset_entities(station, SN) is False
    assert detections.has_zoom(station, SN) is False
    keys = {
        entity.unique_id.split("_", 1)[1]
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN and entity.unique_id.startswith(f"{SN}_")
    }
    assert not keys & {
        DEFAULT_PRESET_KEY,
        LIVE_PRESET_KEY,
        LIVE_ZOOM_KEY,
        PAN_LEFT_KEY,
        REFRESH_PRESETS_KEY,
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
