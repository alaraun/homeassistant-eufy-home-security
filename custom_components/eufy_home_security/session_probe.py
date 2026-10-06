"""The session probe: one cached-token cloud read, a minute after setup and then every 6 hours.

Why it exists: the library latches a kick-out (another client's
login ended Home Assistant's eufy session) on any authenticated cloud call, and
either emits a ``CloudProblem`` or raises to a caller that already routes it to the
account's fixable ``session_replaced`` repair issue. That detection path is complete
but opportunistic: nothing makes an authenticated call while Home Assistant is idle.
Setup answers from the cache, so a takeover stays invisible until some feature
happens to reach the cloud; on an account of HomeBases alone, with no on-demand
station and so no hourly cloud refresh in the library, that may be never. This
module makes the one call
nobody else makes, so a kick-out shows as a repair within 6 hours.

Rules that make it safe against eufy's login budget (about four sign-ins per
afternoon, a 24 h lock after about four failures):

- A probe is never a login. Each tick reads the library's no-I/O latch and login
  need first. While the latch is set nothing is sent (the issue is shown instead).
  While no usable cached session exists nothing is sent either, because the library
  would sign in with the cached password to answer, and that is a decision for a
  setup or a user, never a timer.
- One call per tick, the library's ``EufySecurity.async_probe_cloud_session()``, and
  never a retry inside a tick. A failure is routed once through
  ``errors.route_cloud_error``.
- The call is a device-list fetch that applies nothing: no ``DevicesChanged``, no
  station rebuilt, no reload. It never answers from the cached list, so a refusal
  (a kick-out, a key identity a new exchange did not restore, an unreachable cloud)
  raises instead of passing for a healthy session.
- Guard mode is polled locally; the cloud session is probed rarely. The interval is
  not user-tunable.

Every tick writes exactly one DEBUG line starting with ``Session probe:`` that names
its decision. The line carries an error type name or the login need's value only,
never the e-mail, a token, a serial or the device list.

This module names no client type: it reaches the client through
``entry.runtime_data.eufy``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from eufy_home_security import EufySecurityError, LoginNeed

from . import errors
from .const import (
    CONF_SESSION_PROBE,
    DOMAIN,
    SESSION_PROBE_FIRST_DELAY_SECONDS,
    SESSION_PROBE_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)


@callback
def async_start_session_probe(hass: HomeAssistant, entry: EufyConfigEntry) -> None:
    """Arm the probe's two timers for a loaded entry, unless the option turned it off.

    Called at the end of ``async_setup_entry``, after ``entry.runtime_data`` is set,
    so the first tick can read the client. Both timer cancels are registered with
    ``entry.async_on_unload`` and each tick runs as an entry background task, which
    Home Assistant cancels at unload too, so no tick survives the entry. A change of
    the option applies on the reload the options flow performs, because the option
    is read here, once, at setup.
    """
    if not entry.options.get(CONF_SESSION_PROBE, True):
        _LOGGER.debug("Session probe: off by option, not scheduled")
        return

    @callback
    def _run(_now: datetime) -> None:
        entry.async_create_background_task(
            hass, async_probe_session(hass, entry), name=f"{DOMAIN} session probe"
        )

    entry.async_on_unload(async_call_later(hass, SESSION_PROBE_FIRST_DELAY_SECONDS, _run))
    # The interval is armed at setup, so the second probe runs about 6 h after setup,
    # not 6 h after the first probe.
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _run,
            timedelta(seconds=SESSION_PROBE_INTERVAL_SECONDS),
            name=f"{DOMAIN} session probe",
        )
    )
    _LOGGER.debug(
        "Session probe: scheduled, first in %d s, then every %d h",
        SESSION_PROBE_FIRST_DELAY_SECONDS,
        SESSION_PROBE_INTERVAL_SECONDS // 3600,
    )


async def async_probe_session(hass: HomeAssistant, entry: EufyConfigEntry) -> None:
    """One tick: two no-I/O gates, then at most one cached-token read, routed or cleared.

    - The latch is set (``LoginNeed.REPLACED``): make sure the account's
      session-replaced issue exists and send nothing. An issue is not persistent, so
      this re-creates one the user deleted while the latch is still set (the latch
      mirror); the fix and Reconfigure are the user's two deliberate paths out.
    - No usable cached session (any other need than ``NONE``): send nothing. The
      library would log in with the cached password to satisfy the call, and a probe
      is never a login; the next real call or setup handles that case.
    - Otherwise one ``eufy.async_probe_cloud_session()``. Any library error is
      routed once through ``errors.route_cloud_error`` (a kick-out becomes the
      fixable issue, a throttle the login-limited issue, a rejected session one
      reauth flow, anything else a DEBUG line); never a retry. The library reports
      the same failure as a ``CloudProblem`` too, and routing is idempotent. Success
      deletes the session-replaced issue and only that.

    Anything that is not a library error propagates to Home Assistant's
    background-task logging: a programming error must show.
    """
    eufy = entry.runtime_data.eufy
    status = await eufy.async_cloud_status()
    if status.login_need is LoginNeed.REPLACED:
        errors.raise_session_replaced_issue(hass, entry)
        _LOGGER.debug(
            "Session probe: skipped, the session-replaced latch is set; "
            "the repair issue is shown, nothing sent"
        )
        return
    if status.login_need is not LoginNeed.NONE:
        _LOGGER.debug(
            "Session probe: skipped, no cached session to send with (%s); nothing sent",
            status.login_need.value,
        )
        return
    try:
        await eufy.async_probe_cloud_session()
    except EufySecurityError as err:
        errors.route_cloud_error(hass, entry, err)
        _LOGGER.debug("Session probe: failed (%s), routed", type(err).__name__)
        return
    errors.clear_session_replaced_issue(hass, entry)
    _LOGGER.debug("Session probe: ok, eufy still accepts the cached session")
