"""The LAN-reply-blocked repair: a station on Home Assistant's own network that connects
only through eufy's servers, and the host networks the library is given."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from ipaddress import IPv4Network
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import add_entry, setup_entry
from eufy_home_security import (
    ConnectionChanged,
    EufySecurity,
    HostSource,
    LanPath,
    Station,
    redact_serial,
)
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from custom_components.eufy_home_security import errors, runtime
from custom_components.eufy_home_security.const import DOMAIN, FIREWALL_DOCS_URL

_STATION_IP = "192.0.2.10"
_OTHER_IP = "192.0.2.11"


def _path(serial: str, *, blocked: bool, ip: str | None = _STATION_IP) -> LanPath:
    """A station's LAN path as the library reports it after a link."""
    return LanPath(
        serial=serial,
        name="",
        host=ip,
        host_source=HostSource.CLOUD,
        cloud_ip=ip,
        observed_ip=ip,
        local_port=0,
        lan_reply_blocked=blocked,
    )


@pytest.fixture
def blocked_serials(monkeypatch: pytest.MonkeyPatch) -> Iterator[set[str]]:
    """The serials whose ``Station.lan_path`` warns ``lan_reply_blocked``; empty: none."""
    serials: set[str] = set()
    monkeypatch.setattr(
        Station,
        "lan_path",
        property(lambda station: _path(station.serial, blocked=station.serial in serials)),
    )
    yield serials


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, errors.lan_reply_blocked_issue_id(entry.entry_id)
    )


def _station_name(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    assert device is not None
    return errors.station_label(device)


async def _connected(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> None:
    entry.runtime_data.router.handle(ConnectionChanged(station_sn=serial, connected=True))
    await hass.async_block_till_done()


async def test_a_link_with_a_blocked_lan_reply_raises_the_repair_and_a_lan_link_clears_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    blocked_serials: set[str],
) -> None:
    """Each connect decides again: blocked raises the warning naming the station and its
    address, with the firewall docs; a later LAN link withdraws it."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    assert _issue(hass, entry) is None

    blocked_serials.add(fake_station.serial)
    await _connected(hass, entry, fake_station.serial)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "lan_reply_blocked"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert not issue.is_persistent
    assert issue.learn_more_url == FIREWALL_DOCS_URL
    name = _station_name(hass, entry, fake_station.serial)
    assert issue.translation_placeholders == {
        "account": errors.account_label(entry),
        "stations": f"- {_STATION_IP} ({name})",
    }

    blocked_serials.clear()
    await _connected(hass, entry, fake_station.serial)
    assert _issue(hass, entry) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_lan_link_of_one_station_keeps_another_blocked_one_named(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    blocked_serials: set[str],
) -> None:
    """Every connect decides over all the account's stations, not only the one that linked."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    other = "T8030P0000000009"
    coordinators: dict[str, Any] = entry.runtime_data.coordinators
    coordinators[other] = SimpleNamespace(
        station=SimpleNamespace(serial=other, lan_path=_path(other, blocked=True, ip=_OTHER_IP))
    )
    try:
        await _connected(hass, entry, fake_station.serial)
        issue = _issue(hass, entry)
        assert issue is not None
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["stations"] == (
            f"- {_OTHER_IP} ({errors.markdown_literal(redact_serial(other))})"
        )
    finally:
        del coordinators[other]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_record_kept_from_the_last_run_raises_it_at_setup(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A store that says the station's last link needed the punch raises the repair
    before any link of this run; the run's LAN link then withdraws it."""
    seed_warm_cache()
    document = hass_storage[runtime.store_key(SYNTHETIC.email)]["data"]
    document.setdefault("stations", {})[fake_station.serial] = {
        "link_path": "lan",
        "lan_reply_blocked": True,
    }
    actions: list[str] = []

    def record(event: Event[Any]) -> None:
        if event.data["issue_id"].startswith("lan_reply_blocked_"):
            actions.append(event.data["action"])

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, record)
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    await hass.async_block_till_done()

    assert actions[0] == "create"
    assert entry.runtime_data.eufy.stations[fake_station.serial].connected
    assert actions[-1] == "remove"
    assert _issue(hass, entry) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_ignore_lasts_until_the_entry_is_removed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    blocked_serials: set[str],
) -> None:
    """An ignored repair stays ignored across a LAN link, a reload and a new blocked
    link; removing the entry deletes it."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    blocked_serials.add(fake_station.serial)
    await _connected(hass, entry, fake_station.serial)
    issue_id = errors.lan_reply_blocked_issue_id(entry.entry_id)
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)

    blocked_serials.clear()
    await _connected(hass, entry, fake_station.serial)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.dismissed_version is not None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.dismissed_version is not None

    blocked_serials.add(fake_station.serial)
    await _connected(hass, entry, fake_station.serial)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.dismissed_version is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_a_plain_unload_keeps_it_and_disabling_the_entry_deletes_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    blocked_serials: set[str],
) -> None:
    """A disabled entry never decides the repair again, so disabling deletes it."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    blocked_serials.add(fake_station.serial)
    await _connected(hass, entry, fake_station.serial)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None

    assert await setup_entry(hass, entry)
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_stations_are_named_sorted_with_their_address_when_known(
    hass: HomeAssistant,
) -> None:
    """One markdown list line per station, address first, in numeric address order
    (``.9`` before ``.11``), the redacted serial's asterisks escaped; unregistered
    stations go by their
    redacted serial, one without a known address by its name alone; stations without
    the warning are left out."""
    entry = MockConfigEntry(domain=DOMAIN, title="eufy", data={"email": SYNTHETIC.email})
    entry.add_to_hass(hass)
    first, second, third = "T8030P0000000002", "T8030P0000000001", "T8030P0000000003"
    fourth = "T8030P0000000004"
    stations: Sequence[Any] = [
        SimpleNamespace(serial=first, lan_path=_path(first, blocked=True, ip=None)),
        SimpleNamespace(serial=second, lan_path=_path(second, blocked=True, ip=_OTHER_IP)),
        SimpleNamespace(serial=third, lan_path=_path(third, blocked=False)),
        SimpleNamespace(serial=fourth, lan_path=_path(fourth, blocked=True, ip="192.0.2.9")),
    ]
    errors.sync_lan_reply_blocked_issue(hass, entry, stations)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["stations"] == (
        "- 192.0.2.9 (T8030\\*\\*\\*0004)\n- 192.0.2.11 (T8030\\*\\*\\*0001)\n- T8030\\*\\*\\*0002"
    )


async def test_the_host_networks_come_from_home_assistants_adapters(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    client_local_networks: list[Sequence[IPv4Network] | None],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Every adapter's IPv4 networks, enabled or not, once each, are passed to the entry's
    client; with none, ``None`` lets the library read the interfaces."""
    adapters: list[dict[str, Any]] = [
        {"name": "eth0", "enabled": True, "ipv4": [{"address": "192.0.2.5", "network_prefix": 24}]},
        {
            "name": "eth1",
            "enabled": False,
            "ipv4": [
                {"address": "198.51.100.7", "network_prefix": 25},
                {"address": "192.0.2.6", "network_prefix": 24},
            ],
        },
        {"name": "eth2", "enabled": True, "ipv4": []},
        {"name": "lo", "enabled": False, "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}]},
    ]

    async def get_adapters(_hass: HomeAssistant) -> list[dict[str, Any]]:
        return adapters

    monkeypatch.setattr(network, "async_get_adapters", get_adapters)
    assert await runtime.async_local_networks(hass) == [
        IPv4Network("192.0.2.0/24"),
        IPv4Network("198.51.100.0/25"),
    ]
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    assert client_local_networks == [[IPv4Network("192.0.2.0/24"), IPv4Network("198.51.100.0/25")]]
    assert await hass.config_entries.async_unload(entry.entry_id)

    adapters.clear()
    assert await runtime.async_local_networks(hass) is None


async def test_build_client_hands_the_host_networks_to_the_library(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one construction site passes ``local_networks`` on, ``None`` by default."""
    seen: list[dict[str, Any]] = []

    def recording(*args: Any, **kwargs: Any) -> object:
        del args
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(runtime, "EufySecurity", recording)
    ours = [IPv4Network("192.0.2.0/24")]
    runtime.build_client(hass, SYNTHETIC.email, None, local_networks=ours)
    runtime.build_client(hass, SYNTHETIC.email, None)
    assert [kwargs["local_networks"] for kwargs in seen] == [ours, None]
