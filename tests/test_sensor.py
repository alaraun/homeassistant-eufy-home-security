"""What each device reports about itself: diagnostics, and readable settings with no control.

Every value here comes from the library's typed state, never from a parameter the
integration parsed itself. The parameter ids below are fixture values only: they say
what the fake station reports, and the integration never names one.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import Final

import pytest
from conftest import (
    SENSOR_SN,
    add_motion_sensor,
    advance_to_poll,
    entity_id_for,
    seed_setting,
    set_up_warm,
    state_of,
    wait_until,
)
from eufy_home_security import EufySecurity, Station, entity_unique_id
from eufy_home_security.devices import Setting, model_for_serial
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.update import DOMAIN as UPDATE_DOMAIN
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.eufy_home_security.const import (
    BATTERY_KEY,
    DOMAIN,
    EMMC_USED_KEY,
    FIRMWARE_KEY,
    MODEL_KEY,
    POLL_INTERVAL_SECONDS,
    SIGNAL_STRENGTH_KEY,
)

# The fake station's blocks and the parameters it reports on them. Test-side
# fixture values: the integration reads the library's typed state instead.
_STATION_BLOCK: Final = 255
_CAMERA_CHANNEL: Final = 0
_SENSOR_CHANNEL: Final = 1
_BATTERY_PARAM: Final = 1101
_SUB1G_RSSI_PARAM: Final = 1141
# The sub-device's online flag: 1 online, 0 offline, above 1 offline with a reason.
_DEV_STATUS_PARAM: Final = 1131
_EMMC_USED_PARAM: Final = 1190
_FIRMWARE_PARAM: Final = 7013

# What the fake station's dump says its own firmware is.
_STATION_FIRMWARE: Final = "3.8.7.4"
# The camera's own signal and battery, from the fake's default block.
_CAMERA_BATTERY: Final = "87"
_CAMERA_WIFI_RSSI: Final = "-52"
# The motion sensor's, from ``add_motion_sensor``.
_SENSOR_BATTERY: Final = "90"
_SENSOR_SUB1G_RSSI: Final = "-70"


def _set_cloud_firmware(cloud: FakeCloud, serial: str, firmware: str) -> None:
    """Record a firmware version against one device of the fake cloud's list.

    Call before ``set_up_warm``: the warm cache copies the cloud's device list when
    it is seeded, so a later change is not in the list the entry is set up from.
    """
    for device in cloud.devices:
        if device["device_sn"] == serial:
            device["main_sw_version"] = firmware
            return
    raise AssertionError(f"{serial} is not in the fake cloud's device list")


def _entity_id_or_none(hass: HomeAssistant, serial: str, key: str) -> str | None:
    """The sensor of ``key`` on ``serial``, or None when no such entity was made."""
    return er.async_get(hass).async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, entity_unique_id(serial, key)
    )


async def test_station_diagnostics_are_diagnostic_sensors(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The station's firmware, eMMC use and model are diagnostic sensors.

    Each one is built from the library's typed state, and each is a diagnostic
    reading rather than a control.
    """
    add_motion_sensor(fake_station, fake_cloud)
    fake_station.params[_STATION_BLOCK][_EMMC_USED_PARAM] = "37"
    entry = await set_up_warm(hass, seed_warm_cache)

    model = model_for_serial(SYNTHETIC.station_sn)
    assert model is not None
    registry = er.async_get(hass)
    for key, shown in (
        (FIRMWARE_KEY, _STATION_FIRMWARE),
        (EMMC_USED_KEY, "37"),
        (MODEL_KEY, model.name),
    ):
        entity_id = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, key)
        assert state_of(hass, entity_id) == shown, key
        registered = registry.async_get(entity_id)
        assert registered is not None, key
        assert registered.entity_category is EntityCategory.DIAGNOSTIC, key

    emmc = hass.states.get(entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, EMMC_USED_KEY))
    assert emmc is not None
    assert emmc.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_camera_and_motion_sensor_diagnostics_with_the_firmware_fallback(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each paired device shows its own battery, firmware and model.

    Firmware comes from the live dump wherever the dump has it: the camera reports
    its own, and the cloud's older record for it is not shown. The motion sensor
    never reports one, so the cloud's record is the only thing there is, and that is
    what the fallback is for.
    """
    add_motion_sensor(fake_station, fake_cloud)
    # The dump wins: the cloud's record for the camera is deliberately different.
    _set_cloud_firmware(fake_cloud, SYNTHETIC.camera_sn, "9.9.9")
    _set_cloud_firmware(fake_cloud, SENSOR_SN, "1.2.3")
    fake_station.params[_CAMERA_CHANNEL][_FIRMWARE_PARAM] = "2.0.1"
    entry = await set_up_warm(hass, seed_warm_cache)

    for serial, battery in ((SYNTHETIC.camera_sn, _CAMERA_BATTERY), (SENSOR_SN, _SENSOR_BATTERY)):
        state = hass.states.get(entity_id_for(hass, SENSOR_DOMAIN, serial, BATTERY_KEY))
        assert state is not None, serial
        assert state.state == battery, serial
        assert state.attributes[ATTR_DEVICE_CLASS] == "battery", serial
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%", serial

    camera_firmware = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, FIRMWARE_KEY)
    assert state_of(hass, camera_firmware) == "2.0.1"
    sensor_firmware = entity_id_for(hass, SENSOR_DOMAIN, SENSOR_SN, FIRMWARE_KEY)
    assert state_of(hass, sensor_firmware) == "1.2.3"

    for serial in (SYNTHETIC.camera_sn, SENSOR_SN):
        model = model_for_serial(serial)
        assert model is not None, serial
        entity_id = entity_id_for(hass, SENSOR_DOMAIN, serial, MODEL_KEY)
        assert state_of(hass, entity_id) == model.name, serial

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_device_dropped_from_the_cloud_list_reads_unknown_rather_than_raising(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The firmware fallback reads unknown for a device the paired list dropped.

    Between a newer cloud list (``Station.update_sub_devices``) and its reload, the
    snapshot still has the device's block while ``station.sub_devices`` does not; a
    fan-out in that window shows unknown, logs no error, and updates every other
    entity.
    """
    add_motion_sensor(fake_station, fake_cloud)
    # The sensor never reports its own firmware, so the cloud's record is the only
    # one there is: the fallback runs on every read of this entity.
    _set_cloud_firmware(fake_cloud, SENSOR_SN, "1.2.3")
    entry = await set_up_warm(hass, seed_warm_cache)

    sensor_firmware = entity_id_for(hass, SENSOR_DOMAIN, SENSOR_SN, FIRMWARE_KEY)
    station_firmware = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, FIRMWARE_KEY)
    camera_battery = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, BATTERY_KEY)
    assert state_of(hass, sensor_firmware) == "1.2.3"

    # A newer cloud list without the sensor, as ``update_sub_devices`` leaves it. Set
    # directly: the event that method emits would schedule the reload this window is
    # defined as being before.
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    coordinator.station.sub_devices = tuple(
        device for device in coordinator.station.sub_devices if device.device_sn != SENSOR_SN
    )
    caplog.clear()
    caplog.set_level(logging.ERROR)

    # The fan-out a confirmed write or a push runs, on the state read before the swap.
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert state_of(hass, sensor_firmware) == STATE_UNKNOWN
    assert [record.getMessage() for record in caplog.records] == []
    # The fan-out finished: the rest of the station's entities still have their values.
    assert state_of(hass, station_firmware) == _STATION_FIRMWARE
    assert state_of(hass, camera_battery) == _CAMERA_BATTERY

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_signal_strength_is_disabled_by_default_and_reads_wifi_else_sub1g(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Signal strength is registered disabled, and reads each device's own radio.

    Home Assistant's own practice is that a signal reading is off until someone asks
    for it. Once enabled, the camera shows its Wi-Fi value and the motion sensor its
    sub-1 GHz one: the library picks per device, so keying on one radio would report
    nothing for the sensor.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)

    registry = er.async_get(hass)
    for serial in (SYNTHETIC.camera_sn, SENSOR_SN):
        entity_id = entity_id_for(hass, SENSOR_DOMAIN, serial, SIGNAL_STRENGTH_KEY)
        registered = registry.async_get(entity_id)
        assert registered is not None, serial
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION, serial
        # Registered but never added, so it has no state at all.
        assert state_of(hass, entity_id) is None, serial
        registry.async_update_entity(entity_id, disabled_by=None)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    for serial, rssi in (
        (SYNTHETIC.camera_sn, _CAMERA_WIFI_RSSI),
        (SENSOR_SN, _SENSOR_SUB1G_RSSI),
    ):
        state = hass.states.get(entity_id_for(hass, SENSOR_DOMAIN, serial, SIGNAL_STRENGTH_KEY))
        assert state is not None, serial
        assert state.state == rssi, serial
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "dBm", serial
        assert state.attributes[ATTR_DEVICE_CLASS] == "signal_strength", serial

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_device_whose_block_stops_reporting_goes_unavailable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A device the station stops reporting goes unavailable, and comes back.

    A full read drops the block of a channel that did not answer, and that absence is
    the whole availability signal: the station's own entities and the other device's
    keep their values throughout, so one silent sensor never blanks the system.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()

    sensor_battery = entity_id_for(hass, SENSOR_DOMAIN, SENSOR_SN, BATTERY_KEY)
    camera_battery = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, BATTERY_KEY)
    station_firmware = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, FIRMWARE_KEY)
    assert state_of(hass, sensor_battery) == _SENSOR_BATTERY

    del fake_station.params[_SENSOR_CHANNEL]
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    await wait_until(lambda: state_of(hass, sensor_battery) == STATE_UNAVAILABLE, timeout=15)

    # The rest of the system is untouched: this is one device, not the station.
    assert state_of(hass, camera_battery) == _CAMERA_BATTERY
    assert state_of(hass, station_firmware) == _STATION_FIRMWARE

    fake_station.params[_SENSOR_CHANNEL] = {
        _BATTERY_PARAM: _SENSOR_BATTERY,
        _SUB1G_RSSI_PARAM: _SENSOR_SUB1G_RSSI,
    }
    await advance_to_poll(hass, 2 * POLL_INTERVAL_SECONDS + 2, anchor=anchor)
    await wait_until(lambda: state_of(hass, sensor_battery) == _SENSOR_BATTERY, timeout=15)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_device_the_station_reports_offline_goes_unavailable(
    hass: HomeAssistant,
    fake_station: FakeStation,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A block the station still serves, flagged offline, makes the device unavailable.

    Verified on hardware: a motion sensor that stopped reporting keeps a live-looking
    block. ``SubDeviceState.online`` False (a code of 0 or above 1) is unavailable,
    True available, None (not reported) stays available. Every entity of the device
    follows it, the update entity included; the camera and the station do not.
    """
    add_motion_sensor(fake_station, fake_cloud)
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]

    sensor_battery = entity_id_for(hass, SENSOR_DOMAIN, SENSOR_SN, BATTERY_KEY)
    sensor_update = entity_id_for(hass, UPDATE_DOMAIN, SENSOR_SN, FIRMWARE_KEY)
    camera_battery = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, BATTERY_KEY)
    station_firmware = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, FIRMWARE_KEY)

    def sensor_online() -> bool | None:
        device = next(d for d in coordinator.data.devices.values() if d.serial == SENSOR_SN)
        return device.online

    def sensor_available() -> bool:
        return all(
            state_of(hass, entity_id) not in (None, STATE_UNAVAILABLE)
            for entity_id in (sensor_battery, sensor_update)
        )

    # No flag in the block: None, which is not offline.
    assert sensor_online() is None
    assert sensor_available()
    assert state_of(hass, sensor_battery) == _SENSOR_BATTERY

    # The library keeps a parameter a later dump leaves out, so each step reports the
    # flag explicitly; None is the block that never carried it, as at setup above.
    polls = 0

    async def report(status: str) -> None:
        nonlocal polls
        polls += 1
        fake_station.params[_SENSOR_CHANNEL][_DEV_STATUS_PARAM] = status
        await advance_to_poll(hass, polls * (POLL_INTERVAL_SECONDS + 1), anchor=anchor)

    for status, online, available in (
        ("0", False, False),  # offline: the station only remembers the device
        ("1", True, True),  # back online
        ("3", False, False),  # offline with a reason code: still offline
    ):
        await report(status)
        await wait_until(
            lambda online=online, available=available: (
                sensor_online() is online and sensor_available() is available
            ),
            timeout=15,
        )
        if not available:
            assert state_of(hass, sensor_battery) == STATE_UNAVAILABLE
            assert state_of(hass, sensor_update) == STATE_UNAVAILABLE
        else:
            assert state_of(hass, sensor_battery) == _SENSOR_BATTERY
        # One device, not the system: the camera and the station are untouched.
        assert state_of(hass, camera_battery) == _CAMERA_BATTERY, status
        assert state_of(hass, station_firmware) == _STATION_FIRMWARE, status

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── readable settings without a control ──────────────────────────────────────

# A slot the station reports that the cloud's device list does not name.
_SERIALLESS_CHANNEL: Final = 2
# The capability attribute an enum sensor publishes its choices under.
_OPTIONS_ATTRIBUTE: Final = "options"
# The station's clock format: writable, but of kind ``other`` (no control), so a sensor.
_CLOCK_FORMAT: Final = "time_format_set"


def _as_read_only(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    """Have the library list ``keys`` as read-only, as a model file can."""
    real = Station.settings_for

    def settings_for(self: Station, device_sn: str | None = None) -> tuple[Setting, ...]:
        return tuple(
            dataclasses.replace(s, writable=False) if s.key in keys else s
            for s in real(self, device_sn)
        )

    monkeypatch.setattr(Station, "settings_for", settings_for)


async def test_a_value_setting_without_a_control_is_a_disabled_diagnostic_sensor(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A readable setting with no control kind is a diagnostic sensor, registered off."""
    seed_setting(fake_station, _CLOCK_FORMAT, 1, channel=_STATION_BLOCK)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    entity_id = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.station_sn, _CLOCK_FORMAT)
    registered = registry.async_get(entity_id)
    assert registered is not None
    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registered.entity_category is EntityCategory.DIAGNOSTIC
    assert state_of(hass, entity_id) is None

    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, entity_id) == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_read_only_duration_reads_in_whole_seconds(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only range in seconds is a duration sensor with no decimals."""
    _as_read_only(monkeypatch, "alarm_delay_custom_2")
    seed_setting(fake_station, "alarm_delay_custom_2", 60)
    entry = await set_up_warm(hass, seed_warm_cache)
    registry = er.async_get(hass)
    entity_id = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, "alarm_delay_custom_2")
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "60"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "s"
    assert state.attributes[ATTR_DEVICE_CLASS] == "duration"
    registered = registry.async_get(entity_id)
    assert registered is not None
    assert registered.options[SENSOR_DOMAIN]["suggested_display_precision"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_read_only_enum_is_an_enum_sensor_shown_by_its_labels(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only enum shows its value's label; a code outside its values is unknown."""
    _as_read_only(monkeypatch, "nightvision_type")
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()
    registry = er.async_get(hass)
    entity_id = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, "nightvision_type")
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    setting = settings_of("T8160")["nightvision_type"]

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[_OPTIONS_ATTRIBUTE] == [setting.label(v) for v in setting.values]
    assert state.attributes[ATTR_DEVICE_CLASS] == "enum"

    seed_setting(fake_station, "nightvision_type", 2)
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == setting.label(2), timeout=15)

    seed_setting(fake_station, "nightvision_type", 7)
    await advance_to_poll(hass, 2 * POLL_INTERVAL_SECONDS + 2, anchor=anchor)
    await wait_until(lambda: state_of(hass, entity_id) == STATE_UNKNOWN, timeout=15)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_paired_device_with_no_serial_gets_no_entities_and_one_debug_line(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slot the station reports but the cloud does not name gets nothing.

    No serial is no identity that survives the device being moved, so it becomes no
    device and no entities rather than a slot-numbered one that breaks on the next
    re-pair. It is said once per channel rather than on every 45 s poll, and the line
    carries the station's redacted serial and the slot number only.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security.coordinator")
    fake_station.params[_SERIALLESS_CHANNEL] = {_BATTERY_PARAM: "50"}
    entry = await set_up_warm(hass, seed_warm_cache)
    anchor = dt_util.utcnow()

    registry = er.async_get(hass)
    registered = er.async_entries_for_config_entry(registry, entry.entry_id)
    batteries = [
        entity
        for entity in registered
        if entity.domain == SENSOR_DOMAIN and entity.unique_id.endswith(BATTERY_KEY)
    ]
    # The camera's, and nothing for the slot with no name.
    assert len(batteries) == 1, [entity.entity_id for entity in batteries]

    # Station and paired-device rows only, not the account's service device.
    devices = [
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if device.entry_type is None
    ]
    assert {device.serial_number for device in devices} == {
        SYNTHETIC.station_sn,
        SYNTHETIC.camera_sn,
    }

    # A second poll, proven to have happened by a value only it could have read.
    camera_battery = entity_id_for(hass, SENSOR_DOMAIN, SYNTHETIC.camera_sn, BATTERY_KEY)
    fake_station.params[_CAMERA_CHANNEL][_BATTERY_PARAM] = "71"
    await advance_to_poll(hass, POLL_INTERVAL_SECONDS + 1, anchor=anchor)
    await wait_until(lambda: state_of(hass, camera_battery) == "71", timeout=15)

    noted = [record for record in caplog.records if "no serial" in record.getMessage()]
    assert len(noted) == 1, [record.getMessage() for record in noted]
    said = noted[0].getMessage()
    assert str(_SERIALLESS_CHANNEL) in said
    assert SYNTHETIC.station_sn not in said

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
