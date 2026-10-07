"""The only place library errors become Home Assistant control flow.

Every function that builds a Home Assistant exception returns it, so the caller
raises it ``from err`` and keeps the library error as the cause; ``flow_error_key``
returns a config-flow form error key instead.

The repair-issue helpers are the only mapping from library cloud and key errors to
repair issues, and none of them logs in or calls the cloud: a kick-out or a throttle
becomes something the user sees and decides on, never a retry. Issue ids and data
carry only the entry id and a device-registry id; placeholders carry an account
label, a device name and minutes, never a serial, owner account id, e-mail address
or password.

:func:`arm_failed` and :func:`setting_write_failed` share five of their six messages
through one placeholder, ``target`` (a lower-case mode name or an entity name), so
the two write paths cannot drift.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from eufy_home_security import (
    AuthenticationError,
    CameraWakeError,
    CipherUnavailableError,
    CloudError,
    CloudProblem,
    CloudStatus,
    CommandError,
    ConnectionChanged,
    CredentialsRefreshed,
    EufySecurityError,
    GuardMode,
    KeyRejectedError,
    LiveStreamLimitError,
    LoginChallengeError,
    RateLimitedError,
    RefreshCooldownError,
    RegionStatus,
    SessionReplacedError,
    Station,
    redact,
    redact_serial,
)

from .const import (
    DOMAIN,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_AUTH,
    ERROR_LOGIN_CHALLENGE,
    ERROR_LOGIN_LIMITED,
    ERROR_SESSION_REPLACED,
    EXC_AUTH_FAILED,
    EXC_CACHE_UNAVAILABLE,
    EXC_CAMERA_UNAVAILABLE,
    EXC_CAPTURE_IN_PROGRESS,
    EXC_CLOUD_UNAVAILABLE,
    EXC_DEFAULT_PRESET_NEEDS_CONFIRMATION,
    EXC_DEVICE_LIST_REFRESH_FAILED,
    EXC_GUARD_MODE_NOT_APPLIED,
    EXC_LIVE_STREAM_LIMIT,
    EXC_ON_DEMAND_UNREACHABLE,
    EXC_PAN_TILT_NOT_APPLIED,
    EXC_PAN_TILT_UNSUPPORTED,
    EXC_PRESET_NOT_DELETED,
    EXC_PRESET_NOT_SAVED,
    EXC_PRESET_NOT_SET,
    EXC_PRESET_SAVED_NOT_DEFAULT,
    EXC_PRESET_SLOT_UNKNOWN,
    EXC_PRESETS_FULL,
    EXC_PRESETS_UNSUPPORTED,
    EXC_PTZ_COMMAND_NOT_HANDLED,
    EXC_RECORDING_FAILED,
    EXC_RECORDING_IN_PROGRESS,
    EXC_RECORDING_NEEDS_HISTORY,
    EXC_RECORDING_UNSUPPORTED,
    EXC_SESSION_REPLACED_SEE_REPAIRS,
    EXC_SETTING_DEVICE_UNAVAILABLE,
    EXC_SETTING_MODE_TABLE_REFUSED,
    EXC_SETTING_NOT_APPLIED,
    EXC_SETTING_UNCONFIRMED,
    EXC_SETTING_VALUE_INVALID,
    EXC_STATION_KEY_REJECTED,
    EXC_STATION_UNREACHABLE,
    EXC_ZOOM_NEEDS_SINGLE_VIEW,
    EXC_ZOOM_UNSUPPORTED,
    ISSUE_ACCOUNT_ID_MISMATCH,
    ISSUE_CIPHER_UNAVAILABLE,
    ISSUE_CREDENTIALS_REFRESHED,
    ISSUE_CREDENTIALS_REFRESHED_LOGIN,
    ISSUE_KEY_REJECTED,
    ISSUE_LOGIN_LIMITED,
    ISSUE_LOGIN_LIMITED_NO_WAIT,
    ISSUE_MEDIA_NOT_PERSISTENT,
    ISSUE_NO_DEVICES,
    ISSUE_PUSH_NOT_RUNNING,
    ISSUE_SESSION_REPLACED,
    MEDIA_DOCS_URL,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def update_failed(err: EufySecurityError) -> UpdateFailed:
    """A failed station poll; every library error, cloud errors included.

    A credential problem reaches the user through the library's own event, not as a
    second reauth path from the poll. The message is the error type and the library's
    own message, which carries no secret; no serial is added.
    """
    return UpdateFailed(f"{type(err).__name__}: {err}")


def connection_lost(event: ConnectionChanged) -> UpdateFailed:
    """A station session that went down, or did not come up.

    Handed to ``async_set_update_error``, which logs it once per outage. The message
    names the cause and the error type only: never the serial, and not the error's
    own text either, since that is logged by the library already.
    """
    cause = event.cause.value if event.cause is not None else "unknown"
    error = type(event.error).__name__ if event.error is not None else "no error"
    return UpdateFailed(f"station connection lost ({cause}): {error}")


def station_not_up(error: BaseException) -> UpdateFailed:
    """A station that did not come up at setup.

    Kept as the coordinator's last exception and never logged: setup's one WARNING
    already named the station by its redacted serial. The message names the error
    type only.
    """
    return UpdateFailed(f"station did not come up: {type(error).__name__}")


def connection_lost_at_setup() -> UpdateFailed:
    """A station that came up at setup but whose session was down by the end of it.

    Its ``ConnectionChanged(False)`` arrived before setup followed availability, so
    the event itself is gone; the message says what happened and names nothing else.
    """
    return UpdateFailed("station connection lost during setup")


def no_station_started(count: int) -> ConfigEntryNotReady:
    """None of the account's local stations came up on its first-ever setup.

    Home Assistant retries the setup, which is cloud-free on the warm cache. The
    message holds only the count.
    """
    return ConfigEntryNotReady(f"none of the account's {count} stations came up")


def cache_unavailable(err: EufySecurityError) -> ConfigEntryNotReady:
    """No cached device list, and the cloud cannot supply one right now.

    Raised only when ``async_discover`` fails, which on a warm cache it does not:
    a degraded login alone never makes the entry not ready. The message is the
    translation only.
    """
    return ConfigEntryNotReady(translation_domain=DOMAIN, translation_key=EXC_CACHE_UNAVAILABLE)


def auth_failed(err: AuthenticationError) -> ConfigEntryAuthFailed:
    """Setup met a password eufy rejected, or no password to sign in with.

    Home Assistant starts the reauth flow from this exception.
    The library has already dropped a rejected password,
    so nothing signs in again until the user types a new one. The message is the
    translation only: it never carries the password, a serial or the library's
    own text.
    """
    return ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key=EXC_AUTH_FAILED)


def is_session_replaced_failure(err: BaseException) -> bool:
    """Did this command or capture fail because another client ended the eufy session?

    Yes when ``err`` or any link of its ``__cause__`` chain is a
    ``SessionReplacedError``, on any station: the write reconnected and eufy refused
    the ended session when the key or owner id was fetched again. That includes an
    on-demand camera whose wake needs a device session key while the latch is set:
    the library fails that connect at once with ``SessionReplacedError``.
    """
    cause: BaseException | None = err
    while cause is not None:
        if isinstance(cause, SessionReplacedError):
            return True
        cause = cause.__cause__
    return False


def failure_reason(err: BaseException) -> str:
    """What a DEBUG outcome line says failed: the error type, and the session when it is the cause.

    The type name alone when the failure is the station's or the cloud's own; with
    :func:`is_session_replaced_failure` true, the type name followed by a clause that
    names the eufy session as replaced, points at Repairs and says the camera could
    not be woken for it. For DEBUG lines only: never the
    library's message text, never a serial.
    """
    name = type(err).__name__
    if is_session_replaced_failure(err):
        return (
            f"{name}; another client ended the eufy session, so the camera could not be "
            "woken; see Settings > System > Repairs"
        )
    return name


def arm_failed(
    err: EufySecurityError,
    target: GuardMode,
    *,
    on_demand: bool = False,
) -> HomeAssistantError:
    """An arm or disarm the station did not apply, or could not receive.

    Checked in this order, because the classes nest:

    - :func:`is_session_replaced_failure`: another client's login ended the eufy
      session; ``session_replaced_see_repairs`` points at the account's repair.
    - ``CameraWakeError`` (also a ``CommandError``): the station answered but could
      not wake the camera; the message says the camera may be asleep.
    - ``CommandError``: the station was reached and did not apply the mode
      (``CommandNotAppliedError``, usually an owner id it does not hold) or answered
      with an error or another mode (``CommandRejectedError``, a -108
      ``CommandUnsupportedError`` included).
    - ``KeyRejectedError``: the write reconnected and the station rejected even its
      re-fetched key; the station's key-rejected repair is the remedy.
    - ``CloudError``: the reconnect needed the key or owner id fetched again and eufy
      was down or refused. Nothing here signs in.
    - Anything else (``CommunicationError``, or a ``ProtocolError`` met while the
      session came back up): the station could not be reached. With ``on_demand``
      (a battery camera without a HomeBase) the message says the camera may be
      asleep: a wake takes about ten seconds and may fail.

    The only placeholder is ``target``, the requested mode's lower-case name: no
    serial and no library text. Five of these six messages are shared with
    :func:`setting_write_failed`. The caller lets a plain ``UnsupportedError`` propagate.
    """
    if is_session_replaced_failure(err):
        key = EXC_SESSION_REPLACED_SEE_REPAIRS
    elif isinstance(err, CameraWakeError):
        key = EXC_ON_DEMAND_UNREACHABLE
    elif isinstance(err, CommandError):
        key = EXC_GUARD_MODE_NOT_APPLIED
    elif isinstance(err, KeyRejectedError):
        key = EXC_STATION_KEY_REJECTED
    elif isinstance(err, CloudError):
        key = EXC_CLOUD_UNAVAILABLE
    else:
        key = EXC_ON_DEMAND_UNREACHABLE if on_demand else EXC_STATION_UNREACHABLE
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"target": target.name.lower()},
    )


def setting_write_failed(
    err: EufySecurityError,
    target: str,
    *,
    on_demand: bool = False,
) -> HomeAssistantError:
    """A setting write the station did not apply, or could not receive.

    The outcomes and order of :func:`arm_failed`, except that ``CommandError`` (the
    station did not apply the value, or answered with an error) has its own key:
    "the change was not applied" is not "the alarm was not armed".

    ``target`` is the only placeholder: the entity's translated name, or its catalog
    key when it has none; never a serial, an owner account id or the library's text.
    A plain ``UnsupportedError`` does not arrive here: the caller maps it to
    :func:`setting_device_unavailable` or :func:`setting_mode_table_refused`, or lets
    a catalog-shape programming error propagate.
    """
    if is_session_replaced_failure(err):
        key = EXC_SESSION_REPLACED_SEE_REPAIRS
    elif isinstance(err, CameraWakeError):
        key = EXC_ON_DEMAND_UNREACHABLE
    elif isinstance(err, CommandError):
        key = EXC_SETTING_NOT_APPLIED
    elif isinstance(err, KeyRejectedError):
        key = EXC_STATION_KEY_REJECTED
    elif isinstance(err, CloudError):
        key = EXC_CLOUD_UNAVAILABLE
    else:
        key = EXC_ON_DEMAND_UNREACHABLE if on_demand else EXC_STATION_UNREACHABLE
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"target": target},
    )


def setting_device_unavailable(target: str) -> HomeAssistantError:
    """A setting write for a device the station cannot address right now; nothing sent.

    ``Station.channel_for`` raises ``UnsupportedError`` when eufy's latest device list
    does not pair the device to this station (until the reload it schedules) or its
    cloud record names no channel. The device keeps its entities (the catalog
    resolves them from the serial), so a user can press one of its controls and gets
    this sentence, not a traceback. ``target`` is the entity's translated name, never
    a serial; the library's message is kept only as the cause.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=EXC_SETTING_DEVICE_UNAVAILABLE,
        translation_placeholders={"target": target},
    )


def setting_mode_table_refused(target: str) -> HomeAssistantError:
    """A per-mode action or delay write the library refused, nothing sent.

    These settings are written as their mode's whole table, and the library refuses
    with ``UnsupportedError`` when that table cannot be written back unchanged: a
    paired device reports no action for the mode, a device is of no known kind (a
    siren accessory whose triggers the table would clear), or devices hold different
    values for a delay the table carries as one. A retry cannot fix it. ``target`` is
    the entity's translated name, never a serial; no library text.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=EXC_SETTING_MODE_TABLE_REFUSED,
        translation_placeholders={"target": target},
    )


def setting_write_unconfirmed(target: str) -> HomeAssistantError:
    """A setting write that got no answer in time; it may still apply.

    The entity shows unknown until the next state, which shows whether it did.
    ``target`` is the entity's name, never a serial.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=EXC_SETTING_UNCONFIRMED,
        translation_placeholders={"target": target},
    )


def setting_value_invalid(target: str) -> ServiceValidationError:
    """A setting value the library refused with ``ValueError``; nothing sent.

    Home Assistant checks a number's range but not its step, and a string only
    against its length and pattern; the library's ``Setting.validate`` checks the
    rest. ``target`` is the entity's name, never a serial; no library text.
    """
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=EXC_SETTING_VALUE_INVALID,
        translation_placeholders={"target": target},
    )


def flow_error_key(err: EufySecurityError) -> str:
    """The config-flow form error for a library error met while signing in.

    Checked most specific first, because the classes nest:
    ``LoginChallengeError`` is an ``AuthenticationError`` but the password was
    right, and ``LoginLimitedError`` and ``RefreshCooldownError`` are both
    ``RateLimitedError``. Everything else, transport and station errors included,
    means eufy could not be reached or could not finish.
    """
    if isinstance(err, LoginChallengeError):
        return ERROR_LOGIN_CHALLENGE
    if isinstance(err, AuthenticationError):
        return ERROR_INVALID_AUTH
    if isinstance(err, RateLimitedError):
        return ERROR_LOGIN_LIMITED
    if isinstance(err, SessionReplacedError):
        return ERROR_SESSION_REPLACED
    return ERROR_CANNOT_CONNECT


# ── repair issues ────────────────────────────────────────────────────────────


def session_replaced_issue_id(entry_id: str) -> str:
    """The account's session-replaced issue: one per entry."""
    return f"{ISSUE_SESSION_REPLACED}_{entry_id}"


def login_limited_issue_id(entry_id: str) -> str:
    """The account's login-limited issue, with or without a known wait: one per entry."""
    return f"{ISSUE_LOGIN_LIMITED}_{entry_id}"


def key_rejected_issue_id(entry_id: str, device_id: str) -> str:
    """A station's key-rejected issue, by its device-registry id, never its serial."""
    return f"{ISSUE_KEY_REJECTED}_{entry_id}_{device_id}"


def account_label(entry: ConfigEntry[Any]) -> str:
    """How repair text names the account: the entry title, never an e-mail address.

    The config flow titles an entry with its e-mail address, and repair text is
    shown in the UI and exported with diagnostics. A title that is (or holds) an
    address is shown in the library's redacted form; a title the user renamed is
    shown as it is.
    """
    title = entry.title
    email = str(entry.data.get(CONF_EMAIL, ""))
    if "@" in title or (email and title.strip().lower() == email.strip().lower()):
        return redact(title)
    return title


def media_not_persistent_issue_id(entry_id: str) -> str:
    """The id of an entry's issue for an event history the container does not keep."""
    return f"{ISSUE_MEDIA_NOT_PERSISTENT}_{entry_id}"


def sync_media_not_persistent_issue(
    hass: HomeAssistant, entry: ConfigEntry[Any], path: str | None
) -> None:
    """Show the issue naming ``path`` when it is not kept; ``None`` withdraws it."""
    issue_id = media_not_persistent_issue_id(entry.entry_id)
    if path is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_MEDIA_NOT_PERSISTENT,
        translation_placeholders={"path": path},
        learn_more_url=MEDIA_DOCS_URL,
    )


def raise_session_replaced_issue(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """The account's fixable session-replaced issue.

    Idempotent: ``async_create_issue`` updates an issue that exists. Raised from a
    ``SessionReplacedError`` met at setup and from setup's check of the library's
    persisted latch alike, so a latched store always shows its
    issue whatever path the login took. It logs in nowhere: the issue's fix and
    Reconfigure are the user's two deliberate paths to a new session.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        session_replaced_issue_id(entry.entry_id),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_SESSION_REPLACED,
        translation_placeholders={"account": account_label(entry)},
    )


def raise_cloud_issue(hass: HomeAssistant, entry: ConfigEntry[Any], err: CloudError) -> None:
    """Turn a kick-out or a throttle into the account's repair issue.

    - ``SessionReplacedError``: the fixable issue of :func:`raise_session_replaced_issue`.
      Only its fix logs in again, after the user confirms, because two clients on
      one account that each took the session back by themselves would kick each
      other out into the login lock.
    - ``RefreshCooldownError``: nothing. It is the library's own spacing of key
      refreshes, not eufy refusing the account. Checked before its base class.
    - ``RateLimitedError``, ``LoginLimitedError`` included: a non-fixable issue with
      the wait in whole minutes when eufy gave one. A plain request hold-off is
      shown like a login limit. The next successful login clears it.
    """
    if isinstance(err, SessionReplacedError):
        raise_session_replaced_issue(hass, entry)
    elif isinstance(err, RefreshCooldownError):
        return
    elif isinstance(err, RateLimitedError):
        if err.retry_after is not None:
            translation_key = ISSUE_LOGIN_LIMITED
            placeholders = {
                "account": account_label(entry),
                "minutes": str(max(1, math.ceil(err.retry_after / 60))),
            }
        else:
            translation_key = ISSUE_LOGIN_LIMITED_NO_WAIT
            placeholders = {"account": account_label(entry)}
        ir.async_create_issue(
            hass,
            DOMAIN,
            login_limited_issue_id(entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders=placeholders,
        )


def clear_session_replaced_issue(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """A successful authenticated call proves eufy accepts the session: that issue goes.

    Only the account's session-replaced issue, and nothing else. The login-limited
    issue is left to :func:`clear_cloud_issues`, which alone can read whether a real
    login happened; an authenticated read with the cached token proves nothing about
    eufy's sign-in limit. Deleting an issue that does not exist is a no-op.
    """
    ir.async_delete_issue(hass, DOMAIN, session_replaced_issue_id(entry.entry_id))


def clear_cloud_issues(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    *,
    before: CloudStatus,
    after: CloudStatus,
) -> None:
    """A login returned: withdraw what it proves is over, and only that.

    ``before`` and ``after`` are the library's cloud status around the login;
    reading it never contacts the cloud.

    - The session-replaced issue always goes: the library checks its latch before
      it returns from any login, the cache-only one included.
    - The login-limited issue goes when the login really signed in, which
      ``CloudStatus`` answers by ``logins_in_window`` rising across the call, or
      when the library holds nothing against a login any more
      (``next_login_allowed_in`` is 0: no request or login hold-off, no spent
      budget). A warm-cache login returns without contacting eufy and without
      checking any hold-off, so on its own it proves nothing about the limit, and
      the user keeps the warning while eufy is still refusing sign-ins.
    """
    ir.async_delete_issue(hass, DOMAIN, session_replaced_issue_id(entry.entry_id))
    signed_in = after.logins_in_window > before.logins_in_window
    if signed_in or after.next_login_allowed_in == 0:
        ir.async_delete_issue(hass, DOMAIN, login_limited_issue_id(entry.entry_id))


def route_cloud_problem(
    hass: HomeAssistant, entry: ConfigEntry[Any], problem: CloudProblem
) -> None:
    """A background cloud failure, routed to reauth or a repair issue.

    - ``CipherUnavailableError`` with a station: that station's cipher-unavailable
      issue (:func:`raise_cipher_unavailable_issue`).
    - ``AuthenticationError``: the reauth flow, through
      ``async_start_reauth_if_available`` (the path setup's ``ConfigEntryAuthFailed``
      takes too), which starts at most one flow per entry and nothing for a config
      flow without a reauth step.
    - ``SessionReplacedError`` and ``RateLimitedError`` (``LoginLimitedError`` and
      ``RefreshCooldownError`` included): :func:`raise_cloud_issue`, which leaves
      the cooldown alone.
    - Any other ``CloudError``: logged at DEBUG by type only. The station's own
      ``ConnectionChanged`` already drives its availability.

    All but the first go through :func:`route_cloud_error`. Nothing here logs in or
    calls the cloud. The library emits each error type once until a cloud call
    succeeds, so a problem cannot flood the registry.
    """
    if isinstance(problem.error, CipherUnavailableError) and problem.station_sn:
        raise_cipher_unavailable_issue(hass, entry, problem.station_sn, problem.error.cipher_id)
        return
    route_cloud_error(hass, entry, problem.error)


def route_cloud_error(hass: HomeAssistant, entry: ConfigEntry[Any], err: EufySecurityError) -> None:
    """One cloud failure routed to reauth or a repair issue; see :func:`route_cloud_problem`.

    ``AuthenticationError`` starts the reauth flow when available,
    ``SessionReplacedError`` and ``RateLimitedError`` go to :func:`raise_cloud_issue`,
    and anything else is logged at DEBUG by type only. Nothing here signs in.

    ``KeyExchangeRefusedError`` is one of the "anything else": per the library guide,
    carry on from the cache and retry on the next interval (the library re-keys at
    each attempt); no login helps. No issue: a persistent refusal has no measured
    threshold to raise one at.
    """
    if isinstance(err, AuthenticationError):
        entry.async_start_reauth_if_available(hass)
    elif isinstance(err, (SessionReplacedError, RateLimitedError)):
        raise_cloud_issue(hass, entry, err)
    else:
        _LOGGER.debug("background eufy cloud call failed: %s", type(err).__name__)


def device_list_refresh_failed(
    hass: HomeAssistant, entry: ConfigEntry[Any], err: EufySecurityError
) -> HomeAssistantError:
    """A "Refresh device list" press whose cloud fetch raised; never retried.

    The error is routed first through :func:`route_cloud_error` (reauth, or the
    account's repair issue). The returned error has no placeholder and no library
    text. An unreachable cloud does not raise: the library falls back to the cached
    list, so such a press changes nothing.
    """
    route_cloud_error(hass, entry, err)
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=EXC_DEVICE_LIST_REFRESH_FAILED
    )


def capture_in_progress() -> HomeAssistantError:
    """A capture press while the library holds the camera for another capture.

    Raised before anything is sent. The busy rule is the library's
    (``DeviceBusyError``, one capture per camera); the integration only translates
    it, as a toast rather than an ``UpdateFailed``, so no entity goes unavailable.
    Never the library's text.
    """
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_CAPTURE_IN_PROGRESS)


def preset_not_set(index: int) -> ServiceValidationError:
    """A preset capture for a slot the last read showed unset; nothing was sent.

    The library would refuse it too (``UnsupportedError`` before sending); asking
    first keeps the toast synchronous. The placeholder is the slot index only.
    """
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=EXC_PRESET_NOT_SET,
        translation_placeholders={"index": str(index)},
    )


def presets_unsupported() -> ServiceValidationError:
    """A preset action on a camera whose model has no presets; nothing sent."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=EXC_PRESETS_UNSUPPORTED
    )


def default_preset_needs_confirmation() -> HomeAssistantError:
    """A default-preset write the camera refused with -502, asking to confirm; not changed.

    The app answers that question with a dialog; nothing here answers it for the user.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=EXC_DEFAULT_PRESET_NEEDS_CONFIRMATION
    )


def pan_tilt_not_applied() -> HomeAssistantError:
    """A pan/tilt step the camera refused (``CommandError``); it did not move."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_PAN_TILT_NOT_APPLIED)


def pan_tilt_unsupported() -> ServiceValidationError:
    """A pan/tilt, preset or zoom action on a camera whose model cannot pan/tilt; nothing sent."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=EXC_PAN_TILT_UNSUPPORTED
    )


def zoom_unsupported() -> ServiceValidationError:
    """The zoom action on a pan/tilt camera without zoom or a live stream; nothing sent."""
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=EXC_ZOOM_UNSUPPORTED)


def ptz_command_not_handled() -> HomeAssistantError:
    """A camera command the station refused as one it does not handle (receipt -108).

    The station sent it on its way and answered code -108; nothing changed on the
    camera, and repeating it changes nothing either.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=EXC_PTZ_COMMAND_NOT_HANDLED
    )


def zoom_needs_single_view() -> HomeAssistantError:
    """A zoom the library refused because the camera is in dual view; nothing sent."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_ZOOM_NEEDS_SINGLE_VIEW)


def presets_full(slots: int) -> HomeAssistantError:
    """A save into a camera that already stores ``slots`` presets; delete one first."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=EXC_PRESETS_FULL,
        translation_placeholders={"slots": str(slots)},
    )


def preset_not_saved() -> HomeAssistantError:
    """A save the camera took or refused without the read-back showing the slot stored."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_PRESET_NOT_SAVED)


def preset_saved_not_default() -> HomeAssistantError:
    """A save with make_default whose store took but whose default write did not."""
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=EXC_PRESET_SAVED_NOT_DEFAULT
    )


def preset_not_deleted(index: int) -> HomeAssistantError:
    """A delete the camera took or refused while the read-back still shows slot ``index``."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=EXC_PRESET_NOT_DELETED,
        translation_placeholders={"index": str(index)},
    )


def preset_slot_unknown(index: int) -> ServiceValidationError:
    """A save or delete naming a slot the camera does not have; nothing sent."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=EXC_PRESET_SLOT_UNKNOWN,
        translation_placeholders={"index": str(index)},
    )


def recording_in_progress() -> ServiceValidationError:
    """A record action while the camera's last one still runs; nothing was started."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=EXC_RECORDING_IN_PROGRESS
    )


def recording_needs_history() -> ServiceValidationError:
    """A record action with the event history off: a clip has nowhere to be stored."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=EXC_RECORDING_NEEDS_HISTORY
    )


def recording_unsupported() -> ServiceValidationError:
    """A record action on a camera whose model has no live stream; nothing was opened."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=EXC_RECORDING_UNSUPPORTED
    )


def recording_failed_to_capture(err: EufySecurityError) -> HomeAssistantError:
    """A live capture the camera did not deliver, as a translated error.

    Past the sessions per HomeBase (``LiveStreamLimitError``) names the live-stream
    limit; any other library error (``CameraWakeError``, ``DeviceTimeoutError``, ...)
    is the camera being unavailable. Never the library's text.
    """
    if isinstance(err, LiveStreamLimitError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=EXC_LIVE_STREAM_LIMIT,
            translation_placeholders={"limit": str(err.limit)},
        )
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_CAMERA_UNAVAILABLE)


def recording_failed() -> HomeAssistantError:
    """A clip the camera delivered that could not be stored (ffmpeg or the media folder)."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=EXC_RECORDING_FAILED)


def credentials_refreshed_issue_id(entry_id: str, device_id: str) -> str:
    """A station's credentials-refreshed notice, by its device-registry id."""
    return f"{ISSUE_CREDENTIALS_REFRESHED}_{entry_id}_{device_id}"


def station_label(device: dr.BaseDeviceEntry) -> str:
    """How repair text names a station: the name the user gave it, else its own name."""
    return device.name_by_user or device.name or ""


def _station_device(
    hass: HomeAssistant, entry: ConfigEntry[Any], serial: str
) -> dr.DeviceEntry | None:
    """The station's registered device; its id, never the serial, keys its issues.

    Scoped to the entry: the device registry does not treat identifiers as unique
    across config entries.
    """
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)


def raise_key_rejected_issue(hass: HomeAssistant, entry: ConfigEntry[Any], serial: str) -> None:
    """One fixable issue for a station that rejects even its re-fetched key.

    The library fetches a rejected key again at most once and then latches, so no
    further cloud call follows by itself; the issue's fix is the user's way to allow
    one more fetch. The issue id is per device, so raising it again for the same
    station changes nothing.
    """
    device = _station_device(hass, entry, serial)
    if device is None:
        _LOGGER.debug(
            "HomeBase %s has no registered device; no key-rejected issue", redact_serial(serial)
        )
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        key_rejected_issue_id(entry.entry_id, device.id),
        data={"entry_id": entry.entry_id, "device_id": device.id},
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_KEY_REJECTED,
        translation_placeholders={"station": station_label(device)},
    )


def raise_key_rejected_issues(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    stations: Iterable[Station],
    start_errors: Mapping[str, EufySecurityError],
) -> None:
    """The key-rejected issue for every station either source names.

    A station's rejection can be in the ``async_start`` result, or only in
    ``Station.last_error``: a station that started but whose first refresh failed
    on a rejected key is absent from the start result. Every station is checked,
    and a station named by both sources still has one issue.
    """
    for station in stations:
        if isinstance(start_errors.get(station.serial), KeyRejectedError) or isinstance(
            station.last_error, KeyRejectedError
        ):
            raise_key_rejected_issue(hass, entry, station.serial)


def clear_station_issues(hass: HomeAssistant, entry: ConfigEntry[Any], serial: str) -> None:
    """The station is connected: its key is accepted, so its key issues go.

    Both the key-rejected and the cipher-unavailable issue are deleted. The
    credentials-refreshed notice stays: it is emitted about a second before the
    reconnect it enables, so clearing it here would delete it unseen.
    """
    device = _station_device(hass, entry, serial)
    if device is not None:
        ir.async_delete_issue(hass, DOMAIN, key_rejected_issue_id(entry.entry_id, device.id))
        ir.async_delete_issue(hass, DOMAIN, cipher_unavailable_issue_id(entry.entry_id, device.id))


def cipher_unavailable_issue_id(entry_id: str, device_id: str) -> str:
    """A station's cipher-unavailable issue, by its device-registry id."""
    return f"{ISSUE_CIPHER_UNAVAILABLE}_{entry_id}_{device_id}"


def raise_cipher_unavailable_issue(
    hass: HomeAssistant, entry: ConfigEntry[Any], serial: str, cipher_id: int
) -> None:
    """One issue for a station whose key the eufy cloud does not hold under its owner.

    ``CipherUnavailableError``: the cloud answered with no key for the cipher the
    station named. Retrying does not change that answer, and the library asks again
    at most once an hour per client, so no fix flow is offered: the share or the
    station's binding needs the owner. Not fixable and not persistent; the id is per
    device, so a repeat changes nothing. Cleared when the station connects, and with
    the entry's unload, since a reload asks the cloud once more.
    """
    device = _station_device(hass, entry, serial)
    if device is None:
        _LOGGER.debug(
            "Station %s has no registered device; no cipher-unavailable issue",
            redact_serial(serial),
        )
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        cipher_unavailable_issue_id(entry.entry_id, device.id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_CIPHER_UNAVAILABLE,
        translation_placeholders={"station": station_label(device), "cipher": str(cipher_id)},
    )


def raise_cipher_unavailable_issue_from(
    hass: HomeAssistant, entry: ConfigEntry[Any], station: Station
) -> None:
    """The cipher-unavailable issue for ``station`` when its last error is one.

    For ``ConnectionChanged(CREDENTIALS_UNAVAILABLE)``, which the library emits for
    every station on each failed attempt (setup's first start included) and which
    carries no error. ``CloudProblem`` carries the error once per error type per
    client, so a second station failing the same way is found only here.
    """
    err = station.last_error
    if isinstance(err, CipherUnavailableError):
        raise_cipher_unavailable_issue(hass, entry, station.serial, err.cipher_id)


def raise_credentials_refreshed_notice(
    hass: HomeAssistant, entry: ConfigEntry[Any], event: CredentialsRefreshed
) -> None:
    """A persistent notice that a station's key or owner id was fetched again.

    Every automatic fetch leaves a record, and says whether it cost one of the
    account's few daily sign-ins. Not fixable: the user reads and dismisses it.
    """
    device = _station_device(hass, entry, event.station_sn)
    if device is None:
        _LOGGER.debug(
            "HomeBase %s has no registered device; no credentials notice",
            redact_serial(event.station_sn),
        )
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        credentials_refreshed_issue_id(entry.entry_id, device.id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=(
            ISSUE_CREDENTIALS_REFRESHED_LOGIN if event.login else ISSUE_CREDENTIALS_REFRESHED
        ),
        translation_placeholders={"station": station_label(device)},
    )


def account_id_mismatch_issue_id(entry_id: str, device_id: str) -> str:
    """A station's account-id-mismatch issue, by its device-registry id."""
    return f"{ISSUE_ACCOUNT_ID_MISMATCH}_{entry_id}_{device_id}"


def raise_account_mismatch_issue(hass: HomeAssistant, entry: ConfigEntry[Any], serial: str) -> None:
    """One issue for a station that stamps its records with another owner account.

    Such a station silently drops a command carrying an account id it does not
    recognise, so arming can seem to work and do nothing: an ERROR. The library's
    ``AccountMismatch`` carries neither id, and the issue has no data and only the
    station's name as a placeholder. Nothing is adopted or changed; the issue asks the
    user to check which account serves the station.

    Not fixable and not persistent. The id is per device, so the library's next emit
    (at most once per connection) changes nothing, and a reconnect does not clear it;
    unloading the entry deletes it, so a reload decides afresh.
    """
    device = _station_device(hass, entry, serial)
    if device is None:
        _LOGGER.debug(
            "HomeBase %s has no registered device; no account mismatch issue",
            redact_serial(serial),
        )
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        account_id_mismatch_issue_id(entry.entry_id, device.id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_ACCOUNT_ID_MISMATCH,
        translation_placeholders={"station": station_label(device)},
    )


def delete_reload_scoped_issues(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Delete the entry's account-id-mismatch, cipher-unavailable and no-devices issues.

    Unload, the start of every setup attempt and entry removal call it, so an issue
    raised by an attempt that then failed (and so was never unloaded) does not
    outlive it. Found by the entry's issue id prefixes in the issue registry rather
    than through the device registry, whose rows can already be gone at removal. A
    later setup then decides afresh from the stations' own stamps and keys and the
    cached device list. Logs nothing.
    """
    prefixes = (
        account_id_mismatch_issue_id(entry.entry_id, ""),
        cipher_unavailable_issue_id(entry.entry_id, ""),
        no_devices_issue_id(entry.entry_id),
    )
    stale = [
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith(prefixes)
    ]
    for issue_id in stale:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


def no_devices_issue_id(entry_id: str) -> str:
    """The account's no-devices issue: one per entry."""
    return f"{ISSUE_NO_DEVICES}_{entry_id}"


def sync_no_devices_issue(
    hass: HomeAssistant, entry: ConfigEntry[Any], regions: Mapping[str, RegionStatus]
) -> None:
    """Show the account's no-devices issue while every cloud region is suspended.

    A region is suspended when its last device list was empty; the library asks it
    again only on a fetch with every region (the entry's region option). Non-fixable:
    the remedy is the option and a device-list refresh. No region listed yet (an
    empty mapping) withdraws the issue.
    """
    issue_id = no_devices_issue_id(entry.entry_id)
    if not regions or not all(status.suspended for status in regions.values()):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NO_DEVICES,
        translation_placeholders={
            "account": account_label(entry),
            "regions": ", ".join(sorted(regions)),
        },
    )


def push_not_running_issue_id(entry_id: str) -> str:
    """The account's push-not-running issue: one per entry."""
    return f"{ISSUE_PUSH_NOT_RUNNING}_{entry_id}"


def sync_push_issue(hass: HomeAssistant, entry: ConfigEntry[Any], *, running: bool) -> None:
    """Show the account's push-not-running issue while push is not listening.

    Non-fixable: the library retries by itself, and ``running`` True withdraws it. The
    only placeholder is the account's label. The failure itself is never shown: a
    cloud failure behind it is the account's own ``CloudProblem`` route, and the
    library's text is not passed on.
    """
    issue_id = push_not_running_issue_id(entry.entry_id)
    if running:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_PUSH_NOT_RUNNING,
        translation_placeholders={"account": account_label(entry)},
    )
