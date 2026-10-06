"""Tests for pan/tilt presets on a standalone T8170."""

import json
import logging
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from conftest import (
    add_entry,
    entity_id_for,
    record_states,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import (
    CommandNotAppliedError,
    CommandRejectedError,
    CommandUnsupportedError,
    DeviceBusyError,
    DeviceTimeoutError,
    PresetSlotsFullError,
    Station,
    UnsupportedError,
    redact_serial,
)
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.image import async_get_image
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from PIL import Image

from custom_components.eufy_home_security import history, presets, snapshots, still_cache
from custom_components.eufy_home_security.const import DOMAIN

SN = "T8170P0000000001"
# The debug line of a live view yielding the media slot, after the camera.
YIELD_REASON = "aborting, a media operation needs the station's one media slot"
FOUR_K_JPEG_PREFIX = b"\xff\xd8\xff\xc0\x00\x11\x08\x08\x70\x0f\x00"


def standalone_entry(battery: str | None = None, t: float | None = None) -> dict:
    return {
        "device_sn": SN,
        "device_type": 48,
        "device_name": "Solo",
        "parent_sn": SN,
        "device_channel": 48,
        "p2p_did": SYNTHETIC.did,
        "local_ip": SYNTHETIC.station_ip,
        "params": [
            {"param_type": 1101, "param_value": battery or "61", "update_time": t or 1.7e9},
            {"param_type": 1216, "param_value": "Solo", "update_time": t or 1.7e9},
            {"param_type": 1224, "param_value": "1", "update_time": t or 1.7e9},
            {"param_type": 1142, "param_value": "-27", "update_time": t or 1.7e9},
            {"param_type": 1131, "param_value": "1", "update_time": t or 1.7e9},
        ],
    }


def _state(hass: HomeAssistant, entity_id: str) -> State:
    """The entity's state object; fails when it has none."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def _registry_entry(hass: HomeAssistant, entity_id: str) -> er.RegistryEntry:
    """The entity's registry entry; fails when it is not registered."""
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None, entity_id
    return entry


@pytest.fixture
async def fake_station():
    s = FakeStation(
        serial=SN,
        cipher_id=98,
        receipt_len=STANDALONE_RECEIPT_LEN,
        guard_mode=1,
        params={
            48: {1224: "1", 1101: "61", 1142: "-27", 1131: "1"},
            255: {1216: "Solo"},
        },
    )
    await s.start()
    yield s
    s.stop()


@pytest.fixture
def fake_cloud(fake_station):
    return FakeCloud(
        devices=[standalone_entry()],
        owner_ids={SN: fake_station.account_id},
        cipher_keys={SN: fake_station.ecc_private_key_hex},
    )


@pytest.fixture(autouse=True)
def slot_read_only_refresh(request, monkeypatch):
    """Refresh presets reads the slots and captures none, unless the test asks for the
    ``recapture`` fixture: most tests press it only to get the slot entities."""
    if "recapture" in request.fixturenames:
        return

    async def no_recapture(self, station, device_sn, indexes) -> None:
        return None

    monkeypatch.setattr(presets.PresetManager, "_async_recapture", no_recapture)


@pytest.fixture
def recapture(fast_settle) -> None:
    """Refresh presets captures every enabled slot, as in production."""


@pytest.fixture
def fast_settle(monkeypatch):
    orig = Station.async_preset_image
    monkeypatch.setattr(
        Station,
        "async_preset_image",
        lambda self, sn, preset, **kw: orig(self, sn, preset, settle=0.2),
    )


async def test_a_cold_cache_gives_a_ptz_camera_only_the_refresh_presets_button(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)

    button_id = entity_id_for(hass, "button", SN, "refresh_presets")
    assert button_id is not None
    assert state_of(hass, button_id) != "unavailable"

    registry = er.async_get(hass)
    entry_reg = _registry_entry(hass, button_id)
    assert entry_reg.entity_category == "config"

    assert len(hass.states.async_entity_ids("image")) == 0

    assert not any(
        "_preset_" in entity.unique_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
    )
    assert fake_station.conn_inits == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_refresh_presets_reads_the_slots_once_and_adds_the_entities(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    button_id = entity_id_for(hass, "button", SN, "refresh_presets")

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    await wait_until(
        lambda: any(p.get("commandType") == 6034 for p in fake_station.doorbell_payloads)
    )

    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    assert fake_station.conn_inits >= 1
    assert sum(1 for p in fake_station.doorbell_payloads if p.get("commandType") == 6034) == 1

    for i in (0, 1, 2):
        img_id = entity_id_for(hass, "image", SN, f"preset_{i}_image")
        btn_id = entity_id_for(hass, "button", SN, f"preset_{i}_capture")
        assert img_id is not None
        assert btn_id is not None
        assert state_of(hass, img_id) == "unknown"
        assert state_of(hass, btn_id) != "unavailable"
        assert (
            _registry_entry(hass, img_id).device_id
            == _registry_entry(hass, entity_id_for(hass, "camera", SN, "camera")).device_id
        )

    for i in range(3, 10):
        assert registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_{i}_image") is None
        assert registry.async_get_entity_id("button", DOMAIN, f"{SN}_preset_{i}_capture") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cached_slots_give_the_entities_at_setup_without_a_wake(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    button_id = entity_id_for(hass, "button", SN, "refresh_presets")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    conn_inits_before = fake_station.conn_inits

    entry2 = add_entry(hass)
    await setup_entry(hass, entry2)
    await hass.async_block_till_done()

    for i in (0, 1, 2):
        img_id = entity_id_for(hass, "image", SN, f"preset_{i}_image")
        btn_id = entity_id_for(hass, "button", SN, f"preset_{i}_capture")
        assert img_id is not None
        assert btn_id is not None

    assert fake_station.conn_inits == conn_inits_before

    assert await hass.config_entries.async_unload(entry2.entry_id)
    await hass.async_block_till_done()


async def test_a_capture_press_shows_the_preset_image(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")

    t0 = time.monotonic()
    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    assert time.monotonic() - t0 < 1.0

    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    assert fake_station.preset_gotos == [1]

    dt = datetime.fromisoformat(state_of(hass, image_1))
    assert dt.tzinfo is not None

    img = await async_get_image(hass, image_1)
    assert img.content_type == "image/jpeg"
    assert img.content.startswith(FOUR_K_JPEG_PREFIX)

    state = _state(hass, image_1)
    assert state.attributes.get("preset_index") == 1

    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_0_image")) == "unknown"
    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_2_image")) == "unknown"

    assert entry.runtime_data.presets.image_for(SN, 1) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_preset_image_and_its_time_come_back_after_a_restart(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    """A reload shows the captured preset image and its time again, with no wake."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")
    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()
    taken = state_of(hass, image_1)
    shown = (await async_get_image(hass, image_1)).content
    cached = still_cache.cache_dir(hass, entry.entry_id) / f"{SN}.preset_1.jpg"
    await wait_until(cached.exists)
    conn_inits = fake_station.conn_inits

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, image_1) == taken
    assert (await async_get_image(hass, image_1)).content == shown
    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_0_image")) == "unknown"
    assert fake_station.conn_inits == conn_inits
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_preset_image_is_kept_in_the_history_named_by_its_preset(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    """The captured preset image is a history file ending in ``_preset_1.jpg``."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")
    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    shown = (await async_get_image(hass, image_1)).content
    root = history.history_dir(hass)

    def saved() -> list[Path]:
        return list(root.rglob("*_preset_1.jpg")) if root.is_dir() else []

    await wait_until(lambda: bool(saved()))
    assert [path.read_bytes() for path in saved()] == [shown]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_second_preset_while_one_runs_is_refused_and_nothing_is_sent(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")
    button_2 = entity_id_for(hass, "button", SN, "preset_2_capture")

    states_1 = record_states(hass, button_1)
    states_2 = record_states(hass, button_2)

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: fake_station.preset_gotos == [1])

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("button", "press", {"entity_id": button_2}, blocking=True)
    assert exc.value.translation_key == "capture_in_progress"

    await wait_until(lambda: state_of(hass, image_1) != "unknown")
    await hass.async_block_till_done()

    assert fake_station.preset_gotos == [1]
    assert "unavailable" not in states_1 and "unavailable" not in states_2
    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_2_image")) == "unknown"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_same_preset_pressed_twice_joins_one_capture(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)

    await wait_until(lambda: state_of(hass, image_1) != "unknown")
    await hass.async_block_till_done()

    assert sum(1 for p in fake_station.doorbell_payloads if p.get("commandType") == 6035) == 1
    assert fake_station.preset_gotos == [1]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_capture_live_image_while_a_preset_holds_the_camera_is_refused(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")
    live_button = entity_id_for(hass, "button", SN, "capture_live_image")
    camera_id = entity_id_for(hass, "camera", SN, "camera")

    camera_states = record_states(hass, camera_id)

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: fake_station.preset_gotos == [1])

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("button", "press", {"entity_id": live_button}, blocking=True)
    assert exc.value.translation_key == "capture_in_progress"

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await wait_until(lambda: state_of(hass, image_1) != "unknown")
    await hass.async_block_till_done()

    assert "unavailable" not in camera_states
    assert len(fake_station.live_opens) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_presets_changed_adds_a_new_slot_and_unavailables_a_removed_one(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    fake_station.preset_points[2]["enable"] = 0
    fake_station.preset_points[5]["enable"] = 1

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )

    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_5_image") is not None
    )
    await hass.async_block_till_done()

    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_5_image")) != "unavailable"
    assert state_of(hass, entity_id_for(hass, "button", SN, "preset_5_capture")) != "unavailable"

    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_2_image")) == "unavailable"
    assert state_of(hass, entity_id_for(hass, "button", SN, "preset_2_capture")) == "unavailable"

    assert registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    assert registry.async_get_entity_id("button", DOMAIN, f"{SN}_preset_2_capture") is not None

    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_0_image")) != "unavailable"
    assert state_of(hass, entity_id_for(hass, "image", SN, "preset_1_image")) != "unavailable"

    assert sum(1 for p in fake_station.doorbell_payloads if p.get("commandType") == 6034) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_capture_preset_action_targets_the_camera(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "capture_preset")

    camera_id = entity_id_for(hass, "camera", SN, "camera")
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")

    await hass.services.async_call(
        DOMAIN, "capture_preset", {"entity_id": camera_id, "preset": 1}, blocking=True
    )
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    assert fake_station.preset_gotos == [1]

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, "capture_preset", {"entity_id": camera_id, "preset": 7}, blocking=True
        )

    assert exc.value.translation_key == "preset_not_set"
    assert fake_station.preset_gotos == [1]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_failed_preset_capture_keeps_the_previous_image(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    monkeypatch,
    caplog,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    prev_state = state_of(hass, image_1)
    prev_image = entry.runtime_data.presets.image_for(SN, 1)

    # The integration's seam is the public async_preset_image; the next capture fails
    # the way a stream that ends before the camera settles does.
    from eufy_home_security import DeviceTimeoutError

    async def _timed_out(self, sn, preset, **kw):
        raise DeviceTimeoutError("the stream ended before the camera settled")

    monkeypatch.setattr(Station, "async_preset_image", _timed_out)

    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: "DeviceTimeoutError" in caplog.text, timeout=15)
    await hass.async_block_till_done()

    assert state_of(hass, image_1) == prev_state
    assert entry.runtime_data.presets.image_for(SN, 1) == prev_image

    assert not any(
        record.levelno == logging.ERROR
        for record in caplog.records
        if record.name.startswith("custom_components.eufy_home_security")
    )
    assert fake_station.preset_gotos == [1]  # the failed capture sent nothing

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_list_the_preset_slots_by_index_only(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    from custom_components.eufy_home_security import diagnostics

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    stations = result.get("stations", {})
    assert len(stations) == 1
    station_dict = next(iter(stations.values()))

    presets = station_dict.get("presets", {})
    assert len(presets) == 1

    preset_info = next(iter(presets.values()))
    assert preset_info == {"count": 10, "enabled": [0, 1, 2]}

    assert SN not in json.dumps(result)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_preset_capture_that_fails_while_the_session_is_replaced_names_the_session(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    monkeypatch,
    caplog,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    prev_state = state_of(hass, image_1)
    prev_image = entry.runtime_data.presets.image_for(SN, 1)

    built_clients[-1].cache.set_replaced()

    from eufy_home_security import SessionReplacedError, Station

    # What the library raises for a camera it cannot wake while the session is
    # latched; the integration names the session from the error.
    async def _unreachable(self, sn, preset, **kw):
        raise SessionReplacedError()

    monkeypatch.setattr(Station, "async_preset_image", _unreachable)

    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: "SessionReplacedError" in caplog.text, timeout=15)
    await hass.async_block_till_done()

    failed_record = next(r for r in reversed(caplog.records) if "SessionReplacedError" in r.message)
    assert "session" in failed_record.message
    assert "Repairs" in failed_record.message

    assert state_of(hass, image_1) == prev_state
    assert entry.runtime_data.presets.image_for(SN, 1) == prev_image

    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name.startswith("custom_components.eufy_home_security")
    ]
    assert not error_records

    assert "login" not in fake_cloud.calls
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_preset_capture_makes_the_live_stream_yield_the_media_slot(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    monkeypatch,
    hass_client_no_auth,
    caplog,
):
    """A preset capture ends the camera's own live view, then runs.

    A standalone camera carries one stream on its station session, and a preset
    capture opens live video, so the library names this camera as the slot's holder
    and its live view yields: the viewer's response ends normally and the capture
    succeeds.
    """
    import asyncio
    import logging
    from urllib.parse import urlparse

    from homeassistant.components.camera import async_get_stream_source

    entry = await set_up_warm(hass, seed_warm_cache)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    button_1 = entity_id_for(hass, "button", SN, "preset_1_capture")
    camera_id = entity_id_for(hass, "camera", SN, "camera")

    client = await hass_client_no_auth()
    url = await async_get_stream_source(hass, camera_id)
    assert url is not None
    response = await client.get(urlparse(url)._replace(scheme="", netloc="").geturl())
    assert response.status == 200
    await wait_until(lambda: fake_station.streaming, timeout=20)

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await hass.services.async_call("button", "press", {"entity_id": button_1}, blocking=True)
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=45)
    await hass.async_block_till_done()

    assert entry.runtime_data.presets.image_for(SN, 1) is not None
    assert [r.getMessage() for r in caplog.records if "aborting" in r.getMessage()] == [
        f"Live stream of {redact_serial(SN)}: {YIELD_REASON}"
    ]

    # The viewer saw a normal end of stream: no traceback, no 500, no ERROR record.
    async with asyncio.timeout(10):
        await response.content.read()
    assert response.status == 200
    assert [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name.startswith("custom_components.eufy_home_security")
    ] == []

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_live_capture_of_a_standalone_camera_ends_its_own_live_view(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
    caplog,
):
    """A standalone camera has one stream: a "Capture live image" press ends its view first."""
    import asyncio
    from urllib.parse import urlparse

    from homeassistant.components.camera import async_get_stream_source

    entry = await set_up_warm(hass, seed_warm_cache)
    camera_id = entity_id_for(hass, "camera", SN, "camera")
    client = await hass_client_no_auth()
    url = await async_get_stream_source(hass, camera_id)
    assert url is not None
    response = await client.get(urlparse(url)._replace(scheme="", netloc="").geturl())
    assert response.status == 200
    await wait_until(lambda: fake_station.streaming, timeout=20)

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "capture_live_image")},
        blocking=True,
    )
    await wait_until(lambda: entry.runtime_data.snapshots.source_for(SN) == "live", timeout=30)

    assert [r.getMessage() for r in caplog.records if "aborting" in r.getMessage()] == [
        f"Live stream of {redact_serial(SN)}: {YIELD_REASON}"
    ]
    async with asyncio.timeout(10):
        await response.content.read()
    assert response.status == 200

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _read_slots(hass: HomeAssistant) -> None:
    """Press Refresh presets and wait until the slot entities exist."""
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_2_image") is not None
    )
    await hass.async_block_till_done()


async def _select_default(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id_for(hass, "select", SN, "default_preset"), "option": option},
        blocking=True,
    )


async def test_a_cold_cache_gives_the_default_preset_select_without_a_wake(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    """The select exists from setup, knows nothing, and never wakes the camera to learn."""
    entry = await set_up_warm(hass, seed_warm_cache)

    select_id = entity_id_for(hass, "select", SN, "default_preset")
    assert select_id is not None
    assert _registry_entry(hass, select_id).entity_category == "config"
    state = _state(hass, select_id)
    assert state.state == "unknown"
    assert state.attributes["options"] == []
    assert fake_station.conn_inits == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_default_preset_select_follows_the_slots_read(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    """Options are the enabled slots, the value is the library's default, both from reads."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "default_preset")

    state = _state(hass, select_id)
    assert state.attributes["options"] == ["0", "1", "2"]
    assert state.state == "0"

    fake_station.preset_points[5]["enable"] = 1
    for point in fake_station.preset_points:
        point["isdefault"] = int(point["index"] == 5)
    await _read_slots(hass)
    await wait_until(lambda: state_of(hass, select_id) == "5")

    assert _state(hass, select_id).attributes["options"] == ["0", "1", "2", "5"]
    assert fake_station.default_preset_sets == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_selecting_a_default_preset_writes_it_and_shows_the_read_back(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "default_preset")

    await _select_default(hass, "2")
    await hass.async_block_till_done()

    assert fake_station.default_preset_sets == [(2, 0)]
    assert fake_station.preset_gotos == [2]
    assert state_of(hass, select_id) == "2"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_default_preset_outside_the_options_is_refused_and_nothing_is_sent(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)

    with pytest.raises(ServiceValidationError):
        await _select_default(hass, "7")

    assert fake_station.default_preset_sets == []
    assert fake_station.preset_gotos == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_default_preset_while_a_capture_holds_the_camera_is_refused(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    """The write would turn the camera away from the view being captured: nothing is sent."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "default_preset")
    states = record_states(hass, select_id)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "preset_1_capture")},
        blocking=True,
    )
    await wait_until(lambda: fake_station.preset_gotos == [1])

    with pytest.raises(HomeAssistantError) as exc:
        await _select_default(hass, "2")
    assert exc.value.translation_key == "capture_in_progress"

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    assert fake_station.default_preset_sets == []
    assert fake_station.preset_gotos == [1]
    assert state_of(hass, select_id) == "0"
    assert "unavailable" not in states

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (CommandRejectedError(1350, -502), "default_preset_needs_confirmation"),
        (CommandNotAppliedError(1350), "setting_not_applied"),
        (DeviceTimeoutError("no answer"), "on_demand_unreachable"),
        (DeviceBusyError("busy"), "capture_in_progress"),
        (UnsupportedError("preset 2 is not set"), "preset_not_set"),
        (CommandUnsupportedError(1350, -108), "ptz_command_not_handled"),
    ],
)
async def test_a_failed_default_preset_write_keeps_the_value_and_says_why(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    monkeypatch,
    error,
    translation_key,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "default_preset")
    calls: list[tuple[str, int, dict]] = []

    async def _fails(self, sn, preset, **kw):
        calls.append((sn, preset, kw))
        raise error

    monkeypatch.setattr(Station, "async_set_default_preset", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await _select_default(hass, "2")

    assert exc.value.translation_key == translation_key
    # A -502 is never answered with confirm=True on the user's behalf.
    assert calls == [(SN, 2, {})]
    assert state_of(hass, select_id) == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def fast_pan_tilt(monkeypatch):
    orig = Station.async_pan_tilt
    monkeypatch.setattr(
        Station,
        "async_pan_tilt",
        lambda self, sn, direction, **kw: orig(self, sn, direction, settle=0),
    )


@pytest.mark.parametrize(
    ("key", "rotate_type"),
    # the camera's rotate_type: 1 turns left, 2 right, 3 up, 4 down
    [("pan_left", 1), ("pan_right", 2), ("tilt_up", 3), ("tilt_down", 4)],
)
async def test_a_pan_tilt_button_moves_the_camera_one_step(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_pan_tilt,
    key,
    rotate_type,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    button_id = entity_id_for(hass, "button", SN, key)
    assert button_id is not None
    assert _registry_entry(hass, button_id).entity_category is None

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    assert fake_station.pan_tilts == [rotate_type]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_pan_tilt_step_while_a_capture_holds_the_camera_is_refused(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    fast_pan_tilt,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    left = entity_id_for(hass, "button", SN, "pan_left")
    states = record_states(hass, left)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "preset_1_capture")},
        blocking=True,
    )
    await wait_until(lambda: fake_station.preset_gotos == [1])

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("button", "press", {"entity_id": left}, blocking=True)
    assert exc.value.translation_key == "capture_in_progress"

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    assert fake_station.pan_tilts == []
    assert "unavailable" not in states

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (CommandRejectedError(1700, 1), "pan_tilt_not_applied"),
        (CommandUnsupportedError(1700, -108), "ptz_command_not_handled"),
        (DeviceTimeoutError("no answer"), "on_demand_unreachable"),
        (DeviceBusyError("busy"), "capture_in_progress"),
    ],
)
async def test_a_failed_pan_tilt_step_says_why(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    monkeypatch,
    error,
    translation_key,
):
    entry = await set_up_warm(hass, seed_warm_cache)

    async def _fails(self, sn, direction, **kw):
        raise error

    monkeypatch.setattr(Station, "async_pan_tilt", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id_for(hass, "button", SN, "tilt_up")},
            blocking=True,
        )
    assert exc.value.translation_key == translation_key

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _live_opens(station: FakeStation) -> int:
    return len(station.live_opens)


async def _select_live(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id_for(hass, "select", SN, "live_preset"), "option": option},
        blocking=True,
    )


async def _watch(hass: HomeAssistant, client, fake_station: FakeStation):
    """Open the camera's live stream URL and wait until the camera streams."""
    from urllib.parse import urlparse

    from homeassistant.components.camera import async_get_stream_source

    url = await async_get_stream_source(hass, entity_id_for(hass, "camera", SN, "camera"))
    assert url is not None
    response = await client.get(urlparse(url)._replace(scheme="", netloc="").geturl())
    assert response.status == 200
    await wait_until(lambda: fake_station.streaming, timeout=20)
    return response


async def _read_some(response, n: int = 188) -> bytes:
    import asyncio

    async with asyncio.timeout(10):
        return await response.content.readexactly(n)


async def test_the_live_view_preset_select_offers_the_camera_default_and_the_set_slots(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    """The select exists from setup, wakes nothing, and lists the slots once read."""
    entry = await set_up_warm(hass, seed_warm_cache)
    select_id = entity_id_for(hass, "select", SN, "live_preset")
    assert select_id is not None
    assert _registry_entry(hass, select_id).entity_category is None
    state = _state(hass, select_id)
    assert state.state == "camera_default"
    assert state.attributes["options"] == ["camera_default"]
    assert fake_station.conn_inits == 0

    await _read_slots(hass)
    await wait_until(
        lambda: _state(hass, select_id).attributes["options"] == ["camera_default", "0", "1", "2"]
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_live_view_preset_chosen_with_no_viewer_sends_nothing_and_the_view_opens_there(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "live_preset")

    await _select_live(hass, "2")
    assert state_of(hass, select_id) == "2"
    assert fake_station.preset_gotos == []
    assert _live_opens(fake_station) == 0

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    assert fake_station.preset_gotos == [2]
    assert fake_station.gotos_while_streaming == [False]
    assert _live_opens(fake_station) == 1

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_camera_default_opens_the_live_view_with_no_turn(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    assert fake_station.preset_gotos == []

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_changing_the_live_view_preset_while_watching_turns_the_running_view(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    """One go-to during the stream; the view keeps running and is not reopened."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "live_preset")
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    await _select_live(hass, "1")

    assert fake_station.preset_gotos == [1]
    assert fake_station.gotos_while_streaming == [True]
    assert state_of(hass, select_id) == "1"
    assert await _read_some(response, 188 * 4)
    assert fake_station.streaming
    assert _live_opens(fake_station) == 1

    await _select_live(hass, "camera_default")

    assert fake_station.preset_gotos == [1, 0]
    assert fake_station.gotos_while_streaming == [True, True]
    assert state_of(hass, select_id) == "camera_default"
    assert _live_opens(fake_station) == 1

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (DeviceBusyError("busy"), "capture_in_progress"),
        (UnsupportedError("preset 1 is not set"), "preset_not_set"),
        (CommandUnsupportedError(1700, -108), "ptz_command_not_handled"),
        (CommandRejectedError(1700, 1), "setting_not_applied"),
    ],
)
async def test_a_failed_turn_while_watching_keeps_the_choice_and_says_why(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
    monkeypatch,
    error,
    translation_key,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "live_preset")
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    async def _fails(self, sn, preset, **kw):
        raise error

    monkeypatch.setattr(Station, "async_goto_preset", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await _select_live(hass, "1")
    assert exc.value.translation_key == translation_key
    assert state_of(hass, select_id) == "camera_default"
    assert fake_station.streaming

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_live_view_preset_survives_a_reload(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "live_preset")
    await _select_live(hass, "2")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, select_id) == "2"
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    assert fake_station.preset_gotos == [2]

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_removed_slot_resets_the_live_view_preset_to_the_camera_default(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    """A choice the camera does not have opens at the default, never fails the view."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    select_id = entity_id_for(hass, "select", SN, "live_preset")
    await _select_live(hass, "2")

    fake_station.preset_points[2]["enable"] = 0
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_presets")},
        blocking=True,
    )
    await wait_until(lambda: state_of(hass, select_id) == "camera_default")
    assert _state(hass, select_id).attributes["options"] == ["camera_default", "0", "1"]

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    assert fake_station.preset_gotos == []

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _set_zoom(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id_for(hass, "number", SN, "live_zoom"), "value": value},
        blocking=True,
    )


async def test_the_live_view_zoom_is_a_primary_slider_that_wakes_nothing(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    assert zoom_id is not None
    assert _registry_entry(hass, zoom_id).entity_category is None
    state = _state(hass, zoom_id)
    assert float(state.state) == 1.0
    assert (state.attributes["min"], state.attributes["max"], state.attributes["step"]) == (
        1.0,
        12.0,
        0.5,
    )
    assert state.attributes["mode"] == "slider"
    assert fake_station.conn_inits == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_zoom_chosen_with_no_viewer_sends_nothing_and_the_view_opens_zoomed(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")

    await _set_zoom(hass, 3)
    assert float(state_of(hass, zoom_id)) == 3.0
    assert fake_station.zoom_writes == []
    assert _live_opens(fake_station) == 0

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    assert fake_station.zoom_writes == [3.0]
    assert _live_opens(fake_station) == 1

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_zooming_while_watching_zooms_the_running_view(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    assert fake_station.zoom_writes == []

    await _set_zoom(hass, 2.5)

    assert fake_station.zoom_writes == [2.5]
    assert float(state_of(hass, zoom_id)) == 2.5
    assert await _read_some(response, 188 * 4)
    assert _live_opens(fake_station) == 1

    await _set_zoom(hass, 1)
    assert fake_station.zoom_writes == [2.5, 1.0]

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (DeviceBusyError("busy"), "capture_in_progress"),
        (UnsupportedError("dual view"), "zoom_needs_single_view"),
        (CommandUnsupportedError(1350, -108), "ptz_command_not_handled"),
        (CommandRejectedError(1350, 1), "setting_not_applied"),
    ],
)
async def test_a_failed_zoom_while_watching_keeps_the_zoom_and_says_why(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
    monkeypatch,
    error,
    translation_key,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    async def _fails(self, sn, zoom):
        raise error

    monkeypatch.setattr(Station, "async_set_zoom", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await _set_zoom(hass, 4)
    assert exc.value.translation_key == translation_key
    assert float(state_of(hass, zoom_id)) == 1.0
    assert fake_station.streaming

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_zoom_the_camera_refuses_at_open_does_not_fail_the_view(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
    monkeypatch,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _set_zoom(hass, 3)

    async def _fails(self, sn, zoom):
        raise UnsupportedError("dual view")

    monkeypatch.setattr(Station, "async_set_zoom", _fails)

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_preset_choice_shows_and_keeps_that_slots_own_zoom(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    """The camera applies a slot's stored zoom itself; HA sends no zoom over it."""
    fake_station.preset_points[2]["zoom"] = 3
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    await _set_zoom(hass, 5)

    await _select_live(hass, "2")
    assert float(state_of(hass, zoom_id)) == 3.0

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    assert fake_station.preset_gotos == [2]
    assert fake_station.zoom_writes == []

    await _select_live(hass, "camera_default")
    assert float(state_of(hass, zoom_id)) == 1.0

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_live_view_zoom_and_preset_survive_a_reload_together(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    await _select_live(hass, "1")
    await _set_zoom(hass, 4)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert float(state_of(hass, zoom_id)) == 4.0
    assert state_of(hass, entity_id_for(hass, "select", SN, "live_preset")) == "1"
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    assert fake_station.preset_gotos == [1]
    assert fake_station.zoom_writes == [4.0]

    response.close()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _camera_action(hass: HomeAssistant, action: str, **data) -> None:
    await hass.services.async_call(
        DOMAIN,
        action,
        {"entity_id": entity_id_for(hass, "camera", SN, "camera"), **data},
        blocking=True,
    )


@pytest.mark.parametrize(
    ("direction", "rotate_type"), [("left", 1), ("right", 2), ("up", 3), ("down", 4)]
)
async def test_the_pan_tilt_action_on_the_camera_moves_one_step(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_pan_tilt,
    direction,
    rotate_type,
):
    entry = await set_up_warm(hass, seed_warm_cache)

    await _camera_action(hass, "pan_tilt", direction=direction)

    assert fake_station.pan_tilts == [rotate_type]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_goto_preset_action_turns_the_camera_and_leaves_the_view_choices(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    """A one-off turn: the camera re-parks when idle, so the next view is unchanged."""
    fake_station.preset_points[2]["zoom"] = 3
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    select_id = entity_id_for(hass, "select", SN, "live_preset")

    await _camera_action(hass, "goto_preset", preset=2)

    assert fake_station.preset_gotos == [2]
    assert float(state_of(hass, zoom_id)) == 1.0
    assert state_of(hass, select_id) == "camera_default"

    with pytest.raises(ServiceValidationError) as exc:
        await _camera_action(hass, "goto_preset", preset=7)
    assert exc.value.translation_key == "preset_not_set"
    assert fake_station.preset_gotos == [2]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_goto_the_station_does_not_handle_says_so(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, monkeypatch
):
    """A -108 receipt is the station's refusal of a sent command, not an unset slot."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)

    async def _fails(self, sn, index, **kw):
        raise CommandUnsupportedError(1700, -108)

    monkeypatch.setattr(Station, "async_goto_preset", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await _camera_action(hass, "goto_preset", preset=2)
    assert exc.value.translation_key == "ptz_command_not_handled"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_zoom_action_steps_the_live_view_zoom_within_its_range(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")

    await _camera_action(hass, "zoom", direction="in")
    await _camera_action(hass, "zoom", direction="in")
    assert float(state_of(hass, zoom_id)) == 3.0
    await _camera_action(hass, "zoom", direction="out")
    assert float(state_of(hass, zoom_id)) == 2.0
    for _ in range(3):
        await _camera_action(hass, "zoom", direction="out")
    assert float(state_of(hass, zoom_id)) == 1.0
    assert fake_station.zoom_writes == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_goto_preset_action_while_watching_shows_that_slots_zoom(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    fake_station.preset_points[2]["zoom"] = 3
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    await _camera_action(hass, "goto_preset", preset=2)

    assert fake_station.preset_gotos == [2]
    assert fake_station.gotos_while_streaming == [True]
    assert float(state_of(hass, zoom_id)) == 3.0
    assert fake_station.zoom_writes == []

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_live_view_zoom_follows_the_cameras_reports_while_watching(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
):
    """A zoom from elsewhere (the eufy app) shows; an idle reset keeps the choice."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SN].station
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)

    fake_station.send_zoom_report(4)
    await wait_until(lambda: float(state_of(hass, zoom_id)) == 4.0)

    response.close()
    broadcast = hass.data["eufy_home_security_streams"].broadcasts[SN]
    await wait_until(lambda: not broadcast.running, timeout=10)
    fake_station.send_zoom_report(1)
    await wait_until(lambda: station.zoom(SN) == 1.0)
    await hass.async_block_till_done()
    assert float(state_of(hass, zoom_id)) == 4.0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_cameras_1x_report_while_a_view_opens_does_not_replace_the_zoom(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
    monkeypatch,
):
    """Every open resets the camera to its slot's zoom; the chosen zoom is sent again."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SN].station
    zoom_id = entity_id_for(hass, "number", SN, "live_zoom")
    await _set_zoom(hass, 3)
    states = record_states(hass, zoom_id)
    orig = Station.async_open_live

    async def _open_then_reset(self, sn, **kw):
        stream = await orig(self, sn, **kw)
        fake_station.send_zoom_report(1)
        await wait_until(lambda: station.zoom(SN) == 1.0)
        return stream

    monkeypatch.setattr(Station, "async_open_live", _open_then_reset)

    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    await hass.async_block_till_done()

    assert fake_station.zoom_writes == [3.0]
    assert "1.0" not in states
    assert float(state_of(hass, zoom_id)) == 3.0

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_zoom_that_changes_the_picture_size_keeps_the_view_running(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    hass_client_no_auth,
    monkeypatch,
):
    """The broadcast follows the camera's new size; the viewer's stream carries on."""
    entry = await set_up_warm(hass, seed_warm_cache)
    response = await _watch(hass, await hass_client_no_auth(), fake_station)
    assert await _read_some(response)
    broadcast = hass.data["eufy_home_security_streams"].broadcasts[SN]
    send_video = fake_station.send_video

    def _smaller(body, **kw):
        send_video(body, **{**kw, "width": 2304, "height": 1296})

    await _set_zoom(hass, 3)
    monkeypatch.setattr(fake_station, "send_video", _smaller)
    await wait_until(lambda: broadcast.resizes == 1, timeout=10)
    assert await _read_some(response, 188 * 8)

    assert broadcast.running
    assert broadcast.size == (2304, 1296)
    assert _live_opens(fake_station) == 1
    assert fake_station.zoom_writes == [3.0]

    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def real_jpegs(monkeypatch) -> list[bytes]:
    """Decode each preset keyframe to a real 1280x720 JPEG of its own colour; lists them."""
    made: list[bytes] = []

    async def decode(command, hevc: bytes, timeout: float) -> bytes:
        out = BytesIO()
        Image.new("RGB", (1280, 720), (20 * len(made), 80, 160)).save(out, "JPEG")
        made.append(out.getvalue())
        return made[-1]

    monkeypatch.setattr(snapshots, "async_hevc_to_jpeg", decode)
    return made


def _small_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as picture:
        return picture.size


async def test_refresh_presets_recaptures_every_set_slot_and_its_small_copy(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    recapture,
    real_jpegs,
):
    """One press: slot read, then slots 0, 1 and 2 turned to and captured in turn, each
    image and small file replaced; a second press replaces them again."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    images = [entity_id_for(hass, "image", SN, f"preset_{i}_image") for i in range(3)]
    await wait_until(lambda: all(state_of(hass, i) != "unknown" for i in images), timeout=30)
    await wait_until(lambda: entry.runtime_data.presets.capturing_index(SN) is None, timeout=30)
    await hass.async_block_till_done()
    assert fake_station.preset_gotos[:3] == [0, 1, 2]
    directory = still_cache.cache_dir(hass, entry.entry_id)
    small = [still_cache.small_path(directory, f"{SN}.preset_{i}") for i in range(3)]
    await wait_until(lambda: all(path.exists() for path in small))
    assert all(_small_size(path) == (480, 270) for path in small)
    first = [path.read_bytes() for path in small]
    pictures = [_state(hass, i).attributes["entity_picture"] for i in images]
    assert all(
        p.startswith(f"/api/eufy_home_security/image/{i}/small?v=")
        for p, i in zip(pictures, images, strict=True)
    )

    await _read_slots(hass)
    await wait_until(lambda: len(real_jpegs) == 6, timeout=30)
    await wait_until(
        lambda: all(path.read_bytes() != old for path, old in zip(small, first, strict=True))
    )
    await hass.async_block_till_done()
    assert [_state(hass, i).attributes["entity_picture"] for i in images] != pictures
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_capture_press_during_a_refresh_joins_its_slot_and_refuses_another(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    recapture,
    real_jpegs,
):
    """While the refresh captures slot n, pressing slot n joins; another slot is refused."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    manager = entry.runtime_data.presets
    await wait_until(lambda: manager.capturing_index(SN) is not None, timeout=15)
    running = manager.capturing_index(SN)
    assert running is not None
    other = (running + 1) % 3
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, f"preset_{running}_capture")},
        blocking=True,
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id_for(hass, "button", SN, f"preset_{other}_capture")},
            blocking=True,
        )
    await wait_until(lambda: len(real_jpegs) == 3, timeout=30)
    await wait_until(lambda: manager.capturing_index(SN) is None, timeout=15)
    assert fake_station.preset_gotos.count(running) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_captured_preset_image_is_served_small_by_its_entity_picture(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    real_jpegs,
    hass_client_no_auth,
):
    """A capture press gives a small copy; image_proxy still serves the full image."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "preset_1_capture")},
        blocking=True,
    )
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    response = await client.get(_state(hass, image_1).attributes["entity_picture"])
    assert response.status == 200
    assert "immutable" in response.headers["Cache-Control"]
    with Image.open(BytesIO(await response.read())) as picture:
        assert picture.size == (480, 270)
    assert (await async_get_image(hass, image_1)).content == real_jpegs[-1]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── saving the current view, deleting a slot ─────────────────────────────────


def _commands_sent(station: FakeStation, command: int) -> int:
    return sum(1 for p in station.doorbell_payloads if p.get("commandType") == command)


async def _save(hass: HomeAssistant, **data) -> dict:
    """Call save_preset on the camera and return its response for the camera."""
    camera_id = entity_id_for(hass, "camera", SN, "camera")
    response = await hass.services.async_call(
        DOMAIN,
        "save_preset",
        {"entity_id": camera_id, **data},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return dict(response[camera_id])  # type: ignore[arg-type]


async def test_save_current_view_stores_the_lowest_free_slot_and_adds_its_entities(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    """A cold press re-reads the slots, stores into slot 3 and gains slot 3's entities."""
    entry = await set_up_warm(hass, seed_warm_cache)
    button_id = entity_id_for(hass, "button", SN, "save_view")
    assert _registry_entry(hass, button_id).entity_category is None
    assert _state(hass, button_id).attributes["friendly_name"].endswith("Save current view")

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    assert fake_station.preset_stores == [3]
    assert fake_station.default_preset_sets == []
    registry = er.async_get(hass)
    await wait_until(
        lambda: registry.async_get_entity_id("image", DOMAIN, f"{SN}_preset_3_image") is not None
    )
    await hass.async_block_till_done()
    assert state_of(hass, entity_id_for(hass, "button", SN, "preset_3_capture")) != "unavailable"
    default_id = entity_id_for(hass, "select", SN, "default_preset")
    assert _state(hass, default_id).attributes["options"] == ["0", "1", "2", "3"]
    assert state_of(hass, default_id) == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_save_preset_action_responds_with_the_slot_and_can_make_it_default(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    default_id = entity_id_for(hass, "select", SN, "default_preset")

    assert await _save(hass, make_default=True) == {"preset": 3}

    assert fake_station.preset_stores == [3]
    assert fake_station.default_preset_sets == [(3, 0)]
    await wait_until(lambda: state_of(hass, default_id) == "3")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_saving_over_a_slot_overwrites_it_and_drops_its_stale_image(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    real_jpegs,
):
    """The slot's picture changed: its captured image, cached file and small copy go."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "preset_1_capture")},
        blocking=True,
    )
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()
    cached = still_cache.cache_dir(hass, entry.entry_id) / f"{SN}.preset_1.jpg"
    small = still_cache.small_path(cached.parent, f"{SN}.preset_1")
    await wait_until(lambda: cached.exists() and small.exists())
    assert _state(hass, image_1).attributes.get("entity_picture")

    assert await _save(hass, preset=1) == {"preset": 1}

    assert fake_station.preset_stores == [1]
    assert fake_station.default_preset_sets == []
    assert entry.runtime_data.presets.image_for(SN, 1) is None
    assert state_of(hass, image_1) == "unknown"
    assert _state(hass, image_1).attributes.get("entity_picture") is None
    await wait_until(lambda: not cached.exists() and not small.exists())
    assert not cached.with_suffix(".json").exists()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_full_camera_refuses_the_save_before_sending_anything(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    for point in fake_station.preset_points:
        point["enable"] = int(point["index"] < 5)
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    button_id = entity_id_for(hass, "button", SN, "save_view")
    states = record_states(hass, button_id)
    reads = _commands_sent(fake_station, 6034)

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    assert exc.value.translation_key == "presets_full"
    assert exc.value.translation_placeholders == {"slots": "5"}
    assert isinstance(exc.value.__cause__, PresetSlotsFullError)
    with pytest.raises(HomeAssistantError) as exc:
        await _save(hass, preset=7)
    assert exc.value.translation_key == "presets_full"
    assert isinstance(exc.value.__cause__, PresetSlotsFullError)

    assert fake_station.preset_stores == []
    assert _commands_sent(fake_station, 6034) == reads
    assert "unavailable" not in states

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_camera_filled_in_the_app_since_the_last_read_says_full(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    """The save re-reads first; five slots in use then refuse it, nothing stored."""
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    for point in fake_station.preset_points:
        point["enable"] = int(point["index"] < 5)

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id_for(hass, "button", SN, "save_view")},
            blocking=True,
        )
    assert exc.value.translation_key == "presets_full"
    assert isinstance(exc.value.__cause__, PresetSlotsFullError)
    assert fake_station.preset_stores == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_save_while_a_capture_holds_the_camera_is_refused(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, fast_settle
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "preset_1_capture")},
        blocking=True,
    )
    await wait_until(lambda: fake_station.preset_gotos == [1])

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id_for(hass, "button", SN, "save_view")},
            blocking=True,
        )
    assert exc.value.translation_key == "capture_in_progress"

    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()
    assert fake_station.preset_stores == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "data", "translation_key"),
    [
        (CommandNotAppliedError(6032), {}, "preset_not_saved"),
        (CommandNotAppliedError(6242), {"make_default": True}, "preset_saved_not_default"),
        (CommandNotAppliedError(6032), {"preset": 4}, "preset_not_saved"),
        (PresetSlotsFullError(6032, "full", slots=5, in_use=(0, 1, 2, 3, 4)), {}, "presets_full"),
        (CommandRejectedError(1700, -502), {}, "default_preset_needs_confirmation"),
        (CommandRejectedError(1700, 1), {}, "preset_not_saved"),
        (DeviceTimeoutError("no answer"), {}, "on_demand_unreachable"),
        (UnsupportedError("no slot 8"), {"preset": 8}, "preset_slot_unknown"),
        (CommandUnsupportedError(1700, -108), {"preset": 4}, "ptz_command_not_handled"),
    ],
)
async def test_a_failed_save_says_why(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    monkeypatch,
    error,
    data,
    translation_key,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    calls: list[dict] = []

    async def _fails(self, sn, **kw):
        calls.append(kw)
        raise error

    monkeypatch.setattr(Station, "async_save_preset", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await _save(hass, **data)

    assert exc.value.translation_key == translation_key
    # A -502 is never answered with confirm=True on the user's behalf.
    assert calls == [
        {"preset": data.get("preset"), "make_default": data.get("make_default", False)}
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_save_preset_action_refuses_a_slot_outside_the_range(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    import voluptuous as vol

    entry = await set_up_warm(hass, seed_warm_cache)

    with pytest.raises(vol.Invalid):
        await _save(hass, preset=10)
    assert fake_station.preset_stores == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("action", ["capture_preset", "goto_preset"])
async def test_a_preset_action_refuses_a_slot_outside_the_range_before_the_slots_are_read(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station, action
):
    """Slots 0-9 only, by the schema: an unread camera is never turned to slot 10."""
    import voluptuous as vol

    entry = await set_up_warm(hass, seed_warm_cache)

    with pytest.raises(vol.Invalid):
        await _camera_action(hass, action, preset=10)
    await hass.async_block_till_done()
    assert fake_station.preset_gotos == []
    assert entry.runtime_data.presets.capturing_index(SN) is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_delete_preset_clears_the_slot_and_its_entities_go_unavailable(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    real_jpegs,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    default_id = entity_id_for(hass, "select", SN, "default_preset")
    image_2 = entity_id_for(hass, "image", SN, "preset_2_image")
    button_2 = entity_id_for(hass, "button", SN, "preset_2_capture")
    await hass.services.async_call("button", "press", {"entity_id": button_2}, blocking=True)
    await wait_until(lambda: state_of(hass, image_2) != "unknown", timeout=15)
    await hass.async_block_till_done()

    await _camera_action(hass, "delete_preset", preset=2)

    assert fake_station.preset_deletes == [2]
    assert entry.runtime_data.presets.image_for(SN, 2) is None
    await wait_until(lambda: state_of(hass, image_2) == "unavailable")
    assert state_of(hass, button_2) == "unavailable"
    assert _state(hass, default_id).attributes["options"] == ["0", "1"]
    assert state_of(hass, default_id) == "0"

    with pytest.raises(ServiceValidationError) as exc:
        await _camera_action(hass, "delete_preset", preset=2)
    assert exc.value.translation_key == "preset_not_set"
    assert fake_station.preset_deletes == [2]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_deleting_the_default_slot_leaves_the_default_select_unknown(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    default_id = entity_id_for(hass, "select", SN, "default_preset")
    assert state_of(hass, default_id) == "0"

    await _camera_action(hass, "delete_preset", preset=0)

    assert fake_station.preset_deletes == [0]
    await wait_until(lambda: state_of(hass, default_id) == "unknown")
    assert _state(hass, default_id).attributes["options"] == ["1", "2"]
    assert state_of(hass, entity_id_for(hass, "select", SN, "live_preset")) == "camera_default"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (CommandNotAppliedError(6033), "preset_not_deleted"),
        (CommandUnsupportedError(1700, -108), "ptz_command_not_handled"),
        (DeviceBusyError("busy"), "capture_in_progress"),
        (DeviceTimeoutError("no answer"), "on_demand_unreachable"),
    ],
)
async def test_a_failed_delete_says_why(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    monkeypatch,
    error,
    translation_key,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)

    async def _fails(self, sn, preset):
        raise error

    monkeypatch.setattr(Station, "async_delete_preset", _fails)

    with pytest.raises(HomeAssistantError) as exc:
        await _camera_action(hass, "delete_preset", preset=1)
    assert exc.value.translation_key == translation_key

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_save_stored_but_not_made_default_still_drops_the_slots_image(
    hass: HomeAssistant,
    fake_cloud,
    seed_warm_cache,
    built_clients,
    fake_station,
    fast_settle,
    real_jpegs,
    monkeypatch,
):
    entry = await set_up_warm(hass, seed_warm_cache)
    await _read_slots(hass)
    image_1 = entity_id_for(hass, "image", SN, "preset_1_image")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "preset_1_capture")},
        blocking=True,
    )
    await wait_until(lambda: state_of(hass, image_1) != "unknown", timeout=15)
    await hass.async_block_till_done()

    async def _not_default(self, sn, preset, **kw):
        raise CommandNotAppliedError(6242)

    monkeypatch.setattr(Station, "async_set_default_preset", _not_default)

    with pytest.raises(HomeAssistantError) as exc:
        await _save(hass, preset=1, make_default=True)
    assert exc.value.translation_key == "preset_saved_not_default"
    assert fake_station.preset_stores == [1]
    assert state_of(hass, image_1) == "unknown"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
