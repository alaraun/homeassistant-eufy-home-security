"""The key-unusable repair: eufy serves a station a key that does not parse."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import add_entry, setup_entry
from eufy_home_security import (
    CipherUnusableError,
    ConnectionChanged,
    DisconnectCause,
    EufySecurity,
    GuardMode,
    KeyRejectedError,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import errors
from custom_components.eufy_home_security.const import DOMAIN

_CIPHER_CALLS_PREFIX = "cipher:"
_RSA_VERSION = 1


@pytest.fixture
def unusable_key(fake_cloud: FakeCloud, fake_station: FakeStation) -> None:
    """The station answers the RSA handshake; the cloud serves its key lowercased.

    Applied before the warm cache is seeded, so the cached key is the unusable one.
    """
    fake_station.conn_init_version = _RSA_VERSION
    fake_cloud.rsa_cipher_keys[fake_station.serial] = fake_station.rsa_private_key_pem.lower()


def _issues(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> dict[str, ir.IssueEntry]:
    prefix = f"{kind}_{entry.entry_id}_"
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue_id.startswith(prefix)
    }


def _cipher_calls(cloud: FakeCloud) -> int:
    return sum(call.startswith(_CIPHER_CALLS_PREFIX) for call in cloud.calls)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unusable_key_raises_one_key_unusable_issue_and_no_fetch(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    unusable_key: None,
) -> None:
    """Not fixable, names the station, no key-rejected issue, no credentials notice, no fetch."""
    seed_warm_cache()
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY  # first-ever, none came up

    (issue,) = _issues(hass, entry, "key_unusable").values()
    assert not issue.is_fixable
    assert not issue.is_persistent
    assert issue.severity is ir.IssueSeverity.ERROR
    assert (issue.translation_placeholders or {})["station"]
    assert _issues(hass, entry, "key_rejected") == {}
    assert _issues(hass, entry, "credentials_refreshed") == {}
    assert _cipher_calls(fake_cloud) == 0


async def test_a_key_unusable_issue_follows_the_connection_events(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """KEY_UNUSABLE raises it, a reconnect clears it; a rejected key keeps its own issue."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    router = entry.runtime_data.router

    router.handle(
        ConnectionChanged(
            station_sn=SYNTHETIC.station_sn,
            connected=False,
            cause=DisconnectCause.KEY_UNUSABLE,
            error=CipherUnusableError("x", cipher_id=202, reason="rsa_unparsable"),
        )
    )
    assert len(_issues(hass, entry, "key_unusable")) == 1
    assert _issues(hass, entry, "key_rejected") == {}

    router.handle(ConnectionChanged(station_sn=SYNTHETIC.station_sn, connected=True))
    await hass.async_block_till_done()
    assert _issues(hass, entry, "key_unusable") == {}

    router.handle(
        ConnectionChanged(
            station_sn=SYNTHETIC.station_sn,
            connected=False,
            cause=DisconnectCause.KEY_REJECTED,
            error=KeyRejectedError("x"),
        )
    )
    assert _issues(hass, entry, "key_unusable") == {}
    assert len(_issues(hass, entry, "key_rejected")) == 1

    await _unload(hass, entry)


async def test_a_start_error_that_is_unusable_raises_key_unusable(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The setup path maps a CipherUnusableError start result to its own issue."""
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    stations = entry.runtime_data.eufy.stations

    errors.raise_key_rejected_issues(
        hass, entry, stations.values(), {SYNTHETIC.station_sn: CipherUnusableError("x")}
    )

    assert len(_issues(hass, entry, "key_unusable")) == 1
    assert _issues(hass, entry, "key_rejected") == {}
    await _unload(hass, entry)


async def test_every_setup_attempt_drops_an_earlier_key_unusable_issue(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    seed_warm_cache()
    entry = add_entry(hass)
    assert await setup_entry(hass, entry)
    errors.raise_key_unusable_issue(hass, entry, SYNTHETIC.station_sn)
    assert len(_issues(hass, entry, "key_unusable")) == 1

    await _unload(hass, entry)

    assert _issues(hass, entry, "key_unusable") == {}


def test_a_write_on_an_unusable_key_names_its_cause() -> None:
    err = errors.arm_failed(CipherUnusableError("x"), GuardMode.AWAY)
    assert err.translation_key == "station_key_unusable"
    err = errors.setting_write_failed(CipherUnusableError("x"), "motion detection")
    assert err.translation_key == "station_key_unusable"
