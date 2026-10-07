"""Fixtures for the integration tests, built on ``eufy_home_security.testing``.

The tests run the real library end to end: a real ``EufySecurity`` with its real
cache, throttle and claims, wired to the library's ``FakeCloud`` and a loopback
``FakeStation``. They never mock the library, and they use the synthetic
identities in ``eufy_home_security.testing.SYNTHETIC`` only, never a real serial,
P2P id, owner account id or e-mail.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from eufy_home_security import (
    DEFAULT_STATION_SESSIONS,
    AlarmChanged,
    AlarmStopSource,
    DetectionType,
    EufySecurity,
    EventSource,
    FrameCipher,
    PushMessageType,
    SecurityEvent,
    StationClaims,
    entity_unique_id,
)
from eufy_home_security import station as station_module
from eufy_home_security.devices import Scope, model_for_serial
from eufy_home_security.devices.model_settings import mode_table_settings, settings_of
from eufy_home_security.p2p import session as session_module
from eufy_home_security.testing import (
    SYNTHETIC,
    FakeCloud,
    FakeStation,
    build_eufy_security,
    camera_device,
    warm_store,
)
from fake_ffmpeg import FAKE_FFMPEG_PREFIX
from homeassistant.components.alarm_control_panel import DOMAIN as ALARM_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, CONF_EMAIL, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.eufy_home_security import runtime, snapshots
from custom_components.eufy_home_security.config_flow import EufyHomeSecurityConfigFlow
from custom_components.eufy_home_security.const import (
    DOMAIN,
    GUARD_MODE_KEY,
)

__all__ = [
    "PUSHED_THUMBNAIL",
    "PUSHED_THUMB_PATH",
    "SENSOR_SN",
    "SYNTHETIC",
    "add_entry",
    "add_motion_sensor",
    "advance_to_poll",
    "cloud_calls",
    "detection_event",
    "entity_id_for",
    "now_ms",
    "panel_entity_id",
    "record_states",
    "seed_setting",
    "set_guard_mode",
    "set_up_warm",
    "setting_param",
    "setup_entry",
    "stamp_product_codes",
    "state_of",
    "station_event",
    "wait_until",
]


@pytest.fixture(autouse=True)
def _loopback_sockets(socket_enabled: None) -> None:
    """Let the library fakes open their sockets.

    The HA test harness blocks sockets for every test, and the fakes speak UDP on
    127.0.0.1, so without this every setup fails with ``HASocketBlockedError``.
    The cloud is faked below HTTP and ``built_clients`` replaces the
    construction site, so nothing leaves loopback.
    """


@pytest.fixture(autouse=True)
def _fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decode camera keyframes with ``tests/fake_ffmpeg.py``, never the host's ffmpeg.

    Every detection a test pushes also fetches the camera's trigger frame, so without
    this a test would run whatever ffmpeg the host has installed.
    """
    monkeypatch.setattr(snapshots, "ffmpeg_command", lambda _hass: shlex.split(FAKE_FFMPEG_PREFIX))


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A config directory per test: the still cache writes under its ``.cache``."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def _media_dir_per_test(hass: HomeAssistant, hass_tmp_config_dir: str) -> None:
    """A media folder per test, inside its config directory, for the event history.

    The harness points ``local`` media at a directory inside its installed package.
    """
    hass.config.media_dirs = {"local": os.path.join(hass_tmp_config_dir, "media")}


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load ``custom_components``."""


# A worker imports Home Assistant and collects the suite before its first test (about
# 3 s), so below this many tests one process finishes first; ten tests per worker above.
_TESTS_PER_WORKER: Final = 10
_MIN_DISTRIBUTED_TESTS: Final = 40
_TEST_DEF = re.compile(r"^(?:async )?def test_", re.MULTILINE)


def _estimated_tests(config: pytest.Config) -> int:
    """Test functions the command line names, counted from source (parametrize ignored)."""
    count = 0
    for arg in config.args:
        path, _, node = arg.partition("::")
        target = Path(path)
        if node:
            count += 1
        elif target.is_dir():
            count += sum(len(_TEST_DEF.findall(f.read_text())) for f in target.rglob("test_*.py"))
        elif target.is_file():
            count += len(_TEST_DEF.findall(target.read_text()))
    return count


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Workers for ``-n auto``: none for a few tests, else up to one per CPU."""
    tests = _estimated_tests(config)
    if tests < _MIN_DISTRIBUTED_TESTS:
        return 0
    return min(os.cpu_count() or 1, tests // _TESTS_PER_WORKER)


# The station's guard-mode parameter in its dump. The fake's `guard_mode` field
# reaches the dump only after a guard-mode command, so a fresh fake reports no
# mode at all; the library's own tests seed it this way (tests/test_station.py).
_GUARD_MODE_PARAM = 1224
_STATION_BLOCK = 255


def set_guard_mode(station: FakeStation, mode: int) -> None:
    """Put ``mode`` on the fake station as a mode changed on the station itself.

    Sets the fake's field and its dump together: the dump is what a poll reads,
    and the fake copies the field into it only when a guard-mode command arrives.
    """
    station.guard_mode = mode
    station.params[_STATION_BLOCK][_GUARD_MODE_PARAM] = str(mode)


# A T8910 motion sensor's serial. The library's ``SYNTHETIC`` has no sensor serial,
# so this is the one its own tests use.
SENSOR_SN: Final = "T8910P0000000001"

# The thumbnail path ``FakeStation.push_camera_event`` carries, and a small JPEG the
# fixture station serves for it.
PUSHED_THUMB_PATH: Final = "/zx/thumb.jpg"
PUSHED_THUMBNAIL: Final = b"\xff\xd8PUSHED-THUMBNAIL\xff\xd9"


# The model of each fake-station block: the station (T8030) and the synthetic camera.
_BLOCK_MODELS: Final = {_STATION_BLOCK: "T8030", 0: "T8160"}


def setting_param(key: str, *, model: str = "T8160") -> int:
    """The parameter that reports setting ``key`` of ``model`` (a per-mode key on any camera)."""
    found = settings_of(model).get(key) or next(
        (s for s in mode_table_settings(Scope.CAMERA) if s.key == key), None
    )
    assert found is not None and found.read_param is not None, f"{model} reports no {key}"
    return found.read_param


def seed_setting(
    station: FakeStation, key: str, raw: int | str, *, channel: int = 0, model: str | None = None
) -> None:
    """Put a setting's wire value in the fake station's dump, before it is read.

    The parameter comes from the library's model file, so a model change cannot leave
    a fixture seeding a parameter nothing reads.
    """
    param = setting_param(key, model=model or _BLOCK_MODELS[channel])
    station.params.setdefault(channel, {})[param] = str(raw)


def add_motion_sensor(
    station: FakeStation, cloud: FakeCloud, *, channel: int = 1, name: str = "Yard"
) -> None:
    """Pair a T8910 motion sensor to the fake station, with a battery and a sub-1 GHz signal.

    Call it BEFORE ``set_up_warm`` for a device that is present at setup: the warm
    cache copies the cloud's device list when it is seeded, so a device added after
    that is not in the cached list the entry is set up from.
    """
    cloud.devices.append(
        camera_device(SENSOR_SN, station_sn=station.serial, channel=channel, name=name)
    )
    station.params[channel] = {1101: "90", 1141: "-70"}


@pytest.fixture
async def fake_station() -> AsyncIterator[FakeStation]:
    """A started loopback HomeBase in away mode, stopped after the test.

    Its parameter dump carries its ``guard_mode`` field from the start, as a real
    station's does.
    """
    station = FakeStation()
    set_guard_mode(station, station.guard_mode)
    # The thumbnail its ``push_camera_event`` names, so the camera's still fetch that
    # push starts is answered: the fake raises for a path it does not hold.
    station.images[PUSHED_THUMB_PATH] = PUSHED_THUMBNAIL
    await station.start()
    yield station
    station.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    """A cloud that lists exactly the started fake station.

    A cloud listing a station without a started fake would send discovery to the
    synthetic documentation-range address instead of loopback.
    """
    return FakeCloud.for_stations(fake_station)


def cloud_calls(cloud: FakeCloud) -> list[str]:
    """``cloud.calls`` without the model scan (``things``) every first discovery makes."""
    return [call for call in cloud.calls if call != "things"]


def stamp_product_codes(cloud: FakeCloud) -> None:
    """Give each catalogued device of ``cloud`` its product code, as the real device list does.

    The library's fake entries carry no ``device_new_pn``; setup refreshes a cached
    list that lacks one.
    """
    for device in cloud.devices:
        model = model_for_serial(str(device.get("device_sn", "")))
        if model is not None:
            device.setdefault("device_new_pn", model.model)


@pytest.fixture
def built_clients(
    monkeypatch: pytest.MonkeyPatch, fake_cloud: FakeCloud, fake_station: FakeStation
) -> list[EufySecurity]:
    """Replace ``runtime.build_client``; returns every client built, in order.

    The replacement keeps Home Assistant's real account ``Store`` and forwards the
    caller's password as it is, ``None`` from setup included, never the testing
    helper's synthetic default.
    """
    built: list[EufySecurity] = []

    def build(
        hass: HomeAssistant,
        email: str,
        password: str | None,
        *,
        claims: StationClaims | None = None,
        max_sessions: int = DEFAULT_STATION_SESSIONS,
        scan_regions: bool = False,
    ) -> EufySecurity:
        stamp_product_codes(fake_cloud)
        eufy = build_eufy_security(
            email=email,
            store=runtime.cache_store(hass, email),
            cloud=fake_cloud,
            stations={fake_station.serial: fake_station},
            password=password,
            claims=claims,
            max_sessions=max_sessions,
            scan_regions=scan_regions,
        )
        built.append(eufy)
        return eufy

    monkeypatch.setattr(runtime, "build_client", build)
    return built


@pytest.fixture
def seed_warm_cache(hass_storage: dict[str, Any], fake_cloud: FakeCloud) -> Callable[..., None]:
    """Seed the account store as one earlier login left it; returns ``seed(email=...)``.

    The document lands under ``runtime.store_key(email)``, the key the entry's
    ``Store`` reads, and is written by the library's own cache writers
    (``warm_store``), so a setup on it makes no cloud call.
    """

    def seed(email: str = SYNTHETIC.email) -> None:
        stamp_product_codes(fake_cloud)
        key = runtime.store_key(email)
        hass_storage[key] = {
            "version": 1,
            "minor_version": 1,
            "key": key,
            "data": warm_store(email=email, cloud=fake_cloud).data,
        }

    return seed


@pytest.fixture
def short_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """One short LAN search, so an unreachable station fails in a third of a second.

    The library's defaults are three searches of six seconds each.
    """
    monkeypatch.setattr(session_module, "DISCOVERY_ATTEMPTS", 1)
    monkeypatch.setattr(session_module, "DISCOVERY_TIMEOUT", 0.3)


@pytest.fixture
def short_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-second handshake, so a station that never answers CONN_INIT fails fast.

    The library waits 6 s for a real station.
    """
    monkeypatch.setattr(session_module, "HANDSHAKE_TIMEOUT", 0.5)


@pytest.fixture
def short_media_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream with no frame for half a second ends; the library waits 3 s."""
    monkeypatch.setattr(session_module, "MEDIA_IDLE_TIMEOUT", 0.5)


@pytest.fixture
def short_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting read-back retries 50 ms apart; the library waits 0.8 s between dumps."""
    monkeypatch.setattr(station_module, "READBACK_DELAY", 0.05)


def add_entry(
    hass: HomeAssistant,
    *,
    email: str = SYNTHETIC.email,
    data: dict[str, Any] | None = None,
    unique_id: str | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """One account entry as the config flow creates it, added but not set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=email,
        unique_id=email if unique_id is None else unique_id,
        data={CONF_EMAIL: email} if data is None else data,
        options=options or {},
        version=EufyHomeSecurityConfigFlow.VERSION,
        minor_version=EufyHomeSecurityConfigFlow.MINOR_VERSION,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Set up ``entry`` and let everything its setup started finish."""
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


async def advance_to_poll(
    hass: HomeAssistant, seconds: float, *, anchor: datetime | None = None
) -> None:
    """Move Home Assistant's clock forward and let any poll it starts finish.

    ``anchor`` is the moment ``seconds`` is measured from, and it matters whenever
    the question is which side of the interval boundary a sample falls on.
    ``async_fire_time_changed`` decides whether a timer is due by comparing the
    requested offset against how much real time is left on that timer, so an
    offset measured from "now" silently shrinks by however long the test spent
    getting here: a 44-second sample against a 45-second interval fires a poll
    once real setup costs more than the helper's slack. Anchor on a moment the
    test knows the schedule was set, and the comparison becomes exact. Defaults
    to now, which is right for any sample comfortably past the boundary.
    """
    at = (anchor or dt_util.utcnow()) + timedelta(seconds=seconds)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for ``predicate`` to hold, polling the loop; fail after ``timeout`` seconds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def set_up_warm(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    *,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Seed the warm cache, add the synthetic account's entry and set it up."""
    seed_warm_cache()
    entry = add_entry(hass, options=options)
    assert await setup_entry(hass, entry)
    return entry


def panel_entity_id(hass: HomeAssistant) -> str:
    """The synthetic station's guard-mode panel, found by its unique id."""
    entity_id = er.async_get(hass).async_get_entity_id(
        ALARM_DOMAIN, DOMAIN, entity_unique_id(SYNTHETIC.station_sn, GUARD_MODE_KEY)
    )
    assert entity_id is not None
    return entity_id


def entity_id_for(hass: HomeAssistant, domain: str, serial: str, key: str) -> str:
    """The entity of ``key`` on the device ``serial``, found by its unique id.

    Unique ids are scoped per platform domain, so a ``firmware`` sensor and a
    ``firmware`` update entity of one device do not collide.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, entity_unique_id(serial, key)
    )
    assert entity_id is not None
    return entity_id


def state_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """The entity's current state string, or None when it has no state."""
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def record_states(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Every state ``entity_id`` is written with from now on, in order."""
    states: list[str] = []

    @callback
    def _on_change(event: Event[Any]) -> None:
        if event.data[ATTR_ENTITY_ID] == entity_id and event.data["new_state"] is not None:
            states.append(event.data["new_state"].state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
    return states


def now_ms() -> int:
    """Home Assistant's current time in epoch milliseconds: a detection happening now."""
    return int(dt_util.utcnow().timestamp() * 1000)


def detection_event(
    event_type: DetectionType,
    *,
    t_ms: int | None,
    device_sn: str = SYNTHETIC.camera_sn,
    station_sn: str = SYNTHETIC.station_sn,
    msg_type: int = PushMessageType.INDOOR,
    cipher: FrameCipher | None = FrameCipher.GCM,
    **fields: Any,
) -> SecurityEvent:
    """A real library ``SecurityEvent`` for a device, to hand to the router.

    ``FakeStation`` cannot push a station message or a current trigger time (its
    ``trigger_time`` is fixed years in the past), which is why
    this exists. ``t_ms`` has no default: ``now_ms()`` for a current event, None for
    one whose time the library rejected. ``cipher`` None is a cloud push.
    """
    return SecurityEvent(
        source=EventSource.CLOUD if cipher is None else EventSource.P2P,
        station_sn=station_sn,
        device_sn=device_sn,
        channel=0,
        msg_type=int(msg_type),
        event_type=int(event_type),
        event_time_ms=t_ms,
        frame_cipher=cipher,
        **fields,
    )


def station_event(
    msg_type: PushMessageType,
    *,
    t_ms: int | None,
    cipher: FrameCipher | None = FrameCipher.GCM,
    station_sn: str = SYNTHETIC.station_sn,
    **fields: Any,
) -> SecurityEvent:
    """A real library ``SecurityEvent`` about the station itself, with no device.

    ``FakeStation`` cannot push a station message (arming, alarm, alarm delay) or a
    current trigger time, which is why this exists. ``t_ms`` has
    no default, as in ``detection_event``; ``cipher`` None is a cloud push.
    """
    return SecurityEvent(
        source=EventSource.CLOUD if cipher is None else EventSource.P2P,
        station_sn=station_sn,
        msg_type=int(msg_type),
        event_time_ms=t_ms,
        frame_cipher=cipher,
        **fields,
    )


def alarm_changed(
    alarming: bool = True, stop_source: AlarmStopSource | None = None
) -> AlarmChanged:
    """The library's ``AlarmChanged``, as the P2P tone frames make it.

    A start on the camera's channel for 30 s; an end ``[0, 0]``, or ``[16, 0]`` on the
    station's channel when ``stop_source`` is the app. For the router-fed tests; the
    end-to-end ones send the frames with ``FakeStation.send_alarm_frame``.
    """
    if alarming:
        return AlarmChanged(
            station_sn=SYNTHETIC.station_sn,
            alarming=True,
            source=EventSource.P2P,
            channel=1,
            event_type=3,
            duration_s=30,
        )
    return AlarmChanged(
        station_sn=SYNTHETIC.station_sn,
        alarming=False,
        source=EventSource.P2P,
        channel=255 if stop_source is not None else 0,
        event_type=int(stop_source) if stop_source is not None else 0,
        duration_s=0,
        stop_source=stop_source,
    )
