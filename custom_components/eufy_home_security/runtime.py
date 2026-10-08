"""The one place a ``EufySecurity`` is built, and what a loaded entry owns.

This is the only module that imports ``EufySecurity``. The config flow and
``async_setup_entry`` both reach the library through :func:`build_client`, always
as ``runtime.build_client(...)``: the tests replace that module attribute, so a
caller that imported the function by name would bypass the replacement and build
a real client. The config flow's second entry point here is
:func:`async_login_with_saved_password`, the one forced login the flow performs:
a helper that takes the client belongs in this module, because
no other module may name the client type; so does :class:`PendingVerification`, the
client a sign-in waiting for a two-step code keeps open; and :func:`async_run_push`, the
cloud push start that setup runs in the background. The integration never writes the
library's cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    DEFAULT_STATION_SESSIONS,
    MIN_STATION_SESSIONS,
    STATION_SESSION_LIMIT,
    CloudInvite,
    EufySecurity,
    EufySecurityError,
    LoginNeed,
    RateLimitedError,
    StationClaims,
)

from .const import (
    CONF_COUNTRY,
    CONF_EXTRA_COUNTRIES,
    CONF_STATION_SESSIONS,
    DOMAIN,
    PUSH_START_RETRY_MAX_SECONDS,
    PUSH_START_RETRY_MIN_SECONDS,
)

if TYPE_CHECKING:
    from .coordinator import StationCoordinator
    from .events import EventRouter
    from .history import EventHistory
    from .presets import PresetManager
    from .recordings import RecordingManager
    from .snapshots import SnapshotManager
    from .station_recordings import StationRecordings
    from .storage import StorageCoordinator
    from .streaming import StreamManager

_LOGGER = logging.getLogger(__name__)

CLAIMS: HassKey[StationClaims] = HassKey(f"{DOMAIN}_claims")
# Entries whose next setup asks every login scope once (the login country changed).
RESCAN_AT_SETUP: HassKey[set[str]] = HassKey(f"{DOMAIN}_rescan_at_setup")

_STORE_VERSION = 1


def store_key(email: str) -> str:
    """The storage key of one eufy account's cache document.

    A sha256 prefix of the normalised e-mail, so the address itself never appears
    in a ``.storage`` file name.
    """
    account = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    return f"{DOMAIN}.{account}"


def cache_store(hass: HomeAssistant, email: str) -> Store[dict[str, Any]]:
    """The account's private store, shared by the config flow and the entry.

    Keyed by the account rather than the entry id: the flow has no entry id yet,
    and an account key lets setup reuse the session the flow's login cached.
    ``private=True`` because the document holds the password, the session token
    and the station keys; ``atomic_writes=True`` because HA's default is a plain
    overwrite, and a crash mid-write would lose the session and the install's
    identity.
    """
    return Store(hass, _STORE_VERSION, store_key(email), private=True, atomic_writes=True)


def login_country(hass: HomeAssistant, options: Mapping[str, Any]) -> str:
    """The country the account logs in with: the entry's option, else Home Assistant's.

    Empty when neither is set; the library then uses the country of the host's IP.
    """
    option = options.get(CONF_COUNTRY)
    if isinstance(option, str) and option:
        return option
    return hass.config.country or ""


def login_countries(hass: HomeAssistant, options: Mapping[str, Any]) -> str | list[str]:
    """What the library's ``country`` takes: the login country, then each extra one.

    A single country (or ``""``) as a string; duplicates and empty codes are left out.
    """
    extras = options.get(CONF_EXTRA_COUNTRIES)
    codes = [login_country(hass, options)]
    if isinstance(extras, list):
        codes += [code for code in extras if isinstance(code, str)]
    unique = [code for index, code in enumerate(codes) if code and code not in codes[:index]]
    if len(unique) > 1:
        return unique
    return unique[0] if unique else ""


def request_rescan_at_setup(hass: HomeAssistant, entry_id: str) -> None:
    """Make the entry's next setup ask every login scope once (a changed login country)."""
    hass.data.setdefault(RESCAN_AT_SETUP, set()).add(entry_id)


def take_rescan_at_setup(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether this setup asks every login scope; the request is used up."""
    pending = hass.data.get(RESCAN_AT_SETUP)
    if pending is None or entry_id not in pending:
        return False
    pending.discard(entry_id)
    return True


async def async_pending_invites(eufy: EufySecurity) -> list[CloudInvite] | None:
    """The invitations the account has not accepted, or None when they cannot be read.

    Login-free (only scopes with a session are asked); a cloud error is logged at
    debug, by type only, and leaves the caller's issue as it is.
    """
    try:
        return await eufy.async_pending_invites()
    except EufySecurityError as err:
        _LOGGER.debug("Pending eufy invitations not read: %s", type(err).__name__)
        return None


def station_claims(hass: HomeAssistant) -> StationClaims:
    """The one claim registry of this Home Assistant instance, created on first use.

    When the library moves a station between accounts it names the account whose
    stations changed; that is the normalised e-mail, which is exactly the entry's
    unique id, so the entry is found and reloaded.
    """
    claims = hass.data.get(CLAIMS)
    if claims is None:

        def _on_change(account: str) -> None:
            entry = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, account)
            if entry is not None:
                hass.config_entries.async_schedule_reload(entry.entry_id)

        claims = StationClaims(_on_change)
        hass.data[CLAIMS] = claims
    return claims


def build_client(
    hass: HomeAssistant,
    email: str,
    password: str | None,
    *,
    claims: StationClaims | None = None,
    max_sessions: int = DEFAULT_STATION_SESSIONS,
    scan_regions: bool = False,
    country: str | Sequence[str] = "",
) -> EufySecurity:
    """Build a ``EufySecurity`` on the account store: the single construction site.

    Call it as ``runtime.build_client(...)``, never through a name imported from
    this module, because the tests patch this module attribute.

    ``password`` is what the user typed in a flow, and ``None`` at entry setup,
    where the library uses the password its last successful login cached.
    ``claims`` is passed only by entry setup: a short-lived flow instance must not
    claim stations, because releasing a claim on close could reload another
    account's entry. No station inclusion map and no shared install
    state. ``max_sessions`` is every station's session budget
    (:func:`session_budget`); a flow's short-lived client keeps the library default.
    ``scan_regions`` makes every device-list fetch ask every cloud region (the
    entry's option); a flow's client keeps the library default. ``country`` is the
    login country, or it and the extra ones (:func:`login_countries`); every client
    sends Home Assistant's time zone, as the eufy app sends the phone's.
    """
    return EufySecurity(
        async_get_clientsession(hass),
        email,
        password,
        store=cache_store(hass, email),
        claims=claims,
        max_sessions=max_sessions,
        scan_regions=scan_regions,
        country=country,
        timezone=hass.config.time_zone,
    )


def session_budget(options: Mapping[str, Any]) -> int:
    """The entry's sessions-per-HomeBase option as a budget the library takes.

    The library default when unset, and for a stored value outside the library's
    range (a hand edit or another version), so a bad value never breaks setup.
    """
    value = options.get(CONF_STATION_SESSIONS)
    if isinstance(value, int) and MIN_STATION_SESSIONS <= value <= STATION_SESSION_LIMIT:
        return value
    return DEFAULT_STATION_SESSIONS


def apply_session_budget(entry: EufyConfigEntry, budget: int) -> None:
    """Give every HomeBase of a loaded entry ``budget`` sessions, without a reload.

    A lower budget ends no running stream; a higher one serves live opens waiting for
    a session at once. A standalone camera keeps its one stream and is left alone.
    """
    for coordinator in entry.runtime_data.coordinators.values():
        station = coordinator.station
        if not station.is_standalone:
            station.max_sessions = budget
    _LOGGER.debug("Session budget per HomeBase set to %d (%d live streams)", budget, budget - 1)


async def async_login_with_saved_password(eufy: EufySecurity, *, take_over: bool) -> bool:
    """One real login with the password the account store holds, whatever session it caches.

    This is the call the ``session_replaced`` repair makes, and the config flow's
    reconfigure step for a submit with the password field left empty. ``force=True``
    performs the login even on a warm cache, which the plain ``async_login`` would
    answer from the cache without contacting eufy.

    ``force`` also takes a replaced session back, and there is no way to force a
    login and still refuse a take-over. So the latch is read first, through
    ``async_cloud_status`` (the cache only, never the cloud): unless ``take_over``
    is True, while it is set the function returns False without sending anything,
    so the caller can ask the user. The library releases the latch only once the
    take-over's login succeeds, so a failed take-over leaves the store latched, the
    repair issue stays, and the next setup raises on the latch before any I/O instead
    of signing in by itself with the cached password.
    True means the login ran; a refusal raises the library's error as it is.

    It lives here rather than in the flow for two reasons. Only this module may
    name the client type (``test_eufy_security_is_built_only_in_runtime``).
    And the reauth gate ``test_reauth_never_uses_the_cached_login_path`` forbids the
    flow's reauth methods from calling ``async_login`` at all, which is right for
    the cached path a plain call would take; this is the one call of it that never
    takes the cached path. There is no retry: one call per user submit.
    """
    status = await eufy.async_cloud_status()
    if status.login_need is LoginNeed.REPLACED and not take_over:
        return False
    await eufy.async_login(force=True)
    return True


@dataclass
class PendingVerification:
    """A sign-in waiting for the user's two-step verification code.

    Holds the client that met the challenge, open between the flow's steps: the library
    sends the answer to the region whose login asked, and only that instance knows it.
    ``reauthenticate_with`` is a typed password the flow is checking (answered through
    ``async_reauthenticate``); None answers through ``async_login`` with the password
    the client already holds (the typed one of a new account, else the saved one).
    ``take_over`` is the flow's take-over choice for this sign-in.
    """

    eufy: EufySecurity
    login_id: str
    code_requested: bool
    reauthenticate_with: str | None
    take_over: bool

    async def async_answer(self, verify_code: str) -> None:
        """One sign-in carrying ``verify_code``; the library's error is raised as it is.

        A wrong or expired code raises ``LoginChallengeError`` again.
        """
        if self.reauthenticate_with is not None:
            await self.eufy.async_reauthenticate(
                self.reauthenticate_with,
                verify_code=verify_code,
                login_id=self.login_id,
                take_over=self.take_over,
            )
        else:
            await self.eufy.async_login(
                verify_code=verify_code, login_id=self.login_id, force=self.take_over
            )

    async def async_close(self) -> None:
        """Close the held client, which saves the account store."""
        await self.eufy.async_close()


async def async_run_push(eufy: EufySecurity) -> None:
    """Start the cloud push listener, and start it again until it listens.

    For a background task of setup, after the platforms are forwarded: a start can
    take the library's whole deadline. ``async_start`` never raises for push. Once
    the listener has listened, the library supervises it and restarts it by itself;
    a first start that failed is not retried by the library, so it is retried here,
    with a doubling wait from ``PUSH_START_RETRY_MIN_SECONDS`` to
    ``PUSH_START_RETRY_MAX_SECONDS``, never sooner than a cloud hold-off allows. The
    entry's unload cancels the task. No local session is started here.
    """
    delay = PUSH_START_RETRY_MIN_SECONDS
    while True:
        await eufy.async_start(p2p=False, push=True)
        if eufy.push_running:
            return
        error = eufy.push_error
        hold_off = error.retry_after if isinstance(error, RateLimitedError) else None
        wait = max(delay, hold_off or 0.0)
        _LOGGER.debug(
            "Cloud push did not start (%s); next attempt in %.0f s",
            type(error).__name__ if error is not None else "no error",
            wait,
        )
        await asyncio.sleep(wait)
        delay = min(delay * 2, PUSH_START_RETRY_MAX_SECONDS)


def streaming(entry: EufyConfigEntry) -> StreamManager | None:
    """The entry's live-stream manager, or ``None`` before setup has built it.

    For the entity modules, and for the same reason as :func:`session_replaced`:
    they may not name the manager's module (a platform importing ``streaming`` would
    break the one-directional module graph the boundary gate enforces), and a setting
    can in principle be written while ``runtime_data`` is still being assembled.
    ``None`` then, and the caller does nothing.
    """
    return getattr(entry.runtime_data, "streaming", None)


@dataclass(slots=True)
class EufyRuntimeData:
    """What one loaded config entry owns: the client, the coordinators per station, the router.

    ``coordinators`` poll each station's parameter dump; ``storage`` reads each
    station's storage record on its own schedule. Both are keyed by station serial.
    ``snapshots`` holds every camera's latest still; ``presets`` the preset
    images of every pan/tilt camera; ``streaming`` the live
    broadcast of every camera the library grades capable of one, each opened only when
    a viewer connects. ``history`` writes every still and clip file; ``recordings``
    copies each HomeBase recording into it, None while the event-videos option is off;
    ``station_recordings`` lists and fetches them on request.
    """

    eufy: EufySecurity
    coordinators: dict[str, StationCoordinator]
    router: EventRouter
    storage: dict[str, StorageCoordinator]
    snapshots: SnapshotManager
    presets: PresetManager
    streaming: StreamManager
    history: EventHistory
    recordings: RecordingManager | None = None
    station_recordings: StationRecordings | None = None


type EufyConfigEntry = ConfigEntry[EufyRuntimeData]
