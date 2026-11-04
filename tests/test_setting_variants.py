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
from conftest import add_entry, entity_id_for, setup_entry
from eufy_home_security import EufySecurity, Station, entity_unique_id
from eufy_home_security.devices.model_settings import settings_of
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eufy_home_security import (
    variant_mark,
    variant_reset_plan,
    variant_reset_summary,
)
from custom_components.eufy_home_security.const import ATTR_IN_USE, DOMAIN
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
    assert _all(hass, entry, V2, (OFF, "off"))
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


def _in_use(hass: HomeAssistant, entry: MockConfigEntry, member: str) -> list[Any]:
    """The ``in_use`` attribute of each row of ``member`` (``absent`` when it has none)."""
    found = []
    for row in _rows(hass, entry, member):
        state = hass.states.get(row.entity_id)
        assert state is not None, row.entity_id
        found.append(state.attributes.get(ATTR_IN_USE, "absent"))
    return found


async def test_each_member_says_whether_the_app_uses_it(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``in_use`` is true on the member in use, false on another one, absent outside a
    group and while the library cannot tell; it is read at every state write and kept
    out of the recorder."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    _set(hass, entry, PRIMARY, None)
    await _settle(hass, entry)
    assert set(_in_use(hass, entry, V1)) == {True}
    assert set(_in_use(hass, entry, PRIMARY)) == {False}
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    night = hass.states.get(
        entity_id_for(hass, Platform.SELECT, SYNTHETIC.camera_sn, "nightvision_type_new")
    )
    assert night is not None
    expected = station.setting_in_use("nightvision_type_new", SYNTHETIC.camera_sn)
    assert expected is not None
    assert night.attributes[ATTR_IN_USE] is expected
    assert night.state_info is not None
    assert ATTR_IN_USE in night.state_info["unrecorded_attributes"]
    # a setting outside any group: the library calls it in use, the entity says nothing
    assert station.setting_in_use("watermark_set", SYNTHETIC.camera_sn) is True
    watermark = hass.states.get(
        entity_id_for(hass, Platform.SELECT, SYNTHETIC.camera_sn, "watermark_set")
    )
    assert watermark is not None
    assert ATTR_IN_USE not in watermark.attributes

    monkeypatch.setattr(Station, "setting_in_use", lambda self, key, device_sn=None: None)
    coordinator = entry.runtime_data.coordinators[SYNTHETIC.station_sn]
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()
    assert set(_in_use(hass, entry, V1)) == {"absent"}
    assert set(_in_use(hass, entry, PRIMARY)) == {"absent"}
    await _stop(hass, entry)


def _reset_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"variant_reset_{entry.entry_id}")


def _unmark(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Drop the marks, as on rows from before them."""
    registry = er.async_get(hass)
    for member in (PRIMARY, V1, V2):
        for row in _rows(hass, entry, member):
            registry.async_update_entity_options(row.entity_id, DOMAIN, {})


def _mark_user(hass: HomeAssistant, row: er.RegistryEntry) -> None:
    er.async_get(hass).async_update_entity_options(
        row.entity_id, DOMAIN, {"variant_default": "user"}
    )


async def _all_on_from_before_the_marks(
    hass: HomeAssistant, fake_station: FakeStation, seed_warm_cache: Callable[..., None]
) -> MockConfigEntry:
    """Every member's rows enabled and unmarked, then a start on which ``__v1`` is in use:
    the primary moves off, ``__v1`` and ``__v2`` stay on."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=False)
    _set(hass, entry, V1, None)
    _set(hass, entry, V2, None)
    await _settle(hass, entry)
    _unmark(hass, entry)
    _ai(fake_station, True)
    await _restart(hass, entry)
    assert _all(hass, entry, PRIMARY, (OFF, "off"))
    assert _all(hass, entry, V1, (None, None))
    assert _all(hass, entry, V2, (None, None))
    return entry


async def _confirm(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open the reset fix and confirm it; the form's placeholders."""
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_init(DOMAIN, data={"issue_id": f"variant_reset_{entry.entry_id}"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    placeholders = dict(result["description_placeholders"] or {})
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return placeholders


async def test_controls_of_unused_versions_left_on_raise_a_reset_repair(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Unmarked rows of a member not in use that stay on raise one fixable issue counting
    them; the fix disables them and marks every row, and leaves the user's rows: one
    marked "user" and one they enabled after the integration turned it off."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    v2 = _rows(hass, entry, V2)
    issue = _reset_issue(hass, entry)
    assert issue is not None and issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.data == {"entry_id": entry.entry_id}
    placeholders = issue.translation_placeholders or {}
    assert (placeholders["disable"], placeholders["enable"]) == (str(len(v2)), "0")
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, SYNTHETIC.camera_sn), entry.entry_id
    )
    assert device is not None and device.name
    assert placeholders["devices"] == device.name

    user_on = v2[0]
    _mark_user(hass, user_on)
    primary_on = _rows(hass, entry, PRIMARY)[0]
    er.async_get(hass).async_update_entity(primary_on.entity_id, disabled_by=None)
    dr.async_get(hass).async_update_device(device.id, name_by_user="Porch")
    shown = await _confirm(hass, entry)
    assert (shown["disable"], shown["enable"]) == (str(len(v2) - 1), "0")
    assert shown["devices"] == "Porch"
    registry = er.async_get(hass)
    assert _all(hass, entry, V1, (None, "on"))
    for row in _rows(hass, entry, V2):
        expected = (None, "user") if row.entity_id == user_on.entity_id else (OFF, "off")
        assert (row.disabled_by, variant_mark(row)) == expected, row.entity_id
    after = registry.async_get(primary_on.entity_id)
    assert after is not None and after.disabled_by is None
    assert _reset_issue(hass, entry) is None

    await _settle(hass, entry)
    await _restart(hass, entry)
    assert _reset_issue(hass, entry) is None
    await _stop(hass, entry)


async def test_the_reset_enables_the_version_in_use_and_the_reload_withdraws_the_issue(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Under "disable new entities", a primary the user turned on while ``__v1`` is in use:
    the fix turns ``__v1`` on (Home Assistant reloads), the primary off, marks the unused
    ``__v2``, and keeps a row the user disabled. A group with nothing on to disable (night
    vision, its version in use off) is left as it is."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True, pref=True)
    assert _reset_issue(hass, entry) is None
    _set(hass, entry, PRIMARY, None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    primary = _rows(hass, entry, PRIMARY)
    v1 = _rows(hass, entry, V1)
    # the night-vision select in use is off under the preference too
    station = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station
    night_key = next(
        k
        for k in ("nightvision_type", "nightvision_type_new")
        if station.setting_in_use(k, SYNTHETIC.camera_sn)
    )
    night = entity_id_for(hass, Platform.SELECT, SYNTHETIC.camera_sn, night_key)
    night_before = er.async_get(hass).async_get(night)
    assert night_before is not None and night_before.disabled_by is OFF
    issue = _reset_issue(hass, entry)
    assert issue is not None
    placeholders = issue.translation_placeholders or {}
    counts = (str(len(primary)), str(len(v1)))
    assert (placeholders["disable"], placeholders["enable"]) == counts

    user_off = v1[0]
    er.async_get(hass).async_update_entity(user_off.entity_id, disabled_by=USER)
    shown = await _confirm(hass, entry)
    assert (shown["disable"], shown["enable"]) == (str(len(primary)), str(len(v1) - 1))
    await _settle(hass, entry)
    # nothing of the night-vision group is on to move: it stays as the preference left it
    night_after = er.async_get(hass).async_get(night)
    assert night_after is not None
    assert (night_after.disabled_by, variant_mark(night_after)) == (OFF, None)
    for row in _rows(hass, entry, V1):
        expected = (USER, "user") if row.entity_id == user_off.entity_id else (None, "on")
        assert (row.disabled_by, variant_mark(row)) == expected, row.entity_id
        if row.disabled_by is None:
            assert hass.states.get(row.entity_id) is not None, row.entity_id
    assert _all(hass, entry, PRIMARY, (OFF, "off"))
    assert _all(hass, entry, V2, (OFF, "off"))
    assert _reset_issue(hass, entry) is None
    await _stop(hass, entry)


@pytest.mark.parametrize("answer", [None, "user"])
async def test_no_reset_repair_without_a_control_to_reset(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    answer: str | None,
) -> None:
    """No issue while the library cannot tell (``None``), or when every extra row on is
    the user's (``"user"``); an issue raised before is withdrawn at the next start."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    assert _reset_issue(hass, entry) is not None
    if answer is None:
        monkeypatch.setattr(Station, "setting_in_use", lambda self, key, device_sn=None: None)
    else:
        for row in _rows(hass, entry, V2):
            _mark_user(hass, row)
    await _restart(hass, entry)
    assert _reset_issue(hass, entry) is None
    await _stop(hass, entry)


async def test_an_ignored_reset_repair_stays_ignored_and_goes_with_the_entry(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Ignoring the issue survives a restart of the entry; removing the entry deletes it."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    ir.async_ignore_issue(hass, DOMAIN, f"variant_reset_{entry.entry_id}", True)
    await _restart(hass, entry)
    issue = _reset_issue(hass, entry)
    assert issue is not None and issue.dismissed_version is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _reset_issue(hass, entry) is None


async def test_the_reset_repair_outlives_an_unload_and_its_fix_waits_for_the_entry(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The issue stays while the entry is unloaded; its fix aborts until the entry is
    loaded, and nothing changes."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    await _stop(hass, entry)
    assert _reset_issue(hass, entry) is not None
    before = _snapshot(hass, entry)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_init(DOMAIN, data={"issue_id": f"variant_reset_{entry.entry_id}"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    assert _snapshot(hass, entry) == before
    assert _reset_issue(hass, entry) is not None


async def test_a_version_the_user_enables_after_an_upgrade_stays_theirs(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A row from before the marks that the integration had off gets "off" at the next
    start, so the user's later enable makes it theirs and raises no reset repair."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    _unmark(hass, entry)
    await _restart(hass, entry)
    assert _all(hass, entry, V2, (OFF, "off"))
    assert _reset_issue(hass, entry) is None
    _set(hass, entry, V2, None)
    await _settle(hass, entry)
    assert _all(hass, entry, V2, (None, "user"))
    assert _reset_issue(hass, entry) is None
    await _stop(hass, entry)


@pytest.mark.parametrize(
    ("start", "kept"), [("undecided", True), ("device_disabled", True), ("user", False)]
)
async def test_an_ignored_reset_repair_outlives_a_start_that_cannot_count(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    kept: bool,
) -> None:
    """At a count of 0 an ignored issue stays while a group is undecided or its device
    disabled, and stays ignored after the next decided start; with every group decided
    and every row the user's, it goes."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    ir.async_ignore_issue(hass, DOMAIN, f"variant_reset_{entry.entry_id}", True)
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN, SYNTHETIC.camera_sn), entry.entry_id)
    assert device is not None
    real = Station.setting_in_use
    if start == "undecided":
        monkeypatch.setattr(Station, "setting_in_use", lambda self, key, device_sn=None: None)
    elif start == "device_disabled":
        devices.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
        await _settle(hass, entry)
    else:
        for row in _rows(hass, entry, V2):
            _mark_user(hass, row)
    await _restart(hass, entry)
    issue = _reset_issue(hass, entry)
    if not kept:
        assert issue is None
        await _stop(hass, entry)
        return
    assert issue is not None and issue.dismissed_version is not None
    monkeypatch.setattr(Station, "setting_in_use", real)
    devices.async_update_device(device.id, disabled_by=None)
    await _settle(hass, entry)
    await _restart(hass, entry)
    issue = _reset_issue(hass, entry)
    assert issue is not None and issue.dismissed_version is not None
    await _stop(hass, entry)


async def test_a_disabled_entry_withdraws_the_reset_repair(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Disabling the entry deletes the issue its fix could never apply."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _reset_issue(hass, entry) is None


@pytest.mark.parametrize(
    ("pref", "disabler"), [(True, USER), (False, USER), (False, er.RegistryEntryDisabler.HASS)]
)
async def test_the_reset_leaves_a_setting_whose_version_in_use_stays_off(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    pref: bool,
    disabler: er.RegistryEntryDisabler,
) -> None:
    """With the version in use disabled by the user or Home Assistant and another version
    on, the group is left as it is, with or without "disable new entities": no issue,
    nothing to reset, and the rows stay across a restart."""
    if pref:
        entry = await _start(hass, fake_station, seed_warm_cache, ai=True, pref=True)
        _set(hass, entry, V1, None)
        await _settle(hass, entry)
        _set(hass, entry, V1, USER)
        _set(hass, entry, V2, None)
        await _settle(hass, entry)
    else:
        entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
        assert _reset_issue(hass, entry) is not None
        _set(hass, entry, V1, disabler)
    await _restart(hass, entry)
    assert {row.disabled_by for row in _rows(hass, entry, V1)} == {disabler}
    assert {row.disabled_by for row in _rows(hass, entry, V2)} == {None}
    assert _reset_issue(hass, entry) is None
    assert variant_reset_plan(hass, entry, entry.runtime_data.coordinators.values()) == []
    before = _snapshot(hass, entry)
    await _restart(hass, entry)
    assert _snapshot(hass, entry) == before
    assert _reset_issue(hass, entry) is None
    await _stop(hass, entry)


async def test_a_version_in_use_the_user_turned_on_lets_the_reset_disable_the_others(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The version in use counts as working when it is on and the user's: the reset still
    disables the other version that is on, and leaves the user's rows as they are."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    for row in _rows(hass, entry, V1):
        _mark_user(hass, row)
    shown = await _confirm(hass, entry)
    assert (shown["disable"], shown["enable"]) == (str(len(_rows(hass, entry, V2))), "0")
    assert _all(hass, entry, V1, (None, "user"))
    assert _all(hass, entry, V2, (OFF, "off"))
    await _stop(hass, entry)


@pytest.mark.parametrize("when", ["open", "confirm"])
async def test_a_reset_with_nothing_left_to_change_closes_without_a_form(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    when: str,
) -> None:
    """The user sets every row the reset would disable before the fix opens, or while its
    form is open: the fix aborts, changes no row (marks neither) and deletes the issue."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    assert _reset_issue(hass, entry) is not None
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    data = {"issue_id": f"variant_reset_{entry.entry_id}"}
    if when == "confirm":
        result = await manager.async_init(DOMAIN, data=data)
        assert result["type"] is FlowResultType.FORM
    for row in _rows(hass, entry, V2):
        _mark_user(hass, row)
    before = _snapshot(hass, entry)
    if when == "open":
        result = await manager.async_init(DOMAIN, data=data)
    else:
        result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "nothing_to_reset"
    assert _snapshot(hass, entry) == before
    assert _reset_issue(hass, entry) is None
    await _stop(hass, entry)


async def test_a_reset_left_with_only_enables_still_asks(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A plan that only enables the version in use is a change: the form shows it."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    _set(hass, entry, V2, USER)
    _set(hass, entry, V1, OFF)
    v1 = _rows(hass, entry, V1)
    shown = await _confirm(hass, entry)
    assert (shown["disable"], shown["enable"]) == ("0", str(len(v1)))
    assert _all(hass, entry, V1, (None, "on"))
    assert _all(hass, entry, V2, (USER, None))
    await _settle(hass, entry)
    await _stop(hass, entry)


async def test_the_reset_names_the_devices_it_enables_on(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A device whose only change is an enable is named, by the name Home Assistant shows."""
    entry = await _start(hass, fake_station, seed_warm_cache, ai=True)
    _set(hass, entry, V1, OFF)
    devices = dr.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN, SYNTHETIC.camera_sn), entry.entry_id)
    assert device is not None
    devices.async_update_device(device.id, name_by_user="Porch")
    plan = variant_reset_plan(hass, entry, entry.runtime_data.coordinators.values())
    summary = variant_reset_summary(hass, plan)
    assert (summary.disable, summary.enable) == (0, len(_rows(hass, entry, V1)))
    assert summary.devices == ["Porch"]
    _set(hass, entry, V1, None)
    await _settle(hass, entry)
    await _stop(hass, entry)


async def test_the_reset_form_shows_again_when_the_plan_changed(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A change between opening the form and confirming it applies nothing: the form
    shows again with the new counts, and only a confirmation of the plan shown applies it."""
    entry = await _all_on_from_before_the_marks(hass, fake_station, seed_warm_cache)
    v2 = _rows(hass, entry, V2)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_init(DOMAIN, data={"issue_id": f"variant_reset_{entry.entry_id}"})
    assert (result["description_placeholders"] or {})["disable"] == str(len(v2))
    er.async_get(hass).async_update_entity(v2[0].entity_id, disabled_by=USER)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert (result["description_placeholders"] or {})["disable"] == str(len(v2) - 1)
    rest = [r for r in _rows(hass, entry, V2) if r.entity_id != v2[0].entity_id]
    assert [(r.disabled_by, variant_mark(r)) for r in rest] == [(None, None)] * (len(v2) - 1)
    # a row already in the plan whose state changed: same rows, other counts
    v1 = _rows(hass, entry, V1)[0]
    er.async_get(hass).async_update_entity(v1.entity_id, disabled_by=OFF)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert (result["description_placeholders"] or {})["enable"] == "1"
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    for row in _rows(hass, entry, V2):
        expected = (USER, None) if row.entity_id == v2[0].entity_id else (OFF, "off")
        assert (row.disabled_by, variant_mark(row)) == expected, row.entity_id
    await _settle(hass, entry)
    await _stop(hass, entry)
