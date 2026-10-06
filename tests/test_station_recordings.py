"""A camera's recordings on its HomeBase, listed and fetched on request (``station_recordings.py``).

End to end on the library's loopback ``FakeStation`` over Home Assistant's websocket
and HTTP test clients: its ``rows`` are the station's history, its ``images`` the
stills on its disk, and a fetched clip's MPEG-TS is remuxed by ``tests/fake_ffmpeg.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import (
    PUSHED_THUMB_PATH,
    SYNTHETIC,
    add_entry,
    detection_event,
    entity_id_for,
    panel_entity_id,
    setup_entry,
    wait_until,
)
from eufy_home_security import (
    ClipWriter,
    DetectionType,
    DeviceTimeoutError,
    EufySecurity,
    HistoryRecord,
    LiveStreamLimitError,
    MediaClip,
    Station,
)
from eufy_home_security.testing import FakeStation
from fake_ffmpeg import REMUX_MAGIC
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.eufy_home_security import history, recordings, station_recordings
from custom_components.eufy_home_security.const import (
    CAMERA_KEY,
    CONF_EVENT_VIDEOS,
    DOMAIN,
    RECORDING_SYNC_FIRST_DELAY_SECONDS,
)

CAMERA_NAME: Final = "Front"
# A history row's record id: its day (YYYYMMDD) times 100000 plus a sequence.
_DAY_FACTOR: Final = 100_000
_ROW_TIME: Final = "%Y-%m-%d %H:%M:%S"
JPEG: Final = b"\xff\xd8RECORDING-STILL\xff\xd9"
OBFUSCATED: Final = b"v8_eufysecurity" + b"\x00" * 32
OTHER_SN: Final = "T8160P2000099999"


def _row(
    counter: int,
    *,
    ago: timedelta = timedelta(minutes=10),
    length_s: int = 10,
    **extra: Any,
) -> dict[str, Any]:
    """A history row of the synthetic camera with a recording that started ``ago``."""
    start = (datetime.now().astimezone() - ago).replace(microsecond=0)
    return {
        "record_id": int(start.strftime("%Y%m%d")) * _DAY_FACTOR + counter,
        "device_sn": SYNTHETIC.camera_sn,
        "start_time": start.strftime(_ROW_TIME),
        "end_time": (start + timedelta(seconds=length_s)).strftime(_ROW_TIME),
        "storage_path": f"/zx/hdd_data0/Camera00/{counter}.zxvideo",
        "frame_num": 6,
        **extra,
    }


def _day_rows(back: int, count: int, **extra: Any) -> list[dict[str, Any]]:
    """``count`` recordings of the day ``back`` days before today, newest first."""
    day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start = day - timedelta(days=back)
    rows = []
    for counter in range(count, 0, -1):
        at = start + timedelta(minutes=counter)
        rows.append(
            {
                "record_id": int(at.strftime("%Y%m%d")) * _DAY_FACTOR + counter,
                "device_sn": SYNTHETIC.camera_sn,
                "start_time": at.strftime(_ROW_TIME),
                "end_time": (at + timedelta(seconds=10)).strftime(_ROW_TIME),
                "storage_path": f"/zx/hdd_data0/Camera00/{back}_{counter}.zxvideo",
                **extra,
            }
        )
    return rows


def _day(back: int) -> date:
    return datetime.now().astimezone().date() - timedelta(days=back)


def _queried_days(fake_station: FakeStation) -> list[date]:
    """The day of every history query the station received, in order."""
    return [date.fromisoformat(str(q["start_date"])) for q in fake_station.history_queries]


def _camera(hass: HomeAssistant) -> str:
    return entity_id_for(hass, CAMERA_DOMAIN, SYNTHETIC.camera_sn, CAMERA_KEY)


def _videos(hass: HomeAssistant) -> list[Path]:
    root = history.history_dir(hass)
    return sorted(root.rglob("*.mp4")) if root.is_dir() else []


def _manager(entry: MockConfigEntry) -> station_recordings.StationRecordings:
    manager = entry.runtime_data.station_recordings
    assert manager is not None
    return manager


async def _set_up(
    hass: HomeAssistant,
    seed_warm_cache: Callable[..., None],
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    seed_warm_cache()
    entry = add_entry(hass, options=options)
    assert await setup_entry(hass, entry)
    return entry


async def _ws(client: MockHAClientWebSocket, command: str, **fields: Any) -> dict[str, Any]:
    await client.send_json_auto_id({"type": command, **fields})
    reply: dict[str, Any] = await client.receive_json()
    return reply


async def _list(client: MockHAClientWebSocket, entity_id: str, **fields: Any) -> dict[str, Any]:
    reply = await _ws(client, station_recordings.WS_LIST, entity_id=entity_id, **fields)
    assert reply["success"], reply
    result: dict[str, Any] = reply["result"]
    return result


async def _fetch(client: MockHAClientWebSocket, entity_id: str, record_id: int) -> dict[str, Any]:
    return await _ws(client, station_recordings.WS_FETCH, entity_id=entity_id, record_id=record_id)


def _count_downloads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the id of every recording the station is asked to download."""
    calls: list[int] = []
    real = Station.async_download_recording

    async def counting(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        calls.append(record.record_id)
        return await real(self, record, write, wait=wait)

    monkeypatch.setattr(Station, "async_download_recording", counting)
    return calls


def _fail_downloads(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def failing(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        raise error

    monkeypatch.setattr(Station, "async_download_recording", failing)


def _store_data(hass_storage: dict[str, Any], entry: MockConfigEntry) -> dict[str, Any]:
    data: dict[str, Any] = hass_storage[recordings.store_key(entry.entry_id)]["data"]
    return data


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── listing ──────────────────────────────────────────────────────────────────


async def test_the_listing_carries_each_recording_of_the_camera_newest_first(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Times, length, size, settled, a signed thumbnail URL; another camera's row and an
    arming row without a recording are left out."""
    running = _row(3, ago=timedelta(seconds=5), thumb_path="/zx/rec3.jpg", folder_size=2048)
    settled = _row(2, ago=timedelta(minutes=10))
    fake_station.rows = [
        running,
        _row(4, device_sn=OTHER_SN),
        _row(5, storage_path=""),
        settled,
    ]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    result = await _list(client, _camera(hass))

    assert result["supported"] is True
    assert (result["more"], result["next"]) == (False, None)
    assert [r["record_id"] for r in result["recordings"]] == [
        running["record_id"],
        settled["record_id"],
    ]
    first, second = result["recordings"]
    started = HistoryRecord.from_row(running).started_at
    assert started is not None
    assert first["started_at"] == started.isoformat()
    assert dt_util.parse_datetime(first["ended_at"]) == started + timedelta(seconds=10)
    assert (first["duration_s"], first["size_bytes"]) == (10.0, 2048)
    assert (first["settled"], second["settled"]) == (False, True)
    assert (first["kind"], first["media_content_id"]) == (None, None)
    assert first["thumb_url"].startswith(
        f"/api/{DOMAIN}/recording_thumb/{_camera(hass)}/{running['record_id']}?authSig="
    )
    assert second["thumb_url"] is None
    assert second["size_bytes"] is None
    await _unload(hass, entry)


async def test_the_listing_names_the_kind_of_the_detections_still(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A recording whose detection's still was written carries that still's kind."""
    row = _row(7)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=int(dt_util.utcnow().timestamp() * 1000) - 1000,
            thumb_path=PUSHED_THUMB_PATH,
            record_id=row["record_id"],
        )
    )
    snapshots = entry.runtime_data.snapshots
    await wait_until(lambda: snapshots.image_for(SYNTHETIC.camera_sn) is not None)
    await wait_until(lambda: not snapshots.busy)
    client = await hass_ws_client(hass)

    result = await _list(client, _camera(hass))

    assert [r["kind"] for r in result["recordings"]] == ["person"]
    await _unload(hass, entry)


async def test_a_listing_is_reused_for_a_minute_and_identical_calls_share_one(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls in flight cost one listing, a call within a minute none, a later one
    or another day count one more."""
    fake_station.rows = [_row(1)]
    entry = await _set_up(hass, seed_warm_cache)
    clock = [1000.0]
    monkeypatch.setattr(station_recordings, "_clock", lambda: clock[0])
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    await client.send_json_auto_id({"type": station_recordings.WS_LIST, "entity_id": entity_id})
    await client.send_json_auto_id({"type": station_recordings.WS_LIST, "entity_id": entity_id})
    replies = [await client.receive_json(), await client.receive_json()]
    assert all(reply["success"] for reply in replies)
    assert _manager(entry).stats.list_queries == 1

    clock[0] += station_recordings.LIST_CACHE_SECONDS - 1
    await _list(client, entity_id)
    assert _manager(entry).stats.list_queries == 1

    await _list(client, entity_id, days=2)
    assert _manager(entry).stats.list_queries == 2

    clock[0] += 2
    await _list(client, entity_id)
    assert _manager(entry).stats.list_queries == 3
    assert _manager(entry).stats.list_calls == 5
    await _unload(hass, entry)


async def test_a_page_asks_only_the_days_it_needs_and_names_where_the_next_starts(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``limit`` rows newest first from today backwards: no day after the page filled is
    asked; ``next`` continues below the page's last row, in its day; the last page
    reaches the window's first day, no day before it, and says ``more`` False."""
    today, day_before, two_back = _day_rows(0, 3), _day_rows(1, 4), _day_rows(2, 5)
    old = _day_rows(5, 2)
    fake_station.rows = [*today, *day_before, *two_back, *old, *_day_rows(1, 2, device_sn=OTHER_SN)]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)
    ids = [r["record_id"] for r in (*today, *day_before, *two_back, *old)]

    first = await _list(client, entity_id, days=30, limit=5)

    assert [r["record_id"] for r in first["recordings"]] == ids[:5]
    assert (first["more"], first["next"]) == (True, str(ids[4]))
    assert _queried_days(fake_station) == [_day(0), _day(1)]

    fake_station.history_queries.clear()
    second = await _list(client, entity_id, days=30, limit=5, before=first["next"])

    assert [r["record_id"] for r in second["recordings"]] == ids[5:10]
    assert (second["more"], second["next"]) == (True, str(ids[9]))
    assert _queried_days(fake_station) == [_day(1), _day(2)]
    assert fake_station.history_queries[0]["start_id"] == ids[4]

    fake_station.history_queries.clear()
    rest = await _list(client, entity_id, days=30, limit=50, before=second["next"])

    assert [r["record_id"] for r in rest["recordings"]] == ids[10:]
    assert (rest["more"], rest["next"]) == (False, None)
    assert _queried_days(fake_station) == [_day(back) for back in range(2, 30)]
    assert _manager(entry).stats.list_queries == 3
    await _unload(hass, entry)


async def test_a_page_ending_on_a_days_last_row_continues_with_the_day_before(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The cursor is that day's last row; the next page finds nothing older in its day
    and goes on with the day before. The default ``limit`` is 10."""
    today, day_before = _day_rows(0, 3), _day_rows(1, 12)
    fake_station.rows = [*today, *day_before]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    first = await _list(client, entity_id, days=3, limit=3)
    assert (first["more"], first["next"]) == (True, str(today[-1]["record_id"]))

    second = await _list(client, entity_id, days=3, before=first["next"])

    assert [r["record_id"] for r in second["recordings"]] == [
        r["record_id"] for r in day_before[:10]
    ]
    assert second["more"] is True
    assert _queried_days(fake_station) == [_day(0), _day(0), _day(1)]
    await _unload(hass, entry)


async def test_a_page_stays_inside_the_days_window(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """With ``days`` 2 a short page asks today and the day before only and ends the list;
    a page that fills on the window's last row says ``more``, and the page after it is
    empty, asks no day before the window and ends the list."""
    fake_station.rows = [*_day_rows(1, 2), *_day_rows(4, 3)]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    page = await _list(client, entity_id, days=2, limit=10)

    assert len(page["recordings"]) == 2
    assert (page["more"], page["next"]) == (False, None)
    assert _queried_days(fake_station) == [_day(0), _day(1)]

    full = await _list(client, entity_id, days=2, limit=2)
    assert (len(full["recordings"]), full["more"]) == (2, True)
    fake_station.history_queries.clear()
    after = await _list(client, entity_id, days=2, limit=2, before=full["next"])

    assert (after["recordings"], after["more"], after["next"]) == ([], False, None)
    assert _queried_days(fake_station) == [_day(1)]
    await _unload(hass, entry)


async def test_a_day_pages_only_that_days_recordings(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``day`` lists that day's recordings only, newest first, in pages: no query of a
    later or an earlier day, the cursor stays in the day, the last page ends the list.
    The window reaches back to the day whatever the entry's history days."""
    today, day_before, two_back = _day_rows(0, 3), _day_rows(1, 12), _day_rows(2, 5)
    fake_station.rows = [*today, *day_before, *two_back]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)
    ids = [r["record_id"] for r in day_before]

    first = await _list(client, entity_id, day=_day(1).isoformat(), limit=5)
    second = await _list(client, entity_id, day=_day(1).isoformat(), limit=5, before=first["next"])
    last = await _list(client, entity_id, day=_day(1).isoformat(), limit=5, before=second["next"])

    listed = [r["record_id"] for p in (first, second, last) for r in p["recordings"]]
    assert listed == ids
    assert (first["more"], second["more"], last["more"], last["next"]) == (True, True, False, None)
    assert set(_queried_days(fake_station)) == {_day(1)}

    fake_station.history_queries.clear()
    older = await _list(client, entity_id, day=_day(2).isoformat())
    assert [r["record_id"] for r in older["recordings"]] == [r["record_id"] for r in two_back]
    assert older["more"] is False
    assert set(_queried_days(fake_station)) == {_day(2)}
    await _unload(hass, entry)


async def test_a_day_outside_the_listing_bounds(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A day older than ``MAX_LIST_DAYS`` lists nothing and asks nothing; a later day
    than today lists today."""
    fake_station.rows = [*_day_rows(0, 2), *_day_rows(1, 2)]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    old = await _list(client, entity_id, day=_day(station_recordings.MAX_LIST_DAYS).isoformat())
    assert (old["recordings"], old["more"]) == ([], False)
    assert fake_station.history_queries == []

    later = await _list(client, entity_id, day=_day(-3).isoformat())
    assert len(later["recordings"]) == 2
    assert set(_queried_days(fake_station)) == {_day(0)}
    await _unload(hass, entry)


async def test_a_page_request_out_of_bounds_is_refused(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``limit`` 1..50, ``before`` a cursor a page named; nothing is queried."""
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    for fields in (
        {"limit": 0},
        {"limit": 51},
        {"before": "last-week"},
        {"before": "2000-01-02/12"},  # hygiene: ok
        {"before": "12345"},
        {"day": "yesterday"},  # hygiene: ok
    ):
        reply = await _ws(client, station_recordings.WS_LIST, entity_id=entity_id, **fields)
        assert reply["error"]["code"] == "invalid_format", fields
    assert fake_station.history_queries == []
    await _unload(hass, entry)


async def test_listing_errors_name_what_failed(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``not_found`` for an unknown entity or one that is no camera of this integration;
    ``unavailable`` for a station that does not answer and for an unloaded entry."""
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    for entity_id in ("camera.nowhere", panel_entity_id(hass)):
        reply = await _ws(client, station_recordings.WS_LIST, entity_id=entity_id)
        assert reply["error"]["code"] == "not_found", entity_id

    async def silent(self: Station, *args: Any, **kwargs: Any) -> list[HistoryRecord]:
        raise DeviceTimeoutError("no answer")

    monkeypatch.setattr(Station, "async_list_recordings", silent)
    reply = await _ws(client, station_recordings.WS_LIST, entity_id=_camera(hass))
    assert reply["error"]["code"] == "unavailable"
    reply = await _ws(client, station_recordings.WS_LIST, entity_id=_camera(hass), limit=10)
    assert reply["error"]["code"] == "unavailable"

    await _unload(hass, entry)
    reply = await _ws(client, station_recordings.WS_LIST, entity_id=_camera(hass))
    assert reply["error"]["code"] == "unavailable"


# ── fetch ────────────────────────────────────────────────────────────────────


async def test_a_fetch_stores_the_recording_as_the_sync_would_and_returns_a_playable_url(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``<start>_<camera>_event.mp4`` in the history, noted in the sync's Store; the URL
    is a signed media path a ``<video>`` loads without a header; a second fetch
    downloads nothing; the listing shows the stored file."""
    downloads = _count_downloads(monkeypatch)
    row = _row(4)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    reply = await _fetch(client, entity_id, row["record_id"])

    assert reply["success"], reply
    (video,) = _videos(hass)
    started = HistoryRecord.from_row(row).started_at
    assert started is not None
    assert video.name == (
        f"{dt_util.as_local(started).strftime('%Y-%m-%d_%H-%M-%S')}_{CAMERA_NAME}_event.mp4"
    )
    content_id = f"media-source://media_source/local/{DOMAIN}/{CAMERA_NAME}/{video.name}"
    assert reply["result"]["media_content_id"] == content_id
    url = reply["result"]["url"]
    assert url.startswith(f"/media/local/{DOMAIN}/{CAMERA_NAME}/{video.name}?authSig=")
    response = await (await hass_client_no_auth()).get(url)
    assert response.status == 200
    assert (await response.read()).startswith(REMUX_MAGIC)

    again = await _fetch(client, entity_id, row["record_id"])
    assert again["result"] == {**reply["result"], "url": again["result"]["url"]}
    assert downloads == [row["record_id"]]

    listed = await _list(client, entity_id)
    assert listed["recordings"][0]["media_content_id"] == content_id

    await _unload(hass, entry)
    stored = _store_data(hass_storage, entry)["records"][str(row["record_id"])]
    assert stored == {
        "file": video.name,
        "folder": CAMERA_NAME,
        "station": SYNTHETIC.station_sn,
        "device": SYNTHETIC.camera_sn,
        "started": started.isoformat(),
    }


async def test_a_fetch_with_a_key_it_does_not_take_is_refused(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``entity_id`` and ``record_id``: anything else is ``invalid_format``, and
    nothing is downloaded."""
    downloads = _count_downloads(monkeypatch)
    row = _row(4)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)

    reply = await _ws(
        client,
        station_recordings.WS_FETCH,
        entity_id=_camera(hass),
        record_id=row["record_id"],
        save=False,
    )

    assert reply["error"]["code"] == "invalid_format"
    assert downloads == []
    assert _videos(hass) == []
    await _unload(hass, entry)


async def test_a_fetched_recording_takes_its_detections_still_name(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Same stamp and kind as the still its detection wrote, as the sync names it."""
    row = _row(7)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    entry.runtime_data.router.handle(
        detection_event(
            DetectionType.PERSON,
            t_ms=int((dt_util.utcnow() - timedelta(minutes=9, seconds=57)).timestamp() * 1000),
            thumb_path=PUSHED_THUMB_PATH,
            record_id=row["record_id"],
        )
    )
    snapshots = entry.runtime_data.snapshots
    await wait_until(lambda: snapshots.image_for(SYNTHETIC.camera_sn) is not None)
    await wait_until(lambda: not snapshots.busy)
    await hass.async_block_till_done()
    (still,) = sorted(history.history_dir(hass).rglob("*.jpg"))
    client = await hass_ws_client(hass)

    reply = await _fetch(client, _camera(hass), row["record_id"])

    assert reply["success"], reply
    (video,) = _videos(hass)
    assert video.stem == still.stem
    await _unload(hass, entry)


async def test_the_sync_never_downloads_a_fetched_recording_again(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_storage: dict[str, Any],
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With event videos on since before the recording, a fetch first and a pass after
    cost one download and leave one file."""
    downloads = _count_downloads(monkeypatch)
    row = _row(5)
    fake_station.rows = [row]
    seed_warm_cache()
    entry = add_entry(hass, options={CONF_EVENT_VIDEOS: True})
    key = recordings.store_key(entry.entry_id)
    since = dt_util.utcnow() - timedelta(hours=1)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"records": {}, "since": since.isoformat()},
    }
    assert await setup_entry(hass, entry)
    client = await hass_ws_client(hass)

    reply = await _fetch(client, _camera(hass), row["record_id"])
    assert reply["success"], reply
    sync = entry.runtime_data.recordings
    assert sync is not None
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_FIRST_DELAY_SECONDS + 1)
    )
    await hass.async_block_till_done()
    await wait_until(lambda: (sync.stats(SYNTHETIC.station_sn) or {}).get("passes") == 1)
    await wait_until(lambda: not sync.busy)

    assert downloads == [row["record_id"]]
    assert len(_videos(hass)) == 1
    await _unload(hass, entry)


# A file beside the history folder, in Home Assistant's media folder.
_OUTSIDE: Final = "outside.mp4"


@pytest.mark.parametrize(
    ("folder", "file"),
    [
        ("..", _OUTSIDE),
        (CAMERA_NAME, f"../../{_OUTSIDE}"),
        (CAMERA_NAME, "{media}/" + _OUTSIDE),
        ("{media}", _OUTSIDE),
        ("Linked", _OUTSIDE),
    ],
    ids=["dotdot-folder", "dotdot-file", "absolute-file", "absolute-folder", "symlink"],
)
async def test_a_stored_name_that_leaves_the_history_folder_is_never_served(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_storage: dict[str, Any],
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    folder: str,
    file: str,
) -> None:
    """A Store whose names reach outside the history is not followed: the listing shows
    no file, and a fetch downloads the recording into the camera's folder."""
    row = _row(6)
    fake_station.rows = [row]
    media = history.media_dir(hass)
    outside = media / _OUTSIDE
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"not a recording")
    (history.history_dir(hass)).mkdir(parents=True, exist_ok=True)
    (history.history_dir(hass) / "Linked").symlink_to(media, target_is_directory=True)
    seed_warm_cache()
    entry = add_entry(hass)
    key = recordings.store_key(entry.entry_id)
    started = HistoryRecord.from_row(row).started_at
    assert started is not None
    record = {
        "file": file.format(media=media),
        "folder": folder.format(media=media),
        "station": SYNTHETIC.station_sn,
        "device": SYNTHETIC.camera_sn,
        "started": started.isoformat(),
    }
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"records": {str(row["record_id"]): record}},
    }
    assert await setup_entry(hass, entry)
    client = await hass_ws_client(hass)

    with caplog.at_level(logging.DEBUG, logger=recordings.__name__):
        listed = await _list(client, _camera(hass))
        reply = await _fetch(client, _camera(hass), row["record_id"])

    assert listed["recordings"][0]["media_content_id"] is None
    assert reply["success"], reply
    (video,) = _videos(hass)
    assert video.parent == history.history_dir(hass) / CAMERA_NAME
    assert reply["result"]["media_content_id"].endswith(f"/{CAMERA_NAME}/{video.name}")
    assert any("outside the history folder" in r.getMessage() for r in caplog.records)
    assert outside.read_bytes() == b"not a recording"
    await _unload(hass, entry)


async def test_two_fetches_of_one_recording_share_its_download(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second fetch while the first downloads joins it; both get the same file."""
    downloads = _count_downloads(monkeypatch)
    row = _row(6)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    first = await hass_ws_client(hass)
    second = await hass_ws_client(hass)
    entity_id = _camera(hass)

    replies = await asyncio.gather(
        _fetch(first, entity_id, row["record_id"]), _fetch(second, entity_id, row["record_id"])
    )

    assert all(reply["success"] for reply in replies), replies
    assert replies[0]["result"]["media_content_id"] == replies[1]["result"]["media_content_id"]
    assert downloads == [row["record_id"]]
    assert _manager(entry).stats.fetch_calls == 2
    await _unload(hass, entry)


async def test_fetch_errors_name_what_failed(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``not_found``, ``not_settled``, ``busy``, ``unavailable`` and ``failed``; none
    leaves a file behind."""
    running = _row(1, ago=timedelta(seconds=5))
    settled = _row(2)
    incomplete = _row(3, frame_num=40)
    other = _row(4, device_sn=OTHER_SN)
    fake_station.rows = [running, settled, incomplete, other]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    entity_id = _camera(hass)

    async def code(record_id: int) -> str:
        reply = await _fetch(client, entity_id, record_id)
        assert not reply["success"], reply
        result: str = reply["error"]["code"]
        return result

    assert await code(other["record_id"]) == "not_found"
    assert await code(settled["record_id"] + 50) == "not_found"
    assert await code(running["record_id"]) == "not_settled"
    assert await code(incomplete["record_id"]) == "failed"
    _fail_downloads(monkeypatch, LiveStreamLimitError("spent", limit=1))
    assert await code(settled["record_id"]) == "busy"
    _fail_downloads(monkeypatch, DeviceTimeoutError("no keyframe"))
    assert await code(settled["record_id"]) == "unavailable"
    assert _videos(hass) == []
    assert _manager(entry).stats.fetch_failed == 3
    await _unload(hass, entry)


async def test_a_fetch_stopped_by_an_unload_is_answered(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download the unload cancels answers ``failed`` instead of leaving the call open."""
    started = asyncio.Event()

    async def hanging(
        self: Station, record: HistoryRecord, write: ClipWriter, *, wait: bool = True
    ) -> MediaClip:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    monkeypatch.setattr(Station, "async_download_recording", hanging)
    row = _row(8)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": station_recordings.WS_FETCH,
            "entity_id": _camera(hass),
            "record_id": row["record_id"],
        }
    )
    await asyncio.wait_for(started.wait(), 5)

    await _unload(hass, entry)
    reply = await asyncio.wait_for(client.receive_json(), 5)

    assert reply["error"]["code"] == "failed"
    assert _videos(hass) == []


# ── thumbnails ───────────────────────────────────────────────────────────────


async def test_the_thumbnail_view_serves_the_still_by_its_signed_path_and_caches_it(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """``image/jpeg`` off the station's disk once; the path unsigned is 401; an
    obfuscated still is 404."""
    fake_station.images["/zx/rec1.jpg"] = JPEG
    fake_station.images["/zx/rec2.jpg"] = OBFUSCATED
    fake_station.rows = [
        _row(1, thumb_path="/zx/rec1.jpg"),
        _row(2, thumb_path="/zx/rec2.jpg"),
    ]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    listed = await _list(client, _camera(hass))
    by_id = {r["record_id"]: r["thumb_url"] for r in listed["recordings"]}
    jpeg_url = by_id[fake_station.rows[0]["record_id"]]
    http = await hass_client_no_auth()

    for _ in range(2):
        response = await http.get(jpeg_url)
        assert response.status == 200
        assert response.content_type == "image/jpeg"
        assert await response.read() == JPEG
    assert fake_station.image_requests.count("/zx/rec1.jpg") == 1

    unsigned = jpeg_url.split("?", 1)[0]
    assert (await http.get(unsigned)).status == 401
    assert (await http.get(by_id[fake_station.rows[1]["record_id"]])).status == 404
    await _unload(hass, entry)


# ── event videos: only upcoming recordings ───────────────────────────────────


async def test_event_videos_sync_only_recordings_after_the_option_came_on(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first setup with the option on marks the moment; a recording from before it
    is not synced. A setup with the option off drops the mark; on again marks anew."""
    downloads = _count_downloads(monkeypatch)
    fake_station.rows = [_row(1)]
    entry = await _set_up(hass, seed_warm_cache, {CONF_EVENT_VIDEOS: True})
    before = dt_util.utcnow()
    sync = entry.runtime_data.recordings
    assert sync is not None
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_FIRST_DELAY_SECONDS + 1)
    )
    await hass.async_block_till_done()
    await wait_until(lambda: (sync.stats(SYNTHETIC.station_sn) or {}).get("passes") == 1)
    await wait_until(lambda: not sync.busy)
    assert downloads == []
    await _unload(hass, entry)
    first = dt_util.parse_datetime(_store_data(hass_storage, entry)["since"])
    assert first is not None
    assert before - timedelta(minutes=1) < first <= before

    hass.config_entries.async_update_entry(entry, options={CONF_EVENT_VIDEOS: False})
    assert await setup_entry(hass, entry)
    await _unload(hass, entry)
    assert "since" not in _store_data(hass_storage, entry)

    hass.config_entries.async_update_entry(entry, options={CONF_EVENT_VIDEOS: True})
    assert await setup_entry(hass, entry)
    await _unload(hass, entry)
    second = dt_util.parse_datetime(_store_data(hass_storage, entry)["since"])
    assert second is not None
    assert second > first


async def test_a_pass_lists_nothing_before_the_mark(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the mark half an hour back, a recording from an hour ago stays on the
    station and one from ten minutes ago is synced."""
    downloads = _count_downloads(monkeypatch)
    old, new = _row(1, ago=timedelta(hours=1)), _row(2, ago=timedelta(minutes=10))
    fake_station.rows = [new, old]
    seed_warm_cache()
    entry = add_entry(hass, options={CONF_EVENT_VIDEOS: True})
    key = recordings.store_key(entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "records": {},
            "since": (dt_util.utcnow() - timedelta(minutes=30)).isoformat(),
        },
    }
    assert await setup_entry(hass, entry)
    sync = entry.runtime_data.recordings
    assert sync is not None

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECORDING_SYNC_FIRST_DELAY_SECONDS + 1)
    )
    await hass.async_block_till_done()
    await wait_until(lambda: (sync.stats(SYNTHETIC.station_sn) or {}).get("passes") == 1)
    await wait_until(lambda: not sync.busy)

    assert downloads == [new["record_id"]]
    await _unload(hass, entry)


# ── diagnostics ──────────────────────────────────────────────────────────────


async def test_diagnostics_count_the_requests(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    fake_station: FakeStation,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Listings and fetches as counts only."""
    # diagnostics registers its view, so it is set up before the websocket client
    # starts the HTTP server and freezes the router.
    assert await async_setup_component(hass, "diagnostics", {})
    row = _row(1)
    fake_station.rows = [row]
    entry = await _set_up(hass, seed_warm_cache)
    client = await hass_ws_client(hass)
    await _list(client, _camera(hass))
    assert (await _fetch(client, _camera(hass), row["record_id"]))["success"]

    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    counts = data["station_recordings"]
    assert (counts["list_calls"], counts["list_queries"]) == (1, 1)
    assert (counts["fetch_calls"], counts["fetch_downloads"], counts["fetch_failed"]) == (1, 1, 0)
    assert str(row["record_id"]) not in str(data)
    await _unload(hass, entry)
