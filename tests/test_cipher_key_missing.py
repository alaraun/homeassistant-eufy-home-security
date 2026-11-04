"""The cipher-key-missing repair: eufy answers for a station's cipher without its key."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import add_entry, set_up_warm, setup_entry
from eufy_home_security import (
    CipherKeyMissingError,
    CloudProblem,
    ConnectionChanged,
    DisconnectCause,
    EufySecurity,
)
from eufy_home_security.testing import SYNTHETIC, FakeCloud, FakeStation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import errors
from custom_components.eufy_home_security.const import DOMAIN


def _issues(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> dict[str, ir.IssueEntry]:
    prefix = f"{kind}_{entry.entry_id}_"
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue_id.startswith(prefix)
    }


def _missing(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, ir.IssueEntry]:
    return _issues(hass, entry, "cipher_key_missing")


def _error(cipher_id: int) -> CipherKeyMissingError:
    return CipherKeyMissingError("no key in the record", cipher_id=cipher_id, retry_after=3600.0)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def keyless_record(fake_cloud: FakeCloud, fake_station: FakeStation) -> None:
    """The cloud answers for the station's cipher with a record that holds no key.

    Applied before the warm cache is seeded, so the cached credentials lack it too.
    """
    fake_cloud.cipher_keys.clear()
    fake_cloud.cipher_records = {fake_station.cipher_id: {}}


async def test_a_keyless_cipher_answer_raises_one_issue_naming_station_and_cipher(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    keyless_record: None,
) -> None:
    """Not fixable, not persistent; not the cipher-unavailable issue; gone with the unload."""
    seed_warm_cache()
    entry = add_entry(hass)

    assert not await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    (issue,) = _missing(hass, entry).values()
    assert not issue.is_fixable
    assert not issue.is_persistent
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "cipher_key_missing"
    placeholders = issue.translation_placeholders or {}
    assert placeholders["cipher"] == str(fake_station.cipher_id)
    assert placeholders["station"]
    assert _issues(hass, entry, "cipher_unavailable") == {}

    errors.delete_reload_scoped_issues(hass, entry)
    assert _missing(hass, entry) == {}


async def test_a_cloud_problem_raises_it_and_a_reconnect_clears_it(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    router = entry.runtime_data.router

    router.handle(CloudProblem(error=_error(98), station_sn=SYNTHETIC.station_sn))
    router.handle(CloudProblem(error=_error(98), station_sn=SYNTHETIC.station_sn))
    (issue_id, issue) = next(iter(_missing(hass, entry).items()))
    assert len(_missing(hass, entry)) == 1
    assert (issue.translation_placeholders or {})["cipher"] == "98"
    secrets = (SYNTHETIC.station_sn, SYNTHETIC.account_id, SYNTHETIC.email)
    for text in (issue_id, str(issue.translation_placeholders)):
        assert not any(secret in text for secret in secrets)

    router.handle(ConnectionChanged(station_sn=SYNTHETIC.station_sn, connected=True))
    assert _missing(hass, entry) == {}
    await _unload(hass, entry)


async def test_a_session_lost_for_a_keyless_cipher_raises_it(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """ConnectionChanged(CREDENTIALS_UNAVAILABLE) carries no error; the station's own names it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    station = entry.runtime_data.eufy.stations[SYNTHETIC.station_sn]
    monkeypatch.setattr(type(station), "last_error", property(lambda _self: _error(155)))

    entry.runtime_data.router.handle(
        ConnectionChanged(
            station_sn=SYNTHETIC.station_sn,
            connected=False,
            cause=DisconnectCause.CREDENTIALS_UNAVAILABLE,
        )
    )
    (issue,) = _missing(hass, entry).values()
    assert (issue.translation_placeholders or {})["cipher"] == "155"
    assert _issues(hass, entry, "cipher_unavailable") == {}

    monkeypatch.undo()
    await _unload(hass, entry)


async def test_an_issue_left_by_a_failed_setup_is_deleted_by_the_next_setup(
    hass: HomeAssistant,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    await _unload(hass, entry)
    errors.raise_cipher_key_missing_issue(hass, entry, SYNTHETIC.station_sn, 40)
    assert len(_missing(hass, entry)) == 1

    assert await setup_entry(hass, entry)
    assert _missing(hass, entry) == {}
    await _unload(hass, entry)
