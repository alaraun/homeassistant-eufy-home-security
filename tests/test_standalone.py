"""Tests for standalone battery stations (T8170)."""

import base64
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlparse

import eufy_home_security.station
import pytest
from conftest import (
    add_entry,
    advance_to_poll,
    cloud_calls,
    detection_event,
    entity_id_for,
    now_ms,
    record_states,
    set_up_warm,
    setup_entry,
    state_of,
    wait_until,
)
from eufy_home_security import (
    CloudDevice,
    CommandNotAppliedError,
    CommunicationError,
    ConnectionChanged,
    DetectionType,
    DisconnectCause,
    GuardMode,
    ImageSource,
    SecurityEvent,
    SessionReplacedError,
    Station,
    StationUnreachableError,
    entity_unique_id,
)
from eufy_home_security.devices import Setting, SettingControl
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.push.decode import decode_push
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, v1_still
from homeassistant.components.camera import async_get_image, async_get_stream_source
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.eufy_home_security import diagnostics, errors, history
from custom_components.eufy_home_security.const import (
    BATTERY_KEY,
    CAMERA_KEY,
    CONF_CAMERA_IMAGE,
    CONF_EVENT_VIDEOS,
    DETECTION_EVENT_KEY,
    DOMAIN,
    GUARD_MODE_KEY,
    MOTION_DETECTED_KEY,
    PERSON_DETECTED_KEY,
    POLL_INTERVAL_SECONDS,
    STANDALONE_IMAGE_RETRY_DELAY_SECONDS,
    STANDALONE_THUMBNAIL_DELAY_SECONDS,
)
from custom_components.eufy_home_security.settings import setting_platform

SN = "T8170P0000000001"


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
            {"param_type": 1224, "param_value": "1", "update_time": t or 1.7e9},
        ],
    }


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


@pytest.fixture
def wakeable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the fake T8170 rendezvous servers, so the library builds a wake for it.

    The fake cloud entry carries no ``app_conn``, so ``rendezvous_servers`` is empty
    and the library skips the wake material entirely (LAN discovery only). A real
    T8170 has servers, and it is the wake's device-session-key fetch that meets the
    session-replaced latch. The library's own tests patch the same public property.
    """
    monkeypatch.setattr(CloudDevice, "rendezvous_servers", ("192.0.2.1",))


async def test_a_standalone_station_sets_up_from_cache_without_waking(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    seed_warm_cache()
    n = len(fake_cloud.calls)
    entry = add_entry(hass)
    await setup_entry(hass, entry)

    anchor = dt_util.utcnow()
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    anchor += timedelta(seconds=POLL_INTERVAL_SECONDS + 1)
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    anchor += timedelta(seconds=POLL_INTERVAL_SECONDS + 1)
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)

    assert entry.state.value == "loaded"

    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    assert len(devices) == 2

    account_dev = next(d for d in devices if d.entry_type == dr.DeviceEntryType.SERVICE)
    assert account_dev.identifiers == {(DOMAIN, entry.entry_id)}

    camera_dev = next(d for d in devices if d != account_dev)
    assert camera_dev.via_device_id is None
    assert camera_dev.model_id == "T8170"

    panel_id = entity_id_for(hass, "alarm_control_panel", SN, GUARD_MODE_KEY)
    assert state_of(hass, panel_id) == "armed_home"

    batt_id = entity_id_for(hass, "sensor", SN, BATTERY_KEY)
    assert state_of(hass, batt_id) == "61"

    assert fake_station.conn_inits == 0
    # Setup and the three polls fetched nothing. The one "devices" read is the session
    # probe's at 60 s: a cloud read with the saved session, never a
    # sign-in, and it wakes nothing (conn_inits is still 0 above).
    assert cloud_calls(fake_cloud)[n:] == ["devices"]

    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        state = hass.states.get(ent.entity_id)
        if state is not None:
            assert state.state != STATE_UNAVAILABLE


async def test_a_standalone_station_has_one_of_each_entity_and_no_duplicate_ids(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud, caplog
):
    caplog.set_level(10, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_device_by_identifier((DOMAIN, SN), entry.entry_id)
    assert dev is not None

    def get_ent(domain: str, key: str) -> er.RegistryEntry:
        ent = ent_reg.async_get(entity_id_for(hass, domain, SN, key))
        assert ent is not None
        assert ent.device_id == dev.id
        return ent

    get_ent("sensor", "firmware")
    get_ent("sensor", "model")
    get_ent("sensor", "battery")
    get_ent("update", "firmware")
    get_ent("camera", "camera")
    get_ent("button", "capture_live_image")
    get_ent("button", "refresh_image")
    get_ent("binary_sensor", "motion_detected")
    get_ent("event", "detection")

    sig_ent = ent_reg.async_get(entity_id_for(hass, "sensor", SN, "signal_strength"))
    assert sig_ent is not None and sig_ent.disabled_by is not None

    assert "does not generate unique IDs" not in caplog.text
    assert "already exists" not in caplog.text


async def test_disconnects_never_make_an_on_demand_station_unavailable(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router

    for cause in (DisconnectCause.IDLE, DisconnectCause.STATION_CLOSED):
        router.handle(ConnectionChanged(station_sn=SN, connected=False, cause=cause))
        await hass.async_block_till_done()

        assert entry.runtime_data.coordinators[SN].last_update_success is True

        for domain, key in (
            ("alarm_control_panel", GUARD_MODE_KEY),
            ("sensor", BATTERY_KEY),
            ("camera", "camera"),
            ("binary_sensor", MOTION_DETECTED_KEY),
        ):
            eid = entity_id_for(hass, domain, SN, key)
            assert state_of(hass, eid) not in (STATE_UNAVAILABLE, None)

        assert fake_station.conn_inits == 0


async def test_no_storage_poll_for_an_on_demand_station(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    entry = await set_up_warm(hass, seed_warm_cache)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()

    assert SN not in entry.runtime_data.storage
    assert fake_station.conn_inits == 0

    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    for ent in entries:
        assert not ent.entity_id.startswith("sensor.solo_disk_")
        assert not ent.entity_id.startswith("sensor.solo_emmc_used_space")


async def test_event_videos_never_sync_a_standalone_camera(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    """A T8170 keeps no recordings on a station: no sync, no history query, no wake."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_EVENT_VIDEOS: True})
    recordings = entry.runtime_data.recordings
    assert recordings is not None
    assert recordings.station_serials == frozenset()

    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.MOTION, t_ms=now_ms(), device_sn=SN, station_sn=SN, record_id=1
        )
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()

    assert recordings.stats(SN) is None
    assert fake_station.history_queries == []
    assert fake_station.conn_inits == 0


async def test_a_standalone_camera_lists_no_station_recordings(
    hass: HomeAssistant, hass_ws_client, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    """The card's recordings listing says unsupported, and a fetch is not found; neither
    queries the history nor wakes the camera."""
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = entity_id_for(hass, "camera", SN, CAMERA_KEY)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/recordings", "entity_id": entity_id})
    listed = await client.receive_json()
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/recordings/fetch", "entity_id": entity_id, "record_id": 1}
    )
    fetched = await client.receive_json()

    assert listed["result"] == {"supported": False, "recordings": []}
    assert fetched["error"]["code"] == "not_found"
    assert fake_station.history_queries == []
    assert fake_station.conn_inits == 0
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_detection_naming_the_standalone_station_reaches_its_entities(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud, caplog
):
    caplog.set_level(10, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache)
    events = hass.data.setdefault("eufy_home_security_event_tests", [])

    def capture(ev):
        events.append(ev)

    hass.bus.async_listen("eufy_home_security_event", capture)

    router = entry.runtime_data.router
    router.handle(detection_event(DetectionType.MOTION, t_ms=now_ms(), device_sn=SN, station_sn=SN))
    await hass.async_block_till_done()

    motion_id = entity_id_for(hass, "binary_sensor", SN, MOTION_DETECTED_KEY)
    assert state_of(hass, motion_id) == "on"

    det_id = entity_id_for(hass, "event", SN, DETECTION_EVENT_KEY)
    state = hass.states.get(det_id)
    assert state is not None
    assert state.state != "unknown"
    assert "T" in state.state  # it's a timestamp

    assert "not paired to the delivering station" not in caplog.text
    assert len(events) == 0
    # The detection's live keyframe (hd) has the camera awake: let it finish.
    await wait_until(lambda: not entry.runtime_data.snapshots.busy, timeout=20)


def _cloud_push(inner: dict, *, station_sn: str = SN, device_sn: str = SN) -> SecurityEvent:
    """A cloud push as the FCM listener decodes it: the payload base64 in its data message."""
    event = decode_push(
        {
            "station_sn": station_sn,
            "device_sn": device_sn,
            "event_time": str(inner["trigger_time"] // 1000 + 3),
            "span_id": f"span-{inner['unique_id']}",
            "payload": base64.b64encode(json.dumps(inner).encode()).decode(),
        }
    )
    assert event.frame_cipher is None and event.authenticated
    return event


@pytest.mark.parametrize("record_id", [None, 20260917_00001], ids=["no-record", "record"])
async def test_a_cloud_detection_of_the_standalone_camera_reaches_its_entities(
    hass: HomeAssistant,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    caplog,
    record_id: int | None,
):
    """Its only detection path: the cloud names the camera as its own station.

    Delivered as the client delivers a cloud push, through its de-duplicator, to the
    router. Its person sensor turns on once and its detection event fires once. With
    a record id the camera's still is looked up by the image rules a P2P detection
    follows (a command, so it wakes the camera); the fake has no such row, and that
    raises nothing.
    """
    caplog.set_level(10, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache)
    person_id = entity_id_for(hass, "binary_sensor", SN, PERSON_DETECTED_KEY)
    det_id = entity_id_for(hass, "event", SN, DETECTION_EVENT_KEY)
    person_states = record_states(hass, person_id)
    det_states = record_states(hass, det_id)
    fallback = async_capture_events(hass, "eufy_home_security_event")
    inner = {
        "msg_type": 18,
        "event_type": int(DetectionType.PERSON),
        "device_sn": SN,
        "station_sn": SN,
        "channel": 0,
        "trigger_time": now_ms(),
        "unique_id": "fedcba9876543210fedcba9876543210",
    }
    if record_id is not None:
        inner["record_id"] = record_id
    deduplicator = entry.runtime_data.eufy.deduplicator
    assert deduplicator is not None

    admitted = deduplicator.admit(_cloud_push(inner))
    assert admitted is not None
    entry.runtime_data.router.handle(admitted)
    await hass.async_block_till_done()

    assert person_states == ["on"]
    assert len(det_states) == 1
    state = hass.states.get(det_id)
    assert state is not None and state.attributes["event_type"] == "person"
    assert fallback == []
    assert "not paired to the delivering station" not in caplog.text
    assert entry.runtime_data.router.events_received_by_cipher(SN)["cloud"] == 1

    await wait_until(lambda: not entry.runtime_data.snapshots.busy, timeout=20)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_cloud_state_refresh_updates_entities_without_waking(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.devices[0] = standalone_entry(battery="40", t=1.8e9)
    await entry.runtime_data.eufy.async_refresh_cloud_state()
    await hass.async_block_till_done()

    batt_id = entity_id_for(hass, "sensor", SN, BATTERY_KEY)
    assert state_of(hass, batt_id) == "40"
    assert fake_station.conn_inits == 0
    assert fake_cloud.calls.count("devices") == 1


async def test_arming_wakes_the_camera_and_confirms(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    await set_up_warm(hass, seed_warm_cache)
    panel = entity_id_for(hass, "alarm_control_panel", SN, GUARD_MODE_KEY)
    await hass.services.async_call(
        "alarm_control_panel", "alarm_arm_away", {"entity_id": panel}, blocking=True
    )

    assert state_of(hass, panel) == "armed_away"
    assert fake_station.conn_inits >= 1

    batt_id = entity_id_for(hass, "sensor", SN, BATTERY_KEY)
    assert state_of(hass, batt_id) != STATE_UNAVAILABLE
    assert state_of(hass, panel) != STATE_UNAVAILABLE


async def test_a_wake_that_fails_is_a_retryable_user_error(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    await set_up_warm(hass, seed_warm_cache)
    fake_station.answer_conn_init = False
    panel = entity_id_for(hass, "alarm_control_panel", SN, GUARD_MODE_KEY)

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "alarm_control_panel", "alarm_arm_home", {"entity_id": panel}, blocking=True
        )

    assert exc.value.translation_key == "on_demand_unreachable"
    assert exc.value.translation_placeholders == {"target": "home"}

    assert state_of(hass, panel) == "armed_home"


def test_on_demand_arm_failure_keys():
    err1 = StationUnreachableError("x")
    ha_err1 = errors.arm_failed(err1, GuardMode.HOME, on_demand=True)
    assert ha_err1.translation_key == "on_demand_unreachable"

    ha_err2 = errors.arm_failed(err1, GuardMode.HOME, on_demand=False)
    assert ha_err2.translation_key == "station_unreachable"

    err3 = CommandNotAppliedError("x")
    ha_err3 = errors.arm_failed(err3, GuardMode.HOME, on_demand=True)
    assert ha_err3.translation_key == "guard_mode_not_applied"

    ha_err4 = errors.setting_write_failed(err1, "x", on_demand=True)
    assert ha_err4.translation_key == "on_demand_unreachable"

    ha_err5 = errors.setting_write_failed(err3, "x", on_demand=True)
    assert ha_err5.translation_key == "setting_not_applied"


async def test_diagnostics_describe_the_standalone_station(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    entry = await set_up_warm(hass, seed_warm_cache)
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    stations = result["stations"]
    assert len(stations) == 1
    station_dict = next(iter(stations.values()))

    assert station_dict["standalone"] is True
    assert station_dict["on_demand"] is True
    assert len(station_dict["devices"]) == 1
    assert station_dict["sub_devices"] == []

    assert SN not in json.dumps(result)


async def test_refresh_device_list_brings_in_a_new_standalone_station(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    fake_cloud.devices = []
    seed_warm_cache()
    entry = add_entry(hass)
    await setup_entry(hass, entry)

    assert not entry.runtime_data.coordinators
    before = len(built_clients)

    fake_cloud.devices.append(standalone_entry())

    btn_id = entity_id_for(hass, "button", entry.entry_id, "refresh_device_list")
    await hass.services.async_call("button", "press", {"entity_id": btn_id}, blocking=True)

    await wait_until(
        lambda: len(built_clients) == before + 1 and entry.state.value == "loaded", timeout=15
    )

    panel = entity_id_for(hass, "alarm_control_panel", SN, GUARD_MODE_KEY)
    assert state_of(hass, panel) == "armed_home"
    assert fake_cloud.calls.count("devices") == 1


async def test_a_confirmed_arm_survives_the_next_poll(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    await set_up_warm(hass, seed_warm_cache)
    panel = entity_id_for(hass, "alarm_control_panel", SN, GUARD_MODE_KEY)
    await hass.services.async_call(
        "alarm_control_panel", "alarm_arm_away", {"entity_id": panel}, blocking=True
    )
    assert state_of(hass, panel) == "armed_away"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()

    assert state_of(hass, panel) == "armed_away"


async def test_refresh_event_image_on_a_standalone_camera_shows_its_newest_still(
    hass: HomeAssistant, fake_cloud, seed_warm_cache, built_clients, fake_station
):
    entry = await set_up_warm(hass, seed_warm_cache)
    path = "/media/mmcblk0p1/Camera00/event/202609/20260917/20260917223859_snapshot.jpg"
    fake_station.event_summaries = {
        SN: {"event_count": 3, "crop_hb3_path": path, "crop_cloud_path": ""}
    }
    jpeg = b"\xff\xd8\xff\xe0" + bytes(400) + b"\xff\xd9"
    fake_station.images[path] = v1_still(jpeg, SN, did=SYNTHETIC.did)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "refresh_image")},
        blocking=True,
    )
    await wait_until(lambda: entry.runtime_data.snapshots.image_for(SN) is not None, timeout=15)

    assert entry.runtime_data.snapshots.source_for(SN).value == "thumbnail"

    camera_id = entity_id_for(hass, "camera", SN, "camera")
    assert (await async_get_image(hass, camera_id)).content == jpeg

    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes.get("triggered_at") == dt_util.as_utc(
        datetime(2026, 9, 17, 22, 38, 59, tzinfo=dt_util.get_default_time_zone())
    ).isoformat(timespec="milliseconds")

    assert fake_station.event_count_queries >= 1
    assert fake_station.conn_inits >= 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_arm_that_fails_while_the_session_is_replaced_points_at_repairs(
    hass: HomeAssistant,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    wakeable,
) -> None:
    """The library refuses the wake at once with ``SessionReplacedError``; the
    integration names the session from that error, not from the latch."""
    await set_up_warm(hass, seed_warm_cache)
    built_clients[-1].cache.set_replaced()
    fake_station.answer_conn_init = False

    panel = entity_id_for(hass, "alarm_control_panel", SN, GUARD_MODE_KEY)

    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_home",
            {"entity_id": panel},
            blocking=True,
        )

    assert exc.value.translation_key == "session_replaced_see_repairs"
    assert exc.value.translation_placeholders == {"target": "home"}
    assert state_of(hass, panel) == "armed_home"
    assert "login" not in fake_cloud.calls


def test_session_replaced_failure_keys() -> None:
    """The session is named from the error alone: a ``SessionReplacedError`` anywhere
    in the cause chain. A bare unreachable camera is the camera's, latch or not."""
    assert (
        errors.arm_failed(SessionReplacedError(), GuardMode.HOME, on_demand=True).translation_key
        == "session_replaced_see_repairs"
    )
    wrapped = StationUnreachableError("x")
    wrapped.__cause__ = SessionReplacedError()
    assert (
        errors.arm_failed(wrapped, GuardMode.HOME, on_demand=True).translation_key
        == "session_replaced_see_repairs"
    )
    assert (
        errors.arm_failed(StationUnreachableError("x"), GuardMode.HOME).translation_key
        == "station_unreachable"
    )
    assert (
        errors.arm_failed(
            StationUnreachableError("x"), GuardMode.HOME, on_demand=True
        ).translation_key
        == "on_demand_unreachable"
    )
    assert (
        errors.arm_failed(CommandNotAppliedError(1), GuardMode.HOME, on_demand=True).translation_key
        == "guard_mode_not_applied"
    )
    assert (
        errors.setting_write_failed(SessionReplacedError(), "x", on_demand=True).translation_key
        == "session_replaced_see_repairs"
    )

    assert errors.is_session_replaced_failure(SessionReplacedError()) is True
    assert errors.is_session_replaced_failure(TimeoutError()) is False
    assert errors.is_session_replaced_failure(StationUnreachableError("x")) is False

    reason_true = errors.failure_reason(SessionReplacedError())
    assert "session" in reason_true
    assert "Repairs" in reason_true
    assert "SessionReplacedError" in reason_true
    assert errors.failure_reason(StationUnreachableError("x")) == "StationUnreachableError"


async def test_a_live_capture_that_fails_while_the_session_is_replaced_names_the_session(
    hass: HomeAssistant,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    wakeable,
    caplog,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    built_clients[-1].cache.set_replaced()
    fake_station.answer_conn_init = False

    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_for(hass, "button", SN, "capture_live_image")},
        blocking=True,
    )

    await wait_until(lambda: "Live capture" in caplog.text and "failed" in caplog.text, timeout=30)
    await hass.async_block_till_done()

    failed_record = next(
        r for r in caplog.records if "Live capture" in r.message and "failed" in r.message
    )
    assert "session" in failed_record.message
    assert "Repairs" in failed_record.message

    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name.startswith("custom_components.eufy_home_security")
    ]
    assert not error_records

    assert "login" not in fake_cloud.calls
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── the T8170's model settings ───────────────────────────────────────────────


def _sent(station: FakeStation) -> int:
    """Every write frame the fake received: GCM commands, ECB scalars and 1700 recipes."""
    return len(station.received) + len(station.ecb_received) + len(station.doorbell_payloads)


def _t8170_settings() -> dict[str, Setting]:
    return dict(settings_of("T8170"))


async def test_a_standalone_camera_gets_its_model_settings(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    """Every T8170 setting with a platform is an entity named by the library.

    Controls are enabled config entities, settings with ``variant_of`` disabled; read-only
    values are diagnostic and disabled.
    The station's own list is the expectation; no key is written out here.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.coordinators[SN].station
    settings = station.settings_for(None)
    offered = [s for s in settings if setting_platform(s) is not None]
    assert offered, "the library offers the T8170 no setting, so this proves nothing"

    ent_reg = er.async_get(hass)
    for setting in offered:
        platform = setting_platform(setting)
        assert platform is not None
        # A flags setting is one switch per member, named "<setting>: <member>".
        members = list(setting.flags) if setting.control is SettingControl.TOGGLES else [None]
        for member in members:
            key = setting.key if member is None else f"{setting.key}_{member}"
            entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, entity_unique_id(SN, key))
            assert entity_id is not None, (key, platform)
            ent = ent_reg.async_get(entity_id)
            assert ent is not None
            name = (
                setting.name if member is None else f"{setting.name}: {setting.flag_label(member)}"
            )
            assert ent.original_name == name, key
            control = platform in ("number", "select", "switch", "text")
            assert ent.entity_category is (
                EntityCategory.CONFIG if control else EntityCategory.DIAGNOSTIC
            ), key
            assert (ent.disabled_by is None) is (control and setting.variant_of is None), key

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# T8170 settings the library wrote and read back on hardware, and their platform.
_T8170_CONTROLS: dict[str, str] = {
    "detection_sensitivity": "number",
    "motion_detection_status": "switch",
    "led_on_off": "switch",
    "nightvision_type_new": "select",
    "audio_recording_on_off": "switch",
    "ai_tracking_status": "switch",
    "live_streaming_resolution": "select",
    "disable_ptz_turn_switch": "switch",
    "spotlight_switch": "switch",
}


@pytest.mark.parametrize("key", sorted(_T8170_CONTROLS))
async def test_a_t8170_control_writes_and_shows_the_written_value(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud, key: str
):
    """Each is an enabled control; a write reaches the camera and the entity shows it.

    A select offers the library's labels. A setting the dump never reports
    (``readable=False``) is assumed state and shows the last value written.
    """
    if key == "live_streaming_resolution":
        report = {"mode_0": {"quality": 2}, "mode_1": {"quality": 0}, "cur_mode": 0}
        fake_station.params[48][2730] = base64.b64encode(json.dumps(report).encode()).decode()
        fake_station.params[48][6243] = "0"
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)
    setting = _t8170_settings()[key]
    platform = _T8170_CONTROLS[key]
    assert setting_platform(setting) == platform, key

    ent_reg = er.async_get(hass)
    entity_id = entity_id_for(hass, platform, SN, key)
    registered = ent_reg.async_get(entity_id)
    assert registered is not None
    assert registered.disabled_by is None
    assert registered.entity_category is EntityCategory.CONFIG
    assert registered.original_name == setting.name
    sent_before = _sent(fake_station)

    state = hass.states.get(entity_id)
    assert state is not None
    assert bool(state.attributes.get("assumed_state")) is not setting.readable
    if platform == "switch":
        target = "off" if state.state == "on" else "on"
        await hass.services.async_call(
            "switch", f"turn_{target}", {"entity_id": entity_id}, blocking=True
        )
    elif platform == "number":
        target = str(setting.minimum if state.state != str(setting.minimum) else setting.maximum)
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": target}, blocking=True
        )
    else:
        options = state.attributes["options"]
        assert options == [setting.label(v) or str(v) for v in setting.values]
        target = next(o for o in options if o != state.state)
        await hass.services.async_call(
            "select", "select_option", {"entity_id": entity_id, "option": target}, blocking=True
        )
    await hass.async_block_till_done()

    assert state_of(hass, entity_id) == target
    assert _sent(fake_station) > sent_before, key
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_t8170_streaming_quality_write_restarts_its_live_view(
    hass: HomeAssistant,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    hass_client_no_auth,
    caplog,
):
    """A ``live_streaming_resolution`` write ends a running view, found by the station serial."""
    report = {"mode_0": {"quality": 2}, "mode_1": {"quality": 0}, "cur_mode": 0}
    fake_station.params[48][2730] = base64.b64encode(json.dumps(report).encode()).decode()
    fake_station.params[48][6243] = "0"
    fake_station.reply_to_settings = True
    entry = await set_up_warm(hass, seed_warm_cache)

    camera_id = entity_id_for(hass, "camera", SN, "camera")
    url = await async_get_stream_source(hass, camera_id)
    assert url is not None
    client = await hass_client_no_auth()
    response = await client.get(urlparse(url)._replace(scheme="", netloc="").geturl())
    await wait_until(lambda: fake_station.streaming, timeout=30)

    select_id = entity_id_for(hass, "select", SN, "live_streaming_resolution")
    current = state_of(hass, select_id)
    select_state = hass.states.get(select_id)
    assert select_state is not None
    target = next(o for o in select_state.attributes["options"] if o != current)
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select_id, "option": target}, blocking=True
    )
    await wait_until(lambda: not fake_station.streaming, timeout=20)

    assert state_of(hass, select_id) == target
    assert "a setting changed the picture the camera sends" in caplog.text
    await response.content.read()
    response.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_sensor_left_under_a_control_key_is_forgotten_at_setup(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    """A read-only sensor row under a control's unique id is removed; another entry's is kept."""
    seed_warm_cache()
    entry = add_entry(hass)
    other = add_entry(hass, email="other@example.com", unique_id="other@example.com")
    ent_reg = er.async_get(hass)
    keys = sorted(_T8170_CONTROLS)
    for key in keys:
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            entity_unique_id(SN, key),
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )
    foreign = ent_reg.async_get_or_create(
        "sensor", DOMAIN, entity_unique_id(SN, "foreign_key"), config_entry=other
    )

    assert await setup_entry(hass, entry)

    for key in keys:
        unique_id = entity_unique_id(SN, key)
        assert ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id) is None, key
        control = ent_reg.async_get_entity_id(_T8170_CONTROLS[key], DOMAIN, unique_id)
        assert control is not None, key
    assert ent_reg.async_get(foreign.entity_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── a standalone camera's detection image ────────────────────────────────────

_STILL = b"\xff\xd8\xff\xe0" + bytes(400) + b"\xff\xd9"
_OLDER_STILL = b"\xff\xd8\xff\xe0OLDER" + bytes(400) + b"\xff\xd9"
# What tests/fake_ffmpeg.py answers for a live keyframe that decrypted: a 3840x2160 JPEG.
_DECODED_4K_PREFIX = b"\xff\xd8\xff\xc0\x00\x11\x08\x08\x70\x0f\x00"
# A T8170's resolution climb after a wake.
_T8170_CLIMB = [(1280, 720), (1920, 1080), (2880, 1616)]


@pytest.fixture(autouse=True)
def short_size_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full-resolution live image waits this long for the picture size to settle."""
    monkeypatch.setattr(eufy_home_security.station, "SETTLE_STANDALONE", 0.1)


def _serve_newest_still(station: FakeStation, t_ms: int, jpeg: bytes) -> None:
    """The camera's newest event still is now one written at ``t_ms``.

    Named in the camera's clock: the host's zone, since the fake reports no
    ``timezone_set``. The library matches it to a detection within (-2, +30) s.
    """
    stamp = datetime.fromtimestamp(t_ms / 1000).astimezone().strftime("%Y%m%d%H%M%S")
    path = f"/media/mmcblk0p1/Camera00/event/{stamp[:6]}/{stamp[:8]}/{stamp}_snapshot.jpg"
    station.event_summaries = {SN: {"event_count": 3, "crop_hb3_path": path, "crop_cloud_path": ""}}
    station.images[path] = v1_still(jpeg, SN, did=SYNTHETIC.did)


def _person_push(t_ms: int, unique_id: str, **extra: object) -> dict:
    return {
        "msg_type": 18,
        "event_type": int(DetectionType.PERSON),
        "device_sn": SN,
        "station_sn": SN,
        "channel": 0,
        "trigger_time": t_ms,
        "unique_id": unique_id,
        **extra,
    }


def _second_of_pair(t_ms: int) -> dict:
    """The pair's second push as a T8170 sends it: a re-announcement naming the recording."""
    return _person_push(t_ms, "b" * 32, push_count=2, file_path="/zx/Camera00/clip.zxvideo")


def _deliver(entry, inner: dict) -> None:
    """Hand a cloud push to the router as the client does, through its de-duplicator."""
    deduplicator = entry.runtime_data.eufy.deduplicator
    assert deduplicator is not None
    admitted = deduplicator.admit(_cloud_push(inner))
    assert admitted is not None
    entry.runtime_data.router.handle(admitted)


def _history_stills(hass: HomeAssistant) -> list[str]:
    root = history.history_dir(hass)
    return sorted(p.name for p in root.rglob("*.jpg")) if root.is_dir() else []


async def _fire_after(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _fire_standalone_first(hass: HomeAssistant) -> None:
    """Run the delayed first thumbnail attempt."""
    await _fire_after(hass, STANDALONE_THUMBNAIL_DELAY_SECONDS + 1)


async def _fire_standalone_retry(hass: HomeAssistant) -> None:
    await _fire_after(hass, STANDALONE_IMAGE_RETRY_DELAY_SECONDS + 1)


def _iso(t_ms: int) -> str:
    return dt_util.utc_from_timestamp(t_ms / 1000).isoformat(timespec="milliseconds")


def _thumbnail_mode() -> dict[str, str]:
    return {CONF_CAMERA_IMAGE: "thumbnail"}


async def test_in_thumbnail_mode_a_cloud_detection_fetches_its_still_after_the_delay(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
):
    """No live capture and nothing at once: the detection's still (named 2 s after it,
    as a T8170 writes it) is fetched once, after the delay, and shown as the detection's
    thumbnail at the detection's time."""
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    fetches = _count_fetches(monkeypatch)
    t = now_ms() - 3000
    _serve_newest_still(fake_station, t + 2000, _STILL)

    _deliver(entry, _person_push(t, "a" * 32))
    await hass.async_block_till_done()
    assert not manager.busy
    assert fetches == []
    assert fake_station.event_count_queries == 0

    await _fire_standalone_first(hass)
    await wait_until(lambda: manager.image_for(SN) is not None, timeout=20)
    await wait_until(lambda: not manager.busy)

    assert fetches == ["thumbnail"]
    assert manager.image_for(SN) == _STILL
    camera_id = entity_id_for(hass, "camera", SN, CAMERA_KEY)
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes["image_source"] == "thumbnail"
    assert state.attributes["triggered_at"] == _iso(t)
    assert "image_updated" in state.attributes
    assert fake_station.event_count_queries == 1
    await wait_until(lambda: len(_history_stills(hass)) == 1)
    assert _history_stills(hass)[0].endswith("_person.jpg")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("mode", ["hd", "hd_only"])
async def test_in_hd_modes_a_cloud_detection_shows_a_live_keyframe_as_its_hd_image(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    caplog,
    mode: str,
):
    """The camera is woken at once for one live keyframe at its full resolution, past
    the climb, decoded and shown as the detection's HD image at the detection's time,
    and saved as its still. In hd the delayed thumbnail is fetched too and saved beside
    it, never shown over it; hd_only fetches no thumbnail."""
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: mode})
    manager = entry.runtime_data.snapshots
    fetches = _count_fetches(monkeypatch)
    t = now_ms()
    _serve_newest_still(fake_station, t, _STILL)
    fake_station.live_keyframe_sizes = list(_T8170_CLIMB)

    _deliver(entry, _person_push(t, "a" * 32))
    await wait_until(lambda: manager.image_for(SN) is not None, timeout=20)
    await wait_until(lambda: not manager.busy)

    hd = manager.image_for(SN)
    assert hd is not None and hd.startswith(_DECODED_4K_PREFIX)
    camera_id = entity_id_for(hass, "camera", SN, CAMERA_KEY)
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes["image_source"] == "detection_live"
    assert state.attributes["triggered_at"] == _iso(t)
    assert fetches == ["live"]
    hd_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.") and "Detection HD image of" in r.getMessage()
    ]
    assert any("fetched 2880x1616" in line for line in hd_lines), hd_lines
    assert not any("1280x720" in line or "size unknown" in line for line in hd_lines)
    await wait_until(lambda: len(_history_stills(hass)) == 1)
    assert _history_stills(hass)[0].endswith("_person.jpg")

    await _fire_standalone_first(hass)
    await wait_until(lambda: not manager.busy, timeout=20)
    await _fire_standalone_retry(hass)
    await wait_until(lambda: not manager.busy, timeout=20)
    assert manager.image_for(SN) == hd
    assert manager.source_for(SN).value == "detection_live"
    state = hass.states.get(camera_id)
    assert state is not None
    assert state.attributes["triggered_at"] == _iso(t)
    if mode == "hd":
        assert fetches == ["live", "thumbnail"]
        await wait_until(lambda: len(_history_stills(hass)) == 2)
        names = _history_stills(hass)
        assert names[0].endswith("_person.jpg")
        assert names[1].endswith("_person_thumbnail.jpg")
        assert names[0].removesuffix("_person.jpg") == names[1].removesuffix(
            "_person_thumbnail.jpg"
        )
    else:
        assert fetches == ["live"]
        assert fake_station.event_count_queries == 0
        assert len(_history_stills(hass)) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _fail_live(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """Make the detection's live keyframe fail: the capture itself, or its decode."""
    if how == "decode":
        monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
        return
    for name in ("async_camera_image", "async_event_image"):
        orig = getattr(Station, name)

        async def failing(self, subject, source, *args, _orig=orig, **kw):
            if source is ImageSource.LIVE:
                raise CommunicationError("the camera's stream is open")
            return await _orig(self, subject, source, *args, **kw)

        monkeypatch.setattr(Station, name, failing)


@pytest.mark.parametrize("second", ["in_flight", "after"])
@pytest.mark.parametrize("how", ["capture", "decode"])
async def test_in_hd_mode_a_failed_live_capture_leaves_the_delayed_thumbnail(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    how: str,
    second: str,
):
    """No HD image: the delayed thumbnail is shown. The pair's second push, during the
    capture or after its failure, does not wake the camera for another capture."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = entry.runtime_data.snapshots
    _fail_live(monkeypatch, how)
    t = now_ms()
    _serve_newest_still(fake_station, t, _STILL)

    def second_push() -> None:
        _deliver(entry, _second_of_pair(t))

    fetches = _count_fetches(monkeypatch, second_push if second == "in_flight" else None)
    _deliver(entry, _person_push(t, "a" * 32))
    await wait_until(lambda: fetches == ["live"] and not manager.busy, timeout=20)
    await hass.async_block_till_done()
    assert manager.image_for(SN) is None
    if second == "after":
        second_push()
    await hass.async_block_till_done()
    assert not manager.busy

    await _fire_standalone_first(hass)
    await wait_until(lambda: manager.image_for(SN) is not None, timeout=20)
    await wait_until(lambda: not manager.busy)

    assert fetches == ["live", "thumbnail"]
    assert manager.image_for(SN) == _STILL
    state = hass.states.get(entity_id_for(hass, "camera", SN, CAMERA_KEY))
    assert state is not None
    assert state.attributes["image_source"] == "thumbnail"
    assert state.attributes["triggered_at"] == _iso(t)
    await wait_until(lambda: len(_history_stills(hass)) == 1)
    assert _history_stills(hass)[0].endswith("_person.jpg")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_in_hd_only_mode_a_failed_live_capture_shows_nothing(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
):
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_CAMERA_IMAGE: "hd_only"})
    manager = entry.runtime_data.snapshots
    _fail_live(monkeypatch, "capture")
    fetches = _count_fetches(monkeypatch)
    t = now_ms()
    _serve_newest_still(fake_station, t, _STILL)

    _deliver(entry, _person_push(t, "a" * 32))
    await wait_until(lambda: fetches == ["live"] and not manager.busy, timeout=20)
    await _fire_standalone_first(hass)
    await _fire_standalone_retry(hass)
    await wait_until(lambda: not manager.busy)

    assert fetches == ["live"]
    assert fake_station.event_count_queries == 0
    assert manager.image_for(SN) is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _count_fetches(
    monkeypatch: pytest.MonkeyPatch, on_live: Callable[[], None] | None = None
) -> list[str]:
    """Record the source of every ``Station`` image call (``async_camera_image``,
    ``async_event_image``): each one wakes the camera. ``live`` is a detection's
    full-resolution live image; any other live call is ``live_other``.

    ``on_live`` runs as a live capture starts, before the library is called.
    """
    calls: list[str] = []
    for name in ("async_camera_image", "async_event_image"):
        orig = getattr(Station, name)

        async def counting(self, subject, source, *args, _orig=orig, _name=name, **kw):
            full = _name == "async_event_image" and kw.get("full_resolution") is True
            other = source is ImageSource.LIVE and not full
            calls.append("live_other" if other else source.value)
            if on_live is not None and source is ImageSource.LIVE:
                on_live()
            return await _orig(self, subject, source, *args, **kw)

        monkeypatch.setattr(Station, name, counting)
    return calls


@pytest.mark.parametrize("second", ["queued", "in_flight", "after_shown"])
async def test_the_push_pair_of_one_detection_captures_one_live_keyframe(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    second: str,
):
    """In hd the pair's second push, which names the recording, joins the queued or
    running capture, or is ignored once the HD image is shown: one live keyframe and one
    thumbnail per detection, and no recording is played."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = entry.runtime_data.snapshots
    t = now_ms()
    _serve_newest_still(fake_station, t, _STILL)
    enriched: list[bool] = []

    def second_push() -> None:
        deduplicator = entry.runtime_data.eufy.deduplicator
        admitted = deduplicator.admit(_cloud_push(_second_of_pair(t)))
        assert admitted is not None and admitted.video_path is not None
        enriched.append(admitted.enriches)
        entry.runtime_data.router.handle(admitted)

    fetches = _count_fetches(monkeypatch, second_push if second == "in_flight" else None)
    _deliver(entry, _person_push(t, "a" * 32))
    if second == "queued":
        second_push()
    await wait_until(lambda: manager.image_for(SN) is not None and not manager.busy, timeout=20)
    if second == "after_shown":
        second_push()
    await hass.async_block_till_done()
    await _fire_standalone_first(hass)
    await wait_until(lambda: not manager.busy, timeout=20)
    await _fire_standalone_retry(hass)
    await wait_until(lambda: not manager.busy, timeout=20)

    assert enriched == [True]
    assert fetches == ["live", "thumbnail"]
    assert fake_station.history_queries == []
    assert manager.source_for(SN).value == "detection_live"
    assert manager.event_time_for(SN) == t
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_later_detection_during_the_live_capture_earns_its_own(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
):
    """A detection with a later time while a capture runs gets one more capture after it,
    shown at its own time."""
    entry = await set_up_warm(hass, seed_warm_cache)
    manager = entry.runtime_data.snapshots
    first = now_ms() - 30_000
    later = now_ms()
    pushed: list[bool] = []

    def later_push() -> None:
        if not pushed:
            pushed.append(True)
            _deliver(entry, _person_push(later, "b" * 32))

    fetches = _count_fetches(monkeypatch, later_push)
    _deliver(entry, _person_push(first, "a" * 32))
    await wait_until(lambda: fetches == ["live", "live"] and not manager.busy, timeout=30)

    assert manager.source_for(SN).value == "detection_live"
    assert manager.event_time_for(SN) == later
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("second", ["owed", "in_flight", "after_shown"])
async def test_the_push_pair_of_one_detection_wakes_the_camera_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
    second: str,
):
    """In thumbnail mode the second push of a T8170's pair (same time, another unique
    id) joins the owed or running fetch, and after the image is shown fetches nothing."""
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    fetches = _count_fetches(monkeypatch)
    t = now_ms()
    _serve_newest_still(fake_station, t, _STILL)
    if second == "in_flight":
        # The camera ignores the first query: the library asks again after 2 s.
        fake_station.event_count_ignored = 1

    _deliver(entry, _person_push(t, "a" * 32))
    if second != "owed":
        await _fire_standalone_first(hass)
    if second == "in_flight":
        await wait_until(lambda: fake_station.event_count_queries == 1, timeout=20)
        assert manager.busy
    elif second == "after_shown":
        await wait_until(lambda: manager.image_for(SN) is not None and not manager.busy, timeout=20)
    _deliver(entry, _person_push(t, "b" * 32))
    await _fire_standalone_first(hass)
    await wait_until(lambda: manager.image_for(SN) is not None, timeout=20)
    await wait_until(lambda: not manager.busy)
    await _fire_standalone_retry(hass)
    await hass.async_block_till_done()

    assert fetches == ["thumbnail"]
    assert manager.image_for(SN) == _STILL
    assert manager.event_time_for(SN) == t
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_newer_detection_during_the_fetch_raises_the_bar(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    seed_warm_cache,
    built_clients,
    fake_station,
    fake_cloud,
):
    """A detection that joins a running fetch is the one its still must belong to: a still
    of the first detection is not shown for it, and the later fetch shows its own."""
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    fetches = _count_fetches(monkeypatch)
    first = now_ms() - 30_000
    later = now_ms()
    _serve_newest_still(fake_station, first, _OLDER_STILL)
    fake_station.event_count_ignored = 1

    _deliver(entry, _person_push(first, "a" * 32))
    await _fire_standalone_first(hass)
    await wait_until(lambda: fake_station.event_count_queries == 1, timeout=20)
    _deliver(entry, _person_push(later, "b" * 32))
    await wait_until(lambda: len(fetches) == 1 and not manager.busy, timeout=20)
    await hass.async_block_till_done()
    assert manager.image_for(SN) is None

    _serve_newest_still(fake_station, later, _STILL)
    await _fire_standalone_retry(hass)
    await wait_until(lambda: manager.image_for(SN) is not None, timeout=20)
    await wait_until(lambda: not manager.busy)
    assert fetches == ["thumbnail", "thumbnail"]
    assert manager.image_for(SN) == _STILL
    assert manager.event_time_for(SN) == later
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_still_older_than_the_detection_is_retried_once_later(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud
):
    """The newest still predates the detection: nothing shown, one later fetch shows it.
    A push of the same detection meanwhile waits for that fetch."""
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    t = now_ms()
    _serve_newest_still(fake_station, t - 60_000, _OLDER_STILL)

    _deliver(entry, _person_push(t, "a" * 32))
    await _fire_standalone_first(hass)
    await wait_until(lambda: fake_station.event_count_queries == 1 and not manager.busy, timeout=20)
    await hass.async_block_till_done()
    assert manager.image_for(SN) is None
    # The pair's second push joins the owed later fetch: no wake now.
    _deliver(entry, _person_push(t, "b" * 32))
    await hass.async_block_till_done()
    assert not manager.busy
    assert fake_station.event_count_queries == 1

    _serve_newest_still(fake_station, t, _STILL)
    await _fire_standalone_retry(hass)
    await wait_until(lambda: manager.image_for(SN) is not None, timeout=20)
    await wait_until(lambda: not manager.busy)

    assert manager.image_for(SN) == _STILL
    state = hass.states.get(entity_id_for(hass, "camera", SN, CAMERA_KEY))
    assert state is not None
    assert state.attributes["image_source"] == "thumbnail"
    assert state.attributes["triggered_at"] == _iso(t)
    assert fake_station.event_count_queries == 2

    await _fire_standalone_retry(hass)
    await hass.async_block_till_done()
    assert fake_station.event_count_queries == 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_retry_that_finds_an_older_still_again_gives_up_and_keeps_the_image(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud, caplog
):
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    first = now_ms() - 120_000
    _serve_newest_still(fake_station, first, _OLDER_STILL)
    _deliver(entry, _person_push(first, "a" * 32))
    await _fire_standalone_first(hass)
    await wait_until(lambda: manager.image_for(SN) == _OLDER_STILL, timeout=20)
    await wait_until(lambda: not manager.busy)

    t = now_ms()
    _deliver(entry, _person_push(t, "b" * 32))
    await _fire_standalone_first(hass)
    await wait_until(lambda: fake_station.event_count_queries == 2 and not manager.busy, timeout=20)
    await _fire_standalone_retry(hass)
    await wait_until(lambda: fake_station.event_count_queries == 3 and not manager.busy, timeout=20)
    await _fire_standalone_retry(hass)
    await hass.async_block_till_done()

    assert fake_station.event_count_queries == 3
    assert manager.image_for(SN) == _OLDER_STILL
    assert manager.event_time_for(SN) == first
    assert "gave up" in caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_later_detections_still_is_given_up_at_once(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud, caplog
):
    """The camera's newest still is a later detection's: nothing shown, no retry."""
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    t = now_ms() - 120_000
    _serve_newest_still(fake_station, t + 60_000, _STILL)

    _deliver(entry, _person_push(t, "a" * 32))
    await _fire_standalone_first(hass)
    await wait_until(lambda: fake_station.event_count_queries == 1 and not manager.busy, timeout=20)
    await _fire_standalone_retry(hass)
    await hass.async_block_till_done()

    assert fake_station.event_count_queries == 1
    assert manager.image_for(SN) is None
    assert "no still of this detection, gave up" in caplog.text
    assert manager._cameras[SN].cancel_standalone_thumbnail is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("owed", ["first", "later"])
async def test_unload_cancels_an_owed_standalone_fetch(
    hass: HomeAssistant, seed_warm_cache, built_clients, fake_station, fake_cloud, owed: str
):
    entry = await set_up_warm(hass, seed_warm_cache, options=_thumbnail_mode())
    manager = entry.runtime_data.snapshots
    t = now_ms()
    _serve_newest_still(fake_station, t - 60_000, _OLDER_STILL)
    _deliver(entry, _person_push(t, "a" * 32))
    await hass.async_block_till_done()
    if owed == "later":
        await _fire_standalone_first(hass)
        await wait_until(
            lambda: fake_station.event_count_queries == 1 and not manager.busy, timeout=20
        )
    camera = manager._cameras[SN]
    assert camera.cancel_standalone_thumbnail is not None
    queries = fake_station.event_count_queries

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert camera.cancel_standalone_thumbnail is None
    _serve_newest_still(fake_station, t, _STILL)
    await _fire_standalone_retry(hass)
    await hass.async_block_till_done()
    assert fake_station.event_count_queries == queries
    assert manager.image_for(SN) is None
