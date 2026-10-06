"""One coordinator per station: the local P2P parameter read, on a generous interval."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from eufy_home_security import (
    EufySecurityError,
    GuardMode,
    Station,
    StationState,
    redact_serial,
)

from . import errors
from .const import DOMAIN, POLL_INTERVAL_SECONDS

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)


class StationCoordinator(DataUpdateCoordinator[StationState]):
    """A station's latest ``StationState``.

    The poll is the safety net; pushes set ``data`` (``async_apply_guard_mode``,
    ``async_apply_state``) and call ``async_update_listeners``, which leaves the poll
    timer alone. The coordinator's set-updated-data method is never used: it cancels
    and reschedules the poll, so steady push traffic would postpone the guard-mode
    poll without bound.

    **Mode reads.** ``async_add_mode_read_listener`` hears every mode read: each
    successful refresh, and each pushed mode or state, changed or not. Each call
    says whether the read was this coordinator's own poll (``polled=True``) or a
    report the station pushed or a write returned (``polled=False``). The alarm
    panel ends an exit delay on either, but confirms a disarm during an alarm only
    on a poll: a pushed report carries no event time, so it cannot be ordered
    against the alarm and could be a replay. With
    ``always_update=False`` an unchanged read notifies no entity, so it cannot be
    observed through the ordinary listeners.
    """

    # This annotation, not the constructor argument, types `self.config_entry`.
    config_entry: EufyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry, station: Station) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            # HA names the coordinator in every failed-poll log line: never the
            # full serial.
            name=f"{DOMAIN} {redact_serial(station.serial)}",
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
            # A poll that read the same state notifies no entity.
            always_update=False,
        )
        self.station = station
        # Channels already reported without a serial, so a station that keeps
        # reporting one says so once rather than on every 45 s poll.
        self._logged_serialless: set[int] = set()
        self._mode_read_listeners: list[Callable[[bool], None]] = []

    @override
    async def _async_update_data(self) -> StationState:
        """One local parameter read; every library error becomes ``UpdateFailed``."""
        try:
            state = await self.station.async_update()
        except EufySecurityError as err:
            raise errors.update_failed(err) from err
        self._async_note_serialless_devices(state)
        return state

    @callback
    def async_add_mode_read_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``listener(polled)`` on every mode read; returns the function that removes it."""
        self._mode_read_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._mode_read_listeners:
                self._mode_read_listeners.remove(listener)

        return _remove

    @callback
    def _async_notify_mode_read(self, *, polled: bool) -> None:
        """Tell every mode-read listener that the station's mode was just read."""
        for listener in list(self._mode_read_listeners):
            listener(polled)

    @override
    @callback
    def _async_refresh_finished(self) -> None:
        """Notify mode-read listeners after every successful refresh.

        ``always_update=False`` hides a read that changed nothing from the
        ordinary listeners, yet it is still
        evidence of the mode in force. Called before those listeners are
        notified. A failed refresh read nothing.
        """
        super()._async_refresh_finished()
        # `data` is typed as the state but is None until the first success.
        data: StationState | None = self.data
        if self.last_update_success and data is not None:
            self._async_notify_mode_read(polled=True)

    @callback
    def async_mark_not_up(self, error: BaseException) -> None:
        """Show a station that did not come up at setup as unavailable, with no log line.

        Setup has already logged its one WARNING by redacted serial. This
        does not go through ``async_set_update_error``, which logs an ERROR whenever
        the coordinator has not failed before, and a
        coordinator built by this setup never has. The next successful read, from a
        reconnect or the poll, makes it available again.
        """
        self.last_exception = errors.station_not_up(error)
        self.last_update_success = False
        self.async_update_listeners()

    @callback
    def _async_note_serialless_devices(self, state: StationState) -> None:
        """Say once, per channel, that a paired device with no serial gets nothing.

        A slot the station reports that the cloud's device list does not name has no
        identity that would survive the device being moved to another HomeBase, so it
        becomes no device and no entities rather than a slot-numbered one that breaks
        on the next re-pair. Said at DEBUG once per channel: a station that
        keeps reporting the slot would otherwise repeat it on every 45 s poll. The
        line carries the station's redacted serial and the slot number only.
        """
        for device in state.devices.values():
            if device.serial is not None or device.channel in self._logged_serialless:
                continue
            self._logged_serialless.add(device.channel)
            _LOGGER.debug(
                "HomeBase %s reports a paired device on channel %d with no serial; "
                "it gets no device and no entities",
                redact_serial(self.station.serial),
                device.channel,
            )

    @callback
    def async_apply_guard_mode(
        self, mode: GuardMode | int, active_mode: GuardMode | int | None = None
    ) -> None:
        """Show a guard mode the station reported, without touching the poll timer.

        Called with the mode a write returned and with a pushed
        ``GuardModeChanged``. ``mode`` is the selected mode and ``active_mode`` the
        mode in force. Without ``active_mode`` the mode in force is ``mode``
        itself, except when ``mode`` is ``SCHEDULE``: a write returns only the
        selection, and the slot's mode arrives in the report that follows, so the
        mode in force shown until then is kept. It replaces ``data`` and calls
        ``async_update_listeners``, which only notifies
        the entities. The coordinator's set-updated-data method would also cancel
        and reschedule the poll, so a steady stream
        of reports would postpone the 45 s poll that stays authoritative.

        Nothing happens before the first read (there is no state to amend, and the
        first refresh reads the mode) or when the mode is already shown, so a
        repeated report writes no entity state. A repeated report is still a mode
        read, so mode-read listeners hear it.
        """
        data: StationState | None = self.data
        if data is None:
            return
        if active_mode is None:
            active_mode = data.active_mode if mode == GuardMode.SCHEDULE else mode
        if data.guard_mode == mode and data.active_mode == active_mode:
            self._async_notify_mode_read(polled=False)
            return
        self.data = dataclasses.replace(data, guard_mode=mode, active_mode=active_mode)
        self._async_notify_mode_read(polled=False)
        self.async_update_listeners()

    @callback
    def async_apply_state(self, state: StationState | None) -> None:
        """Show a station state the library built, without touching the poll timer.

        Called with the state left behind by a confirmed setting write, and with a
        pushed ``StationStateChanged``. It replaces
        ``data`` and calls ``async_update_listeners``,
        which only notifies the entities. The coordinator's set-updated-data method
        would also cancel and reschedule the poll,
        so a steady stream of dumps would postpone the 45 s poll that stays
        authoritative for guard mode.

        Nothing happens before the station has a state, or when that state is the
        one already shown, so a repeated dump writes no entity state. A repeated
        dump is still a mode read, so mode-read listeners hear it.
        """
        if state is None:
            return
        self._async_note_serialless_devices(state)
        if state == self.data:
            self._async_notify_mode_read(polled=False)
            return
        self.data = state
        self._async_notify_mode_read(polled=False)
        self.async_update_listeners()
