"""Config flow: add a eufy account, replace its password or sign in again, set its options."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from eufy_home_security import (
    DEFAULT_STATION_SESSIONS,
    IMAGE_SOURCES,
    MIN_STATION_SESSIONS,
    STATION_SESSION_LIMIT,
    EufySecurityError,
    ImageSource,
    LoginChallengeError,
    SessionReplacedError,
    async_forget_account,
)

from . import errors, history, runtime
from .const import (
    CONF_ALARM_TIMEOUT,
    CONF_CAMERA_IMAGE,
    CONF_CLOUD_PUSH,
    CONF_DETECTION_HOLD,
    CONF_EVENT_HISTORY_DAYS,
    CONF_EVENT_VIDEOS,
    CONF_LIVE_SNAPSHOT,
    CONF_RECORD_LENGTH,
    CONF_SCAN_REGIONS,
    CONF_SESSION_PROBE,
    CONF_STATION_SESSIONS,
    CONF_VERIFY_CODE,
    DEFAULT_ALARM_TIMEOUT_MINUTES,
    DEFAULT_CAMERA_IMAGE,
    DEFAULT_DETECTION_HOLD_SECONDS,
    DEFAULT_EVENT_HISTORY_DAYS,
    DEFAULT_RECORD_LENGTH_SECONDS,
    DOMAIN,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_VERIFY_CODE,
    ERROR_SESSION_REPLACED,
    MAX_ALARM_TIMEOUT_MINUTES,
    MAX_DETECTION_HOLD_SECONDS,
    MAX_EVENT_HISTORY_DAYS,
    MAX_RECORD_LENGTH_SECONDS,
    MIN_ALARM_TIMEOUT_MINUTES,
    MIN_DETECTION_HOLD_SECONDS,
    MIN_RECORD_LENGTH_SECONDS,
    OPTIONS_STEP_INIT,
    STEP_REAUTH_CONFIRM,
    STEP_REAUTH_TAKE_OVER,
    STEP_RECONFIGURE,
    STEP_USER,
    STEP_VERIFY_CODE,
    CameraImageMode,
)

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

# Optional on purpose: an empty field means the saved password.
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

VERIFY_CODE_SCHEMA = vol.Schema(
    {vol.Required(CONF_VERIFY_CODE): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))}
)

# The entry's options, in this order: how long a detection sensor stays on after the
# detection's own time; the alarm panel's safety-net timeout; the camera image a
# detection shows; a live keyframe for a camera with no detection image (wakes a
# battery camera); the session probe (one read with the saved session 60 s after start
# and every 6 h, never a sign-in); eufy's cloud push (off by default: the detections
# of a camera without a HomeBase, through eufy's cloud); every cloud region on each
# device-list fetch (off by default: a region that listed no devices may cost a
# sign-in each time); the event-history days; event
# videos (each HomeBase recording copied into the history, off by default); the record
# action's default length; the P2P sessions held to each HomeBase (range and default
# are the library's). Read once at setup, so a change applies on the reload, except the
# recording length and the sessions per HomeBase.
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DETECTION_HOLD, default=DEFAULT_DETECTION_HOLD_SECONDS): NumberSelector(
            NumberSelectorConfig(
                min=MIN_DETECTION_HOLD_SECONDS,
                max=MAX_DETECTION_HOLD_SECONDS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfTime.SECONDS,
            )
        ),
        vol.Required(CONF_ALARM_TIMEOUT, default=DEFAULT_ALARM_TIMEOUT_MINUTES): NumberSelector(
            NumberSelectorConfig(
                min=MIN_ALARM_TIMEOUT_MINUTES,
                max=MAX_ALARM_TIMEOUT_MINUTES,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfTime.MINUTES,
            )
        ),
        vol.Required(CONF_CAMERA_IMAGE, default=DEFAULT_CAMERA_IMAGE.value): SelectSelector(
            SelectSelectorConfig(
                options=[mode.value for mode in CameraImageMode],
                translation_key=CONF_CAMERA_IMAGE,
                mode=SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_LIVE_SNAPSHOT, default=False): BooleanSelector(),
        vol.Required(CONF_SESSION_PROBE, default=True): BooleanSelector(),
        vol.Required(CONF_CLOUD_PUSH, default=False): BooleanSelector(),
        vol.Required(CONF_SCAN_REGIONS, default=False): BooleanSelector(),
        vol.Required(CONF_EVENT_HISTORY_DAYS, default=DEFAULT_EVENT_HISTORY_DAYS): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=MAX_EVENT_HISTORY_DAYS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfTime.DAYS,
            )
        ),
        vol.Required(CONF_EVENT_VIDEOS, default=False): BooleanSelector(),
        vol.Required(CONF_RECORD_LENGTH, default=DEFAULT_RECORD_LENGTH_SECONDS): NumberSelector(
            NumberSelectorConfig(
                min=MIN_RECORD_LENGTH_SECONDS,
                max=MAX_RECORD_LENGTH_SECONDS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfTime.SECONDS,
            )
        ),
        vol.Required(CONF_STATION_SESSIONS, default=DEFAULT_STATION_SESSIONS): NumberSelector(
            NumberSelectorConfig(
                min=MIN_STATION_SESSIONS,
                max=STATION_SESSION_LIMIT,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)
# Options applied to the running entry without a reload: the recording length is read
# at each record action.
_LIVE_OPTIONS = frozenset({CONF_STATION_SESSIONS, CONF_RECORD_LENGTH})


def _camera_image_placeholders() -> dict[str, str]:
    """The camera image description's timings, from the library's image source data.

    The English around them relies on facts tests/test_options_flow.py pins: neither
    source wakes a camera, the trigger frame needs a recording, and the thumbnail is
    not high resolution.
    """
    return {
        "thumbnail_seconds": f"{IMAGE_SOURCES[ImageSource.THUMBNAIL].typical_seconds:g}",
        "hd_seconds": f"{IMAGE_SOURCES[ImageSource.TRIGGER_FRAME].typical_seconds:g}",
    }


def _asks_for_code(err: LoginChallengeError) -> bool:
    """Whether the challenge is eufy's two-step verification code (the flow's own step)."""
    return err.kind == "verify_code"


def _session_placeholders() -> dict[str, str]:
    """The sessions per HomeBase description's numbers, from the library's budget constants."""
    return {
        "min_sessions": str(MIN_STATION_SESSIONS),
        "max_sessions": str(STATION_SESSION_LIMIT),
        "default_sessions": str(DEFAULT_STATION_SESSIONS),
        # The station session carries the first live view; one session is for event images.
        "default_live": str(DEFAULT_STATION_SESSIONS - 1),
    }


class EufyHomeSecurityConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per eufy account, identified by the normalised e-mail."""

    VERSION = 2
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EufyHomeSecurityOptionsFlow:
        """The entry's options flow, which reads the entry through its own property."""
        del config_entry  # the flow reaches it as self.config_entry, after init
        return EufyHomeSecurityOptionsFlow()

    def __init__(self) -> None:
        """Start with no password held, no take-over pending and no entry unloaded by this flow."""
        # The typed password, held only while the take-over confirmation is shown
        # and cleared as soon as that step submits. None means the saved password
        # (a reconfigure submit with the field left empty). Never entry.data, a log
        # line or a form suggestion.
        self._held_password: str | None = None
        # Whether a take-over confirmation is being shown: the guard that says the
        # take-over step was reached through a submit, not opened on its own.
        self._take_over_pending = False
        # Whether this flow unloaded the entry, so a failed attempt reloads it.
        self._unloaded_entry = False
        # A sign-in waiting for the two-step code, its client held open, and the
        # e-mail of the account being added (the user step only).
        self._pending: runtime.PendingVerification | None = None
        self._new_account: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Log in once and cache the session, then create the entry.

        The e-mail is normalised, since it is the entry's unique id and the library's
        account name; the password goes to the library exactly as typed and nowhere
        else. The flow's instance claims no station. The discover puts the device
        list in the account store too, so the entry set up next makes no cloud call.

        Every refusal that can be decided locally comes before the sign-in, because
        each sign-in spends from eufy's small login budget: an empty e-mail, an
        account already set up, and an address the library refuses (``ValueError``)
        build nothing or send nothing. A two-step verification challenge keeps the
        client open and moves to the code step. Any other library error from the
        sign-in becomes a form error through ``errors.flow_error_key``; any other
        exception is a bug and propagates. The re-shown form suggests the e-mail as
        typed, never the password.
        """
        form_errors: dict[str, str] = {}
        schema = CREDENTIALS_SCHEMA
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            if not email:
                form_errors[CONF_EMAIL] = ERROR_INVALID_EMAIL
            else:
                await self.async_set_unique_id(email)
                self._abort_if_unique_id_configured()
                try:
                    eufy = runtime.build_client(
                        self.hass, email, user_input[CONF_PASSWORD], claims=None
                    )
                except ValueError:
                    form_errors[CONF_EMAIL] = ERROR_INVALID_EMAIL
                else:
                    challenge: LoginChallengeError | None = None
                    try:
                        await eufy.async_login()
                        await eufy.async_discover()
                    except LoginChallengeError as err:
                        if _asks_for_code(err):
                            challenge = err
                        else:
                            form_errors["base"] = errors.flow_error_key(err)
                    except EufySecurityError as err:
                        form_errors["base"] = errors.flow_error_key(err)
                    finally:
                        if challenge is None:
                            await eufy.async_close()
                    if challenge is not None:
                        self._new_account = email
                        self._pending = runtime.PendingVerification(
                            eufy,
                            login_id=challenge.login_id,
                            code_requested=challenge.code_requested,
                            reauthenticate_with=None,
                            take_over=False,
                        )
                        return self._verify_code_form({})
                    if not form_errors:
                        return self.async_create_entry(title=email, data={CONF_EMAIL: email})
                    # No entry holds this account, so nothing else would ever forget
                    # what the sign-in cached. Only after the close, which saves
                    # the client's document. The hold-offs and the install identity
                    # stay, so the login budget is not reset; and with no cached
                    # session the next submit checks the typed password with eufy.
                    await async_forget_account(
                        runtime.cache_store(self.hass, email), keep_install_identity=True
                    )
            schema = self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, {CONF_EMAIL: user_input[CONF_EMAIL]}
            )
        return self.async_show_form(step_id=STEP_USER, data_schema=schema, errors=form_errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """eufy rejected the cached password, or none is cached: ask for the current one."""
        del entry_data
        return await self.async_step_reauth_confirm()

    def _flow_entry(self) -> ConfigEntry:
        """The entry this flow is about, by source: each getter raises for the other."""
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return self._get_reauth_entry()

    async def _async_reauthenticate(
        self, entry: ConfigEntry, password: str | None, *, take_over: bool
    ) -> str | None:
        """One real login on the account store, with ``password`` or, for None, the saved one.

        Returns None on success, ``STEP_REAUTH_TAKE_OVER`` when another client holds
        the session and ``take_over`` is False, ``STEP_VERIFY_CODE`` when eufy asks for
        the two-step code (the client stays open in ``self._pending`` and the entry
        stays unloaded), or a form error key.

        Under ``entry.setup_lock`` (which waits out a setup in progress) the entry is
        unloaded first, so two clients never write the same store; an entry that does
        not reach NOT_LOADED is refused before a sign-in is spent. A typed password goes
        through ``async_reauthenticate`` (``async_login`` on a warm cache would not
        check it), the saved one through ``runtime.async_login_with_saved_password``.
        One call per submit, no retry; a failed take-over leaves the session-replaced
        latch set.
        """
        async with entry.setup_lock:
            if entry.state is not ConfigEntryState.NOT_LOADED:
                if not entry.state.recoverable:
                    return ERROR_CANNOT_CONNECT
                await self.hass.config_entries.async_unload(entry.entry_id, _lock=False)
                self._unloaded_entry = True
            if entry.state is not ConfigEntryState.NOT_LOADED:
                # Never two live instances on one store, and no sign-in spent beside one.
                return ERROR_CANNOT_CONNECT
            eufy = runtime.build_client(self.hass, entry.data[CONF_EMAIL], password, claims=None)
            held = False
            try:
                if password is None:
                    if not await runtime.async_login_with_saved_password(eufy, take_over=take_over):
                        return STEP_REAUTH_TAKE_OVER
                else:
                    await eufy.async_reauthenticate(password, take_over=take_over)
            except LoginChallengeError as err:
                if not _asks_for_code(err):
                    return errors.flow_error_key(err)
                held = True
                self._pending = runtime.PendingVerification(
                    eufy,
                    login_id=err.login_id,
                    code_requested=err.code_requested,
                    reauthenticate_with=password,
                    take_over=take_over,
                )
                return STEP_VERIFY_CODE
            except SessionReplacedError:
                return ERROR_SESSION_REPLACED if take_over else STEP_REAUTH_TAKE_OVER
            except EufySecurityError as err:
                return errors.flow_error_key(err)
            finally:
                if not held:
                    await eufy.async_close()
        return None

    async def _async_submit(
        self, entry: ConfigEntry, password: str | None, *, take_over: bool
    ) -> ConfigFlowResult:
        """The one submit path of reauth_confirm, reconfigure and reauth_take_over.

        Success reloads the entry and aborts; Home Assistant picks the reason by
        source, ``reauth_successful`` or ``reconfigure_successful``.
        No ``data=``: the password lives in the
        account store, never in entry.data. When another client holds the session
        the password is held, the entry is brought back (local control while the
        user decides) and the take-over step is shown. That branch cannot occur
        with ``take_over=True``: the runtime helper and the library both take the
        session back then, so the login either runs or fails with a form error. Any
        other outcome brings the entry back and re-shows the source's form.
        """
        outcome = await self._async_reauthenticate(entry, password, take_over=take_over)
        _LOGGER.debug(
            "Sign-in from the %s flow: %s", self.source, "signed in" if outcome is None else outcome
        )
        if outcome is None:
            self._unloaded_entry = False
            return self.async_update_reload_and_abort(entry)
        if outcome == STEP_VERIFY_CODE:
            return self._verify_code_form({})
        if outcome == STEP_REAUTH_TAKE_OVER:
            self._held_password = password
            self._take_over_pending = True
            self._reload_if_unloaded(entry)
            return await self.async_step_reauth_take_over()
        self._reload_if_unloaded(entry)
        return self._credentials_form(entry, {"base": outcome})

    def _reload_if_unloaded(self, entry: ConfigEntry) -> None:
        """Bring back an entry this flow unloaded, so local control does not stay off.

        A set-up, not ``async_schedule_reload``: ``ConfigEntries.async_reload``
        aborts every reauth flow in progress for the entry before it unloads,
        and the task starts
        eagerly, so a reload here would abort this very flow while its form is being
        shown. The entry is NOT_LOADED after this flow's own unload, which is exactly
        the state ``async_setup`` requires, and setting up
        aborts no flow. An entry that left NOT_LOADED meanwhile is left alone.

        ``_abort_reauth_flows`` matches the reauth source only,
        so that argument is about reauth; the set-up
        call is right for the reconfigure source too, because the entry is
        NOT_LOADED after this flow's own unload either way.
        """
        if self._unloaded_entry:
            self._unloaded_entry = False
            if entry.state is ConfigEntryState.NOT_LOADED:
                self.hass.async_create_task(
                    self.hass.config_entries.async_setup(entry.entry_id),
                    f"{DOMAIN} set up again after reauth",
                )

    def _credentials_form(
        self, entry: ConfigEntry, form_errors: dict[str, str]
    ) -> ConfigFlowResult:
        """The source's own password form: reconfigure's or reauth's."""
        if self.source == SOURCE_RECONFIGURE:
            return self._reconfigure_form(entry, form_errors)
        return self._reauth_form(entry, form_errors)

    def _reauth_form(self, entry: ConfigEntry, form_errors: dict[str, str]) -> ConfigFlowResult:
        """The password form, naming the account by its redacted label, never the e-mail.

        ``name`` is set too: without it Home Assistant fills it with the entry title
        (config_entries.py, ``async_show_form``), which is the e-mail address.
        """
        label = errors.account_label(entry)
        return self.async_show_form(
            step_id=STEP_REAUTH_CONFIRM,
            data_schema=REAUTH_SCHEMA,
            errors=form_errors,
            description_placeholders={"account": label, CONF_NAME: label},
        )

    def _reconfigure_form(
        self, entry: ConfigEntry, form_errors: dict[str, str]
    ) -> ConfigFlowResult:
        """The optional-password form, naming the account as ``_reauth_form`` does.

        Never the e-mail, and never a suggested password: the schema is shown as it
        is, so a rejected password is not offered back.
        """
        label = errors.account_label(entry)
        return self.async_show_form(
            step_id=STEP_RECONFIGURE,
            data_schema=RECONFIGURE_SCHEMA,
            errors=form_errors,
            description_placeholders={"account": label, CONF_NAME: label},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in to eufy again, on the user's say-so, from the entry's menu.

        Offered at any time (Home Assistant shows Reconfigure for a flow class that
        defines this step): after another client's login ended Home Assistant's
        session, after a password change, or when the connection to eufy looks stale.
        Opening the form spends nothing. Each submit is one login and never automatic,
        since every sign-in spends from eufy's small login budget: with the field left
        empty, a forced login with the saved password; with a password typed, a check
        of it that saves it in place of the old one on success. Success reloads the
        entry and aborts with ``reconfigure_successful``; a failure re-shows this form
        with the reauth form's error keys and brings the entry back from the cache.
        """
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self._reconfigure_form(entry, {})
        # The frontend omits an empty optional field; a typed-then-cleared one
        # arrives as an empty string. Both mean the saved password.
        password = user_input.get(CONF_PASSWORD) or None
        _LOGGER.debug(
            "Reconfigure: signing in to eufy again with the %s password",
            "saved" if password is None else "typed",
        )
        return await self._async_submit(entry, password, take_over=False)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the typed password with one login, then reload the entry with it cached."""
        entry = self._get_reauth_entry()
        if user_input is None:
            return self._reauth_form(entry, {})
        return await self._async_submit(entry, user_input[CONF_PASSWORD], take_over=False)

    async def async_step_reauth_take_over(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before signing Home Assistant in and the other client out.

        Nothing reaches eufy until this step is submitted, and only then with
        ``take_over=True``: two clients that each took the session back by
        themselves would kick each other out into the login lock.

        Shared by reauth and reconfigure: the entry and the
        abort reason follow the source. Reached on its own, without a pending
        take-over, it goes back to the source's own password step.
        """
        entry = self._flow_entry()
        if not self._take_over_pending:
            if self.source == SOURCE_RECONFIGURE:
                return await self.async_step_reconfigure()
            return await self.async_step_reauth_confirm()
        if user_input is None:
            label = errors.account_label(entry)
            return self.async_show_form(
                step_id=STEP_REAUTH_TAKE_OVER,
                data_schema=vol.Schema({}),
                description_placeholders={"account": label, CONF_NAME: label},
            )
        # Cleared before the login: the password then lives only in this local for
        # the duration of the call.
        password = self._held_password
        self._held_password = None
        self._take_over_pending = False
        _LOGGER.debug(
            "Take-over confirmed: signing in with the %s password and signing the other client out",
            "saved" if password is None else "typed",
        )
        return await self._async_submit(entry, password, take_over=True)

    async def async_step_verify_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for eufy's two-step verification code, then sign in once with it.

        Shared by the user, reauth and reconfigure steps: each moves here when eufy
        answers a correct password with a code challenge, keeping the client that met it
        open (the answer must reach the region that asked). A wrong or expired code
        re-shows this form; any other failure closes the client and returns to the
        source's own form with the error. Reached without a pending sign-in, it goes
        back to the source's first step.
        """
        pending = self._pending
        if pending is None:
            return await self._async_source_start()
        if user_input is None:
            return self._verify_code_form({})
        code = str(user_input.get(CONF_VERIFY_CODE, "")).strip()
        if not code:
            return self._verify_code_form({CONF_VERIFY_CODE: ERROR_INVALID_VERIFY_CODE})
        if self.source == SOURCE_USER:
            return await self._async_answer_new_account(pending, code)
        return await self._async_answer_entry(pending, code)

    async def _async_answer_new_account(
        self, pending: runtime.PendingVerification, code: str
    ) -> ConfigFlowResult:
        """Answer the user step's challenge; on success list the devices and add the entry."""
        email = self._new_account or ""
        try:
            await pending.async_answer(code)
            await pending.eufy.async_discover()
        except LoginChallengeError as err:
            if _asks_for_code(err):
                return self._code_refused(pending, err)
            error = errors.flow_error_key(err)
        except EufySecurityError as err:
            error = errors.flow_error_key(err)
        else:
            await self._async_release_pending()
            return self.async_create_entry(title=email, data={CONF_EMAIL: email})
        await self._async_release_pending()
        # As in the user step: no entry holds this account, so forget what was cached.
        await async_forget_account(
            runtime.cache_store(self.hass, email), keep_install_identity=True
        )
        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, {CONF_EMAIL: email}
            ),
            errors={"base": error},
        )

    async def _async_answer_entry(
        self, pending: runtime.PendingVerification, code: str
    ) -> ConfigFlowResult:
        """Answer a reauth or reconfigure challenge; success reloads the entry and aborts.

        Under ``entry.setup_lock``, and only while the entry is still unloaded, so the
        held client never runs beside the entry's own.
        """
        entry = self._flow_entry()
        outcome: str | None
        async with entry.setup_lock:
            if entry.state is not ConfigEntryState.NOT_LOADED:
                outcome = ERROR_CANNOT_CONNECT
            else:
                try:
                    await pending.async_answer(code)
                    outcome = None
                except LoginChallengeError as err:
                    if _asks_for_code(err):
                        return self._code_refused(pending, err)
                    outcome = errors.flow_error_key(err)
                except SessionReplacedError:
                    outcome = ERROR_SESSION_REPLACED
                except EufySecurityError as err:
                    outcome = errors.flow_error_key(err)
            await self._async_release_pending()
        _LOGGER.debug(
            "Two-step sign-in from the %s flow: %s",
            self.source,
            "signed in" if outcome is None else outcome,
        )
        if outcome is None:
            self._unloaded_entry = False
            return self.async_update_reload_and_abort(entry)
        self._reload_if_unloaded(entry)
        return self._credentials_form(entry, {"base": outcome})

    def _code_refused(
        self, pending: runtime.PendingVerification, err: LoginChallengeError
    ) -> ConfigFlowResult:
        """eufy did not take the code: keep the client and ask again with the new login id."""
        pending.login_id = err.login_id
        pending.code_requested = err.code_requested
        return self._verify_code_form({"base": ERROR_INVALID_VERIFY_CODE})

    def _verify_code_form(self, form_errors: dict[str, str]) -> ConfigFlowResult:
        """The code form; it names no account, so no e-mail address reaches it."""
        return self.async_show_form(
            step_id=STEP_VERIFY_CODE, data_schema=VERIFY_CODE_SCHEMA, errors=form_errors
        )

    async def _async_source_start(self) -> ConfigFlowResult:
        """The first step of this flow's source."""
        if self.source == SOURCE_USER:
            return await self.async_step_user()
        if self.source == SOURCE_RECONFIGURE:
            return await self.async_step_reconfigure()
        return await self.async_step_reauth_confirm()

    async def _async_release_pending(self) -> None:
        """Close the client held for the code; closing saves the account store."""
        pending, self._pending = self._pending, None
        if pending is not None:
            await pending.async_close()

    @callback
    def async_remove(self) -> None:
        """A flow left at the code step: close its client, then undo what the step held.

        Home Assistant calls this whenever the flow ends; after a finished sign-in
        nothing is pending. A new account's store is forgotten (no entry holds it); an
        entry this flow unloaded is set up again.
        """
        pending, self._pending = self._pending, None
        if pending is None:
            return
        entry: ConfigEntry | None = None
        if self.source != SOURCE_USER and self._unloaded_entry:
            entry = self._flow_entry()
            self._unloaded_entry = False
        self.hass.async_create_task(
            self._async_abandon(pending, entry, self._new_account),
            f"{DOMAIN} close an unfinished two-step sign-in",
        )

    async def _async_abandon(
        self,
        pending: runtime.PendingVerification,
        entry: ConfigEntry | None,
        new_account: str | None,
    ) -> None:
        """Close ``pending``, then forget a new account's store or set ``entry`` up again."""
        await pending.async_close()
        if self.source == SOURCE_USER and new_account:
            await async_forget_account(
                runtime.cache_store(self.hass, new_account), keep_install_identity=True
            )
        elif entry is not None and entry.state is ConfigEntryState.NOT_LOADED:
            await self.hass.config_entries.async_setup(entry.entry_id)


class EufyHomeSecurityOptionsFlow(OptionsFlowWithReload):
    """The entry's eleven options, from the detection hold to sessions per HomeBase.

    Three things worth knowing about this class:

    - Home Assistant reloads the entry only when the submitted options differ from
      the stored ones. Every option but the recording length and the sessions per
      HomeBase is read once at platform setup, so that reload is what applies a
      change. A submit that changes only those two is applied to the running entry
      instead, with no reload, so no live view is ended.
    - ``self.config_entry`` must not be read in ``__init__``, where it raises.
      This class therefore defines no ``__init__``
      and holds nothing from the entry.
    - The integration registers no config-entry update listener. Home Assistant
      forbids one beside this class and raises when the flow finishes;
      an AST gate in ``tests/test_packaging.py``
      keeps it that way, so the two reload paths can never both exist.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options, and save exactly the eleven they offer: five bools, five ints, a choice.

        The saved mapping is rebuilt from those eleven keys rather than passing
        ``user_input`` through, so a submission carrying more than the schema asked
        for cannot persist anything else into the entry's options. The selectors
        have already refused a duration outside its range.
        """
        if user_input is not None:
            data: dict[str, Any] = {
                # Rounded, not cut: the selector does not enforce its step, so 9.7 s
                # saves as 10, not 9. Half rounds up. The range was checked on the
                # float and every range has whole-number bounds, so the rounded
                # value stays inside it.
                CONF_DETECTION_HOLD: int(user_input[CONF_DETECTION_HOLD] + 0.5),
                CONF_ALARM_TIMEOUT: int(user_input[CONF_ALARM_TIMEOUT] + 0.5),
                CONF_CAMERA_IMAGE: CameraImageMode(user_input[CONF_CAMERA_IMAGE]).value,
                CONF_LIVE_SNAPSHOT: bool(user_input[CONF_LIVE_SNAPSHOT]),
                CONF_SESSION_PROBE: bool(user_input[CONF_SESSION_PROBE]),
                CONF_CLOUD_PUSH: bool(user_input[CONF_CLOUD_PUSH]),
                CONF_SCAN_REGIONS: bool(user_input[CONF_SCAN_REGIONS]),
                CONF_EVENT_HISTORY_DAYS: int(user_input[CONF_EVENT_HISTORY_DAYS] + 0.5),
                CONF_EVENT_VIDEOS: bool(user_input[CONF_EVENT_VIDEOS]),
                CONF_RECORD_LENGTH: int(user_input[CONF_RECORD_LENGTH] + 0.5),
                CONF_STATION_SESSIONS: int(user_input[CONF_STATION_SESSIONS] + 0.5),
            }
            self._async_apply_live_options(data)
            return self.async_create_entry(data=data)
        return self.async_show_form(
            step_id=OPTIONS_STEP_INIT,
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
            description_placeholders={
                **_camera_image_placeholders(),
                **_session_placeholders(),
                "history_path": str(history.history_dir(self.hass)),
            },
        )

    @callback
    def _async_apply_live_options(self, data: Mapping[str, Any]) -> None:
        """Apply the sessions per HomeBase to a loaded entry; reload only for a change of an
        option read at setup.

        The stored options are compared with their schema defaults filled in, so a
        first save that changes nothing else does not reload either.
        """
        stored = {str(key): key.default() for key in OPTIONS_SCHEMA.schema}
        stored.update(self.config_entry.options)
        others_changed = any(
            data[key] != stored.get(key) for key in data if key not in _LIVE_OPTIONS
        )
        if others_changed:
            return  # the reload builds the client with the new budget
        self.automatic_reload = False
        if self.config_entry.state is ConfigEntryState.LOADED:
            runtime.apply_session_budget(self.config_entry, runtime.session_budget(data))
