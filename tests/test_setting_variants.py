"""Setting variant groups: the control the eufy app uses for a device is the one on.

The synthetic camera (T8160 behind a T8030) uses ``detection_type_set__v1`` when it reports
AI empowerment (param 1299 = 1), else the primary ``detection_type_set``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from conftest import add_entry, setup_entry
from eufy_home_security import EufySecurity, Station, entity_unique_id
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import variant_mark
from custom_components.eufy_home_security.const import DOMAIN
from custom_components.eufy_home_security.settings import SettingEntitySpec, setting_specs

AI_EMPOWER_PARAM = 1299
PRIMARY = "detection_type_set"
V1 = "detection_type_set__v1"
V2 = "detection_type_set__v2"
OFF = er.RegistryEntryDisabler.INTEGRATION
USER = er.RegistryEntryDisabler.USER


def _ai(fake_station: FakeStation, on: bool) -> None:
    fake_station.params[0][AI_EMPOWER_PARAM] = "1" if on else "0"


def _rows(hass: HomeAssistant, entry: MockConfigEntry, member: str) -> list[er.RegistryEntry]:
    """The camera's registry rows of one member of the detection-types group."""
    prefix = entity_unique_id(SYNTHETIC.camera_sn, f"{member}_")
    rows = [
        r
        for r in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if r.domain == "switch"
        and r.unique_id.startswith(prefix)
        and r.unique_id[len(prefix) :].isdigit()
    ]
    assert rows, member
    return rows


def _state(hass: HomeAssistant, entry: MockConfigEntry, member: str) -> list[tuple[Any, Any]]:
    """(disabled_by, mark) of each row of ``member``."""
    return [(r.disabled_by, variant_mark(r)) for r in _rows(hass, entry, member)]


def _all(hass: HomeAssistant, entry: MockConfigEntry, member: str, value: Any) -> bool:
    return _state(hass, entry, member) == [value] * len(_rows(hass, entry, member))


def _snapshot(hass: HomeAssistant, entry: MockConfigEntry) -> list[tuple[Any, Any]]:
    return [x for member in (PRIMARY, V1, V2) for x in _state(hass, entry, member)]


def _set(hass: HomeAssistant, entry: MockConfigEntry, member: str, disabled_by: Any) -> None:
    registry = er.async_get(hass)
    for row in _rows(hass, entry, member):
        registry.async_update_entity(row.entity_id, disabled_by=disabled_by)


async def _start(
    hass: HomeAssistant,
    fake_station: FakeStation,
    seed: Callable[..., None],
    *,
    ai: bool,
    pref: bool = False,
) -> MockConfigEntry:
    _ai(fake_station, ai)
    seed()
    entry = add_entry(hass)
    if pref:
        hass.config_entries.async_update_entry(entry, pref_disable_new_entities=True)
    assert await setup_entry(hass, entry)
    return entry


async def _restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unload and set the entry up again, then let a reload it scheduled run."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await setup_entry(hass, entry)
    await _settle(hass, entry)


async def _settle(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run a reload Home Assistant schedules 30 s after a row is enabled."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


async def _stop(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("variant", "in_use", "advanced", "expected"),
    [
        (False, None, False, True),
        (True, None, False, False),
        (False, True, False, True),
        (True, True, False, True),
        (False, False, False, False),
        (True, False, False, False),
        (True, True, True, False),
    ],
)
def test_the_member_in_use_is_on_by_default_else_the_primary(
    variant: bool, in_use: bool | None, advanced: bool, expected: bool
) -> None:
    """In use wins; while the library cannot tell, the primary; advanced stays off."""
    setting = dataclasses.replace(settings_of("T8160")[PRIMARY], advanced=advanced)
    spec = SettingEntitySpec(
        platform=Platform.SWITCH, setting=setting, device_sn=None, variant=variant, in_use=in_use
    )
    assert spec.enabled_by_default is expected


async def test_a_new_install_turns_on_the_member_the_camera_uses(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With AI empowerment reported, ``__v1`` is on (marked) and the others off, from the start."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    in_use = {
        s.setting.key: s.in_use
        for s in setting_specs(station)
        if s.device_sn == SYNTHETIC.camera_sn
    }
    assert (in_use[PRIMARY], in_use[V1], in_use[V2]) == (False, True, False)
    assert in_use["nightvision_type_new"] is not None
    # a setting outside any group is not asked about
    assert [k for k, v in in_use.items() if v is None]
    assert _all(hass, entry, PRIMARY, (OFF, "off"))
    assert _all(hass, entry, V1, (None, "on"))
    assert _all(hass, entry, V2, (OFF, "off"))
    await _stop(hass, entry)


async def test_an_install_moves_to_the_member_in_use_and_back(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Rows from the primary-default rule (on, unmarked) move over at the next start, and
    back again."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False)
    assert _all(hass, entry, PRIMARY, (None, "on"))
    assert _all(hass, entry, V1, (OFF, "off"))
    # rows from before the marks
    registry = er.async_get(hass)
    for member in (PRIMARY, V1, V2):
        for row in _rows(hass, entry, member):
            registry.async_update_entity_options(row.entity_id, DOMAIN, {})

    _ai(fake_station, True)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (OFF, "off"))
    assert _all(hass, entry, V1, (None, "on"))
    assert _all(hass, entry, V2, (OFF, None))
    # only variant-group rows carry the mark
    groups = (PRIMARY, V1, V2, "nightvision_type", "automation_config")
    marked = [
        r.unique_id
        for r in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if variant_mark(r) is not None
    ]
    assert marked
    assert all(
        any(u.startswith(entity_unique_id(SYNTHETIC.camera_sn, g)) for g in groups) for u in marked
    ), marked
    # the entities of the member in use exist after the reload
    for row in _rows(hass, entry, V1):
        assert hass.states.get(row.entity_id) is not None, row.entity_id
    # another start with the same answer writes nothing to these rows
    before = _snapshot(hass, entry)
    ids = {r.entity_id for m in (PRIMARY, V1, V2) for r in _rows(hass, entry, m)}
    updates: list[str] = []
    unsub = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: (
            updates.append(event.data["entity_id"])
            if event.data["action"] == "update" and event.data["entity_id"] in ids
            else None
        ),
    )
    await _restart(hass, entry)
    unsub()
    assert _snapshot(hass, entry) == before
    assert updates == []

    _ai(fake_station, False)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (None, "on"))
    assert _all(hass, entry, V1, (OFF, "off"))
    await _stop(hass, entry)


async def test_a_new_install_moves_back_when_the_answer_changes(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Rows created on as the member in use are the integration's: one member on after a flip."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    _ai(fake_station, False)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (None, "on"))
    assert _all(hass, entry, V1, (OFF, "off"))
    await _stop(hass, entry)


async def test_rows_a_user_set_stay_as_they_are(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A user's choice wins: an in-use row they disabled, an extra member they enabled,
    a primary they turned on again after the move."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False)
    registry = er.async_get(hass)
    v1_user_off = _rows(hass, entry, V1)[0]
    registry.async_update_entity(v1_user_off.entity_id, disabled_by=USER)
    v2_user_on = _rows(hass, entry, V2)[0]
    registry.async_update_entity(v2_user_on.entity_id, disabled_by=None)
    await _settle(hass, entry)

    _ai(fake_station, True)
    await _restart(hass, entry)
    row = registry.async_get(v1_user_off.entity_id)
    assert row is not None and (row.disabled_by, variant_mark(row)) == (USER, "user")
    row = registry.async_get(v2_user_on.entity_id)
    assert row is not None and (row.disabled_by, variant_mark(row)) == (None, "user")
    assert all(
        r.disabled_by is None
        for r in _rows(hass, entry, V1)
        if r.entity_id != v1_user_off.entity_id
    )

    primary = _rows(hass, entry, PRIMARY)[0]
    assert primary.disabled_by is OFF
    registry.async_update_entity(primary.entity_id, disabled_by=None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    row = registry.async_get(primary.entity_id)
    assert row is not None and row.disabled_by is None
    await _stop(hass, entry)


async def test_a_member_the_user_turned_on_by_hand_stays_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The workaround (primary off, ``__v1`` on by hand) survives a move and a move back."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False)
    _set(hass, entry, PRIMARY, USER)
    _set(hass, entry, V1, None)
    await _settle(hass, entry)
    for on in (True, False):
        _ai(fake_station, on)
        await _restart(hass, entry)
        assert _all(hass, entry, V1, (None, "user")), on
        assert _all(hass, entry, PRIMARY, (USER, "user")), on
    await _stop(hass, entry)


async def test_a_user_disabled_row_loses_its_mark(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A marked row the user disabled is the user's: enabled again by hand, it stays on."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    registry = er.async_get(hass)
    row = _rows(hass, entry, V1)[0]
    registry.async_update_entity(row.entity_id, disabled_by=USER)
    await _restart(hass, entry)
    after = registry.async_get(row.entity_id)
    assert after is not None and variant_mark(after) == "user"
    _ai(fake_station, False)
    await _restart(hass, entry)
    registry.async_update_entity(row.entity_id, disabled_by=None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    after = registry.async_get(row.entity_id)
    assert after is not None and after.disabled_by is None
    await _stop(hass, entry)


async def test_a_new_entities_preference_moves_only_marked_rows(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With "enable newly added entities" off, a move turns nothing on, and a primary
    row the user enabled stays on."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False, pref=True)
    assert _all(hass, entry, PRIMARY, (OFF, None))
    assert _all(hass, entry, V1, (OFF, None))
    _set(hass, entry, PRIMARY, None)
    await _settle(hass, entry)
    _ai(fake_station, True)
    await _restart(hass, entry)
    assert _all(hass, entry, V1, (OFF, None))
    assert _all(hass, entry, PRIMARY, (None, None))
    await _stop(hass, entry)


async def test_under_the_new_entities_preference_marked_rows_still_move(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Rows this integration turned on or off before the preference was set keep following."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    hass.config_entries.async_update_entry(entry, pref_disable_new_entities=True)
    _ai(fake_station, False)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (None, "on"))
    assert _all(hass, entry, V1, (OFF, "off"))
    await _stop(hass, entry)


@pytest.mark.parametrize("answer", [None, False])
async def test_nothing_moves_while_the_library_names_no_member_in_use(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    answer: bool | None,
) -> None:
    """A group with no member in use (cannot tell, or no member True) keeps every row,
    marked ones included."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False)
    _ai(fake_station, True)
    await _restart(hass, entry)
    before = _snapshot(hass, entry)
    monkeypatch.setattr(Station, "setting_in_use", lambda self, key, device_sn=None: answer)
    _ai(fake_station, False)
    await _restart(hass, entry)
    assert _snapshot(hass, entry) == before
    await _stop(hass, entry)


async def test_a_device_disabled_and_enabled_again_keeps_following(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Rows Home Assistant disabled with their device are not the user's: after the device
    is enabled again they still move, and one member is on."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN, SYNTHETIC.camera_sn), entry.entry_id)
    assert device is not None
    devices.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await _settle(hass, entry)
    _ai(fake_station, False)
    await _restart(hass, entry)
    # nothing of a disabled device moves
    assert _all(hass, entry, PRIMARY, (OFF, "off"))
    assert _all(hass, entry, V1, (er.RegistryEntryDisabler.DEVICE, "on"))
    devices.async_update_device(device.id, disabled_by=None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (None, "on"))
    assert _all(hass, entry, V1, (OFF, "off"))
    await _stop(hass, entry)


async def test_a_restored_row_keeps_the_users_mark(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A row the user enabled, removed and created again with its options, stays the user's."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False)
    registry = er.async_get(hass)
    row = _rows(hass, entry, V2)[0]
    registry.async_update_entity(row.entity_id, disabled_by=None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    after = registry.async_get(row.entity_id)
    assert after is not None and variant_mark(after) == "user"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    registry.async_remove(row.entity_id)
    assert await setup_entry(hass, entry)
    await _settle(hass, entry)
    restored = registry.async_get(row.entity_id)
    assert restored is not None and (restored.disabled_by, variant_mark(restored)) == (None, "user")
    await _stop(hass, entry)


async def test_a_row_created_while_its_device_is_disabled_is_marked(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A row of the member in use that Home Assistant creates disabled with its device is
    this integration's: after the device is enabled, a change of answer moves it."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN, SYNTHETIC.camera_sn), entry.entry_id)
    assert device is not None
    devices.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await _settle(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for row in _rows(hass, entry, V1):
        registry.async_remove(row.entity_id)
        registry.deleted_entities.pop((row.domain, row.platform, row.unique_id), None)
    assert await setup_entry(hass, entry)
    await _settle(hass, entry)
    assert _all(hass, entry, V1, (er.RegistryEntryDisabler.DEVICE, "on"))
    devices.async_update_device(device.id, disabled_by=None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    _ai(fake_station, False)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (None, "on"))
    assert _all(hass, entry, V1, (OFF, "off"))
    await _stop(hass, entry)


async def test_a_row_home_assistant_disabled_stays(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A row disabled by Home Assistant itself is neither moved nor taken as the user's."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    registry = er.async_get(hass)
    for row in _rows(hass, entry, V1):
        registry.async_update_entity(row.entity_id, disabled_by=er.RegistryEntryDisabler.HASS)
    await _settle(hass, entry)
    _ai(fake_station, False)
    await _restart(hass, entry)
    assert _all(hass, entry, V1, (er.RegistryEntryDisabler.HASS, "on"))
    await _stop(hass, entry)
