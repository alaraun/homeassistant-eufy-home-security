"""Garage doors: a cover per door of a garage-door camera, open and close where a motor is
bound, run as background tasks whose state follows the library's door events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

import pytest
from conftest import entity_id_for, record_states, set_up_warm, state_of, wait_until
from eufy_home_security import (
    CommandRejectedError,
    DoorNotMovedError,
    EufySecurityError,
    GarageDoorState,
    Station,
    entity_unique_id,
)
from eufy_home_security.p2p.messages import STANDALONE_RECEIPT_LEN
from eufy_home_security.station import garage as garage_module
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.cover import ATTR_CURRENT_POSITION, CoverEntityFeature, CoverState
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_home_security import cover as cover_module
from custom_components.eufy_home_security.const import (
    DOMAIN,
    EXC_GARAGE_DOOR_MOVING,
    EXC_GARAGE_DOOR_POSITION_UNKNOWN,
    EXC_GARAGE_OTHER_DOOR_MOVING,
    GARAGE_DOOR_KEY,
)

# A synthetic serial on a real garage-door camera prefix; the camera is its own station.
GARAGE_SN: Final = "T8453P0000000001"
GARAGE_NAME: Final = "Garage"
# The camera's cloud device type, under which its own parameter block is filed.
GARAGE_TYPE: Final = 133
# The door-state, motor and sensor parameters (library guide, Garage doors).
STATE_PARAM: Final = 7500
MOTOR_PARAM: Final = 7501
SENSOR_PARAM: Final = 7502
# State bits: door 1 open/closed/unknown, door 2 open/closed.
DOOR1_OPEN: Final = 0x01
DOOR1_CLOSED: Final = 0x02
DOOR1_UNKNOWN: Final = 0x10
DOOR2_CLOSED: Final = 0x08
# Binding bits per door.
DOOR1_BOUND: Final = 0x01
DOOR2_BOUND: Final = 0x02

_COVER_LOGGER: Final = "custom_components.eufy_home_security.cover"

pytestmark = pytest.mark.usefixtures("built_clients")


def _block(*, state: int, motors: int, sensors: int) -> dict[int, str]:
    return {STATE_PARAM: str(state), MOTOR_PARAM: str(motors), SENSOR_PARAM: str(sensors)}


_TWO_DOORS: Final = _block(
    state=DOOR1_CLOSED | DOOR2_CLOSED,
    motors=DOOR1_BOUND | DOOR2_BOUND,
    sensors=DOOR1_BOUND | DOOR2_BOUND,
)


@pytest.fixture
def door_params() -> dict[int, str]:
    """One door with a bound motor and sensor, closed."""
    return _block(state=DOOR1_CLOSED, motors=DOOR1_BOUND, sensors=DOOR1_BOUND)


@pytest.fixture
async def fake_station(door_params: dict[int, str]) -> AsyncIterator[FakeStation]:
    """A standalone garage-door camera that answers a door command with its report."""
    station = FakeStation(
        serial=GARAGE_SN,
        cipher_id=98,
        receipt_len=STANDALONE_RECEIPT_LEN,
        guard_mode=1,
        params={GARAGE_TYPE: dict(door_params), 255: {1216: GARAGE_NAME}},
        garage_moves=True,
    )
    await station.start()
    yield station
    station.stop()


@pytest.fixture
def fake_cloud(fake_station: FakeStation) -> FakeCloud:
    return FakeCloud(
        devices=[
            {
                "device_sn": GARAGE_SN,
                "device_type": GARAGE_TYPE,
                "device_name": GARAGE_NAME,
                "parent_sn": GARAGE_SN,
                "device_channel": GARAGE_TYPE,
                "p2p_did": SYNTHETIC.did,
                "local_ip": SYNTHETIC.station_ip,
                "params": [],
            }
        ],
        owner_ids={GARAGE_SN: fake_station.account_id},
        cipher_keys={GARAGE_SN: fake_station.ecc_private_key_hex},
    )


def _door_id(hass: HomeAssistant, door: int = 1) -> str:
    return entity_id_for(hass, COVER_DOMAIN, GARAGE_SN, f"{GARAGE_DOOR_KEY}_{door}")


def _door_commands(station: FakeStation) -> list[dict[str, Any]]:
    """The door commands the camera received, in order."""
    return [body for body in station.doorbell_payloads if body.get("commandType") == 7500]


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _COVER_LOGGER and r.levelno == logging.WARNING]


async def test_a_single_door_is_one_garage_cover(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """One door: one cover named "Garage door", device class garage, open and close,
    closed as reported; the camera gets no lock."""
    await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    covers = [e for e in registry.entities.values() if e.domain == COVER_DOMAIN]
    assert [e.unique_id for e in covers] == [entity_unique_id(GARAGE_SN, f"{GARAGE_DOOR_KEY}_1")]
    assert not [e for e in registry.entities.values() if e.domain == LOCK_DOMAIN]
    state = hass.states.get(_door_id(hass))
    assert state is not None
    assert state.name == f"{GARAGE_NAME} Garage door"
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_DEVICE_CLASS] == "garage"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    )
    assert ATTR_CURRENT_POSITION not in state.attributes


@pytest.mark.parametrize(
    "door_params",
    [
        _block(
            state=DOOR1_OPEN | DOOR2_CLOSED,
            motors=DOOR1_BOUND | DOOR2_BOUND,
            sensors=DOOR1_BOUND | DOOR2_BOUND,
        )
    ],
)
async def test_two_doors_are_named_by_their_letters(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """Doors 1 and 2 are "Garage door A" and "Garage door B", each with its own state."""
    await set_up_warm(hass, seed_warm_cache)
    first = hass.states.get(_door_id(hass, 1))
    second = hass.states.get(_door_id(hass, 2))
    assert first is not None and second is not None
    assert (first.name, first.state) == (f"{GARAGE_NAME} Garage door A", CoverState.OPEN)
    assert (second.name, second.state) == (f"{GARAGE_NAME} Garage door B", CoverState.CLOSED)


@pytest.mark.parametrize("door_params", [_block(state=DOOR1_CLOSED, motors=0, sensors=DOOR1_BOUND)])
async def test_a_door_without_a_motor_shows_its_state_only(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """A door with a sensor and no motor: a cover with no open or close."""
    await set_up_warm(hass, seed_warm_cache)
    state = hass.states.get(_door_id(hass))
    assert state is not None
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == 0


@pytest.mark.parametrize("door_params", [_block(state=0, motors=0, sensors=0)])
async def test_a_door_appears_with_its_first_binding_report(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No door bound at setup: no cover; the first report binding one adds it, once."""
    built: list[int] = []
    original = cover_module.EufyGarageDoor.__init__

    def counting(self: cover_module.EufyGarageDoor, *args: Any, **kwargs: Any) -> None:
        built.append(args[2])
        original(self, *args, **kwargs)

    monkeypatch.setattr(cover_module.EufyGarageDoor, "__init__", counting)
    await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    assert not [e for e in registry.entities.values() if e.domain == COVER_DOMAIN]

    fake_station.params[GARAGE_TYPE].update(
        _block(state=DOOR1_CLOSED, motors=DOOR1_BOUND, sensors=DOOR1_BOUND)
    )
    fake_station.send_param_dump()
    await wait_until(
        lambda: (
            registry.async_get_entity_id(
                COVER_DOMAIN, DOMAIN, entity_unique_id(GARAGE_SN, f"{GARAGE_DOOR_KEY}_1")
            )
            is not None
        )
    )
    await hass.async_block_till_done()
    assert state_of(hass, _door_id(hass)) == CoverState.CLOSED

    fake_station.params[GARAGE_TYPE][STATE_PARAM] = str(DOOR1_OPEN)
    fake_station.send_param_dump()
    await wait_until(lambda: state_of(hass, _door_id(hass)) == CoverState.OPEN)
    assert built == [1]


@pytest.mark.parametrize(
    "door_params", [_block(state=DOOR1_CLOSED, motors=DOOR1_BOUND, sensors=DOOR1_BOUND)]
)
async def test_a_second_door_bound_later_letters_the_first_after_a_reload(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> None:
    """Door 2 bound after setup is "Garage door B"; door 1 keeps "Garage door" until the
    entry reloads, then reads "Garage door A" under the same entity id."""
    entry = await set_up_warm(hass, seed_warm_cache)
    first_id = _door_id(hass, 1)
    registry = er.async_get(hass)
    fake_station.params[GARAGE_TYPE].update(_TWO_DOORS)
    fake_station.send_param_dump()
    unique_id = entity_unique_id(GARAGE_SN, f"{GARAGE_DOOR_KEY}_2")
    await wait_until(
        lambda: registry.async_get_entity_id(COVER_DOMAIN, DOMAIN, unique_id) is not None
    )
    await hass.async_block_till_done()
    second_id = _door_id(hass, 2)
    assert _name(hass, first_id) == f"{GARAGE_NAME} Garage door"
    assert _name(hass, second_id) == f"{GARAGE_NAME} Garage door B"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _door_id(hass, 1) == first_id
    assert _name(hass, first_id) == f"{GARAGE_NAME} Garage door A"
    assert _name(hass, second_id) == f"{GARAGE_NAME} Garage door B"


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.name


async def test_open_shows_opening_then_the_reported_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Open returns at once; the door shows opening, then open from the camera's report."""
    fake_station.garage_report_delay = 0.3
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    states = record_states(hass, entity_id)

    await _call(hass, "open_cover", entity_id)
    await wait_until(lambda: state_of(hass, entity_id) == CoverState.OPENING)
    await wait_until(lambda: state_of(hass, entity_id) == CoverState.OPEN)

    assert states == [CoverState.OPENING, CoverState.OPEN]
    assert [c["data"]["type"] for c in _door_commands(fake_station)] == [1]
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "door_params", [_block(state=DOOR1_OPEN, motors=DOOR1_BOUND, sensors=DOOR1_BOUND)]
)
async def test_close_shows_closing_then_the_reported_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Close of an open door sends door 1's close (type 0); the door shows closing, then
    closed from the camera's report."""
    fake_station.garage_report_delay = 0.3
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    assert state_of(hass, entity_id) == CoverState.OPEN
    states = record_states(hass, entity_id)

    await _call(hass, "close_cover", entity_id)
    await wait_until(lambda: state_of(hass, entity_id) == CoverState.CLOSED)

    assert states == [CoverState.CLOSING, CoverState.CLOSED]
    assert [c["data"]["type"] for c in _door_commands(fake_station)] == [0]
    assert _warnings(caplog) == []


async def _hold_open(hass: HomeAssistant, fake_station: FakeStation, entity_id: str) -> None:
    """Open ``entity_id`` against a camera that only takes the command: it waits."""
    fake_station.garage_moves = False
    await _call(hass, "open_cover", entity_id)
    await wait_until(lambda: state_of(hass, entity_id) == CoverState.OPENING)


async def test_the_same_command_again_keeps_the_waiting_one(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Open again while the open waits: nothing is sent, the waiting command runs on."""
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    entity = hass.data[COVER_DOMAIN].get_entity(entity_id)
    assert entity is not None
    await _hold_open(hass, fake_station, entity_id)
    waiting = entity._slot.task
    assert waiting is not None

    await _call(hass, "open_cover", entity_id)
    await hass.async_block_till_done()

    assert entity._slot.task is waiting
    assert not waiting.done()
    assert len(_door_commands(fake_station)) == 1
    assert state_of(hass, entity_id) == CoverState.OPENING
    assert _warnings(caplog) == []


async def test_two_opens_at_once_send_one_command(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two opens at the same moment: one command is sent, neither call is refused."""
    fake_station.garage_moves = False
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)

    await asyncio.gather(_call(hass, "open_cover", entity_id), _call(hass, "open_cover", entity_id))
    await wait_until(lambda: state_of(hass, entity_id) == CoverState.OPENING)
    await hass.async_block_till_done()

    assert len(_door_commands(fake_station)) == 1
    assert _warnings(caplog) == []


async def test_a_reverse_while_the_door_moves_is_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Close while the open waits: refused before sending; the open waits on."""
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    entity = hass.data[COVER_DOMAIN].get_entity(entity_id)
    assert entity is not None
    await _hold_open(hass, fake_station, entity_id)
    waiting = entity._slot.task

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "close_cover", entity_id)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == EXC_GARAGE_DOOR_MOVING
    assert entity._slot.task is waiting
    assert waiting is not None and not waiting.done()
    assert len(_door_commands(fake_station)) == 1
    assert state_of(hass, entity_id) == CoverState.OPENING
    assert _warnings(caplog) == []


@pytest.mark.parametrize("door_params", [_TWO_DOORS])
@pytest.mark.parametrize("service", ["open_cover", "close_cover"])
async def test_a_command_of_the_other_door_while_one_waits_is_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    service: str,
) -> None:
    """The camera takes one door command at a time: door B's command while door A's open
    waits is refused before sending, and A's open waits on."""
    await set_up_warm(hass, seed_warm_cache)
    first_id, second_id = _door_id(hass, 1), _door_id(hass, 2)
    await _hold_open(hass, fake_station, first_id)

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, service, second_id)

    assert raised.value.translation_key == EXC_GARAGE_OTHER_DOOR_MOVING
    assert len(_door_commands(fake_station)) == 1
    assert state_of(hass, first_id) == CoverState.OPENING
    assert state_of(hass, second_id) == CoverState.CLOSED
    assert _warnings(caplog) == []


@pytest.mark.parametrize("door_params", [_TWO_DOORS])
async def test_the_other_door_takes_a_command_once_the_first_ended(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Door A's open reported: door B's open is sent and shown."""
    fake_station.garage_report_delay = 0.1
    await set_up_warm(hass, seed_warm_cache)
    first_id, second_id = _door_id(hass, 1), _door_id(hass, 2)

    await _call(hass, "open_cover", first_id)
    await wait_until(lambda: state_of(hass, first_id) == CoverState.OPEN)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _call(hass, "open_cover", second_id)
    await wait_until(lambda: state_of(hass, second_id) == CoverState.OPEN)

    assert [c["data"]["type"] for c in _door_commands(fake_station)] == [1, 3]


@pytest.mark.parametrize(
    "door_params", [_block(state=DOOR1_UNKNOWN, motors=DOOR1_BOUND, sensors=DOOR1_BOUND)]
)
@pytest.mark.parametrize("service", ["open_cover", "close_cover"])
async def test_a_door_of_unknown_position_refuses_and_sends_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    service: str,
) -> None:
    """Unknown position: the translated refusal; nothing sent or logged."""
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    assert state_of(hass, entity_id) == "unknown"

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, service, entity_id)
    await hass.async_block_till_done()

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == EXC_GARAGE_DOOR_POSITION_UNKNOWN
    assert _door_commands(fake_station) == []
    assert _warnings(caplog) == []


async def test_a_command_without_a_report_times_out_to_the_reported_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No report within the library's wait: one warning, and the door shows closed again."""
    monkeypatch.setattr(garage_module, "GARAGE_CONFIRM_TIMEOUT", 0.3)
    fake_station.garage_moves = False
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    states = record_states(hass, entity_id)

    await _call(hass, "open_cover", entity_id)
    await wait_until(lambda: len(_warnings(caplog)) == 1)
    await hass.async_block_till_done()

    assert states == [CoverState.OPENING, CoverState.CLOSED]
    assert len(_warnings(caplog)) == 1
    assert "reported no new position" in _warnings(caplog)[0].getMessage()
    assert GARAGE_SN not in caplog.text


# (failure, a fragment of its warning)
_FAILURES: Final = (
    (
        DoorNotMovedError(
            7500, "not moved", door=1, state=GarageDoorState.CLOSED, device_sn=GARAGE_SN
        ),
        "the open command ended with the door reporting closed",
    ),
    (CommandRejectedError(1700, 1, "refused"), "the camera refused the open command"),
    (EufySecurityError("other"), "the open command failed (EufySecurityError)"),
)


@pytest.mark.parametrize(
    ("failure", "fragment"), _FAILURES, ids=[type(f).__name__ for f, _ in _FAILURES]
)
async def test_a_failed_command_logs_one_warning_and_keeps_the_reported_state(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: EufySecurityError,
    fragment: str,
) -> None:
    """A command that ends in an error: one warning without the serial, state as reported."""

    async def failing(*args: Any, **kwargs: Any) -> GarageDoorState:
        raise failure

    monkeypatch.setattr(Station, "async_open_garage_door", failing)
    await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)

    await _call(hass, "open_cover", entity_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [r.getMessage() for r in _warnings(caplog)] == [f"{entity_id}: {fragment}"]
    assert GARAGE_SN not in caplog.text
    assert state_of(hass, entity_id) == CoverState.CLOSED


async def test_unload_cancels_a_running_command(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A command still waiting for its report ends with the entry, without a warning."""
    fake_station.garage_moves = False
    entry = await set_up_warm(hass, seed_warm_cache)
    entity_id = _door_id(hass)
    entity = hass.data[COVER_DOMAIN].get_entity(entity_id)
    assert entity is not None

    await _call(hass, "open_cover", entity_id)
    await wait_until(lambda: state_of(hass, entity_id) == CoverState.OPENING)
    task = entity._slot.task
    assert task is not None and not task.done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert task.cancelled()
    assert _warnings(caplog) == []
