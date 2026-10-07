"""The user step: adding a eufy account, and every way it is refused without a wasted sign-in."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import cloud_calls, set_up_warm, wait_until
from eufy_home_security import (
    AuthenticationError,
    CommunicationError,
    EufySecurity,
    EufySecurityError,
    KeyRejectedError,
    LoginChallengeError,
    LoginLimitedError,
    RateLimitedError,
    RefreshCooldownError,
    SessionCache,
    SessionReplacedError,
    StationClaims,
    StationUnreachableError,
)
from eufy_home_security.cloud import const as cloud_const
from eufy_home_security.testing import SYNTHETIC, FakeCloud
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.eufy_home_security import errors, runtime
from custom_components.eufy_home_security.const import DOMAIN

_EN_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "eufy_home_security"
    / "translations"
    / "en.json"
)


async def _submit(hass: HomeAssistant, email: str, password: str = SYNTHETIC.password) -> Any:
    """Open the user step and submit ``email`` and ``password``; returns the flow result."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: email, CONF_PASSWORD: password}
    )
    await hass.async_block_till_done()
    return result


def _suggested_value(result: Any, field: str) -> Any:
    """The value the re-shown form pre-fills for ``field``, or None."""
    for key in result["data_schema"].schema:
        if key == field:
            return (key.description or {}).get("suggested_value")
    raise AssertionError(f"the form has no {field} field")


async def _unload_all(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_user_step_creates_an_entry_holding_only_the_email(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity]
) -> None:
    """The e-mail is trimmed and lower-cased; the password is not kept in the entry."""
    result = await _submit(hass, "  User@Example.COM ")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == {CONF_EMAIL: "user@example.com"}
    entry = result["result"]
    assert entry.unique_id == "user@example.com"
    assert SYNTHETIC.password not in str(entry.as_dict())
    assert fake_cloud.calls.count("login") == 1

    await _unload_all(hass)


async def test_emails_differing_by_case_or_whitespace_are_one_account(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity]
) -> None:
    """A second add of the same account, spelled differently, aborts before any build."""
    first = await _submit(hass, "  User@Example.COM ")
    assert first["type"] is FlowResultType.CREATE_ENTRY
    built = len(built_clients)
    logins = fake_cloud.calls.count("login")

    second = await _submit(hass, "USER@example.com")

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert len(built_clients) == built
    assert fake_cloud.calls.count("login") == logins
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    await _unload_all(hass)


@pytest.mark.parametrize("typed", ["", "   ", "not-an-email"])
async def test_an_empty_or_malformed_email_is_refused_without_a_sign_in(
    hass: HomeAssistant, fake_cloud: FakeCloud, built_clients: list[EufySecurity], typed: str
) -> None:
    """A typo costs nothing: no client is built and eufy is never asked."""
    result = await _submit(hass, typed)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EMAIL: "invalid_email"}
    assert cloud_calls(fake_cloud) == []
    assert built_clients == []
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (LoginChallengeError("verify_code"), "login_challenge"),
        (AuthenticationError("rejected"), "invalid_auth"),
        (LoginLimitedError(retry_after=60), "login_limited"),
        (SessionReplacedError(), "session_replaced"),
        (CommunicationError("down"), "cannot_connect"),
    ],
)
async def test_library_errors_are_form_errors(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    error: EufySecurityError,
    key: str,
) -> None:
    """Each sign-in failure is one attempt, one form error, and no entry."""
    fake_cloud.login_error = error

    result = await _submit(hass, SYNTHETIC.email)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    assert fake_cloud.calls.count("login") == 1
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (LoginChallengeError("captcha"), "login_challenge"),
        (AuthenticationError("rejected"), "invalid_auth"),
        (RateLimitedError(), "login_limited"),
        (LoginLimitedError(retry_after=60), "login_limited"),
        (RefreshCooldownError(retry_after=30), "login_limited"),
        (SessionReplacedError(), "session_replaced"),
        (KeyRejectedError("key rejected"), "cannot_connect"),
        (StationUnreachableError("no answer"), "cannot_connect"),
        (CommunicationError("down"), "cannot_connect"),
        (EufySecurityError("anything else"), "cannot_connect"),
    ],
)
def test_flow_error_key_maps_every_library_error(error: EufySecurityError, key: str) -> None:
    """Most specific first: a challenge is not a wrong password, a cooldown is a limit."""
    assert errors.flow_error_key(error) == key


async def test_a_failed_add_forgets_the_password_but_keeps_the_login_record(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    hass_storage: dict[str, Any],
) -> None:
    """A flow that ends without an entry leaves no password or session on disk.

    The sign-in succeeds and the device list then fails, so the form shows an error
    and no entry exists to forget the account later. The store keeps its throttle,
    so the login budget is not reset, and the next submit on the same form checks
    the typed password with eufy instead of taking the warm-cache path.
    """
    real_answer = fake_cloud._answer

    async def device_list_down(api: Any, path: str, payload: Any) -> Any:
        if path == cloud_const.DEVICES_PATH:
            fake_cloud.calls.append("devices")
            raise CommunicationError("the device list is unavailable")
        return await real_answer(api, path, payload)

    monkeypatch.setattr(fake_cloud, "_answer", device_list_down)

    result = await _submit(hass, SYNTHETIC.email)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    # Not vacuous: the sign-in really happened before discovery failed. A cold login
    # signs in to every cloud region.
    assert cloud_calls(fake_cloud)[:3] == ["login", "login@us", "devices"]
    assert hass.config_entries.async_entries(DOMAIN) == []
    cache = await _cached(hass)
    assert cache.password is None
    assert not cache.section("cloud")
    document = hass_storage[runtime.store_key(SYNTHETIC.email)]["data"]
    assert SYNTHETIC.password not in str(document)
    assert document["throttle"]["logins"], "the login record was dropped with the secrets"

    fake_cloud.login_error = AuthenticationError("rejected")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: SYNTHETIC.email, CONF_PASSWORD: "mistyped"}
    )
    await hass.async_block_till_done()

    assert result["errors"] == {"base": "invalid_auth"}
    assert fake_cloud.calls.count("login") == 2


def test_the_login_challenge_message_sends_the_user_to_the_eufy_app() -> None:
    """There is no challenge step: the message must say where to answer it."""
    text = json.loads(_EN_PATH.read_text())["config"]["error"]["login_challenge"]
    assert "eufy app" in text.lower()


async def test_a_failed_sign_in_keeps_the_email_and_never_echoes_the_password(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The re-shown form keeps what the user typed as the e-mail, and only that."""
    fake_cloud.login_error = AuthenticationError("rejected")
    typed = " User@Example.COM "

    with caplog.at_level(logging.DEBUG):
        result = await _submit(hass, typed)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert _suggested_value(result, CONF_EMAIL) == typed
    assert _suggested_value(result, CONF_PASSWORD) is None
    assert SYNTHETIC.password not in str(result)
    # Not vacuous: the sign-in attempt was logged, and the password is in no record.
    assert any(r.name.startswith("eufy_home_security") for r in caplog.records)
    assert SYNTHETIC.password not in caplog.text


# ── reauth ───────────────────────────────────────────────────────────────────

_NEW_PASSWORD = "synthetic-new-password"
_CONFIG_FLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "eufy_home_security"
    / "config_flow.py"
)


async def _cached(hass: HomeAssistant) -> SessionCache:
    """The account store as a fresh library instance would read it."""
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    return cache


async def _submit_reauth(hass: HomeAssistant, flow: Any, password: str = _NEW_PASSWORD) -> Any:
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: password}
    )
    return result


async def _unload(hass: HomeAssistant, entry: Any) -> None:
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reauth_keeps_the_new_password(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """One login checks the new password; the reloaded entry runs with it cached."""
    entry = await set_up_warm(hass, seed_warm_cache)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # The account is named by its redacted label, never the e-mail address.
    assert SYNTHETIC.email not in str(result["description_placeholders"])

    result = await _submit_reauth(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (await _cached(hass)).password == _NEW_PASSWORD
    assert cloud_calls(fake_cloud) == ["login"]
    assert _NEW_PASSWORD not in str(entry.as_dict())

    await _unload(hass, entry)


async def test_reauth_unloads_a_loaded_entry_before_it_builds_an_instance(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Never two live clients on one account store."""
    entry = await set_up_warm(hass, seed_warm_cache)
    assert entry.state is ConfigEntryState.LOADED
    build = runtime.build_client  # the conftest replacement, which builds on the fakes
    records: list[tuple[str | None, ConfigEntryState]] = []

    def recording_build(
        hass: HomeAssistant,
        email: str,
        password: str | None,
        *,
        claims: StationClaims | None = None,
        **kwargs: Any,
    ) -> EufySecurity:
        records.append((password, entry.state))
        return build(hass, email, password, claims=claims, **kwargs)

    monkeypatch.setattr(runtime, "build_client", recording_build)

    result = await entry.start_reauth_flow(hass)
    result = await _submit_reauth(hass, result)
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    at_reauth = [state for password, state in records if password == _NEW_PASSWORD]
    assert at_reauth == [ConfigEntryState.NOT_LOADED]
    # Not vacuous: the reload after the reauth built the entry's own client too.
    assert any(password is None for password, _ in records)

    await _unload(hass, entry)


async def test_a_failed_reauth_keeps_the_old_password_and_reloads_the_entry(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A rejected password is a form error; the entry the flow unloaded comes back."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.login_error = AuthenticationError("rejected")

    result = await entry.start_reauth_flow(hass)
    result = await _submit_reauth(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (await _cached(hass)).password == SYNTHETIC.password
    assert cloud_calls(fake_cloud) == ["login"]  # one attempt, no retry

    await _unload(hass, entry)


async def test_reauth_on_a_replaced_session_asks_before_taking_it_over(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Nothing signs the other client out until the user submits the take-over step."""
    seed_warm_cache()
    cache = await _cached(hass)
    cache.set_replaced()
    await cache.async_save()
    entry = await set_up_warm(hass, lambda: None)

    result = await entry.start_reauth_flow(hass)
    result = await _submit_reauth(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_take_over"
    assert cloud_calls(fake_cloud) == []
    assert SYNTHETIC.email not in str(result["description_placeholders"])
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED  # local control while the user decides

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert cloud_calls(fake_cloud) == ["login"]
    await hass.async_block_till_done()

    cache = await _cached(hass)
    assert cache.replaced_at is None
    assert cache.password == _NEW_PASSWORD
    assert entry.state is ConfigEntryState.LOADED

    await _unload(hass, entry)


async def test_a_take_over_confirmed_while_the_entry_is_still_setting_up_waits_for_it(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The take-over never runs beside the entry's own client on the store.

    The first reauth submit sets the entry up again while the user decides. Here that
    setup is held inside its station start, so the take-over is submitted while the
    entry is still SETUP_IN_PROGRESS. The flow must unload that entry before it builds
    its own client; otherwise the setup's client closes later and writes its stale
    document back, the replaced latch included.
    """
    seed_warm_cache()
    cache = await _cached(hass)
    cache.set_replaced()
    await cache.async_save()
    entry = await set_up_warm(hass, lambda: None)

    build = runtime.build_client  # the conftest replacement, which builds on the fakes
    records: list[tuple[str | None, ConfigEntryState]] = []
    held = asyncio.Event()
    release = asyncio.Event()

    def gated_build(
        hass: HomeAssistant,
        email: str,
        password: str | None,
        *,
        claims: StationClaims | None = None,
        **kwargs: Any,
    ) -> EufySecurity:
        records.append((password, entry.state))
        eufy = build(hass, email, password, claims=claims, **kwargs)
        if password is None and not release.is_set():
            real_start = eufy.async_start

            async def held_start(*args: Any, **kwargs: Any) -> Any:
                held.set()
                await release.wait()
                return await real_start(*args, **kwargs)

            eufy.async_start = held_start  # type: ignore[method-assign]
        return eufy

    monkeypatch.setattr(runtime, "build_client", gated_build)

    result = await entry.start_reauth_flow(hass)
    result = await _submit_reauth(hass, result)
    assert result["step_id"] == "reauth_take_over"
    # Setup awaits other work before it builds its client, so wait for the held start
    # itself, not for SETUP_IN_PROGRESS.
    await wait_until(held.is_set)

    confirm = hass.async_create_task(
        hass.config_entries.flow.async_configure(result["flow_id"], {})
    )
    await asyncio.sleep(0.1)
    held_state = entry.state
    assert held_state is ConfigEntryState.SETUP_IN_PROGRESS
    release.set()
    result = await confirm
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    # Only one client live on the store: the flow's was built once the entry was down.
    at_take_over = [state for password, state in records if password == _NEW_PASSWORD]
    assert at_take_over[-1] is ConfigEntryState.NOT_LOADED, at_take_over
    assert cloud_calls(fake_cloud) == ["login"]
    cache = await _cached(hass)
    assert cache.replaced_at is None
    assert cache.password == _NEW_PASSWORD
    assert entry.state is ConfigEntryState.LOADED

    await _unload(hass, entry)


async def test_a_login_challenge_during_reauth_is_a_form_error(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """No challenge step: the user answers it in the eufy app and tries again."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.login_error = LoginChallengeError("verify_code")

    result = await entry.start_reauth_flow(hass)
    result = await _submit_reauth(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "login_challenge"}
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    await _unload(hass, entry)


_REAUTH_METHOD_PREFIXES = ("async_step_reauth", "_async_reauthenticate")


def _reauth_login_calls(tree: ast.AST) -> tuple[list[str], bool]:
    """(reauth methods calling ``async_login``, whether ``_async_reauthenticate`` calls
    ``async_reauthenticate``) inside class ``EufyHomeSecurityConfigFlow``."""
    offenders: list[str] = []
    reauthenticates = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "EufyHomeSecurityConfigFlow"):
            continue
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not method.name.startswith(_REAUTH_METHOD_PREFIXES):
                continue
            for call in ast.walk(method):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                    continue
                if call.func.attr == "async_login":
                    offenders.append(method.name)
                if call.func.attr == "async_reauthenticate" and (
                    method.name == "_async_reauthenticate"
                ):
                    reauthenticates = True
    return offenders, reauthenticates


def test_reauth_never_uses_the_cached_login_path(tmp_path: Path) -> None:
    """On a warm cache ``async_login`` returns without checking the new password."""
    offenders, reauthenticates = _reauth_login_calls(ast.parse(_CONFIG_FLOW_PATH.read_text()))
    assert offenders == [], f"reauth methods call async_login: {offenders}"
    assert reauthenticates, "_async_reauthenticate does not call async_reauthenticate"

    # Fail-first: a staged flow whose reauth step calls async_login is flagged.
    staged = tmp_path / "staged_flow.py"
    staged.write_text(
        "class EufyHomeSecurityConfigFlow:\n"
        "    async def async_step_reauth_confirm(self, user_input=None):\n"
        "        await self.eufy.async_login()\n"
        "    async def _async_reauthenticate(self, entry, password, *, take_over):\n"
        "        await self.eufy.async_reauthenticate(password)\n"
    )
    assert _reauth_login_calls(ast.parse(staged.read_text())) == (
        ["async_step_reauth_confirm"],
        True,
    )


# ── reconfigure ──────────────────────────────────────────────────────────────


async def test_reconfigure_is_offered_on_the_entry_menu_and_opening_it_spends_nothing(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The menu offers Reconfigure; opening it sends nothing."""
    import voluptuous as vol

    entry = await set_up_warm(hass, seed_warm_cache)
    result = await entry.start_reconfigure_flow(hass)

    assert entry.supports_reconfigure is True
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert any(
        isinstance(k, vol.Optional) and k == CONF_PASSWORD for k in result["data_schema"].schema
    )
    assert SYNTHETIC.email not in str(result["description_placeholders"])
    assert cloud_calls(fake_cloud) == []
    assert entry.state is ConfigEntryState.LOADED

    await _unload(hass, entry)


async def test_reconfigure_with_an_empty_password_signs_in_with_the_saved_one_and_reloads(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Submitting an empty password uses the saved one."""
    entry = await set_up_warm(hass, seed_warm_cache)
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert cloud_calls(fake_cloud) == ["login"]

    cache = await _cached(hass)
    assert cache.password == SYNTHETIC.password
    assert SYNTHETIC.password not in str(entry.as_dict())

    await _unload(hass, entry)


async def test_reconfigure_with_a_new_password_checks_it_once_and_keeps_it(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """Submitting a new password checks it once and keeps it on success."""
    entry = await set_up_warm(hass, seed_warm_cache)
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: _NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert cloud_calls(fake_cloud) == ["login"]

    cache = await _cached(hass)
    assert cache.password == _NEW_PASSWORD
    assert _NEW_PASSWORD not in str(entry.as_dict())
    assert entry.state is ConfigEntryState.LOADED

    await _unload(hass, entry)


@pytest.mark.parametrize(
    "payload", [{}, {CONF_PASSWORD: _NEW_PASSWORD}], ids=["saved_password", "typed_password"]
)
async def test_reconfigure_on_a_replaced_session_asks_before_taking_it_over(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    payload: dict[str, Any],
) -> None:
    """Asks for take-over confirmation before replacing the session."""
    from homeassistant.helpers import issue_registry as ir

    seed_warm_cache()
    cache = await _cached(hass)
    cache.set_replaced()
    await cache.async_save()

    entry = await set_up_warm(hass, lambda: None)
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_take_over"
    assert cloud_calls(fake_cloud) == []
    assert SYNTHETIC.email not in str(result["description_placeholders"])
    assert entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert cloud_calls(fake_cloud) == ["login"]

    cache = await _cached(hass)
    assert cache.replaced_at is None
    expected_password = SYNTHETIC.password if not payload else _NEW_PASSWORD
    assert cache.password == expected_password
    assert entry.state is ConfigEntryState.LOADED

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"session_replaced_{entry.entry_id}") is None

    await _unload(hass, entry)


async def test_a_rejected_password_during_reconfigure_is_a_form_error_and_the_entry_comes_back(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """A rejected password is a form error and the entry comes back."""
    entry = await set_up_warm(hass, seed_warm_cache)
    result = await entry.start_reconfigure_flow(hass)

    fake_cloud.login_error = AuthenticationError("rejected")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: _NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.state is ConfigEntryState.LOADED

    cache = await _cached(hass)
    assert cache.password == SYNTHETIC.password
    assert cloud_calls(fake_cloud) == ["login"]
    assert _NEW_PASSWORD not in str(result)

    await _unload(hass, entry)


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (LoginLimitedError(retry_after=3600), "login_limited"),
        (CommunicationError("down"), "cannot_connect"),
        (LoginChallengeError("verify_code"), "login_challenge"),
    ],
)
async def test_a_login_limit_during_reconfigure_is_a_form_error_and_nothing_retries(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
    error: EufySecurityError,
    key: str,
) -> None:
    """A login limit or other failure is a form error and nothing retries."""
    entry = await set_up_warm(hass, seed_warm_cache)
    result = await entry.start_reconfigure_flow(hass)

    fake_cloud.login_error = error
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": key}
    assert cloud_calls(fake_cloud) == ["login"]
    assert entry.state is ConfigEntryState.LOADED

    await _unload(hass, entry)


async def test_an_empty_reconfigure_with_no_saved_password_reaches_nothing(
    hass: HomeAssistant,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """An empty submit with no saved password reaches nothing."""
    from conftest import add_entry, setup_entry

    seed_warm_cache()
    cache = await _cached(hass)
    cache.section("cloud").clear()
    await cache.async_save()

    fake_cloud.login_error = AuthenticationError("rejected")
    entry = add_entry(hass)
    assert not await setup_entry(hass, entry)

    fake_cloud.login_error = None
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        hass.config_entries.flow.async_abort(flow["flow_id"])

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_auth"}
    assert cloud_calls(fake_cloud) == ["login"]

    cache = await _cached(hass)
    assert cache.password is None

    await _unload(hass, entry)


@pytest.mark.parametrize(
    "payload", [{}, {CONF_PASSWORD: _NEW_PASSWORD}], ids=["saved_password", "typed_password"]
)
async def test_a_reconfigure_take_over_whose_login_fails_leaves_the_latch_set(
    hass: HomeAssistant,
    built_clients,
    seed_warm_cache,
    fake_cloud,
    fake_station,
    payload,
) -> None:
    """A take-over whose login fails keeps the latch and the issue, and shows the error."""
    seed_warm_cache()

    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    cache.set_replaced()
    for key in ("auth_token", "key_ident", "shared_key", "expires_at"):
        cache.section("cloud").pop(key, None)
    await cache.async_save()

    entry = await set_up_warm(hass, lambda: None)
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], payload)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_take_over"
    assert cloud_calls(fake_cloud) == []

    fake_cloud.login_error = CommunicationError("down")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}
    assert cloud_calls(fake_cloud) == ["login"]

    fresh_cache = await _cached(hass)
    assert fresh_cache.replaced_at is not None
    assert entry.state is ConfigEntryState.LOADED

    from homeassistant.helpers import issue_registry as ir

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"session_replaced_{entry.entry_id}") is not None
    )

    if payload:
        assert _NEW_PASSWORD not in str(result)


async def test_a_successful_take_over_clears_the_latch(
    hass: HomeAssistant,
    built_clients,
    seed_warm_cache,
    fake_cloud,
    fake_station,
) -> None:
    """A confirmed take-over from reconfigure releases the latch and its issue."""
    seed_warm_cache()

    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    cache.set_replaced()
    for key in ("auth_token", "key_ident", "shared_key", "expires_at"):
        cache.section("cloud").pop(key, None)
    await cache.async_save()

    entry = await set_up_warm(hass, lambda: None)
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_take_over"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert cloud_calls(fake_cloud) == ["login"]

    fresh_cache = await _cached(hass)
    assert fresh_cache.replaced_at is None

    from homeassistant.helpers import issue_registry as ir

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"session_replaced_{entry.entry_id}") is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
