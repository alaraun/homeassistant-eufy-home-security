"""Fix flows for the integration's fixable repair issues.

Home Assistant's repairs integration calls :func:`async_create_fix_flow` when the
user opens a fixable issue, and deletes the issue itself when the flow ends in
``create_entry``. An abort leaves the
issue in place.

A fix never builds a second ``EufySecurity``: it uses the loaded entry's own client
(``entry.runtime_data.eufy``), because two live instances on one account store would
overwrite each other's cache (library guide, "One live instance per store"). A fix
on an entry that is not loaded aborts.
"""

from __future__ import annotations

import logging
from typing import Final, cast

import probatio as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from eufy_home_security import (
    CloudInvite,
    EufySecurityError,
    InviteCodeError,
    NoCachedSessionError,
    SessionReplacedError,
    UnsupportedError,
)

from . import (
    async_reset_variant_rows,
    errors,
    runtime,
    variant_reset_key,
    variant_reset_plan,
    variant_reset_summary,
)
from .const import (
    CONF_CLOUD_PUSH,
    DOMAIN,
    ISSUE_KEY_REJECTED,
    ISSUE_PENDING_INVITES,
    ISSUE_PUSH_NEEDED,
    ISSUE_SESSION_REPLACED,
    ISSUE_VARIANT_RESET,
)
from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# The pending-invitations fix's form fields: the chosen invitation, the inviter's code.
_INVITATION: Final = "invitation"
_CODE: Final = "code"


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> EufyConfigEntry | None:
    """The entry, when it exists and is loaded; a fix needs its live client."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        return None
    return cast(EufyConfigEntry, entry)


class SessionReplacedFix(RepairsFlow):
    """Take the eufy session back after another client's login ended it.

    This is one of the two places the integration logs in without a password the
    user has just typed (the other is a Reconfigure submit with the field left
    empty), and it does so only after the user confirms: the confirm step's submit
    branch runs one forced login through
    ``runtime.async_login_with_saved_password(take_over=True)``. Taking the session
    back signs the other client out, which is why it is the user's decision and
    never automatic.

    The login goes through the runtime helper, the integration's one forced-login
    call. The library releases the session-replaced latch only once that login
    succeeds, so a failure leaves the store latched, the abort text below is true
    (nothing changed, the repair stays open), and the next setup raises on the latch
    instead of signing in by itself.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Open straight on the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask first; on submit, one forced login through the runtime helper, then reload."""
        if user_input is None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "account": errors.account_label(entry) if entry is not None else ""
                },
            )
        entry = _loaded_entry(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_loaded")
        try:
            await runtime.async_login_with_saved_password(entry.runtime_data.eufy, take_over=True)
        except EufySecurityError as err:
            _LOGGER.warning(
                "Signing in to eufy again failed (%s); the repair stays open",
                type(err).__name__,
            )
            return self.async_abort(reason="login_failed")
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


class KeyRejectedFix(RepairsFlow):
    """Allow one more key fetch for a station that rejects its re-fetched key.

    The library fetches a rejected station key again at most once, then latches.
    On confirmation this releases that station's latch through the loaded client's
    ``async_reset_key_refresh`` and reloads the entry, so the station's next
    rejected handshake may fetch its key once more; the library's per-station
    cipher cooldown still applies. The fix itself fetches nothing and never signs
    in. The station is found through its device, so the issue holds no serial.
    """

    def __init__(self, entry_id: str, device_id: str) -> None:
        self._entry_id = entry_id
        self._device_id = device_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Open straight on the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask first; on submit, release the station's latch and reload the entry."""
        device = dr.async_get(self.hass).async_get(self._device_id)
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "station": errors.station_label(device) if device is not None else ""
                },
            )
        entry = _loaded_entry(self.hass, self._entry_id)
        if entry is None or device is None:
            return self.async_abort(reason="entry_not_loaded")
        serial = next(
            (identifier for domain, identifier in device.identifiers if domain == DOMAIN), None
        )
        if serial is None:
            return self.async_abort(reason="entry_not_loaded")
        try:
            await entry.runtime_data.eufy.async_reset_key_refresh(serial)
        except EufySecurityError as err:
            _LOGGER.warning(
                "Allowing another HomeBase key fetch failed (%s); the repair stays open",
                type(err).__name__,
            )
            return self.async_abort(reason="reset_failed")
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


class PendingInvitesFix(RepairsFlow):
    """Accept a pending eufy invitation here, or look for the shared devices again once
    the user accepted it in the eufy app.

    Opening the fix reads the invitations afresh through the loaded client (login-free);
    with none left, or none readable, it opens on the look-again confirmation. An
    accept goes through the library, which checks an inviter's code on this host and
    sends nothing when it is missing or wrong; the code field shows only for an
    invitation that needs one. Looking again, finishing, and closing the fix after an
    accept each reload the entry once with one rescan at its setup
    (``runtime.request_rescan_at_setup``): every login scope is asked for its devices
    and the invitations are read again; a scope whose session lapsed costs a sign-in
    at setup. An entry that is not loaded is not set up by this; its next setup
    rescans. Home names, inviters and codes are never logged.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._invites: list[CloudInvite] = []
        self._chosen: CloudInvite | None = None
        self._accepted = False
        self._accepting = False
        self._removed = False

    def _placeholders(self) -> dict[str, str]:
        """The account's label and the open invitations: the ones read, else the issue's."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if self._invites:
            invites = errors.invites_text(self._invites)
        else:
            issue = ir.async_get(self.hass).async_get_issue(
                DOMAIN, errors.pending_invites_issue_id(self._entry_id)
            )
            stored = (issue.translation_placeholders if issue is not None else None) or {}
            invites = stored.get("invites", "")
        return {
            "account": errors.account_label(entry) if entry is not None else "",
            "invites": invites,
        }

    def _reload(self) -> None:
        """Ask every login scope once at the entry's next setup; reload it when it runs,
        is being set up or waits to retry, never setting up an entry the user unloaded."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return
        runtime.request_rescan_at_setup(self.hass, self._entry_id)
        if entry.state in (
            ConfigEntryState.LOADED,
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.SETUP_IN_PROGRESS,
        ):
            self.hass.config_entries.async_schedule_reload(self._entry_id)

    @callback
    def async_remove(self) -> None:
        """After an accept, finished or closed: reload, so the shared devices are listed.

        Closed while an accept is still running, :meth:`_async_accept` reloads when it
        ends, so the reload never closes the client an accept is using.
        """
        self._removed = True
        if self._accepted and not self._accepting:
            self._reload()

    async def _async_accept(
        self, entry: EufyConfigEntry, invite: CloudInvite, code: str | None
    ) -> None:
        """Accept through the entry's client; reload at the end when the fix was closed
        meanwhile and something was accepted."""
        self._accepting = True
        try:
            await entry.runtime_data.eufy.async_accept_invite(invite, code=code)
            self._accepted = True
        finally:
            self._accepting = False
            if self._removed and self._accepted:
                self._reload()

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Read the invitations; offer to accept one here or to look again."""
        entry = _loaded_entry(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_loaded")
        self._invites = list(await runtime.async_pending_invites(entry.runtime_data.eufy) or [])
        if not self._invites:
            return await self.async_step_confirm()
        return self.async_show_menu(
            step_id="init",
            menu_options=["pick", "confirm"],
            description_placeholders=self._placeholders(),
        )

    async def async_step_pick(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Choose the invitation to accept; a single one is taken without asking."""
        if len(self._invites) == 1:
            self._chosen = self._invites[0]
            return await self.async_step_accept()
        if user_input is not None:
            self._chosen = self._invites[int(user_input[_INVITATION])]
            return await self.async_step_accept()
        options = [
            SelectOptionDict(value=str(index), label=errors.invite_choice_label(invite))
            for index, invite in enumerate(self._invites)
        ]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(_INVITATION): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            description_placeholders=self._placeholders(),
        )

    async def async_step_accept(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Accept the chosen invitation, with the inviter's code when it needs one."""
        invite = self._chosen
        entry = _loaded_entry(self.hass, self._entry_id)
        if invite is None or entry is None:
            return self.async_abort(reason="entry_not_loaded")
        form_errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_accept(entry, invite, user_input.get(_CODE) or None)
            except InviteCodeError as err:
                form_errors[_CODE] = "code_missing" if err.missing else "wrong_code"
            except UnsupportedError:
                return self.async_abort(reason="accept_in_app")
            except NoCachedSessionError:
                return self.async_abort(reason="session_lapsed")
            except SessionReplacedError:
                errors.raise_session_replaced_issue(self.hass, entry)
                return self.async_abort(reason="session_replaced")
            except EufySecurityError as err:
                _LOGGER.warning(
                    "Accepting a eufy invitation failed (%s); the repair stays open",
                    type(err).__name__,
                )
                return self.async_abort(reason="accept_failed")
            else:
                self._invites = [other for other in self._invites if other is not invite]
                if not self._invites:
                    return await self.async_step_finish()
                return await self.async_step_accepted()
        schema = (
            vol.Schema(
                {
                    vol.Required(_CODE): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            )
            if invite.needs_code
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="accept",
            data_schema=schema,
            errors=form_errors,
            description_placeholders={
                **self._placeholders(),
                "invitation": errors.invite_choice_label(invite),
            },
        )

    async def async_step_accepted(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """One accepted, others open: accept another, or finish."""
        return self.async_show_menu(
            step_id="accepted",
            menu_options=["pick", "finish"],
            description_placeholders=self._placeholders(),
        )

    async def async_step_finish(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Done accepting; the reload follows when the flow is removed (:meth:`async_remove`)."""
        return self.async_create_entry(data={})

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask first, naming the invitations; on submit, reload with one rescan."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders(),
            )
        if _loaded_entry(self.hass, self._entry_id) is None:
            return self.async_abort(reason="entry_not_loaded")
        self._reload()
        return self.async_create_entry(data={})


class PushNeededFix(RepairsFlow):
    """Switch cloud push on for an account whose stations send detections only that way.

    On confirmation the entry's ``cloud_push`` option is set and the entry reloaded,
    which starts push in the background; the fix itself sends nothing.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Open straight on the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask first, naming the stations the issue shows; on submit, set the option and
        reload."""
        if user_input is None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            issue = ir.async_get(self.hass).async_get_issue(
                DOMAIN, errors.push_needed_issue_id(self._entry_id)
            )
            placeholders = (issue.translation_placeholders if issue is not None else None) or {}
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "account": errors.account_label(entry) if entry is not None else "",
                    "stations": placeholders.get("stations", ""),
                },
            )
        entry = _loaded_entry(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_loaded")
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_CLOUD_PUSH: True}
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


class VariantResetFix(RepairsFlow):
    """Reset the controls of setting versions to the eufy app's defaults.

    On confirmation the version the app uses for each device is enabled and the others
    disabled (``async_reset_variant_rows``); controls the user set stay. Enabling one makes
    Home Assistant reload the entry; every setup raises the issue again only while a
    control is left to reset. The form shows the current plan, not the issue's counts, and
    a confirmation applies only that plan: when it changed in between, the form shows the
    new one instead. A plan that changes no control's state aborts and deletes the issue.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._shown: frozenset[object] | None = None

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Open straight on the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask first, with what changes; on submit, apply the reset shown."""
        entry = _loaded_entry(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_loaded")
        plan = variant_reset_plan(self.hass, entry, entry.runtime_data.coordinators.values())
        key = variant_reset_key(plan)
        if user_input is None or key != self._shown:
            summary = variant_reset_summary(self.hass, plan)
            if not summary.disable and not summary.enable:
                errors.delete_variant_reset_issue(self.hass, entry)
                return self.async_abort(reason="nothing_to_reset")
            self._shown = key
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "account": errors.account_label(entry),
                    "disable": str(summary.disable),
                    "enable": str(summary.enable),
                    "devices": ", ".join(summary.devices),
                },
            )
        async_reset_variant_rows(self.hass, plan)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """The fix flow for one of this integration's fixable issues."""
    if issue_id.startswith(f"{ISSUE_SESSION_REPLACED}_") and data is not None:
        return SessionReplacedFix(str(data["entry_id"]))
    if issue_id.startswith(f"{ISSUE_KEY_REJECTED}_") and data is not None:
        return KeyRejectedFix(str(data["entry_id"]), str(data["device_id"]))
    if issue_id.startswith(f"{ISSUE_PENDING_INVITES}_") and data is not None:
        return PendingInvitesFix(str(data["entry_id"]))
    if issue_id.startswith(f"{ISSUE_PUSH_NEEDED}_") and data is not None:
        return PushNeededFix(str(data["entry_id"]))
    if issue_id.startswith(f"{ISSUE_VARIANT_RESET}_") and data is not None:
        return VariantResetFix(str(data["entry_id"]))
    return ConfirmRepairFlow()
