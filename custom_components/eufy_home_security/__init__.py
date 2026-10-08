"""The Anker eufy Home Security integration: one config entry per eufy account."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from eufy_home_security import (
    AuthenticationError,
    EufySecurityError,
    RateLimitedError,
    SessionReplacedError,
    Station,
    async_forget_account,
    entity_unique_id,
    redact_serial,
)

from . import (
    card,
    detections,
    errors,
    history,
    recordings,
    runtime,
    session_probe,
    small_images,
    station_recordings,
    still_cache,
    streaming,
)
from .const import (
    ACCOUNT_DEVICE_NAME,
    CONF_CAMERA_IMAGE,
    CONF_CLOUD_PUSH,
    CONF_EVENT_HISTORY_DAYS,
    CONF_EVENT_VIDEOS,
    CONF_LIVE_SNAPSHOT,
    CONF_SCAN_REGIONS,
    DEFAULT_EVENT_HISTORY_DAYS,
    DOMAIN,
)
from .coordinator import StationCoordinator
from .events import EventRouter
from .presets import PresetManager
from .runtime import EufyConfigEntry
from .settings import setting_specs
from .snapshots import SnapshotManager, camera_image_mode
from .storage import StorageCoordinator
from .streaming import StreamManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.UPDATE,
]


def _log_station_not_up(serial: str, error: BaseException) -> None:
    """One WARNING for a station that did not come up, by redacted serial only."""
    _LOGGER.warning(
        "Station %s did not come up (%s); its entities stay unavailable while the "
        "eufy library keeps reconnecting to it",
        redact_serial(serial),
        type(error).__name__,
    )


def _async_register_devices(
    hass: HomeAssistant, entry: EufyConfigEntry, stations: Iterable[Station]
) -> None:
    """Register each station, then each of its cameras and sensors under it.

    Takes the account client's stations rather than the client: only runtime.py
    may name ``EufySecurity``.

    Every device is identified by its own serial, never by (station, channel): a
    camera moved to another HomeBase keeps its serial, so its history survives,
    while its channel and station change. Nothing here reads the station's
    parameter dump, so a slot the dump lists never becomes a device.

    The station is registered first because its ``DeviceEntry`` is the parent:
    sub-devices link with ``via_device_id``, since ``DeviceInfo["via_device"]`` is
    deprecated since 2026.8 (helpers/device_registry.py, ``async_get_or_create``).
    Every device's ``model`` is the library's catalogued name
    (``CloudDevice.model_name``, None for a model it does not know) and ``model_id`` the
    serial prefix (``CloudDevice.model_id``, set for every model).
    """
    device_registry = dr.async_get(hass)
    for station in stations:
        parent = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, station.serial)},
            manufacturer="eufy",
            name=station.name,
            model=station.device.model_name,
            model_id=station.device.model_id,
            serial_number=station.serial,
            sw_version=station.device.main_sw_version,
        )
        for sub in station.sub_devices:
            if not sub.device_sn:
                # No serial, no identity: an id built from anything else would not
                # survive the device moving.
                continue
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, sub.device_sn)},
                manufacturer="eufy",
                name=sub.name,
                model=sub.model_name,
                model_id=sub.model_id,
                serial_number=sub.device_sn,
                sw_version=sub.main_sw_version,
                via_device_id=parent.id,
            )


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the bundled dashboard card, keep its resource current, and register the
    station-recordings websocket commands and thumbnail view."""
    del config  # config entries only
    await card.async_setup_card(hass)
    station_recordings.async_setup(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry) -> bool:
    """Set up an account: log in from the cache, build its stations, start their sessions.

    The password is ``None``: the library logs in with the one the config flow's
    login cached, and a warm cache performs no cloud round trip at all.
    """
    # Each attempt re-evaluates the stations' stamps and keys: an attempt that raised
    # a mismatch or cipher issue and then failed was never unloaded.
    errors.delete_reload_scoped_issues(hass, entry)
    # A card file changed by an update takes effect on this entry's (re)load.
    await card.async_sync_card_resource(hass)
    eufy = runtime.build_client(
        hass,
        entry.data[CONF_EMAIL],
        None,
        claims=runtime.station_claims(hass),
        max_sessions=runtime.session_budget(entry.options),
        scan_regions=entry.options.get(CONF_SCAN_REGIONS, False) is True,
        country=runtime.login_country(hass, entry.options),
    )
    # The cloud push start, once setup has started it (below).
    push_start: list[asyncio.Task[None]] = []

    async def _async_close() -> None:
        """End the push start first, so no listener starts after the close; then close."""
        for task in push_start:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await eufy.async_close()

    # Unload callbacks also run when setup fails part way.
    entry.async_on_unload(_async_close)

    stop_fired = False

    async def _async_close_at_stop(_: Event) -> None:
        """Close the client when Home Assistant stops.

        HA does not unload config entries at shutdown, so the unload callback
        above never runs then; without this the station sessions die with no
        protocol close and the cache is not saved.
        """
        nonlocal stop_fired
        stop_fired = True
        await _async_close()

    remove_stop_listener = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, _async_close_at_stop
    )

    @callback
    def _remove_stop_listener() -> None:
        """Remove the stop listener unless it fired: a fired one-shot is already gone."""
        if not stop_fired:
            remove_stop_listener()

    entry.async_on_unload(_remove_stop_listener)

    # Read from the cache, never the cloud: whether the login below really signs in.
    status_before_login = await eufy.async_cloud_status()
    try:
        await eufy.async_login()
    except AuthenticationError as err:
        # No password cached (the library raises without contacting the cloud), or
        # eufy rejected the cached one (the library drops it). Either way only the
        # user can supply a new one: reauth, never a retry.
        raise errors.auth_failed(err) from err
    except (SessionReplacedError, RateLimitedError) as err:
        # Kicked out or throttled (a plain RateLimitedError too): show
        # it as a repair issue and carry on from the cache, so local control keeps
        # working. Nothing here logs in again.
        errors.raise_cloud_issue(hass, entry, err)
    except EufySecurityError as err:
        # A cloud outage: the cached account still serves the stations.
        _LOGGER.warning(
            "The eufy cloud is unavailable at setup (%s); setup continues from the cached account",
            type(err).__name__,
        )
    else:
        errors.clear_cloud_issues(
            hass, entry, before=status_before_login, after=await eufy.async_cloud_status()
        )
    # The library's session-replaced latch is persisted in the account store, while a
    # repair issue is not (issue_registry: is_persistent defaults to False). So
    # whichever path the login block took, the SessionReplacedError branch above or
    # a login that neither raised nor cleared the latch, a latched store shows its
    # fixable issue after every setup. A successful login cannot
    # leave the latch set (the library raises on it or, with force, clears it), so
    # this never fights clear_cloud_issues in the else branch. Nothing here logs
    # in: the issue's fix and Reconfigure are the user's two deliberate paths.
    if eufy.session_replaced:
        errors.raise_session_replaced_issue(hass, entry)
    try:
        # Every login scope once after the login country changed (options) or the
        # pending-invitations fix; otherwise the cached list or the usual refresh.
        await eufy.async_discover(rescan_regions=runtime.take_rescan_at_setup(hass, entry.entry_id))
    except EufySecurityError as err:
        # No cached device list either (a cold cache): only now is the entry not ready.
        raise errors.cache_unavailable(err) from err
    errors.sync_no_devices_issue(hass, entry, (await eufy.async_cloud_status()).regions)
    if not eufy.stations:
        # A shared home shows only once its invitation is accepted in the eufy app.
        errors.sync_pending_invites_issue(hass, entry, await runtime.async_pending_invites(eufy))

    coordinators: dict[str, StationCoordinator] = {
        serial: StationCoordinator(hass, entry, station)
        for serial, station in eufy.stations.items()
    }
    # Each station's storage record, on a schedule of its own (see storage.py). Not for
    # a station that connects on demand: a storage read is a P2P command, every
    # command wakes a battery camera, and a 30-minute timer would wake it 48 times a
    # day. Its storage entities are deferred; every consumer of this dict
    # tolerates a station missing from it.
    storage: dict[str, StorageCoordinator] = {
        serial: StorageCoordinator(hass, entry, station)
        for serial, station in eufy.stations.items()
        if not station.connects_on_demand
    }
    for serial, station in eufy.stations.items():
        if station.connects_on_demand:
            _LOGGER.debug(
                "No storage schedule for on-demand %s: a read would wake it",
                redact_serial(serial),
            )
    # Live video. The view is registered once per Home Assistant
    # instance, not once per entry: hass.http.register_view has no unregister, and a
    # second account must not add a duplicate route.
    streaming.async_register_view(hass)
    stream_manager = StreamManager(hass, entry)
    entry.async_on_unload(stream_manager.async_stop)
    # One broadcast per streamable camera. This opens nothing: constructing a broadcast
    # costs no session, no wake and no task, and the camera opens only when something
    # connects to its URL. Unload callbacks run last-in first-out and
    # eufy.async_close was registered first, so every broadcast is closed before the
    # sessions die.
    for coordinator in coordinators.values():
        for device_sn, kind in detections.paired_device_kinds(coordinator.station).items():
            if detections.has_detection_entities(kind) and detections.has_live_stream(device_sn):
                stream_manager.async_add_camera(coordinator.station, device_sn)
    # Each camera's still, one media worker per station. Stopped at unload,
    # before the client closes (unload callbacks run last-in first-out). Both take the
    # stream manager's yield hook as a callable, so a capture can end the live view on
    # the station's media slot without either module importing streaming.py.
    # The stills shown before the last stop, read before any entity or detection can
    # read or replace one; stills of unpaired devices are deleted.
    cache = still_cache.StillCache(hass, entry)
    small_images.async_register_view(hass)
    cached = await cache.async_load(
        {
            device_sn
            for coordinator in coordinators.values()
            for device_sn in detections.paired_device_kinds(coordinator.station)
        }
    )
    # Every shown still also as a dated file in the media folder, when kept at all; a
    # repair issue names the folder if the container would lose it.
    event_history = history.EventHistory(
        hass,
        entry,
        days=int(entry.options.get(CONF_EVENT_HISTORY_DAYS, DEFAULT_EVENT_HISTORY_DAYS)),
        # The devices camera.py adds a camera entity for.
        cameras=(
            device_sn
            for coordinator in coordinators.values()
            for device_sn, kind in detections.paired_device_kinds(coordinator.station).items()
            if detections.has_detection_entities(kind)
        ),
    )
    persistent = not event_history.enabled or await hass.async_add_executor_job(
        history.media_is_persistent, event_history.root
    )
    errors.sync_media_not_persistent_issue(
        hass, entry, None if persistent else str(history.media_dir(hass))
    )
    event_history.async_start()
    snapshots = SnapshotManager(
        hass,
        entry,
        live_snapshot=bool(entry.options.get(CONF_LIVE_SNAPSHOT, False)),
        camera_image=camera_image_mode(entry.options.get(CONF_CAMERA_IMAGE)),
        yield_media=stream_manager.async_yield_media,
        cache=cache,
        history=event_history,
    )
    snapshots.async_restore(cached)
    entry.async_on_unload(snapshots.async_stop)
    # Each pan/tilt camera's preset images. Nothing is read here:
    # the platforms build from the cached slots, and no preset read or capture ever
    # runs at setup.
    presets = PresetManager(
        hass,
        entry,
        yield_media=stream_manager.async_yield_media,
        cache=cache,
        history=event_history,
    )
    presets.async_restore(cached)
    entry.async_on_unload(presets.async_stop)
    # The record of recordings stored in the history, shared by the sync and the
    # fetch on request. With the sync off, its mark goes, so switching it on again
    # syncs only recordings from then on.
    stored_recordings = recordings.StoredRecordings(hass, entry, event_history)
    await stored_recordings.async_load()
    sync_videos = event_history.enabled and entry.options.get(CONF_EVENT_VIDEOS, False) is True
    if not sync_videos:
        stored_recordings.clear_since()
    # Each HomeBase recording as a video in the history, with the option on; never a
    # standalone camera, which keeps no recordings on a station.
    recording_manager = (
        recordings.RecordingManager(hass, entry, stored_recordings, eufy.stations.values())
        if sync_videos
        else None
    )
    cloud_push = entry.options.get(CONF_CLOUD_PUSH, False) is True
    router = EventRouter(
        hass,
        entry,
        coordinators,
        storage,
        snapshots,
        presets,
        recording_manager,
        cloud_push=cloud_push,
    )
    # The push-not-running issue goes with the entry: registered before the
    # subscription below, so it runs after the router has unsubscribed and before the
    # client's close. With the option off, one left by an earlier setup goes now.
    entry.async_on_unload(lambda: errors.sync_push_issue(hass, entry, running=True))
    if not cloud_push:
        errors.sync_push_issue(hass, entry, running=True)
    # Before the sessions start, so a ConnectionChanged or GuardModeChanged emitted
    # during the first start is not missed. Unload callbacks run last-in first-out,
    # so this unsubscribes before the client above is closed.
    entry.async_on_unload(eufy.subscribe(router.handle))

    # "First-ever setup": no device of this entry was registered
    # before this attempt registers them.
    first_ever = not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    _async_register_devices(hass, entry, eufy.stations.values())
    # The account's own service device, which carries the "Refresh device list"
    # button. Identified by the entry id: the entry's unique id is the e-mail
    # address, which must never become an identifier. This is the device row's one
    # writer; the button's DeviceInfo carries the identifier only.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=dr.DeviceEntryType.SERVICE,
        manufacturer="eufy",
        name=ACCOUNT_DEVICE_NAME,
    )

    start_errors = await eufy.async_start(p2p=True, push=False)

    came_up = 0
    for serial, coordinator in coordinators.items():
        start_error = start_errors.get(serial)
        if start_error is not None:
            # Its supervisor keeps reconnecting in the background; refreshing it now
            # would fail the whole entry. ConnectionChanged(True) reads it.
            # Unavailable without a second log line: the WARNING is the one.
            _log_station_not_up(serial, start_error)
            coordinator.async_mark_not_up(start_error)
            continue
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryNotReady as err:
            _log_station_not_up(serial, err.__cause__ or err)
            continue
        came_up += 1

    # One fixable issue per station whose key keeps being rejected, from
    # either source: the start result, or Station.last_error after a failed first
    # refresh. Raised before the first-setup decision, so a not-ready first setup shows it.
    errors.raise_key_rejected_issues(hass, entry, eufy.stations.values(), start_errors)

    if coordinators and not came_up and first_ever:
        # Only a brand-new entry waits for a station; a known one loads unavailable.
        raise errors.no_station_started(len(coordinators))

    # From here on connection events drive availability. Every connection
    # event until now had its availability part ignored, so each station is caught up
    # from where its session stands now. Nothing here reaches the cloud: a
    # read goes to a session that is up, and a lost one is only marked.
    router.async_follow_availability()
    for coordinator in coordinators.values():
        if coordinator.station.connects_on_demand:
            # An on-demand station holds no session between commands, so it is not
            # connected after a good start. That is neither a drop to mark nor a late
            # start to read: its availability is its cached state (library guide).
            _LOGGER.debug(
                "On-demand %s left out of the post-start availability catch-up",
                redact_serial(coordinator.station.serial),
            )
            continue
        connected = coordinator.station.connected
        if connected and not coordinator.last_update_success:
            # Did not start, or its first read failed, and it is up now: read it
            # once. A station that came up was just read, so it is not read again.
            entry.async_create_background_task(
                hass,
                coordinator.async_request_refresh(),
                name=f"{DOMAIN} refresh after a late start",
            )
        elif not connected and coordinator.last_update_success:
            # Came up, then dropped during setup: unavailable now, not at the next
            # poll. One ERROR line by redacted name, as a drop after setup logs.
            coordinator.async_set_update_error(errors.connection_lost_at_setup())

    entry.runtime_data = runtime.EufyRuntimeData(
        eufy=eufy,
        coordinators=coordinators,
        router=router,
        storage=storage,
        snapshots=snapshots,
        presets=presets,
        streaming=stream_manager,
        history=event_history,
        recordings=recording_manager,
        station_recordings=station_recordings.StationRecordings(hass, stored_recordings),
    )
    _async_forget_moved_setting_entities(hass, entry, coordinators.values())
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for serial, storage_coordinator in storage.items():
        if coordinators[serial].station.connected:
            # The first storage read, as a task so a slow or failed one never holds
            # up setup; the storage platforms add their entities when it lands. A
            # tracked task rather than a background one: Home Assistant waits for it
            # like the rest of the entry's start, and it is one short read. A station
            # that is down now is read when it reconnects.
            entry.async_create_task(
                hass,
                storage_coordinator.async_refresh(),
                name=f"{DOMAIN} first storage read",
            )
    if recording_manager is not None:
        # The stored record ids load before any detection can schedule a pass; the
        # first pass waits a minute, so setup itself queries no station history.
        await recording_manager.async_start()
    # Only now do the entities listen on their signals: an event before this went to
    # the fallback bus event rather than to nobody.
    router.async_start_consuming()
    if cloud_push:
        # Only now, so a detection it delivers reaches listening entities. In the
        # background: a start can take its whole deadline, and a failed one is
        # retried until it listens. Unload cancels it.
        push_start.append(
            entry.async_create_background_task(
                hass, runtime.async_run_push(eufy), name=f"{DOMAIN} cloud push start"
            )
        )
    # Scheduled last: entry.runtime_data exists before the first
    # tick can read it, and a setup that failed part way arms no probe. The 60 s first
    # delay keeps setup itself cloud-free.
    session_probe.async_start_session_probe(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EufyConfigEntry) -> bool:
    """Unload the platforms; the on-unload close shuts the account's client.

    A successful unload deletes the entry's account-id-mismatch and cipher-unavailable
    issues, so a reload re-evaluates each station's stamps and asks for its key again.
    A reconnect never clears a mismatch issue: the library reports a mismatch at
    most once per connection.
    """
    # First, before the entities stop listening: from here on events go to the
    # fallback bus event.
    router = entry.runtime_data.router
    router.async_stop_consuming()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        errors.delete_reload_scoped_issues(hass, entry)
    else:
        # The entry stays loaded with its entities listening: without this every
        # later push would go to the fallback bus event until a restart.
        router.async_start_consuming()
    return unloaded


# Every platform a setting entity can be built on (settings.setting_platform).
_SETTING_DOMAINS: frozenset[str] = frozenset(
    {
        Platform.SWITCH,
        Platform.SELECT,
        Platform.NUMBER,
        Platform.TEXT,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
    }
)


def _async_forget_moved_setting_entities(
    hass: HomeAssistant, entry: EufyConfigEntry, coordinators: Iterable[StationCoordinator]
) -> None:
    """Forget a setting's registry entry on a platform other than the one it is built on.

    A library release can make a setting writable or read-only, which builds the same
    unique id on another platform; Home Assistant would keep the old one as an
    unavailable orphan. Only unique ids this setup builds, only the setting platforms,
    and only this config entry's rows are touched. Logs nothing.
    """
    registry = er.async_get(hass)
    for coordinator in coordinators:
        station = coordinator.station
        for spec in setting_specs(station):
            unique_id = entity_unique_id(spec.device_sn or station.serial, spec.key)
            for domain in _SETTING_DOMAINS - {spec.platform}:
                entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
                if entity_id is None:
                    continue
                registered = registry.async_get(entity_id)
                if registered is not None and registered.config_entry_id == entry.entry_id:
                    registry.async_remove(entity_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Forget the account's secrets, keeping its sign-in hold-off and install identity.

    Removing and re-adding an account must not reset eufy's hold-off: roughly four
    failed sign-ins lock the account for 24 hours. The library keeps ``throttle``
    (and ``openudid``) through ``async_forget_account``, which never contacts the
    cloud. The store itself is never deleted (library guide, "Keep it").

    Any account-id-mismatch or cipher-unavailable issue goes too: an entry whose
    setup failed was never unloaded, so nothing else deletes it. The entry's cached
    stills are deleted, and with the last entry the dashboard card's resource. The
    record of the recordings copied into the history goes too; the files stay.
    """
    errors.delete_reload_scoped_issues(hass, entry)
    await still_cache.async_remove_entry_cache(hass, entry.entry_id)
    await recordings.async_remove_store(hass, entry.entry_id)
    if not any(
        other.entry_id != entry.entry_id for other in hass.config_entries.async_entries(DOMAIN)
    ):
        await card.async_remove_card_resource(hass)
    errors.sync_media_not_persistent_issue(hass, entry, None)
    await async_forget_account(runtime.cache_store(hass, entry.data[CONF_EMAIL]))
