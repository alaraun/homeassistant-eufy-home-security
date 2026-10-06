"""The station's guard mode as an alarm panel."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import ExtraStoredData, RestoredExtraData, RestoreEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    AlarmChanged,
    AlarmPhase,
    CommandError,
    EufySecurityError,
    GuardMode,
    PushMessageType,
    SecurityEvent,
    StationState,
    UnsupportedError,
)

from . import detections, errors
from .const import (
    ATTR_SELECTED_MODE,
    CONF_ALARM_TIMEOUT,
    DEFAULT_ALARM_TIMEOUT_MINUTES,
    DOMAIN,
    GUARD_MODE_KEY,
)
from .coordinator import StationCoordinator
from .detections import ha_state
from .entity import EufyStationEntity

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# Reads come from the coordinator, so HA need not serialise entity updates.
PARALLEL_UPDATES = 0

# One token per Home Assistant run, stamped on the restore data: a lifecycle is taken
# back only by the run that saved it. Restore data also survives a restart, but
# a restart tears the session down, so a stop or disarm pushed meanwhile is never seen.
RESTORE_RUN_KEY: HassKey[str] = HassKey(f"{DOMAIN}_restore_run")

# An ``AlarmChanged`` carries no event time, so the alarm it starts is stamped with the
# host clock this many seconds back, the library's ``GUARD_REPORT_SLACK_SECONDS``: a
# disarm push for this alarm (station clock, whole seconds) still orders after it,
# while one from a minute earlier cannot end it.
ALARM_CHANGED_SKEW_SECONDS = 30


def restore_run_token(hass: HomeAssistant) -> str:
    """This Home Assistant run's restore token, minted on first use."""
    return hass.data.setdefault(RESTORE_RUN_KEY, uuid.uuid4().hex)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one guard-mode panel per station."""
    del hass  # the coordinators carry everything this platform needs
    # Read once: an options change reloads the entry.
    alarm_timeout = int(entry.options.get(CONF_ALARM_TIMEOUT, DEFAULT_ALARM_TIMEOUT_MINUTES))
    async_add_entities(
        EufyGuardModePanel(coordinator, alarm_timeout)
        for coordinator in entry.runtime_data.coordinators.values()
    )


class EufyGuardModePanel(EufyStationEntity, AlarmControlPanelEntity, RestoreEntity):
    """A station's guard mode, overlaid with its alarm lifecycle.

    **The mode.** The panel shows the mode in force, ``StationState.active_mode``
    (the slot's mode while Schedule is selected), mapped by ``ha_state``; while
    Schedule is selected, the ``selected_mode`` attribute says ``schedule``. The
    library emits one ``GuardModeChanged`` per change of either, so a slot boundary
    moves the panel once and never through custom bypass.

    **The alarm.** The library's ``AlarmChanged``, on the station's alarm signal, is
    the one source of triggered and of its end: one start and one end per alarm
    across the P2P alarm frames (no push opt-in needed) and the cloud alarm pushes,
    authenticated and ordered by the library. ``alarming=True`` shows triggered (over
    pending or arming) and restarts the alarm timeout; ``alarming=False`` ends a shown
    triggered or pending. It carries no event time, so the alarm is stamped with the
    host clock less ``ALARM_CHANGED_SKEW_SECONDS`` to order the pushes below against
    it. The alarm push's own ``SecurityEvent`` neither raises nor ends triggered, so
    an alarm seen on both channels transitions once.

    **Station pushes** arrive on the station signal. Only an authenticated push changes
    the panel: an ECB frame's static key is known to anyone on the LAN. The alarm
    event entity still fires such a push, marked unauthenticated.

    - An authenticated DELAY shows pending for ``alarm_delay`` seconds from its own
      event time, then the polled mode, unless a trigger came. A DELAY while
      triggered, one whose delay already ran out and one of zero seconds show
      nothing. One with no ``alarm_delay`` shows pending until a stop, a disarm or
      the timeout. While pending, a DELAY older than the running one or with no
      event time is ignored, so a replay cannot shorten or cancel the entry delay.
    - GCM frames carry no sequence number, so a genuine old frame can be replayed
      within a session. Pushes are therefore ordered by event second against a
      floor: the latest authenticated stop or disarm, raised again by the second of
      every alarm that ends. A DELAY older than the floor is ignored. For a DELAY the
      floor is capped at the host clock when raised (a replay is never newer than the
      frame it copies), so a station clock that ran ahead and was set back cannot
      mute a genuine entry delay; stop and disarm evidence is ordered against the
      uncapped floor.
    - An authenticated STOPPED push ends a shown pending (the library emits no
      ``AlarmChanged`` for an entry delay) unless its second is older than the
      delay's. A stop with no event time clears nothing and is not recorded. An alarm
      with no event time of its own ends only on evidence newer than the floor.
    - A confirmed disarm ends triggered or pending: Home Assistant's own disarm
      returning a disarmed mode, an authenticated arming push to a disarmed mode no
      older than the alarm, or a poll showing disarmed when the alarm began in a
      known armed mode. While an alarm is shown, an arming push that cannot be
      ordered after it (older, or with no event time) changes nothing shown, not even
      ``changed_by``. A pushed mode or state never confirms a disarm: neither carries
      an order. An alarm raised while disarmed waits for a stop, a disarm call or
      the timeout. A read showing an armed mode never ends an alarm.
    - An authenticated arming push to an armed mode with ``alarm_delay`` > 0 shows
      arming for that exit delay, from its own event time. It ends at the first mode
      read after the delay, so the push's own same-tick mode report cannot end it,
      or earlier on a disarmed read, a trigger or the timeout.
    - ``changed_by`` is the latest authenticated arming push's source label (Keypad,
      Key fob, App), or None when it names none; never ``user_name``, which any
      client can set.
    - Every accepted alarm starts or restarts the alarm timeout (1-60 minutes), the
      safety net for a stop that never arrives: when it runs out, the panel shows the
      polled mode and asks for one read.

    **Restore.** A shown TRIGGERED or PENDING survives the entity being removed and
    added again within one Home Assistant run (an entry reload, which a
    ``DevicesChanged`` or an options change starts), since the station never re-sends
    a live alarm. Never across a restart: a stop or disarm may have been missed while
    the session was down. The restore data keeps the lifecycle, its second, the
    floors, the mode it began in and the deadlines of the timeout and entry delay on
    the event loop's monotonic clock. It is taken back only while those deadlines
    have not run out, capped at the entity's alarm timeout, so a reload never extends
    an alarm. Arming is not kept. A restored alarm that the setup poll already
    confirms disarmed ends at once.
    """

    # The panel is the station, so it takes the device's own name.
    _attr_name = None
    # For its icon (icons.json); the explicit `_attr_name = None` still wins.
    _attr_translation_key = GUARD_MODE_KEY
    # The class default is True; arming is a P2P command, never gated by a code.
    _attr_code_arm_required = False
    _attr_code_format = None
    # No ARM_CUSTOM_BYPASS. Schedule and the custom modes are shown, never set.
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME | AlarmControlPanelEntityFeature.ARM_AWAY
    )

    def __init__(self, coordinator: StationCoordinator, alarm_timeout_minutes: int) -> None:
        super().__init__(coordinator, GUARD_MODE_KEY)
        # Unknown mode codes already reported, so a station stuck on one logs it
        # once for the entity's lifetime rather than on every 45 s poll.
        self._logged_unknown_codes: set[int] = set()
        # ARMING or DISARMING while a write awaits the station's answer.
        self._in_flight: AlarmControlPanelState | None = None
        self._alarm_timeout_s = alarm_timeout_minutes * 60
        # TRIGGERED, PENDING or push ARMING while the station's alarm lifecycle runs.
        self._lifecycle: AlarmControlPanelState | None = None
        # The event second of the latest accepted alarm, and the ordering floor: the
        # latest authenticated stop or disarm push, for the event-time ordering both ways.
        self._alarm_second: int | None = None
        self._floor_second: int | None = None
        # The floor that orders a new alarm: each raise capped at the host clock.
        self._alarm_floor_second: int | None = None
        # The polled mode when the alarm began: a later disarmed read confirms a
        # disarm only when this was an armed mode.
        self._mode_at_alarm: GuardMode | int | None = None
        # Whether a push ARMING's exit delay has run out.
        self._exit_delay_elapsed = False
        self._cancel_phase: CALLBACK_TYPE | None = None
        self._cancel_timeout: CALLBACK_TYPE | None = None
        # The event loop's monotonic time at which the entry delay and the alarm
        # timeout run out, kept for the restore data.
        self._phase_ends_at: float | None = None
        self._timeout_at: float | None = None

    @property
    @override
    def alarm_state(self) -> AlarmControlPanelState | None:
        """The write in progress, else the alarm lifecycle, else the latest mode.

        Unknown when none of them is known.
        """
        if self._in_flight is not None:
            # The station's report of this write can update the coordinator
            # mid-call; the transitional state holds until the call returns.
            return self._in_flight
        if self._lifecycle is not None:
            return self._lifecycle
        # `DataUpdateCoordinator.data` is typed as the state, but it is None until
        # the first successful poll, so say so.
        data: StationState | None = self.coordinator.data
        if data is None:
            return None
        mode = data.active_mode
        if mode is not None and not isinstance(mode, GuardMode):
            self._log_unknown_code(mode)
        return ha_state(mode)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """``selected_mode: schedule`` while a schedule drives the station, else nothing.

        Only then does the selected mode differ from the one the state shows. Kept
        off otherwise, so an arm between two plain modes writes no extra state while
        the panel shows arming.
        """
        data: StationState | None = self.coordinator.data
        if data is None or data.guard_mode != GuardMode.SCHEDULE:
            return None
        return {ATTR_SELECTED_MODE: detections.mode_label(GuardMode.SCHEDULE)}

    @override
    async def async_added_to_hass(self) -> None:
        """Listen on the station signal, and cancel every timer on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.station_signal(
                    self.coordinator.config_entry.entry_id, self.coordinator.station.serial
                ),
                self._async_on_station_event,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                detections.alarm_signal(
                    self.coordinator.config_entry.entry_id, self.coordinator.station.serial
                ),
                self._async_on_alarm_changed,
            )
        )
        self.async_on_remove(
            self.coordinator.async_add_mode_read_listener(self._async_on_mode_read)
        )
        self.async_on_remove(self._async_cancel_timers)
        if (last := await self.async_get_last_extra_data()) is not None:
            self._async_restore_lifecycle(last.as_dict())

    @property
    @override
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        """A shown TRIGGERED or PENDING, for an entry reload to take back."""
        if not self._alarm_shown() or self._lifecycle is None:
            return None
        mode = self._mode_at_alarm
        return RestoredExtraData(
            {
                "run": restore_run_token(self.hass),
                "lifecycle": self._lifecycle.value,
                "alarm_second": self._alarm_second,
                "floor_second": self._floor_second,
                "alarm_floor_second": self._alarm_floor_second,
                "mode_at_alarm": int(mode) if mode is not None else None,
                "timeout_at": self._timeout_at,
                "phase_ends_at": self._phase_ends_at,
            }
        )

    @callback
    def _async_restore_lifecycle(self, data: dict[str, Any]) -> None:
        """Take back a TRIGGERED or PENDING whose deadlines have not run out."""

        def _int(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        def _time(key: str) -> float | None:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        if data.get("run") != restore_run_token(self.hass):
            # Saved by an earlier run, whose stop or disarm may have been missed.
            return
        lifecycle = data.get("lifecycle")
        if not isinstance(lifecycle, str):
            return
        try:
            state = AlarmControlPanelState(lifecycle)
        except ValueError:
            return
        if state not in (AlarmControlPanelState.TRIGGERED, AlarmControlPanelState.PENDING):
            return
        # Monotonic, so a wall-clock step cannot stretch what is left, and never
        # more than the configured timeout, which an options change can lower.
        now = self.hass.loop.time()
        timeout_at = _time("timeout_at")
        phase_ends_at = _time("phase_ends_at") if state is AlarmControlPanelState.PENDING else None
        if timeout_at is None:
            return
        timeout_left = min(timeout_at - now, self._alarm_timeout_s)
        phase_left = phase_ends_at - now if phase_ends_at is not None else None
        if timeout_left <= 0 or (phase_left is not None and phase_left <= 0):
            return
        mode = _int("mode_at_alarm")
        try:
            self._mode_at_alarm = GuardMode(mode) if mode is not None else None
        except ValueError:
            self._mode_at_alarm = mode
        self._lifecycle = state
        self._alarm_second = _int("alarm_second")
        self._floor_second = _int("floor_second")
        self._alarm_floor_second = _int("alarm_floor_second")
        if self.coordinator.last_update_success and self._read_confirms_disarm():
            # The setup poll, which ran before this entity listened for mode
            # reads, already shows the disarm. Restores run only while the platforms
            # are set up, before the router hands on any push, so the read is a poll.
            self._async_clear_lifecycle()
            return
        self._timeout_at = now + timeout_left
        self._cancel_timeout = async_call_later(
            self.hass, timeout_left, self._async_alarm_timed_out
        )
        if phase_ends_at is not None and phase_left is not None:
            self._phase_ends_at = phase_ends_at
            self._cancel_phase = async_call_later(
                self.hass, phase_left, self._async_entry_delay_ended
            )

    @callback
    def _async_on_station_event(self, event: SecurityEvent) -> None:
        """Apply one station push to the alarm lifecycle.

        A TRIGGERED push changes nothing: the library turns it into the
        ``AlarmChanged`` this panel follows.
        """
        if not event.authenticated:
            # A load-bearing gate: the router sends ECB
            # triggers and delays on this signal for the alarm event entity.
            return
        second = event.event_time_ms // 1000 if event.event_time_ms is not None else None
        phase = event.alarm_phase
        if phase is AlarmPhase.TRIGGERED:
            return
        if phase is AlarmPhase.DELAY:
            self._async_on_delay(event, second)
        elif phase is AlarmPhase.STOPPED:
            self._async_on_stop(second)
        elif event.message_type is PushMessageType.ARMING:
            self._async_on_arming(event)

    @callback
    def _async_on_alarm_changed(self, event: AlarmChanged) -> None:
        """Show triggered on the library's alarm start; end the alarm on its end."""
        if event.alarming:
            second = int(dt_util.utcnow().timestamp()) - ALARM_CHANGED_SKEW_SECONDS
            self._async_begin_alarm(AlarmControlPanelState.TRIGGERED, second)
            self._async_restart_alarm_timeout()
            self.async_write_ha_state()
        elif self._alarm_shown():
            self._async_end_lifecycle()

    @callback
    def _async_on_arming(self, event: SecurityEvent) -> None:
        """Name who changed the mode, confirm a disarm, or show the exit delay."""
        mode = detections.effective_guard_mode(event)
        if mode is None:
            return
        second = event.event_time_ms // 1000 if event.event_time_ms is not None else None
        previous_floor = self._raise_floor(second) if mode.is_disarmed else self._floor_second
        if self._alarm_shown() and not self._orders_after_alarm(second, previous_floor):
            # GCM frames carry no sequence number, so a push that cannot
            # be ordered after the alarm may be a replay. It ends nothing and
            # attributes nothing.
            return
        source = event.arming_source
        self._attr_changed_by = (
            detections.ARMING_SOURCE_LABELS[source] if source is not None else None
        )
        if mode.is_disarmed:
            if self._lifecycle is not None:
                # A disarm the station itself reported, authenticated.
                self._async_end_lifecycle()
                return
        elif not self._alarm_shown() and event.alarm_delay is not None and event.alarm_delay > 0:
            remaining = detections.hold_remaining_seconds(
                event.event_time_ms, event.alarm_delay, dt_util.utcnow().timestamp() * 1000
            )
            if remaining > 0:
                if self._cancel_phase is not None:
                    self._cancel_phase()
                self._lifecycle = AlarmControlPanelState.ARMING
                self._exit_delay_elapsed = False
                self._cancel_phase = async_call_later(
                    self.hass, remaining, self._async_exit_delay_ended
                )
                self._async_restart_alarm_timeout()
        self.async_write_ha_state()

    @callback
    def _async_exit_delay_ended(self, _now: datetime) -> None:
        """Mark the exit delay over; the next mode read ends arming."""
        self._cancel_phase = None
        self._exit_delay_elapsed = True

    @callback
    def _async_on_mode_read(self, polled: bool) -> None:
        """End arming, or confirm a disarm, on a mode the station was just read in.

        Any read ends arming. Only a poll confirms a disarm during an alarm: a pushed
        mode or state has no event time to order against the alarm.
        """
        data: StationState | None = self.coordinator.data
        if data is None or self._lifecycle is None:
            return
        mode = data.active_mode
        read_disarmed = isinstance(mode, GuardMode) and mode.is_disarmed
        if self._lifecycle is AlarmControlPanelState.ARMING:
            if self._exit_delay_elapsed or read_disarmed:
                self._async_end_lifecycle()
            return
        if polled and self._read_confirms_disarm():
            self._async_end_lifecycle()

    def _read_confirms_disarm(self) -> bool:
        """Whether the latest read, if a poll, confirms a disarm of the shown alarm.

        It must show disarmed, and the alarm must have begun in a known armed mode.
        """
        data: StationState | None = self.coordinator.data
        mode = data.active_mode if data is not None else None
        return (
            isinstance(mode, GuardMode)
            and mode.is_disarmed
            and isinstance(self._mode_at_alarm, GuardMode)
            and not self._mode_at_alarm.is_disarmed
        )

    def _older_than_last_stop(self, second: int | None) -> bool:
        """Whether an alarm of ``second`` predates the host-capped ordering floor.

        Equal seconds are not older: showing an alarm is the fail-loud direction.
        """
        floor = self._alarm_floor_second
        return second is not None and floor is not None and second < floor

    def _raise_floor(self, second: int | None) -> int | None:
        """Raise the ordering floors to ``second``, if it has one; return the floor before.

        The floor that orders new alarms is raised no higher than the host clock: the
        library accepts event times up to ``EVENT_TIME_MAX_SKEW_S`` (600 s) ahead of it.
        """
        previous = self._floor_second
        if second is not None:
            self._floor_second = second if previous is None else max(previous, second)
            capped = min(second, int(dt_util.utcnow().timestamp()))
            alarm_floor = self._alarm_floor_second
            self._alarm_floor_second = capped if alarm_floor is None else max(alarm_floor, capped)
        return previous

    def _orders_after_alarm(self, second: int | None, previous_floor: int | None) -> bool:
        """Whether stop or disarm evidence of ``second`` may end the shown alarm.

        Not with no event time, and not when older than the alarm. An
        alarm with no event time of its own cannot be ordered against the evidence,
        so only evidence newer than the floor before it ends it: evidence no newer
        than a stop or disarm already seen is a replay.
        """
        if second is None:
            return False
        if self._alarm_second is not None:
            return second >= self._alarm_second
        return previous_floor is None or second > previous_floor

    @callback
    def _async_on_delay(self, event: SecurityEvent, second: int | None) -> None:
        """Show pending for the entry delay, counted from the push's own time."""
        if self._lifecycle is AlarmControlPanelState.TRIGGERED or self._older_than_last_stop(
            second
        ):
            return
        if self._lifecycle is AlarmControlPanelState.PENDING and (
            second is None or (self._alarm_second is not None and second < self._alarm_second)
        ):
            # A delay that cannot be ordered after the running one may be a
            # replay; it neither shortens nor cancels the running entry delay.
            return
        remaining: float | None = None
        if event.alarm_delay == 0:
            # An entry delay of zero seconds is no entry delay, not pending
            # until the timeout (pending lasts ``alarm_delay`` seconds).
            return
        if event.alarm_delay is not None and event.alarm_delay > 0:
            remaining = detections.hold_remaining_seconds(
                event.event_time_ms, event.alarm_delay, dt_util.utcnow().timestamp() * 1000
            )
            if remaining <= 0:
                return
        self._async_begin_alarm(AlarmControlPanelState.PENDING, second)
        if remaining is not None:
            # Without a delay, pending ends on a stop, a disarm or the timeout.
            self._phase_ends_at = self.hass.loop.time() + remaining
            self._cancel_phase = async_call_later(
                self.hass, remaining, self._async_entry_delay_ended
            )
        self._async_restart_alarm_timeout()
        self.async_write_ha_state()

    @callback
    def _async_entry_delay_ended(self, _now: datetime) -> None:
        """The entry delay ran out with no trigger: show the polled mode."""
        self._cancel_phase = None
        self._phase_ends_at = None
        if self._lifecycle is AlarmControlPanelState.PENDING:
            self._async_end_lifecycle()

    @callback
    def _async_on_stop(self, second: int | None) -> None:
        """End a shown pending on a stop no older than it.

        A shown triggered ends on the library's ``AlarmChanged`` instead, which the
        stop's own push also produces.
        """
        if second is None:
            # The event-time ordering cannot place a stop the library gave no time.
            return
        # The floor before this stop is the evidence that reveals a replay.
        previous_floor = self._raise_floor(second)
        if self._lifecycle is AlarmControlPanelState.PENDING and self._orders_after_alarm(
            second, previous_floor
        ):
            self._async_end_lifecycle()

    def _alarm_shown(self) -> bool:
        """Whether TRIGGERED or PENDING is shown."""
        return self._lifecycle in (
            AlarmControlPanelState.TRIGGERED,
            AlarmControlPanelState.PENDING,
        )

    @callback
    def _async_begin_alarm(self, state: AlarmControlPanelState, second: int | None) -> None:
        """Enter ``state``, remembering the mode the alarm began in and its latest second."""
        if not self._alarm_shown():
            data: StationState | None = self.coordinator.data
            self._mode_at_alarm = data.active_mode if data is not None else None
        if self._cancel_phase is not None:
            self._cancel_phase()
            self._cancel_phase = None
        self._phase_ends_at = None
        self._exit_delay_elapsed = False
        self._lifecycle = state
        if second is not None:
            self._alarm_second = (
                second if self._alarm_second is None else max(self._alarm_second, second)
            )

    @callback
    def _async_restart_alarm_timeout(self) -> None:
        """Start the alarm timeout afresh."""
        if self._cancel_timeout is not None:
            self._cancel_timeout()
        self._timeout_at = self.hass.loop.time() + self._alarm_timeout_s
        self._cancel_timeout = async_call_later(
            self.hass, self._alarm_timeout_s, self._async_alarm_timed_out
        )

    @callback
    def _async_end_lifecycle(self) -> None:
        """Show the polled mode again. The ordering floor and changed_by are kept."""
        self._async_clear_lifecycle()
        self.async_write_ha_state()

    @callback
    def _async_clear_lifecycle(self) -> None:
        """End the lifecycle without writing state."""
        self._async_cancel_timers()
        # The deadlines stay through the timer cancel at removal, which runs before
        # the restore data is read; only an ended lifecycle forgets them.
        self._phase_ends_at = None
        self._timeout_at = None
        self._lifecycle = None
        # A replayed alarm older than the one that ended cannot raise it again.
        self._raise_floor(self._alarm_second)
        self._alarm_second = None
        self._mode_at_alarm = None
        self._exit_delay_elapsed = False

    @callback
    def _async_alarm_timed_out(self, _now: datetime) -> None:
        """The safety net: show the polled mode and ask for one fresh read."""
        self._cancel_timeout = None
        self._timeout_at = None
        if self._lifecycle is None:
            return
        self._async_end_lifecycle()
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self.coordinator.async_request_refresh(),
            name=f"{DOMAIN} refresh after the alarm timeout",
        )

    @callback
    def _async_cancel_timers(self) -> None:
        """Cancel the phase timer and the alarm timeout."""
        if self._cancel_phase is not None:
            self._cancel_phase()
            self._cancel_phase = None
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None

    @override
    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm away; the panel shows arming until the station answers."""
        del code  # no code: arming is a P2P command
        await self._async_set_guard_mode(GuardMode.AWAY, AlarmControlPanelState.ARMING)

    @override
    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Arm home; the panel shows arming until the station answers."""
        del code
        await self._async_set_guard_mode(GuardMode.HOME, AlarmControlPanelState.ARMING)

    @override
    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm; the panel shows disarming until the station answers.

        Writes ``DISARMED`` (63): ``OFF`` is a mode the station reports but the
        library refuses to send.
        """
        del code
        await self._async_set_guard_mode(GuardMode.DISARMED, AlarmControlPanelState.DISARMING)

    async def _async_set_guard_mode(
        self, target: GuardMode, transitional: AlarmControlPanelState
    ) -> None:
        """Write ``target`` and show only the mode the station applied.

        The library confirms the write by the station's report or by reading the
        mode back, and returns the mode in force. That mode is applied before the
        transitional state is cleared, so the panel goes straight from arming to the
        applied mode. If the call raises, nothing is applied and the panel shows the
        last mode read, never the requested one.

        A library error becomes a translated error (``errors.arm_failed``): the write
        reconnects first when the session is down, so a protocol or handshake
        failure, a rejected key or a cloud refusal can arrive here. A
        ``SessionReplacedError`` in the cause chain is named as such, pointing at
        Repairs. A plain ``UnsupportedError`` (not a -108 receipt) and anything that
        is not a library error propagate: they are programming errors, not a
        station's answer.
        """
        self._in_flight = transitional
        self.async_write_ha_state()
        try:
            applied = await self.coordinator.station.async_set_guard_mode(target)
            if (
                isinstance(applied, GuardMode)
                and applied.is_disarmed
                and self._lifecycle is not None
            ):
                # A disarm Home Assistant sent and the station confirmed.
                self._async_end_lifecycle()
            self.coordinator.async_apply_guard_mode(applied)
        except EufySecurityError as err:
            # A -108 receipt is a CommandError and an UnsupportedError: the station's answer.
            if isinstance(err, UnsupportedError) and not isinstance(err, CommandError):
                raise
            raise errors.arm_failed(
                err,
                target,
                on_demand=self.coordinator.station.connects_on_demand,
            ) from err
        finally:
            self._in_flight = None
            self.async_write_ha_state()

    def _log_unknown_code(self, code: int) -> None:
        """Warn once per distinct code, naming the entity and never the serial."""
        if code in self._logged_unknown_codes:
            return
        self._logged_unknown_codes.add(code)
        _LOGGER.warning(
            "%s reports guard mode code %d, which the eufy library does not know; "
            "the panel shows it as unknown",
            self.entity_id,
            code,
        )
