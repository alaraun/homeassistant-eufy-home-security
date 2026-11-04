"""Detections the guard mode in force holds back: the repair, the sensor, diagnostics.

``Station.detection_delivery`` reads the Notification bit of each device's action in the
mode in force; with it off the station sends none of the device's detections. The fake
station's per-channel action params are 1239 (Away) and 1225 (Home); 1224 is the
selected mode and 1177 the stored Disarmed table.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from conftest import (
    SENSOR_SN,
    SYNTHETIC,
    add_motion_sensor,
    advance_to_poll,
    entity_id_for,
    set_guard_mode,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import (
    DetectionDelivery,
    DetectionDeliveryReport,
    DevicesChanged,
    EventSource,
    GuardMode,
    GuardModeChanged,
    StationConfigChanged,
    entity_unique_id,
    guard_mode_label,
    redact_serial,
)
from eufy_home_security.testing import FakeCloud, FakeStation
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import diagnostics, errors
from custom_components.eufy_home_security.const import (
    DETECTION_DELIVERY_KEY,
    DETECTION_EVENT_KEY,
    DOMAIN,
    ISSUE_MODE_HOLDS_DETECTIONS,
    ISSUE_MODE_HOLDS_DETECTIONS_NOWHERE,
    MOTION_DETECTED_KEY,
)

# Every setup here builds its client on the fake cloud and station.
pytestmark = pytest.mark.usefixtures("built_clients")

_AWAY_ACTION: Final = 1239
_HOME_ACTION: Final = 1225
# An action with Notification (bit 8) on, and one that only records.
_NOTIFY: Final = "9"
_RECORD: Final = "1"
_KEYPAD_SN: Final = "T8960P0000000001"


def _hold_back_in_home(station: FakeStation) -> None:
    """The camera records only in Home and notifies in Away; Home is in force."""
    station.params[0].update({_AWAY_ACTION: _NOTIFY, _HOME_ACTION: _RECORD})
    set_guard_mode(station, int(GuardMode.HOME))


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    station = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SYNTHETIC.station_sn), entry.entry_id
    )
    assert station is not None
    return ir.async_get(hass).async_get_issue(
        DOMAIN, errors.mode_holds_detections_issue_id(entry.entry_id, station.id)
    )


def _label(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    return errors.station_label(device)


def _sensor(hass: HomeAssistant, serial: str = SYNTHETIC.camera_sn) -> str:
    return entity_id_for(hass, SENSOR_DOMAIN, serial, DETECTION_DELIVERY_KEY)


async def test_a_camera_held_back_in_the_mode_in_force_raises_an_ignorable_warning(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Home in force, Notification off there and on in Away: one warning naming the
    station, the mode, the camera and Away; not fixable, not persistent."""
    _hold_back_in_home(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.translation_key == ISSUE_MODE_HOLDS_DETECTIONS
    assert issue.translation_placeholders == {
        "station": _label(hass, entry, SYNTHETIC.station_sn),
        "mode": "Home",
        "devices": _label(hass, entry, SYNTHETIC.camera_sn),
        "sending_modes": "Away",
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None, "the issue outlived the unload"


async def test_the_sensor_shows_the_grade_and_the_detection_entities_stay_available(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The camera's diagnostic sensor reads not_sent_in_mode with the mode and the modes
    that send; its motion sensor and Detection event stay available: it records."""
    _hold_back_in_home(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)

    entity_id = _sensor(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == DetectionDelivery.NOT_SENT_IN_MODE.value
    assert state.attributes["mode"] == "Home"
    assert state.attributes["sending_modes"] == ["Away"]
    assert state.attributes["options"] == [d.value for d in DetectionDelivery]
    row = er.async_get(hass).async_get(entity_id)
    assert row is not None
    assert row.entity_category is er.EntityCategory.DIAGNOSTIC
    assert row.disabled_by is None
    for domain, key in (
        (BINARY_SENSOR_DOMAIN, MOTION_DETECTED_KEY),
        (EVENT_DOMAIN, DETECTION_EVENT_KEY),
    ):
        assert state_of(hass, entity_id_for(hass, domain, SYNTHETIC.camera_sn, key)) not in (
            STATE_UNAVAILABLE,
            None,
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_notification_turned_on_in_the_mode_deletes_the_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The next dump carries the changed action: the issue goes, the sensor reads sent."""
    _hold_back_in_home(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _issue(hass, entry) is not None

    fake_station.params[0][_HOME_ACTION] = _NOTIFY
    await advance_to_poll(hass, 60)
    await wait_until(lambda: _issue(hass, entry) is None)

    assert state_of(hass, _sensor(hass)) == DetectionDelivery.SENT.value
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_mode_report_to_a_sending_mode_deletes_the_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station reports Away in force (0x047F): the issue goes at once, no poll."""
    _hold_back_in_home(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _issue(hass, entry) is not None

    fake_station.send_alarm_mode(int(GuardMode.AWAY))
    await wait_until(lambda: _issue(hass, entry) is None)

    state = hass.states.get(_sensor(hass))
    assert state is not None
    assert state.state == DetectionDelivery.SENT.value
    assert state.attributes["mode"] == "Away"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_mode_without_a_reported_table_raises_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Home in force, but no device reports a Home action: unknown, no issue."""
    fake_station.params[0][_AWAY_ACTION] = _NOTIFY
    set_guard_mode(fake_station, int(GuardMode.HOME))
    entry = await set_up_warm(hass, seed_warm_cache)

    assert state_of(hass, _sensor(hass)) == DetectionDelivery.UNKNOWN.value
    assert _issue(hass, entry) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


def _stored_table(account_id: str, mode: int, actions: dict[int, int]) -> str:
    """A stored mode table (1157-1161, 1177) of ``actions``, delays off."""
    table = {
        "account_id": account_id,
        "mode_id": mode,
        "devices": [{"device_channel": ch, "action": a} for ch, a in actions.items()],
        "count_down_alarm": {"channel_list": [], "delay_time": 0},
        "count_down_arm": {"channel_list": [], "delay_time": 0},
        "siren_sensor_action": [{"device_channel": ch, "action": 0} for ch in actions],
    }
    return base64.b64encode(json.dumps(table, separators=(",", ":")).encode()).decode()


async def test_a_disarmed_station_that_holds_a_camera_back_raises_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Disarmed in force, its table holding the camera back: the sensor says so, but a
    station that sends nothing while disarmed is no fault."""
    fake_station.params[0][_AWAY_ACTION] = _NOTIFY
    fake_station.params[255][1177] = _stored_table(
        fake_station.account_id, int(GuardMode.DISARMED), {0: 0}
    )
    set_guard_mode(fake_station, int(GuardMode.DISARMED))
    entry = await set_up_warm(hass, seed_warm_cache)

    state = hass.states.get(_sensor(hass))
    assert state is not None
    assert state.state == DetectionDelivery.NOT_SENT_IN_MODE.value
    assert state.attributes["mode"] == "Disarmed"
    assert _issue(hass, entry) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_only_cameras_and_doorbells_get_the_sensor(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The camera gets one; a motion sensor (its gating is not known) and the station
    itself do not."""
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)

    rows = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    owners = {
        serial
        for serial in (SYNTHETIC.camera_sn, SENSOR_SN, SYNTHETIC.station_sn)
        for row in rows
        if row.unique_id == entity_unique_id(serial, DETECTION_DELIVERY_KEY)
    }
    assert owners == {SYNTHETIC.camera_sn}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_sensor_stays_available_for_a_camera_the_dump_leaves_out(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """The grade comes from the station's tables, which also cover a device without a
    block in the dump (an offline battery camera): the station's availability counts."""
    del fake_station.params[0]
    entry = await set_up_warm(hass, seed_warm_cache)

    assert state_of(hass, entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, "firmware")) == (
        STATE_UNAVAILABLE
    )
    assert state_of(hass, _sensor(hass)) == DetectionDelivery.UNKNOWN.value
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_unknown_mode_shows_no_mode_and_no_sending_modes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    unknown = DetectionDeliveryReport(
        device_sn=SYNTHETIC.camera_sn,
        delivery=DetectionDelivery.UNKNOWN,
        mode=None,
        action=None,
        sending_modes=(),
        reason="the mode in force is not known",
    )
    monkeypatch.setattr(
        coordinator.station, "detection_delivery", lambda serial, *, config=None: unknown
    )
    coordinator.async_update_listeners()

    state = hass.states.get(_sensor(hass))
    assert state is not None
    assert state.state == DetectionDelivery.UNKNOWN.value
    assert state.attributes["mode"] is None
    assert state.attributes["sending_modes"] == []
    monkeypatch.undo()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_carry_each_devices_delivery(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """Per paired device: the grade, the mode, the action and the sending modes; no reason."""
    _hold_back_in_home(fake_station)
    entry = await set_up_warm(hass, seed_warm_cache)

    data = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    [station] = data["stations"].values()
    delivery = station["detection_delivery"]
    assert SYNTHETIC.camera_sn not in repr(delivery)
    camera = next(v for k, v in delivery.items() if k.endswith(SYNTHETIC.camera_sn[-4:]))
    assert camera == {
        "delivery": "not_sent_in_mode",
        "mode": "home",
        "action": 1,
        "sending_modes": ["away"],
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


# ── the repair against a stand-in station ────────────────────────────────────


@dataclass
class _Config:
    """The two things the repair reads off a configuration."""

    mode_tables: dict[GuardMode, Any] = field(
        default_factory=lambda: dict.fromkeys(
            (GuardMode.HOME, GuardMode.AWAY, GuardMode.CUSTOM_1, GuardMode.DISARMED)
        )
    )

    def mode_name(self, code: GuardMode | int) -> str:
        return "Night" if code == GuardMode.CUSTOM_1 else guard_mode_label(code)


@dataclass
class _Device:
    device_sn: str


@dataclass
class _GradedStation:
    """A station stand-in: one report per device serial."""

    serial: str
    reports: dict[str, DetectionDeliveryReport]
    config: _Config = field(default_factory=_Config)
    asked: list[str] = field(default_factory=list)

    @property
    def devices(self) -> list[_Device]:
        return [_Device(serial) for serial in self.reports]

    def detection_delivery(
        self, device_sn: str, *, config: object | None = None
    ) -> DetectionDeliveryReport:
        assert config is self.config, "the configuration is built once per station"
        self.asked.append(device_sn)
        return self.reports[device_sn]


def _report(
    serial: str,
    delivery: DetectionDelivery,
    mode: GuardMode | int | None = GuardMode.HOME,
    sending: tuple[GuardMode, ...] = (GuardMode.AWAY,),
) -> DetectionDeliveryReport:
    return DetectionDeliveryReport(
        device_sn=serial,
        delivery=delivery,
        mode=mode,
        action=1,
        sending_modes=sending,
        reason=f"reason of {serial[-4:]}",
    )


_INDOOR_SN: Final = "T8417P0000000001"


def _entry_with_devices(hass: HomeAssistant, *serials: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Home")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    for serial in serials:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, serial)},
            name=f"Device {serial[-4:]}",
        )
    return entry


def _stand_in_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    station = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SYNTHETIC.station_sn), entry.entry_id
    )
    assert station is not None
    return ir.async_get(hass).async_get_issue(
        DOMAIN, errors.mode_holds_detections_issue_id(entry.entry_id, station.id)
    )


async def test_a_keypad_or_the_station_itself_is_never_named(hass: HomeAssistant) -> None:
    """Only devices with detection entities count, whatever the library reports."""
    serials = (SYNTHETIC.station_sn, _KEYPAD_SN, SYNTHETIC.camera_sn)
    entry = _entry_with_devices(hass, *serials)
    held = {sn: _report(sn, DetectionDelivery.NOT_SENT_IN_MODE) for sn in serials}
    station = _GradedStation(SYNTHETIC.station_sn, held)

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    issue = _stand_in_issue(hass, entry)
    assert issue is not None
    assert (issue.translation_placeholders or {})["devices"] == f"Device {SYNTHETIC.camera_sn[-4:]}"
    assert station.asked == [SYNTHETIC.camera_sn]


async def test_a_device_without_a_registry_row_is_named_by_its_redacted_serial(
    hass: HomeAssistant,
) -> None:
    entry = _entry_with_devices(hass, SYNTHETIC.station_sn)
    station = _GradedStation(
        SYNTHETIC.station_sn,
        {SYNTHETIC.camera_sn: _report(SYNTHETIC.camera_sn, DetectionDelivery.NOT_SENT_IN_MODE)},
    )

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    issue = _stand_in_issue(hass, entry)
    assert issue is not None
    assert (issue.translation_placeholders or {})["devices"] == redact_serial(SYNTHETIC.camera_sn)


async def test_a_station_without_a_registry_row_raises_nothing(hass: HomeAssistant) -> None:
    entry = _entry_with_devices(hass, SYNTHETIC.camera_sn)
    station = _GradedStation(
        SYNTHETIC.station_sn,
        {SYNTHETIC.camera_sn: _report(SYNTHETIC.camera_sn, DetectionDelivery.NOT_SENT_IN_MODE)},
    )

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN]


@pytest.mark.parametrize(
    ("mode", "raised"),
    [
        (GuardMode.HOME, True),
        (GuardMode.DISARMED, False),
        (GuardMode.OFF, False),
        (99, True),
    ],
    ids=["home", "disarmed", "off", "a_code_guard_mode_lacks"],
)
async def test_a_disarmed_mode_in_force_names_nobody(
    hass: HomeAssistant, mode: GuardMode | int, raised: bool
) -> None:
    entry = _entry_with_devices(hass, SYNTHETIC.station_sn, SYNTHETIC.camera_sn)
    station = _GradedStation(
        SYNTHETIC.station_sn,
        {
            SYNTHETIC.camera_sn: _report(
                SYNTHETIC.camera_sn, DetectionDelivery.NOT_SENT_IN_MODE, mode
            )
        },
    )

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    assert (_stand_in_issue(hass, entry) is not None) is raised


@pytest.mark.parametrize(
    "delivery", [DetectionDelivery.SENT, DetectionDelivery.UNKNOWN], ids=["sent", "unknown"]
)
async def test_a_device_not_held_back_deletes_the_issue(
    hass: HomeAssistant, delivery: DetectionDelivery
) -> None:
    entry = _entry_with_devices(hass, SYNTHETIC.station_sn, SYNTHETIC.camera_sn)
    held = _GradedStation(
        SYNTHETIC.station_sn,
        {SYNTHETIC.camera_sn: _report(SYNTHETIC.camera_sn, DetectionDelivery.NOT_SENT_IN_MODE)},
    )
    errors.sync_mode_holds_detections_issue(hass, entry, [held])  # type: ignore[list-item]
    assert _stand_in_issue(hass, entry) is not None

    graded = _GradedStation(
        SYNTHETIC.station_sn, {SYNTHETIC.camera_sn: _report(SYNTHETIC.camera_sn, delivery)}
    )
    errors.sync_mode_holds_detections_issue(hass, entry, [graded])  # type: ignore[list-item]

    assert _stand_in_issue(hass, entry) is None


async def test_the_issue_names_every_held_device_and_the_armed_modes_that_send_them(
    hass: HomeAssistant,
) -> None:
    """Devices in paired order; the sending modes of all of them in the app's order,
    by the station's names, Disarmed left out."""
    entry = _entry_with_devices(hass, SYNTHETIC.station_sn, SYNTHETIC.camera_sn, _INDOOR_SN)
    station = _GradedStation(
        SYNTHETIC.station_sn,
        {
            SYNTHETIC.camera_sn: _report(
                SYNTHETIC.camera_sn,
                DetectionDelivery.NOT_SENT_IN_MODE,
                sending=(GuardMode.CUSTOM_1, GuardMode.DISARMED),
            ),
            _INDOOR_SN: _report(
                _INDOOR_SN, DetectionDelivery.NOT_SENT_IN_MODE, sending=(GuardMode.AWAY,)
            ),
        },
    )

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    issue = _stand_in_issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == ISSUE_MODE_HOLDS_DETECTIONS
    placeholders = issue.translation_placeholders or {}
    assert placeholders["devices"] == f"Device {SYNTHETIC.camera_sn[-4:]}, Device {_INDOOR_SN[-4:]}"
    assert placeholders["sending_modes"] == "Away, Night"


@pytest.mark.parametrize("sending", [(), (GuardMode.DISARMED,)], ids=["no_mode", "only_disarmed"])
async def test_without_an_armed_mode_that_sends_the_issue_names_none(
    hass: HomeAssistant, sending: tuple[GuardMode, ...]
) -> None:
    entry = _entry_with_devices(hass, SYNTHETIC.station_sn, SYNTHETIC.camera_sn)
    station = _GradedStation(
        SYNTHETIC.station_sn,
        {
            SYNTHETIC.camera_sn: _report(
                SYNTHETIC.camera_sn, DetectionDelivery.NOT_SENT_IN_MODE, sending=sending
            )
        },
    )

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    issue = _stand_in_issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == ISSUE_MODE_HOLDS_DETECTIONS_NOWHERE
    assert "sending_modes" not in (issue.translation_placeholders or {})


async def test_each_report_reason_is_logged_at_debug(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _entry_with_devices(hass, SYNTHETIC.station_sn, SYNTHETIC.camera_sn)
    station = _GradedStation(
        SYNTHETIC.station_sn,
        {SYNTHETIC.camera_sn: _report(SYNTHETIC.camera_sn, DetectionDelivery.SENT)},
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security.errors")

    errors.sync_mode_holds_detections_issue(hass, entry, [station])  # type: ignore[list-item]

    assert f"reason of {SYNTHETIC.camera_sn[-4:]}" in caplog.text
    assert SYNTHETIC.camera_sn not in caplog.text


# ── when the router checks again ─────────────────────────────────────────────


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every check the router makes from now on: the serials of the stations it names."""
    calls: list[list[str]] = []
    real = errors.sync_mode_holds_detections_issue

    def record(hass: HomeAssistant, entry: Any, stations: Any) -> None:
        stations = list(stations)
        calls.append([station.serial for station in stations])
        real(hass, entry, stations)

    monkeypatch.setattr(errors, "sync_mode_holds_detections_issue", record)
    return calls


def _mode_changed() -> GuardModeChanged:
    return GuardModeChanged(
        station_sn=SYNTHETIC.station_sn,
        mode=GuardMode.AWAY,
        source=EventSource.P2P,
        active_mode=GuardMode.AWAY,
    )


def _config_changed(*fields: str) -> StationConfigChanged:
    return StationConfigChanged(
        station_sn=SYNTHETIC.station_sn, revision=99, fields=frozenset(fields)
    )


async def test_events_of_one_turn_make_one_check_of_their_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    checks: list[list[str]],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    checks.clear()

    router.handle(_mode_changed())
    router.handle(_config_changed("mode_tables"))
    await hass.async_block_till_done()

    assert checks == [[SYNTHETIC.station_sn]]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("event", "checked"),
    [
        (_mode_changed(), True),
        (_config_changed("mode_tables"), True),
        (_config_changed("guard_mode"), True),
        (_config_changed("effective_mode"), True),
        (_config_changed("alarm_volume", "schedule"), False),
    ],
    ids=["mode", "mode_tables", "guard_mode", "effective_mode", "other_fields"],
)
async def test_the_router_checks_on_a_mode_or_table_change(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    checks: list[list[str]],
    event: Any,
    checked: bool,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    checks.clear()

    router.handle(event)
    await hass.async_block_till_done()

    assert bool(checks) is checked
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_event_of_a_station_not_served_here_checks_nothing(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    checks: list[list[str]],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    checks.clear()

    router.handle(
        GuardModeChanged(station_sn="T8030P0000000009", mode=GuardMode.HOME, source=EventSource.P2P)
    )
    await hass.async_block_till_done()

    assert checks == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_paired_device_change_checks_its_station(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
    checks: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    # The reload the event also schedules is not under test here.
    monkeypatch.setattr(router, "async_reload_soon", lambda reason: None)
    checks.clear()

    router.handle(DevicesChanged(station_sn=SYNTHETIC.station_sn))
    await hass.async_block_till_done()

    assert checks == [[SYNTHETIC.station_sn]]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_no_check_runs_once_the_router_stopped_consuming(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed_warm_cache: Callable[..., None],
) -> None:
    """A check due when unload starts never raises the issue again behind it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    _hold_back_in_home(fake_station)
    router = entry.runtime_data.router
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    await station.async_update()
    await hass.async_block_till_done()
    issue = _issue(hass, entry)
    assert issue is not None
    ir.async_delete_issue(hass, DOMAIN, issue.issue_id)

    router.handle(_config_changed("mode_tables"))
    router.async_stop_consuming()
    await hass.async_block_till_done()

    assert _issue(hass, entry) is None
    router.async_start_consuming()
    assert await hass.config_entries.async_unload(entry.entry_id)
