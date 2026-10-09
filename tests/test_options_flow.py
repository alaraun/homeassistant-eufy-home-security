"""The entry's thirteen options, driven through the real options flow.

Each option but the recording length and the sessions per HomeBase is read once at
setup, so the automatic reload is what applies a change.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from conftest import (
    configure_options,
    set_up_warm,
)
from eufy_home_security import (
    DEFAULT_STATION_SESSIONS,
    IMAGE_SOURCES,
    MIN_STATION_SESSIONS,
    STATION_SESSION_LIMIT,
    EufySecurity,
    ImageSource,
    entity_unique_id,
)
from eufy_home_security.testing import SYNTHETIC, FakeStation
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, section
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_home_security import runtime
from custom_components.eufy_home_security.const import (
    CONF_ALARM_TIMEOUT,
    CONF_CAMERA_IMAGE,
    CONF_CLOUD_PUSH,
    CONF_COUNTRY,
    CONF_DETECTION_HOLD,
    CONF_EVENT_HISTORY_DAYS,
    CONF_EVENT_VIDEOS,
    CONF_EXTRA_COUNTRIES,
    CONF_LIVE_SNAPSHOT,
    CONF_RECORD_LENGTH,
    CONF_SCAN_REGIONS,
    CONF_SESSION_PROBE,
    CONF_STATION_SESSIONS,
    DEFAULT_ALARM_TIMEOUT_MINUTES,
    DEFAULT_DETECTION_HOLD_SECONDS,
    DEFAULT_EVENT_HISTORY_DAYS,
    DEFAULT_RECORD_LENGTH_SECONDS,
    DOMAIN,
    OPTIONS_SECTION_CAMERA_IMAGES,
    OPTIONS_SECTION_DETECTIONS,
    OPTIONS_SECTION_EUFY_ACCOUNT,
    OPTIONS_SECTION_HISTORY,
    OPTIONS_SECTION_LIVE_VIEW,
    OPTIONS_SECTION_MORE_COUNTRIES,
    OPTIONS_STEP_INIT,
)


async def _open_options(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Open the entry's options form and check it is the step the integration names."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == OPTIONS_STEP_INIT
    return result


async def _submit_options(
    hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any]
) -> None:
    """Submit the whole options form and wait out the reload it schedules."""
    result = await _open_options(hass, entry)
    result = await configure_options(hass, result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def _registered(hass: HomeAssistant, domain: str, serial: str, key: str) -> str | None:
    """The entity id registered for this unique id, or None when none is."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, entity_unique_id(serial, key))


async def test_the_options_form_offers_its_thirteen_options_with_their_defaults(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Configure offers the hold, the timeout, the camera image, the live snapshot and more.

    The hold is 10 seconds
    and the alarm timeout 10 minutes by default.
    The live snapshot wakes a battery camera, so it is off by default too.
    The camera image is HD from the recording by default, the library's suggested
    configuration. The event history keeps 7 days, and the form's
    description names the folder it is written to. Event videos are off by default
    (storage); the recording length is 30 s, HA's own camera.record default.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _open_options(hass, entry)
    assert result["description_placeholders"]["history_path"] == str(
        Path(hass.config.media_dirs["local"]) / DOMAIN
    )
    sections = result["data_schema"].schema
    assert [str(name) for name in sections] == [
        OPTIONS_SECTION_DETECTIONS,
        OPTIONS_SECTION_CAMERA_IMAGES,
        OPTIONS_SECTION_LIVE_VIEW,
        OPTIONS_SECTION_HISTORY,
        OPTIONS_SECTION_EUFY_ACCOUNT,
        OPTIONS_SECTION_MORE_COUNTRIES,
    ], "the options form does not show its six sections, in order"
    assert all(
        isinstance(group, section) and group.options["collapsed"] for group in sections.values()
    ), "every options section starts collapsed"
    assert all(marker.default is vol.UNDEFINED for marker in sections), (
        "an options section has a default, so the frontend shows that default instead "
        "of the section's saved values"
    )
    markers = [marker for group in sections.values() for marker in group.schema.schema]
    assert [str(marker) for marker in markers] == [
        CONF_DETECTION_HOLD,
        CONF_ALARM_TIMEOUT,
        CONF_CAMERA_IMAGE,
        CONF_LIVE_SNAPSHOT,
        CONF_STATION_SESSIONS,
        CONF_RECORD_LENGTH,
        CONF_EVENT_HISTORY_DAYS,
        CONF_EVENT_VIDEOS,
        CONF_COUNTRY,
        CONF_SESSION_PROBE,
        CONF_CLOUD_PUSH,
        CONF_EXTRA_COUNTRIES,
        CONF_SCAN_REGIONS,
    ], "the options form does not offer exactly its thirteen options, in order"
    (
        hold,
        timeout,
        camera_image,
        live,
        sessions,
        length,
        history_days,
        videos,
        country,
        probe,
        push,
        extra_countries,
        scan_regions,
    ) = markers
    assert hold.default() == DEFAULT_DETECTION_HOLD_SECONDS == 10
    assert timeout.default() == DEFAULT_ALARM_TIMEOUT_MINUTES == 10
    assert camera_image.default() == "hd", "the camera image does not default to HD"
    assert live.default() is False, "the live snapshot wakes battery cameras: opt-in only"
    assert probe.default() is True, (
        "the session probe is on by default: a kick-out must surface without a user action"
    )
    assert push.default() is False, "cloud push uses eufy's cloud: opt-in only"
    assert country.default() == "", "an empty country means Home Assistant's"
    assert extra_countries.default() == [], "each extra country costs a sign-in: none by default"
    assert scan_regions.default() is False, (
        "asking every region may cost a sign-in on each fetch: opt-in only"
    )
    assert history_days.default() == DEFAULT_EVENT_HISTORY_DAYS == 7
    assert videos.default() is False, "event videos take storage: opt-in only"
    assert length.default() == DEFAULT_RECORD_LENGTH_SECONDS == 30
    assert sessions.default() == DEFAULT_STATION_SESSIONS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_form_shows_each_stored_option_in_its_section(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Stored options are flat; the form suggests each under its section, and an empty
    country suggests Home Assistant's."""
    hass.config.country = "EE"
    entry = await set_up_warm(
        hass,
        seed_warm_cache,
        options={CONF_DETECTION_HOLD: 42, CONF_EXTRA_COUNTRIES: ["CH"], CONF_SCAN_REGIONS: True},
    )

    result = await _open_options(hass, entry)
    suggested = {
        (str(name), str(marker)): (marker.description or {}).get("suggested_value")
        for name, group in result["data_schema"].schema.items()
        for marker in group.schema.schema
    }
    assert suggested[OPTIONS_SECTION_DETECTIONS, CONF_DETECTION_HOLD] == 42
    assert suggested[OPTIONS_SECTION_MORE_COUNTRIES, CONF_EXTRA_COUNTRIES] == ["CH"]
    assert suggested[OPTIONS_SECTION_MORE_COUNTRIES, CONF_SCAN_REGIONS] is True
    assert suggested[OPTIONS_SECTION_EUFY_ACCOUNT, CONF_COUNTRY] == "EE"
    assert suggested[OPTIONS_SECTION_DETECTIONS, CONF_ALARM_TIMEOUT] is None, (
        "an option never stored shows its default, not a suggestion"
    )
    hass.config_entries.options.async_abort(result["flow_id"])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_hold_and_timeout_outside_their_ranges_are_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The hold is 5-300 s and the timeout 1-60 min; outside, nothing is saved.

    Both ends of each range are then saved, so the bounds are inclusive.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _open_options(hass, entry)
    for hold, timeout in ((4, 10), (301, 10), (10, 0), (10, 61)):
        with pytest.raises(InvalidData):
            await configure_options(
                hass,
                result["flow_id"],
                {
                    CONF_DETECTION_HOLD: hold,
                    CONF_ALARM_TIMEOUT: timeout,
                },
            )
        assert entry.options == {}, (hold, timeout)
    hass.config_entries.options.async_abort(result["flow_id"])

    for low_or_high in ((5, 1), (300, 60)):
        await _submit_options(
            hass,
            entry,
            {
                CONF_DETECTION_HOLD: low_or_high[0],
                CONF_ALARM_TIMEOUT: low_or_high[1],
            },
        )
        assert entry.options[CONF_DETECTION_HOLD] == low_or_high[0]
        assert entry.options[CONF_ALARM_TIMEOUT] == low_or_high[1]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_saved_options_are_exactly_the_thirteen_offered(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The save is rebuilt from the thirteen keys, five bools, five ints, a choice, a
    country and a country list; it reloads."""

    async def no_push(_eufy: EufySecurity) -> None:
        """The library fakes no push; tests/test_cloud_push.py covers its start."""

    monkeypatch.setattr(runtime, "async_run_push", no_push)
    entry = await set_up_warm(hass, seed_warm_cache)
    clients_before = len(built_clients)

    await _submit_options(
        hass,
        entry,
        {
            CONF_DETECTION_HOLD: 20.0,
            CONF_ALARM_TIMEOUT: 5.0,
            CONF_CAMERA_IMAGE: "hd_only",
            CONF_LIVE_SNAPSHOT: True,
            CONF_SESSION_PROBE: False,
            CONF_CLOUD_PUSH: True,
            CONF_COUNTRY: "CH",
            CONF_EXTRA_COUNTRIES: ["EE"],
            CONF_SCAN_REGIONS: True,
            CONF_EVENT_HISTORY_DAYS: 29.6,
            CONF_EVENT_VIDEOS: True,
            CONF_RECORD_LENGTH: 44.5,
            CONF_STATION_SESSIONS: 3.4,
        },
    )

    assert entry.options == {
        CONF_DETECTION_HOLD: 20,
        CONF_ALARM_TIMEOUT: 5,
        CONF_CAMERA_IMAGE: "hd_only",
        CONF_LIVE_SNAPSHOT: True,
        CONF_SESSION_PROBE: False,
        CONF_CLOUD_PUSH: True,
        CONF_COUNTRY: "CH",
        CONF_EXTRA_COUNTRIES: ["EE"],
        CONF_SCAN_REGIONS: True,
        CONF_EVENT_HISTORY_DAYS: 30,
        CONF_EVENT_VIDEOS: True,
        CONF_RECORD_LENGTH: 45,
        CONF_STATION_SESSIONS: 3,
    }
    assert type(entry.options[CONF_EVENT_HISTORY_DAYS]) is int
    assert type(entry.options[CONF_STATION_SESSIONS]) is int
    assert type(entry.options[CONF_CAMERA_IMAGE]) is str
    assert type(entry.options[CONF_LIVE_SNAPSHOT]) is bool
    assert type(entry.options[CONF_SESSION_PROBE]) is bool
    assert type(entry.options[CONF_CLOUD_PUSH]) is bool
    assert type(entry.options[CONF_SCAN_REGIONS]) is bool
    assert type(entry.options[CONF_COUNTRY]) is str
    assert type(entry.options[CONF_DETECTION_HOLD]) is int
    assert type(entry.options[CONF_ALARM_TIMEOUT]) is int
    assert type(entry.options[CONF_EVENT_VIDEOS]) is bool
    assert type(entry.options[CONF_RECORD_LENGTH]) is int
    assert len(built_clients) == clients_before + 1, "the entry was not reloaded"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_fractional_hold_or_timeout_is_rounded_not_cut(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The selector does not enforce its step, so 9.7 s saves as 10, never 9."""
    entry = await set_up_warm(hass, seed_warm_cache)

    await _submit_options(
        hass,
        entry,
        {CONF_DETECTION_HOLD: 9.7, CONF_ALARM_TIMEOUT: 1.6},
    )
    assert entry.options[CONF_DETECTION_HOLD] == 10
    assert entry.options[CONF_ALARM_TIMEOUT] == 2
    assert type(entry.options[CONF_DETECTION_HOLD]) is int
    assert type(entry.options[CONF_ALARM_TIMEOUT]) is int

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_camera_image_refuses_a_value_outside_its_three_choices(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Only hd, thumbnail and hd_only; anything else is refused and nothing is saved."""
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _open_options(hass, entry)
    for value in ("live", "trigger_frame", "HD", ""):
        with pytest.raises(InvalidData):
            await configure_options(
                hass,
                result["flow_id"],
                {
                    CONF_DETECTION_HOLD: 10,
                    CONF_ALARM_TIMEOUT: 10,
                    CONF_CAMERA_IMAGE: value,
                },
            )
        assert entry.options == {}, value
    hass.config_entries.options.async_abort(result["flow_id"])

    for value in ("thumbnail", "hd_only", "hd"):
        await _submit_options(hass, entry, {CONF_CAMERA_IMAGE: value})
        assert entry.options[CONF_CAMERA_IMAGE] == value

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_camera_image_description_is_built_from_the_library_image_sources(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The timings shown come from IMAGE_SOURCES; the English relies on the facts pinned here."""
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _open_options(hass, entry)
    placeholders = result["description_placeholders"]
    assert {key: placeholders[key] for key in ("thumbnail_seconds", "hd_seconds")} == {
        "thumbnail_seconds": "0.5",
        "hd_seconds": "1.5",
    }
    thumbnail = IMAGE_SOURCES[ImageSource.THUMBNAIL]
    trigger_frame = IMAGE_SOURCES[ImageSource.TRIGGER_FRAME]
    rewrite = (
        "the library's image source facts changed: rewrite the English in "
        "options.step.init.data_description.camera_image to match"
    )
    assert thumbnail.wakes_camera is False, rewrite
    assert trigger_frame.wakes_camera is False, rewrite
    assert trigger_frame.needs_recording is True, rewrite
    assert thumbnail.high_resolution is False, rewrite
    hass.config_entries.options.async_abort(result["flow_id"])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_region_option_reaches_the_client_at_setup_and_on_a_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Off: the next fetch skips a region that listed no devices; on: it asks every region."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert built_clients[-1].cloud.regions_to_list() == ["eu"]

    await _submit_options(hass, entry, {CONF_SCAN_REGIONS: True})

    assert entry.options[CONF_SCAN_REGIONS] is True
    assert built_clients[-1].cloud.regions_to_list() == ["eu", "us"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("scan_regions", [False, True])
async def test_build_client_hands_the_region_option_to_the_library(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, scan_regions: bool
) -> None:
    """The real construction site passes ``scan_regions`` through, both values."""
    seen: list[dict[str, Any]] = []

    def capture(*_args: Any, **kwargs: Any) -> object:
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(runtime, "EufySecurity", capture)

    runtime.build_client(hass, SYNTHETIC.email, None, scan_regions=scan_regions)

    assert seen[0]["scan_regions"] is scan_regions


def _station_budget(entry: MockConfigEntry) -> int:
    budget: int = entry.runtime_data.coordinators[SYNTHETIC.station_sn].station.max_sessions
    return budget


async def test_sessions_per_homebase_outside_the_library_range_are_refused(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The range is the library's, both ends inclusive; outside it nothing is saved."""
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _open_options(hass, entry)
    for value in (MIN_STATION_SESSIONS - 1, STATION_SESSION_LIMIT + 1):
        with pytest.raises(InvalidData):
            await configure_options(hass, result["flow_id"], {CONF_STATION_SESSIONS: value})
        assert entry.options == {}, value
    hass.config_entries.options.async_abort(result["flow_id"])

    for value in (MIN_STATION_SESSIONS, STATION_SESSION_LIMIT):
        await _submit_options(hass, entry, {CONF_STATION_SESSIONS: value})
        assert entry.options[CONF_STATION_SESSIONS] == value
        assert _station_budget(entry) == value

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_changing_only_the_sessions_per_homebase_applies_without_a_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The running station takes the new budget and no new client is built."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert _station_budget(entry) == DEFAULT_STATION_SESSIONS
    clients_before = len(built_clients)

    await _submit_options(hass, entry, {CONF_STATION_SESSIONS: 2})

    assert entry.options[CONF_STATION_SESSIONS] == 2
    assert _station_budget(entry) == 2
    assert len(built_clients) == clients_before, "the budget change reloaded the entry"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_sessions_per_homebase_reach_the_client_at_setup_and_on_a_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Setup builds the client with the stored budget; a reload for another option keeps it."""
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_STATION_SESSIONS: 3})
    assert _station_budget(entry) == 3
    clients_before = len(built_clients)

    await _submit_options(
        hass,
        entry,
        {CONF_LIVE_SNAPSHOT: True, CONF_STATION_SESSIONS: 4},
    )

    assert len(built_clients) == clients_before + 1, "another option changed without a reload"
    assert _station_budget(entry) == 4

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_stored_budget_outside_the_library_range_falls_back_to_the_default(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A hand-edited or foreign value never breaks setup."""
    entry = await set_up_warm(
        hass, seed_warm_cache, options={CONF_STATION_SESSIONS: STATION_SESSION_LIMIT + 5}
    )
    assert _station_budget(entry) == DEFAULT_STATION_SESSIONS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_recording_length_is_5_to_300_seconds_and_applies_without_a_reload(
    hass: HomeAssistant,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Outside 5-300 s nothing is saved; a change of it alone reloads nothing.

    The record action reads the option at each call; turning event videos on reloads.
    """
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await _open_options(hass, entry)
    for value in (4, 301):
        with pytest.raises(InvalidData):
            await configure_options(hass, result["flow_id"], {CONF_RECORD_LENGTH: value})
        assert entry.options == {}, value
    hass.config_entries.options.async_abort(result["flow_id"])

    clients_before = len(built_clients)
    for value in (5, 300):
        await _submit_options(hass, entry, {CONF_RECORD_LENGTH: value})
        assert entry.options[CONF_RECORD_LENGTH] == value
    assert len(built_clients) == clients_before, "the recording length reloaded the entry"

    await _submit_options(hass, entry, {CONF_RECORD_LENGTH: 300, CONF_EVENT_VIDEOS: True})
    assert len(built_clients) == clients_before + 1, "event videos changed without a reload"
    assert entry.runtime_data.recordings is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
