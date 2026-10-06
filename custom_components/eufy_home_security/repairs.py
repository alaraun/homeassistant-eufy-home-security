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
from typing import cast

import voluptuous as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from eufy_home_security import EufySecurityError

from . import errors, runtime
from .const import DOMAIN, ISSUE_KEY_REJECTED, ISSUE_SESSION_REPLACED
from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)


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
    return ConfirmRepairFlow()
