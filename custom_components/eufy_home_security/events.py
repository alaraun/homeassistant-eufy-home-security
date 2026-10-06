"""Library events routed to the station coordinators and the repair issues.

The account client delivers every station's events on ``eufy.subscribe``, on Home
Assistant's event loop. This router applies the ones that change what an entity
shows or what the user must be told:

- ``GuardModeChanged`` amends the coordinator's data with both modes, the selected
  one (``SCHEDULE`` while a schedule runs) and the one in force
  (``StationCoordinator.async_apply_guard_mode``).
- ``StationStateChanged`` replaces the coordinator's data
  (``StationCoordinator.async_apply_state``), so one dump reconciles every entity of
  that station. It arrives from a dump the station pushes unasked and from the
  read-back of a confirmed setting write.
- ``StorageChanged`` replaces the station's storage record
  (``StorageCoordinator.async_apply_storage``). It arrives after every storage read,
  when a format finishes and when another client reads the record.

  None of these three goes through the coordinator's set-updated-data method, which
  cancels and reschedules its timer: the 45 s poll stays authoritative for guard
  mode, and the storage timer keeps its schedule.
- ``ConnectionChanged`` drives availability once setup has made its first reads: a
  lost session marks the coordinator failed once it has stayed down for
  ``CONNECTION_LOSS_GRACE_SECONDS``, and a restored one cancels a pending mark and
  asks for a fresh read, plus a storage read when the last one failed or never
  happened. A session closed by this integration (``CLOSED``), put to sleep after a
  command (``IDLE``) or lost while Home Assistant stops is not an outage. A station
  that connects on demand (a battery camera without a HomeBase) holds no session
  between commands, so its connection events never touch availability: that follows
  the cached state and ``SubDeviceState.online``. A session lost to a rejected key
  raises the station's key-rejected issue, and a restored one clears it.
- ``CloudProblem`` goes to reauth or the account's repair issue.
- ``CredentialsRefreshed`` leaves a persistent notice, which a reconnect does not
  clear.
- ``AccountMismatch`` raises the station's account-id-mismatch issue. A reconnect
  never clears it; unloading the entry deletes it, so a reload re-evaluates. The
  event carries no account id, and neither does the issue.
- ``DevicesChanged`` schedules one reload of the entry on a later loop turn
  (``async_reload_soon``): every device and entity is keyed by serial, so the entry
  is built again from the new list. The event is emitted inside the library's
  discover, and a reload started there would close the client under it. Nothing here
  refreshes the list: that is a cloud call, made only by the account's "Refresh
  device list" button, since eufy locks the account for 24 h after repeated sign-ins.
- ``SecurityEvent`` goes through the dispatcher, never the coordinator, to the
  entities of the device it names when one consumes it
  (``detections.device_event_consumed``) and the device is paired to the delivering
  station. A station push (alarm, alarm delay, arming) goes to that station's
  entities when one consumes it (``detections.station_event_consumed``). Every other
  one fires the ``eufy_home_security_event`` bus event, with six fixed keys and a
  device registry id, never a serial. An enriching copy and an unauthenticated alarm
  stop fire nothing. Nothing is dispatched outside the consuming window, from after
  the platforms are forwarded to the start of unload: an event then goes to the bus.
  The library has already de-duplicated every event it delivers, so the router keeps
  no seen-set. Each one received for a station this entry serves is counted by
  cipher for the diagnostics. A camera detection carrying a media path also goes to
  the snapshot manager, enriching copies included.
- ``AlarmChanged`` goes to that station's panel and alarm event on its alarm signal,
  inside the consuming window. The library has already authenticated and ordered
  it, so it needs no check here. Outside the window it is dropped: it is not a push,
  so it has no fallback bus event.
- ``PresetsChanged`` goes to the preset manager (``PresetManager.async_apply_presets``),
  which tells the preset platforms only inside the consuming window: they add the
  entities of a slot that appeared and mark a removed one unavailable, never
  deleting anything. Setup reads ``Station.presets`` afresh.
- ``ZoomChanged`` goes to the stream manager (``StreamManager.async_apply_zoom_report``)
  inside the consuming window: while a camera's live view runs, its zoom entity shows
  the zoom the camera reports.
- ``PushChanged``, only with the cloud push option on, raises the account's
  push-not-running issue while the listener is not running and withdraws it when it
  runs. A cloud failure behind a stop also arrives as its own ``CloudProblem``
  (routed as above, the ``PushChanged`` then carrying no error), so both show. The
  client's close at unload is never seen here (the router unsubscribes first, and
  unload deletes the issue); its close while Home Assistant stops is ignored.

Everything else, ``ParamChanged`` included, is ignored: the poll reconciles it.
Nothing here logs in or calls the cloud.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from eufy_home_security import (
    AccountMismatch,
    AlarmChanged,
    CloudProblem,
    ConnectionChanged,
    CredentialsRefreshed,
    DevicesChanged,
    DisconnectCause,
    Event,
    EventScope,
    FrameCipher,
    GuardModeChanged,
    KeyRejectedError,
    PresetsChanged,
    PushChanged,
    SecurityEvent,
    StationStateChanged,
    StorageChanged,
    ZoomChanged,
    redact_serial,
)

from . import detections, errors, runtime
from .const import CONNECTION_LOSS_GRACE_SECONDS, DOMAIN, EVENT_EUFY_HOME_SECURITY

if TYPE_CHECKING:
    from .coordinator import StationCoordinator
    from .presets import PresetManager
    from .recordings import RecordingManager
    from .runtime import EufyConfigEntry
    from .snapshots import SnapshotManager
    from .storage import StorageCoordinator

# The keys of a station's received-event counters: a P2P frame's cipher, or "cloud"
# for an event that came through no P2P frame.
CIPHER_COUNTER_KEYS: Final = ("gcm", "ecb", "cloud")

_LOGGER = logging.getLogger(__name__)


def _cipher_key(event: SecurityEvent) -> str:
    """The counter key of the cipher ``event`` arrived under: gcm, ecb, or cloud."""
    if event.frame_cipher is None:
        return "cloud"
    if event.frame_cipher is FrameCipher.ECB:
        return "ecb"
    return "gcm"


def _subject(event: SecurityEvent) -> str:
    """A push's device as a redacted serial, or ``station`` for a station push."""
    return redact_serial(event.device_sn) if event.device_sn is not None else "station"


class EventRouter:
    """Routes one account's library events to its station coordinators by serial."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EufyConfigEntry,
        coordinators: Mapping[str, StationCoordinator],
        storage: Mapping[str, StorageCoordinator] | None = None,
        snapshots: SnapshotManager | None = None,
        presets: PresetManager | None = None,
        recordings: RecordingManager | None = None,
        *,
        cloud_push: bool = False,
    ) -> None:
        self._hass = hass
        self._cloud_push = cloud_push
        self._entry = entry
        self._coordinators = coordinators
        self._storage: Mapping[str, StorageCoordinator] = storage or {}
        self._snapshots = snapshots
        self._presets = presets
        self._recordings = recordings
        # Off until setup has made its first reads: until then the start result and
        # the first refresh decide each station's availability.
        self._following_availability = False
        # Off until the platforms are forwarded, and off again at unload: only in
        # between are the entities listening on their signals.
        self._consuming = False
        # Per station serial, the SecurityEvents received by cipher.
        self._events_by_cipher: dict[str, dict[str, int]] = {}
        # Set once an entry reload is scheduled, so a second request before it runs
        # schedules nothing more. The reload builds a new router.
        self._reload_pending = False
        # Per station serial, the pending "unavailable" mark of a lost session.
        self._pending_loss: dict[str, CALLBACK_TYPE] = {}
        entry.async_on_unload(self._async_cancel_pending_losses)

    def loss_pending(self, station_sn: str) -> bool:
        """Whether ``station_sn``'s session is down but still inside the loss grace."""
        return station_sn in self._pending_loss

    @callback
    def _async_cancel_pending_losses(self) -> None:
        for cancel in self._pending_loss.values():
            cancel()
        self._pending_loss.clear()

    def events_received_by_cipher(self, station_sn: str) -> dict[str, int]:
        """How many ``SecurityEvent``s this router received for a station, by cipher.

        Counted in memory since setup, after the library's de-duplication (a copy it
        dropped never arrives) and before the router's own drop rule, so an enriching
        copy that fires nothing is counted too. Keys are ``gcm``, ``ecb`` and
        ``cloud``, zero when nothing arrived. Not ``SessionStats.frames_by_cipher``,
        which counts P2P frames before de-duplication.
        """
        counts = self._events_by_cipher.get(station_sn)
        return dict.fromkeys(CIPHER_COUNTER_KEYS, 0) if counts is None else dict(counts)

    @property
    def reload_pending(self) -> bool:
        """Whether an entry reload is scheduled and has not started yet."""
        return self._reload_pending

    @callback
    def async_reload_soon(self, reason: str) -> None:
        """Schedule one reload of the entry, on a later turn of the event loop.

        Never started from the caller's stack: ``DevicesChanged`` is emitted from
        inside the library's discover, and a reload's close empties the station list
        that discover is still indexing. A second request while one is pending
        schedules nothing, so a discover that changes several stations, or a
        "Refresh device list" press that follows one, reloads the entry once.
        """
        if self._reload_pending:
            _LOGGER.debug("Entry reload already pending (%s)", reason)
            return
        self._reload_pending = True
        _LOGGER.debug("Entry reload scheduled for the next loop turn: %s", reason)
        self._hass.loop.call_soon(self._async_reload_entry)

    @callback
    def _async_reload_entry(self) -> None:
        """Start the scheduled reload, unless the entry was removed meanwhile."""
        if self._hass.config_entries.async_get_entry(self._entry.entry_id) is None:
            _LOGGER.debug("Entry reload dropped: the entry was removed")
            return
        self._hass.config_entries.async_schedule_reload(self._entry.entry_id)

    @callback
    def async_follow_availability(self) -> None:
        """Let connection events drive availability from now on; setup calls it last.

        Before this, a failed first start (which the library also reports as a
        ``ConnectionChanged``) would reach a fresh coordinator's error path, which
        logs an ERROR on every setup attempt beside setup's one WARNING; and
        a successful start would ask for a read the first refresh then repeats. The
        repair-issue part of a connection event is applied either way.
        """
        self._following_availability = True

    @callback
    def async_start_consuming(self) -> None:
        """Dispatch consumed events to the entities from now on; setup calls it last.

        Before this, the platforms are not forwarded and no entity listens: a
        dispatched event would reach nobody and be lost, so it goes to the fallback
        bus event instead.
        """
        self._consuming = True

    @callback
    def async_stop_consuming(self) -> None:
        """Send every event to the fallback bus event again; unload calls it first.

        The entities stop listening as their platforms unload, so from here on an
        event dispatched to them would be lost.
        """
        self._consuming = False

    @callback
    def handle(self, event: Event) -> None:
        """Apply one library event; subscribed before the sessions start."""
        if isinstance(event, GuardModeChanged):
            coordinator = self._coordinators.get(event.station_sn)
            # A station with no coordinator is ignored: its first read takes the mode.
            if coordinator is not None:
                coordinator.async_apply_guard_mode(event.mode, event.active_mode)
        elif isinstance(event, StationStateChanged):
            coordinator = self._coordinators.get(event.station_sn)
            # As above, a station with no coordinator is ignored. The state the
            # library built is shown by replacing the coordinator's data and
            # notifying its entities, never through the set-updated-data method,
            # which would reschedule the poll a steady stream of dumps then keeps
            # postponing.
            if coordinator is not None:
                coordinator.async_apply_state(event.state)
        elif isinstance(event, StorageChanged):
            storage = self._storage.get(event.station_sn)
            # Never the set-updated-data method: it would reschedule the storage
            # timer on every pushed record.
            if storage is not None:
                storage.async_apply_storage(event.storage)
        elif isinstance(event, DevicesChanged):
            # Entities are keyed by serial, so the entry is built again from the new
            # list rather than patched in place. The event names the serials
            # that changed; only their counts are logged, and they become device
            # identifiers only, as they already do at setup.
            _LOGGER.debug(
                "Paired devices of %s changed: %d added, %d removed, %d moved",
                redact_serial(event.station_sn),
                len(event.added),
                len(event.removed),
                len(event.moved),
            )
            self.async_reload_soon("paired devices changed")
        elif isinstance(event, ConnectionChanged):
            self._handle_connection(event)
        elif isinstance(event, CloudProblem):
            # Account-level: reauth or a repair issue, never a login.
            errors.route_cloud_problem(self._hass, self._entry, event)
        elif isinstance(event, CredentialsRefreshed):
            errors.raise_credentials_refreshed_notice(self._hass, self._entry, event)
        elif isinstance(event, SecurityEvent):
            self._handle_security_event(event)
        elif isinstance(event, AlarmChanged):
            if self._consuming and event.station_sn in self._coordinators:
                async_dispatcher_send(
                    self._hass,
                    detections.alarm_signal(self._entry.entry_id, event.station_sn),
                    event,
                )
        elif isinstance(event, AccountMismatch):
            # After the push's own SecurityEvent, which was routed as a detection.
            errors.raise_account_mismatch_issue(self._hass, self._entry, event.station_sn)
        elif isinstance(event, PresetsChanged):
            # The station already holds the new slots; the platforms re-read them and
            # add entities, never delete. Not dispatched outside
            # the consuming window: no entity listens then.
            if self._presets is not None and event.station_sn in self._coordinators:
                self._presets.async_apply_presets(event, dispatch=self._consuming)
        elif isinstance(event, PushChanged):
            self._handle_push(event)
        elif isinstance(event, ZoomChanged):
            # Only while the entities listen; setup starts every zoom from its own state.
            streams = runtime.streaming(self._entry)
            if self._consuming and streams is not None:
                streams.async_apply_zoom_report(event.device_sn, event.zoom)

    @callback
    def _handle_push(self, event: PushChanged) -> None:
        """Raise or withdraw the push-not-running issue while the option is on."""
        if not self._cloud_push:
            return
        if not event.running and self._hass.is_stopping:
            # The client's own close at shutdown, not an outage.
            _LOGGER.debug("Cloud push stopped while Home Assistant stops")
            return
        _LOGGER.debug(
            "Cloud push %s (%s)",
            "running" if event.running else "not running",
            type(event.error).__name__ if event.error is not None else "no error",
        )
        errors.sync_push_issue(self._hass, self._entry, running=event.running)

    @callback
    def _handle_security_event(self, event: SecurityEvent) -> None:
        """Dispatch ``event`` to the entities that consume it, else fire the fallback.

        Each routing decision is logged at DEBUG with redacted serials, enum names,
        counts and field names only. The event object, its paths, ids, names and dedupe
        key are never formatted, because its repr prints full serials.
        """
        key = _cipher_key(event)
        _LOGGER.debug(
            "Push received for %s of %s: msg %s:%s, cipher %s, detection %s, enriches %s,"
            " media %s, record %s",
            _subject(event),
            redact_serial(event.station_sn),
            event.msg_type,
            event.event_type,
            key,
            event.detection.name if event.detection is not None else "none",
            event.enriches,
            ",".join(sorted(event.media_paths)) or "none",
            event.record_id is not None,
        )
        if event.station_sn is not None and event.station_sn in self._coordinators:
            counts = self._events_by_cipher.setdefault(
                event.station_sn, dict.fromkeys(CIPHER_COUNTER_KEYS, 0)
            )
            counts[key] += 1
        coordinator = (
            self._coordinators.get(event.station_sn) if event.station_sn is not None else None
        )
        if (
            self._consuming
            and self._snapshots is not None
            and coordinator is not None
            and event.scope is EventScope.DEVICE
            and event.device_sn is not None
        ):
            # Before the drop rule: an enriching copy fires nothing, but its paths are
            # the detection's thumbnail. Paired devices only.
            paired = detections.paired_device_kinds(coordinator.station)
            if event.device_sn not in paired:
                _LOGGER.debug(
                    "No snapshot for %s: not paired to the delivering station",
                    redact_serial(event.device_sn),
                )
            elif detections.standalone_image_wanted(
                coordinator.station, paired[event.device_sn], event
            ):
                _LOGGER.debug(
                    "Standalone event image requested for %s", redact_serial(event.device_sn)
                )
                self._snapshots.async_request_standalone_event(coordinator.station, event)
            elif not detections.snapshot_wanted(paired[event.device_sn], event):
                _LOGGER.debug(
                    "No snapshot for %s: not a catalogued detection with a still to find",
                    redact_serial(event.device_sn),
                )
            else:
                _LOGGER.debug("Snapshot requested for %s", redact_serial(event.device_sn))
                self._snapshots.async_request_event(coordinator.station, event)
                if self._recordings is not None:
                    # The detection's recording, once it has finished (recordings.py).
                    self._recordings.async_detection(coordinator.station.serial, event)
        if detections.dropped_without_trace(event):
            _LOGGER.debug(
                "Push for %s dropped: %s",
                _subject(event),
                "enriching copy of a delivered detection"
                if event.enriches
                else "unauthenticated alarm stop",
            )
            return
        if (
            self._consuming
            and coordinator is not None
            and event.scope is EventScope.DEVICE
            and event.device_sn is not None
        ):
            # Only a device paired to the station that delivered the event: the serial
            # comes from the payload, which an ECB frame can forge.
            kinds = detections.paired_device_kinds(coordinator.station)
            if event.device_sn in kinds and detections.device_event_consumed(
                kinds[event.device_sn], event
            ):
                _LOGGER.debug(
                    "Push for %s dispatched to its device entities", redact_serial(event.device_sn)
                )
                async_dispatcher_send(
                    self._hass,
                    detections.device_signal(self._entry.entry_id, event.device_sn),
                    event,
                )
                return
        if (
            self._consuming
            and coordinator is not None
            and event.scope is EventScope.STATION
            and event.station_sn is not None
            and detections.station_event_consumed(event)
        ):
            # The station entities (alarm, arming) of the station that delivered it.
            _LOGGER.debug(
                "Push for %s dispatched to the station entities", redact_serial(event.station_sn)
            )
            async_dispatcher_send(
                self._hass,
                detections.station_signal(self._entry.entry_id, event.station_sn),
                event,
            )
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Push for %s fired as the fallback bus event: %s",
                _subject(event),
                self._fallback_reason(event, coordinator),
            )
        self._fire_fallback(event, coordinator)

    def _fallback_reason(self, event: SecurityEvent, coordinator: StationCoordinator | None) -> str:
        """Why a push went to the fallback bus event, for its DEBUG line only."""
        if not self._consuming:
            return "outside the consuming window"
        if coordinator is None:
            return "station not served by this entry"
        if (
            event.scope is EventScope.DEVICE
            and event.device_sn is not None
            and event.device_sn not in detections.paired_device_kinds(coordinator.station)
        ):
            return "device not paired to the delivering station"
        return "no entity consumes it"

    @callback
    def _fire_fallback(self, event: SecurityEvent, coordinator: StationCoordinator | None) -> None:
        """Fire the bus event for a push no entity consumes: six keys, no serial."""
        self._hass.bus.async_fire(
            EVENT_EUFY_HOME_SECURITY,
            {
                "device_id": self._registry_device_id(event, coordinator),
                "msg_type": event.msg_type,
                "event_type": event.event_type,
                "triggered_at": detections.triggered_at(event),
                "authenticated": event.authenticated,
                "source": event.source.value,
            },
        )

    @callback
    def _registry_device_id(
        self, event: SecurityEvent, coordinator: StationCoordinator | None
    ) -> str | None:
        """The paired device's registry id, else the delivering station's, else None.

        A serial the station does not pair names the station, never a device of
        another station. Looked up within this entry: identifiers are unique only
        within one config entry.
        """
        if coordinator is None:
            return None
        registry = dr.async_get(self._hass)
        station = coordinator.station
        serials = [station.serial]
        if event.device_sn is not None and event.device_sn in detections.paired_device_kinds(
            station
        ):
            serials.insert(0, event.device_sn)
        for serial in serials:
            device = registry.async_get_device_by_identifier((DOMAIN, serial), self._entry.entry_id)
            if device is not None:
                return device.id
        return None

    @callback
    def _handle_connection(self, event: ConnectionChanged) -> None:
        if event.connected:
            # The key was accepted; the credentials notice stays.
            errors.clear_station_issues(self._hass, self._entry, event.station_sn)
        elif isinstance(event.error, KeyRejectedError):
            errors.raise_key_rejected_issue(self._hass, self._entry, event.station_sn)
        coordinator = self._coordinators.get(event.station_sn)
        if coordinator is None or not self._following_availability:
            # Still inside setup: its start result and first refresh decide.
            return
        if coordinator.station.connects_on_demand:
            # An on-demand station connects for each command and sleeps after it: its
            # availability is the cached state's ``online`` flag, never the session
            # (library guide). A loss mid-command surfaces through that command's own
            # error, so nothing is marked, read or refreshed here.
            _LOGGER.debug(
                "Connection change of on-demand %s ignored for availability"
                " (connected %s, cause %s)",
                redact_serial(event.station_sn),
                event.connected,
                event.cause.name if event.cause is not None else "none",
            )
            return
        if event.connected:
            if (cancel := self._pending_loss.pop(event.station_sn, None)) is not None:
                cancel()
                _LOGGER.debug(
                    "Session of %s back within %d s; not marked unavailable",
                    redact_serial(event.station_sn),
                    CONNECTION_LOSS_GRACE_SECONDS,
                )
            # A read, not a trust in what was shown before the outage: the mode may
            # have changed while the session was down.
            self._entry.async_create_background_task(
                self._hass,
                coordinator.async_request_refresh(),
                name=f"{DOMAIN} refresh after reconnect",
            )
            storage = self._storage.get(event.station_sn)
            if storage is not None and (not storage.last_update_success or storage.data is None):
                # Only a record that is missing or failed: a good one is at most
                # 30 minutes old, and pushes keep it current in between.
                self._entry.async_create_background_task(
                    self._hass,
                    storage.async_request_refresh(),
                    name=f"{DOMAIN} storage read after reconnect",
                )
        elif event.cause not in (DisconnectCause.CLOSED, DisconnectCause.IDLE):
            # CLOSED is this integration's own close at unload or shutdown. IDLE is an
            # idle session put to sleep, which the library says is not an outage.
            if self._hass.is_stopping:
                # Home Assistant is stopping: the station may drop the link before this
                # integration's close reaches it. Not an outage, and nothing is marked.
                _LOGGER.debug(
                    "Session of %s lost while Home Assistant stops (%s); not marked",
                    redact_serial(event.station_sn),
                    event.cause.name if event.cause is not None else "none",
                )
                return
            if event.station_sn in self._pending_loss:
                return
            _LOGGER.debug(
                "Session of %s lost (%s); unavailable unless it is back within %d s",
                redact_serial(event.station_sn),
                event.cause.name if event.cause is not None else "none",
                CONNECTION_LOSS_GRACE_SECONDS,
            )

            @callback
            def _mark(_now: object) -> None:
                self._pending_loss.pop(event.station_sn, None)
                coordinator.async_set_update_error(errors.connection_lost(event))

            self._pending_loss[event.station_sn] = async_call_later(
                self._hass, CONNECTION_LOSS_GRACE_SECONDS, _mark
            )
