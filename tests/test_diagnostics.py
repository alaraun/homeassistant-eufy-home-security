"""The config entry's diagnostics download: session health, and nobody identified.

Every test sets the entry up through Home Assistant on
``eufy_home_security.testing`` (a real ``EufySecurity`` wired to ``FakeCloud`` and a
loopback ``FakeStation``) and downloads diagnostics through Home Assistant's own
diagnostics endpoint, so the payload checked is the one a user attaches to a bug
report.

No test here uses ``freezer``: a FakeStation-backed entry hangs under it (measured).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Final

import pytest
from conftest import detection_event, now_ms, set_up_warm, wait_until
from eufy_home_security import (
    DEFAULT_STATION_SESSIONS,
    DetectionType,
    EufySecurity,
    FrameCipher,
    redact_serial,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    _get_diagnostics_for_config_entry,
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.eufy_home_security.const import DOMAIN

_TOP_LEVEL_KEYS = frozenset(
    {
        "cache",
        "stations",
        "stations_served_elsewhere",
        "skipped_devices",
        "models",
        "push",
        "deduplicator",
        "station_recordings",
        "options",
    }
)
# The owner account a mismatched station stamps on its records: synthetic, 40 hex
# characters, and never the account id the library sends.
_OTHER_ACCOUNT_ID: Final = "f" * 40
_MISMATCH_KEY: Final = "account_id_mismatch"
# The synthetic camera's nickname, which as_redacted_dict() keeps under "name".
_CAMERA_NICKNAME: Final = "Front"


def _station_block(data: Any) -> dict[str, Any]:
    """The synthetic station's block of a diagnostics ``data`` part."""
    assert isinstance(data, dict)
    stations = data["stations"]
    assert isinstance(stations, dict)
    block = stations[redact_serial(SYNTHETIC.station_sn)]
    assert isinstance(block, dict)
    return block


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_diagnostics_download_has_the_cache_summary_and_session_health(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The library's redacted cache view and each station's session health."""
    entry = await set_up_warm(hass, seed_warm_cache)

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert isinstance(data, dict)
    assert set(data) == _TOP_LEVEL_KEYS
    cache = data["cache"]
    assert isinstance(cache, dict)
    assert "cloud_status" in cache

    stations = data["stations"]
    assert isinstance(stations, dict)
    assert list(stations) == [redact_serial(SYNTHETIC.station_sn)]
    station = stations[redact_serial(SYNTHETIC.station_sn)]
    assert isinstance(station, dict)
    assert station["model"] == "T8030"
    assert station["connected"] is True
    assert station["last_error"] is None
    warnings = station["lan_path_warnings"]
    assert isinstance(warnings, list)
    assert all(isinstance(warning, str) for warning in warnings)

    session = station["session"]
    assert isinstance(session, dict)
    assert "ecb_state_refused" in session
    assert "frames_by_cipher" in session
    assert session["extra_live_sessions"] == 0
    assert session["extra_live_sessions_open"] == 0
    assert session["media_slot_channel"] is None
    assert station["max_sessions"] == DEFAULT_STATION_SESSIONS

    sub_devices = station["sub_devices"]
    assert isinstance(sub_devices, list)
    ((camera),) = sub_devices
    assert isinstance(camera, dict)
    assert camera["device_sn"] == redact_serial(SYNTHETIC.camera_sn)
    assert camera["name"] == "**REDACTED**"

    assert data["push"] == {"running": False, "error": None}
    assert data["deduplicator"] == {"dropped_duplicates": 0, "dropped_repeats": 0}
    assert data["options"] == {}
    models = {model["product_code"]: model for model in data["models"]}
    assert models["T8030"]["state"] == "bundled"
    assert models["T8160"]["state"] == "bundled"
    assert models["T8030"]["newer_vendor_data"] is False

    await _unload(hass, entry)


async def test_the_download_has_the_storage_figures_and_nothing_that_names_the_disk(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Each station's last storage record, without any medium's serial or label or the
    format id: the eMMC's are left out as the disk's are."""
    fake_station.storage["format_transaction"] = "synthetic-format-request"
    fake_station.storage["emmc_info"]["serial_number"] = "synthetic-emmc-serial"
    fake_station.storage["emmc_info"]["hdd_label"] = "synthetic-emmc-label"
    entry = await set_up_warm(hass, seed_warm_cache)

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    storage = _station_block(data)["storage"]
    assert isinstance(storage, dict)
    assert storage["storage_days"] == 30
    assert "format_transaction" not in storage
    disk = storage["disk"]
    assert isinstance(disk, dict)
    assert disk["size_mib"] == 238475
    assert disk["temperature_c"] == 38
    assert "serial" not in disk
    assert "label" not in disk
    emmc = storage["emmc"]
    assert isinstance(emmc, dict)
    assert emmc["wear_percent"] == 2
    assert "serial" not in emmc
    assert "label" not in emmc
    text = json.dumps(data)
    for secret in (
        SYNTHETIC.disk_serial,
        SYNTHETIC.disk_label,
        "synthetic-format-request",
        "synthetic-emmc-serial",
        "synthetic-emmc-label",
    ):
        assert secret not in text

    await _unload(hass, entry)


def test_diagnostics_are_config_entry_only() -> None:
    """A download for the whole entry, and none per device."""
    diagnostics = importlib.import_module("custom_components.eufy_home_security.diagnostics")

    assert callable(diagnostics.async_get_config_entry_diagnostics)
    assert not hasattr(diagnostics, "async_get_device_diagnostics")


async def test_diagnostics_count_events_received_by_cipher_after_deduplication(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Every event the router received, by cipher, after the library's dedupe.

    A copy the library dropped is not counted; an enriching copy, which fires nothing,
    is.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    deduplicator = built_clients[-1].deduplicator
    assert deduplicator is not None
    router = entry.runtime_data.router

    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: router.events_received_by_cipher(SYNTHETIC.station_sn)["gcm"] == 1)
    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: deduplicator.dropped_duplicates == 1)
    fake_station.push_camera_event(DetectionType.MOTION, cipher=FrameCipher.ECB)
    await wait_until(lambda: router.events_received_by_cipher(SYNTHETIC.station_sn)["ecb"] == 1)

    router.handle(detection_event(DetectionType.PERSON, t_ms=now_ms(), cipher=None))
    router.handle(detection_event(DetectionType.PERSON, t_ms=now_ms(), enriches=True))
    await hass.async_block_till_done()

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    block = _station_block(data)
    assert block["events_received_by_cipher"] == {"gcm": 2, "ecb": 1, "cloud": 1}
    assert isinstance(data, dict)
    deduplicated = data["deduplicator"]
    assert isinstance(deduplicated, dict)
    assert deduplicated["dropped_duplicates"] == 1

    await _unload(hass, entry)


async def test_the_diagnostics_download_names_no_identifier_or_secret(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The whole downloaded payload, issues included, identifies nobody.

    A push under the right account first, so the counters and session statistics are
    non-trivial; then a push stamped by another account, so the mismatch issue is in
    the payload. The fake's account id changes only after setup.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router
    fake_station.push_camera_event(DetectionType.PERSON)
    await wait_until(lambda: router.events_received_by_cipher(SYNTHETIC.station_sn)["gcm"] == 1)

    fake_station.account_id = _OTHER_ACCOUNT_ID
    # A different type, so the library does not merge it with the first push.
    fake_station.push_camera_event(DetectionType.MOTION)

    def mismatch_issue_ids() -> list[str]:
        return [
            issue_id
            for (domain, issue_id), issue in ir.async_get(hass).issues.items()
            if domain == DOMAIN and issue.translation_key == _MISMATCH_KEY
        ]

    await wait_until(lambda: len(mismatch_issue_ids()) == 1, timeout=10)

    payload = await _get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(payload)

    # Non-vacuity: the issue and the station's redacted key are in what was searched.
    # HA serialises a non-persistent issue without its translation key, so the issue
    # is matched by its id, which the registry above ties to account_id_mismatch.
    assert isinstance(payload, dict)
    assert "issues" in payload
    issues = payload["issues"]
    assert isinstance(issues, list)
    listed = [issue["issue_id"] for issue in issues if isinstance(issue, dict)]
    assert mismatch_issue_ids()[0] in listed
    assert redact_serial(SYNTHETIC.station_sn) in text

    for secret in (
        SYNTHETIC.station_sn,
        SYNTHETIC.camera_sn,
        SYNTHETIC.did,
        SYNTHETIC.account_id,
        SYNTHETIC.email,
        SYNTHETIC.password,
        SYNTHETIC.station_ip,
        _OTHER_ACCOUNT_ID,
    ):
        assert secret not in text
    assert _CAMERA_NICKNAME not in json.dumps(payload["data"])

    await _unload(hass, entry)


async def test_diagnostics_neither_contact_the_cloud_nor_load_the_account_store(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A download spends no sign-in and never reads the password's document."""
    entry = await set_up_warm(hass, seed_warm_cache)
    account_loads = 0
    original_load = Store.async_load

    async def counting_load(self: Store[Any]) -> Any:
        nonlocal account_loads
        if self.key.startswith(f"{DOMAIN}."):
            account_loads += 1
        return await original_load(self)

    monkeypatch.setattr(Store, "async_load", counting_load)
    cloud_calls = len(fake_cloud.calls)

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert isinstance(data, dict)
    assert "cache" in data
    assert len(fake_cloud.calls) == cloud_calls
    assert account_loads == 0

    await _unload(hass, entry)


def _stand_in_coordinator(serial: str, like: Any) -> Any:
    """A served station with ``serial`` and nothing to report, beside the synthetic one."""
    station = SimpleNamespace(
        serial=serial,
        model=None,
        connected=False,
        last_error=None,
        lan_path=SimpleNamespace(warnings=[]),
        # A HomeBase-like stand-in: held session, not standalone.
        is_standalone=False,
        connects_on_demand=False,
        sub_devices=[],
        devices=[],
        stats=like.stats,
        max_sessions=like.max_sessions,
        storage=None,
    )
    return SimpleNamespace(station=station)


async def test_station_labels_name_the_same_station_as_the_cache_labels(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Colliding labels are numbered as the cache numbers them, not as served.

    Three stations share a redacted label. The synthetic one is served first and has
    its cipher cached. A second, whose serial sorts before it, is in the cache with
    nothing cached, so the cache calls it ``label`` and the synthetic one ``label#2``.
    A third is served but not in the cache at all. Each station block must sit under
    the label its cache flags sit under, and the uncached one under a label the
    cache does not use.
    """
    entry = await set_up_warm(hass, seed_warm_cache)
    runtime_data = entry.runtime_data
    coordinators: dict[str, Any] = runtime_data.coordinators
    synthetic = coordinators[SYNTHETIC.station_sn].station
    prefix, tail = SYNTHETIC.station_sn[:5], SYNTHETIC.station_sn[-4:]
    padding = len(SYNTHETIC.station_sn) - 9
    cached_sn = f"{prefix}{'0' * padding}{tail}"
    uncached_sn = f"{prefix}{'Z' * padding}{tail}"
    assert cached_sn < SYNTHETIC.station_sn < uncached_sn
    cache_stations = runtime_data.eufy.cache.section("stations")
    cache_stations[cached_sn] = {}
    coordinators[cached_sn] = _stand_in_coordinator(cached_sn, synthetic)
    coordinators[uncached_sn] = _stand_in_coordinator(uncached_sn, synthetic)
    try:
        data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    finally:
        del coordinators[cached_sn], coordinators[uncached_sn]
        del cache_stations[cached_sn]

    label = redact_serial(SYNTHETIC.station_sn)
    assert isinstance(data, dict)
    stations = data["stations"]
    cached = data["cache"]["stations"]
    assert set(stations) == {label, f"{label}#2", f"{label}#3"}
    assert set(cached) == {label, f"{label}#2"}
    for key in cached:
        is_synthetic = stations[key]["model"] == "T8030"
        assert cached[key]["cipher_cached"] is is_synthetic, key
    assert stations[f"{label}#3"]["model"] is None

    await _unload(hass, entry)
