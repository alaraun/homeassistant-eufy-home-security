"""Smart locks: a lock entity per device the library grades as a lock.

The state is the library's lock reading, mapped as the library's guide suggests, and
follows ``LockStateChanged``. The lock here takes a command form the library does not
send, so lock and unlock are refused with the library's reason and nothing is sent
(commands of a writable lock: ``test_lock_control.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import pytest
from conftest import entity_id_for, record_states, set_up_warm, state_of, wait_until
from eufy_home_security import EventSource, SecurityEvent, UnsupportedError, entity_unique_id
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation, camera_device
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.lock import LockState as HALockState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security.const import (
    ATTR_LOCK_STATE,
    ATTR_RAW_VALUE,
    DOMAIN,
    EXC_LOCK_NOT_WRITABLE,
    LOCK_KEY,
)

# A synthetic serial on a real lock product prefix, paired to the fake HomeBase.
LOCK_SN: Final = "T8500P0000000001"
LOCK_NAME: Final = "Front Door"
LOCK_CHANNEL: Final = 1
# The lock-state parameter of a lock behind a HomeBase (library guide, Locks).
LOCK_STATE_PARAM: Final = 1912
# A lock push reporting a mechanical fault.
JAMMED_PUSH: Final = 517
# A lock push reporting the lock locked (by hand).
LOCK_PUSH: Final = 262

pytestmark = pytest.mark.usefixtures("built_clients")


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    """The fake HomeBase with its camera and a smart lock on channel 1."""
    cloud = FakeCloud.for_stations(fake_station)
    cloud.devices.append(
        camera_device(LOCK_SN, station_sn=fake_station.serial, channel=LOCK_CHANNEL, name=LOCK_NAME)
    )
    return cloud


def _seed_lock(station: FakeStation, raw: str) -> None:
    station.params.setdefault(LOCK_CHANNEL, {})[LOCK_STATE_PARAM] = raw


def _lock_id(hass: HomeAssistant) -> str:
    return entity_id_for(hass, LOCK_DOMAIN, LOCK_SN, LOCK_KEY)


async def test_only_a_lock_gets_a_lock_entity(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """The lock gets one lock entity and no cover; the camera and the HomeBase get none."""
    _seed_lock(fake_station, "4")
    await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    lock_entities = [
        e for e in registry.entities.values() if e.domain == LOCK_DOMAIN and e.platform == DOMAIN
    ]
    assert [e.unique_id for e in lock_entities] == [entity_unique_id(LOCK_SN, LOCK_KEY)]
    assert not [e for e in registry.entities.values() if e.domain == COVER_DOMAIN]
    state = hass.states.get(_lock_id(hass))
    assert state is not None
    assert state.name == f"{LOCK_NAME} Lock"


# (lock-state code, the HA state, the library's state name)
_CODES: Final = (
    ("0", STATE_UNKNOWN, "initial"),
    ("1", STATE_UNKNOWN, "uncalibrated"),
    ("2", STATE_UNKNOWN, "calibrating"),
    ("3", HALockState.UNLOCKED, "unlocked"),
    ("4", HALockState.LOCKED, "locked"),
    ("5", HALockState.JAMMED, "intermediate"),
    ("6", HALockState.UNLOCKING, "unlocking"),
    ("7", HALockState.LOCKING, "locking"),
    ("8", HALockState.UNLOCKED, "passage_mode"),
    ("9", HALockState.LOCKED, "privacy_mode"),
    ("42", STATE_UNKNOWN, "unknown"),
)


@pytest.mark.parametrize(("raw", "shown", "name"), _CODES, ids=[row[2] for row in _CODES])
async def test_each_lock_state_shows_as_the_library_suggests(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    raw: str,
    shown: str,
    name: str,
) -> None:
    """Each lock-state code maps to its HA state; the library's name and raw value are
    attributes."""
    _seed_lock(fake_station, raw)
    await set_up_warm(hass, seed_warm_cache)
    state = hass.states.get(_lock_id(hass))
    assert state is not None
    assert state.state == shown
    assert state.attributes[ATTR_LOCK_STATE] == name
    assert state.attributes[ATTR_RAW_VALUE] == raw


async def test_a_lock_without_a_reading_is_unknown_not_unavailable(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """Nothing reported yet: unknown, without the state attributes."""
    await set_up_warm(hass, seed_warm_cache)
    state = hass.states.get(_lock_id(hass))
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert ATTR_LOCK_STATE not in state.attributes
    assert ATTR_RAW_VALUE not in state.attributes


async def test_the_lock_follows_the_library_state_changes(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """A new report shows through ``LockStateChanged``, without a poll."""
    _seed_lock(fake_station, "4")
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _lock_id(hass)
    states = record_states(hass, entity_id)

    _seed_lock(fake_station, "3")
    fake_station.send_param_dump()
    await wait_until(lambda: state_of(hass, entity_id) == HALockState.UNLOCKED)
    assert states == [HALockState.UNLOCKED]


async def test_a_jammed_push_shows_jammed(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """A lock push reporting a fault shows jammed, with no raw value."""
    _seed_lock(fake_station, "4")
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _lock_id(hass)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    taken = station.note_lock_push(
        SecurityEvent(
            source=EventSource.CLOUD,
            station_sn=SYNTHETIC.station_sn,
            device_sn=LOCK_SN,
            event_type=JAMMED_PUSH,
            frame_cipher=None,
        )
    )
    assert taken
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == HALockState.JAMMED
    assert state.attributes[ATTR_LOCK_STATE] == "jammed"
    assert ATTR_RAW_VALUE not in state.attributes


async def test_a_report_repeating_the_state_shows_its_own_source(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """A push repeating the locked state (``LockStateChanged.changed`` False) is the lock's
    reading now: the state stays and the reported raw value goes with the earlier report."""
    _seed_lock(fake_station, "4")
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _lock_id(hass)
    before = hass.states.get(entity_id)
    assert before is not None
    assert before.attributes[ATTR_RAW_VALUE] == "4"
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station

    assert station.note_lock_push(
        SecurityEvent(
            source=EventSource.CLOUD,
            station_sn=SYNTHETIC.station_sn,
            device_sn=LOCK_SN,
            event_type=LOCK_PUSH,
            frame_cipher=None,
        )
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == HALockState.LOCKED
    assert ATTR_RAW_VALUE not in state.attributes


async def test_a_lock_the_device_list_no_longer_names_shows_unknown(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between a refresh that drops the lock and the reload that follows, the library has
    no reading for it (``UnsupportedError``): the lock shows unknown, and the write does
    not fail."""
    _seed_lock(fake_station, "4")
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _lock_id(hass)
    assert state_of(hass, entity_id) == HALockState.LOCKED

    def unlisted(device_sn: str) -> None:
        raise UnsupportedError(f"{device_sn} is not in the device list")

    monkeypatch.setattr(entry.runtime_data.eufy, "lock_state", unlisted)
    _seed_lock(fake_station, "3")
    fake_station.send_param_dump()

    await wait_until(lambda: state_of(hass, entity_id) == STATE_UNKNOWN)


@pytest.mark.parametrize("service", ["lock", "unlock"])
async def test_lock_and_unlock_are_refused_and_send_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    service: str,
) -> None:
    """Lock and unlock raise the translated refusal with the library's reason; nothing is
    sent and the state stays."""
    _seed_lock(fake_station, "4")
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _lock_id(hass)
    control = entry.runtime_data.eufy.lock_control(LOCK_SN)
    assert not control.writable
    assert control.reason
    sent = len(fake_station.received)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            LOCK_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == EXC_LOCK_NOT_WRITABLE
    assert raised.value.translation_placeholders == {"reason": control.reason}
    assert len(fake_station.received) == sent
    assert state_of(hass, entity_id) == HALockState.LOCKED
