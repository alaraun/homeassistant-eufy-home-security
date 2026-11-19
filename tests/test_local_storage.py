"""A device's local medium from its status answer (``SDINFO_EX``): whether it is usable
and its state, on a standalone camera (T8410)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from conftest import entity_id_for, set_up_warm, state_of
from eufy_home_security import EufySecurity, MediumState, entity_unique_id
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.eufy_home_security.const import (
    DOMAIN,
    EMMC_FREE_KEY,
    EMMC_PROBLEM_KEY,
    EMMC_SIZE_KEY,
    EMMC_STATE_KEY,
    EMMC_USABLE_KEY,
    EMMC_USED_SPACE_KEY,
    EMMC_WEAR_KEY,
)
from custom_components.eufy_home_security.sensor import EMMC_STATE_OPTIONS, MEDIUM_NOT_USABLE

SN = "T8410P0000000001"
CHANNEL = 0
_STORAGE_LOGGER = "custom_components.eufy_home_security.storage"


def _indoor_entry() -> dict[str, Any]:
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
def sd_info() -> tuple[int, int, int]:
    """The camera's status answer: status code, total and free size (MB)."""
    return (0, 32000, 16000)


@pytest.fixture
async def fake_station(sd_info: tuple[int, int, int]):
    station = FakeStation(
        serial=SN,
        receipt_len=STANDALONE_RECEIPT_LEN,
        conn_init_version=1,
        sd_info=sd_info,
        params={CHANNEL: {1142: "-40"}, 255: {1216: "Indoor"}},
    )
    await station.start()
    yield station
    station.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    return FakeCloud(
        devices=[_indoor_entry()],
        owner_ids={SN: fake_station.account_id},
        rsa_cipher_keys={SN: fake_station.rsa_private_key_pem},
    )


def _state(hass: HomeAssistant, domain: str, key: str) -> str | None:
    entity_id = entity_id_for(hass, domain, SN, key)
    return None if entity_id is None else state_of(hass, entity_id)


def _registered(hass: HomeAssistant, domain: str, key: str) -> bool:
    return (
        er.async_get(hass).async_get_entity_id(domain, DOMAIN, entity_unique_id(SN, key))
        is not None
    )


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("sd_info", [(4, 0, 0)])
async def test_no_card_is_a_result_not_an_error(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A status answer without a usable medium: usable off, the library's state, the
    sizes unknown; no problem or wear entity, no failed read and no ERROR."""
    caplog.set_level(logging.DEBUG, logger=_STORAGE_LOGGER)
    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.storage[SN]
    assert coordinator.last_update_success
    medium = coordinator.data.emmc
    assert medium is not None
    assert medium.usable is False
    assert medium.state is not MediumState.NORMAL

    assert _state(hass, "binary_sensor", EMMC_USABLE_KEY) == STATE_OFF
    assert _state(hass, "sensor", EMMC_STATE_KEY) == medium.state.value
    for key in (EMMC_USED_SPACE_KEY, EMMC_FREE_KEY):
        assert _state(hass, "sensor", key) == STATE_UNKNOWN
    assert _registered(hass, "sensor", EMMC_SIZE_KEY)  # off by default
    assert not _registered(hass, "binary_sensor", EMMC_PROBLEM_KEY)
    assert not _registered(hass, "sensor", EMMC_WEAR_KEY)
    assert not [
        r for r in caplog.records if r.name == _STORAGE_LOGGER and r.levelno >= logging.WARNING
    ]
    await _unload(hass, entry)


async def test_a_usable_medium_shows_normal_and_its_sizes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _state(hass, "binary_sensor", EMMC_USABLE_KEY) == STATE_ON
    assert _state(hass, "sensor", EMMC_STATE_KEY) == MediumState.NORMAL.value
    medium = entry.runtime_data.storage[SN].data.emmc
    assert medium is not None
    assert medium.used_gib is not None
    assert _state(hass, "sensor", EMMC_USED_SPACE_KEY) == str(medium.used_gib)
    await _unload(hass, entry)


@pytest.mark.parametrize("sd_info", [(0, 0, 0)])
async def test_a_normal_status_without_a_size_reads_not_usable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``normal`` only while usable: a total of 0 shows ``not_usable``."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _state(hass, "binary_sensor", EMMC_USABLE_KEY) == STATE_OFF
    assert _state(hass, "sensor", EMMC_STATE_KEY) == MEDIUM_NOT_USABLE
    await _unload(hass, entry)


@pytest.mark.parametrize("sd_info", [(4, 0, 0)])
async def test_a_card_that_turns_up_fills_the_size_entities_without_a_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _state(hass, "sensor", EMMC_FREE_KEY) == STATE_UNKNOWN

    fake_station.sd_info = (0, 32000, 16000)
    await entry.runtime_data.storage[SN].async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, "binary_sensor", EMMC_USABLE_KEY) == STATE_ON
    assert _state(hass, "sensor", EMMC_STATE_KEY) == MediumState.NORMAL.value
    assert _state(hass, "sensor", EMMC_FREE_KEY) not in (STATE_UNKNOWN, None)
    await _unload(hass, entry)


def test_the_state_options_cover_every_library_state() -> None:
    assert set(EMMC_STATE_OPTIONS) == {state.value for state in MediumState} | {MEDIUM_NOT_USABLE}
