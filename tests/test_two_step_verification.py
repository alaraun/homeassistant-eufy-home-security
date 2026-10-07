"""eufy's two-step verification code in the user, reauth and reconfigure flows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from conftest import cloud_calls, set_up_warm
from eufy_home_security import EufySecurity, SessionCache
from eufy_home_security.cloud import const as cloud_const
from eufy_home_security.testing import SYNTHETIC, FakeCloud
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.eufy_home_security import runtime
from custom_components.eufy_home_security.const import DOMAIN

CODE = "123456"
NEW_PASSWORD = "synthetic-new-password"
SEND_CODE = "send_code"


class TwoStep:
    """A fake cloud whose ``login_call`` login answers code 0 with ``fa_info.step`` 26052
    until the login carries :data:`CODE` (a wrong code answers step 26050)."""

    def __init__(self, cloud: FakeCloud, login_call: str) -> None:
        self.cloud = cloud
        self.login_call = login_call
        self.codes: list[str] = []
        self.answered_on: list[str] = []

    async def answer(self, real: Callable[..., Any], api: Any, path: str, payload: Any) -> Any:
        if path == cloud_const.SEND_VERIFY_CODE_PATH:
            self.cloud.calls.append(SEND_CODE)
            return None
        result = await real(api, path, payload)
        given = str(payload.get("verify_code") or "") if path == cloud_const.LOGIN_PATH else ""
        if given:
            self.answered_on.append(self.cloud.calls[-1])
        if path == cloud_const.LOGIN_PATH and self.cloud.calls[-1] == self.login_call:
            if given:
                self.codes.append(given)
            if given != CODE:
                step = int(cloud_const.CloudCode.VERIFY_CODE_ERROR if given else 26052)
                return {**result, "fa_info": {"info": "use verify code for 2fa", "step": step}}
        return result


def two_step(
    monkeypatch: pytest.MonkeyPatch, cloud: FakeCloud, login_call: str = "login"
) -> TwoStep:
    fake = TwoStep(cloud, login_call)
    real = cloud._answer

    async def answer(api: Any, path: str, payload: Any) -> Any:
        return await fake.answer(real, api, path, payload)

    monkeypatch.setattr(cloud, "_answer", answer)
    return fake


def _state(entry: ConfigEntry) -> ConfigEntryState:
    """The entry's state, read through a call so mypy does not keep an earlier narrowing."""
    return entry.state


async def _cached(hass: HomeAssistant) -> SessionCache:
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    return cache


async def _add(hass: HomeAssistant) -> Any:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: SYNTHETIC.email, CONF_PASSWORD: SYNTHETIC.password}
    )


async def _code(hass: HomeAssistant, result: Any, code: str = CODE) -> Any:
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"verify_code": code}
    )
    await hass.async_block_till_done()
    return result


async def _unload_all(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_two_step_account_is_added_with_the_e_mailed_code(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
) -> None:
    """The code step follows the password; one client signs in, asks for the code, answers."""
    fake = two_step(monkeypatch, fake_cloud)

    result = await _add(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify_code"
    assert result["errors"] == {}
    assert cloud_calls(fake_cloud) == ["login", SEND_CODE]
    assert SYNTHETIC.email not in str(result.get("description_placeholders"))

    result = await _code(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_EMAIL: SYNTHETIC.email}
    assert fake.codes == [CODE]
    assert cloud_calls(fake_cloud)[2:5] == ["login", "login@us", "devices"]
    assert len(built_clients) == 2  # the flow's one client, then the entry's
    cache = await _cached(hass)
    assert cache.password == SYNTHETIC.password

    await _unload_all(hass)


async def test_the_code_goes_to_the_region_that_asked_for_it(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
) -> None:
    """The second region asks: its answer goes there, not to the first region."""
    fake = two_step(monkeypatch, fake_cloud, login_call="login@us")

    result = await _add(hass)
    assert result["step_id"] == "verify_code"
    assert cloud_calls(fake_cloud) == ["login", "login@us", SEND_CODE]

    result = await _code(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.codes == [CODE]
    assert fake.answered_on == ["login@us"]

    await _unload_all(hass)


async def test_a_wrong_code_asks_again_and_the_right_one_signs_in(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
) -> None:
    fake = two_step(monkeypatch, fake_cloud)
    result = await _add(hass)

    result = await _code(hass, result, "000000")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify_code"
    assert result["errors"] == {"base": "invalid_verify_code"}

    result = await _code(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.codes == ["000000", CODE]

    await _unload_all(hass)


async def test_an_empty_code_is_refused_without_a_sign_in(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
) -> None:
    two_step(monkeypatch, fake_cloud)
    result = await _add(hass)
    calls = len(fake_cloud.calls)

    result = await _code(hass, result, "   ")
    assert result["step_id"] == "verify_code"
    assert result["errors"] == {"verify_code": "invalid_verify_code"}
    assert len(fake_cloud.calls) == calls

    await _unload_all(hass)


async def test_a_flow_left_at_the_code_step_forgets_the_new_account(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
) -> None:
    """Closing the dialog closes the held client and leaves no password in the store.

    The second region asks, so the first region's session and the password are already
    cached when the dialog closes.
    """
    two_step(monkeypatch, fake_cloud, login_call="login@us")
    result = await _add(hass)
    assert result["step_id"] == "verify_code"

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    cache = await _cached(hass)
    assert cache.password is None
    assert not cache.section("cloud")
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_reauth_of_a_two_step_account_asks_for_the_code_with_the_entry_down(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    """The entry stays unloaded while the code is asked; the answer reloads it."""
    entry = await set_up_warm(hass, seed_warm_cache)
    fake = two_step(monkeypatch, fake_cloud)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    assert result["step_id"] == "verify_code"
    await hass.async_block_till_done()
    assert _state(entry) is ConfigEntryState.NOT_LOADED

    result = await _code(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert fake.codes == [CODE]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    cache = await _cached(hass)
    assert cache.password == NEW_PASSWORD

    await _unload_all(hass)


async def test_reconfigure_with_the_saved_password_answers_the_code(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    fake = two_step(monkeypatch, fake_cloud)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "verify_code"

    result = await _code(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert fake.codes == [CODE]
    assert entry.state is ConfigEntryState.LOADED
    cache = await _cached(hass)
    assert cache.password == SYNTHETIC.password

    await _unload_all(hass)


async def test_a_reauth_left_at_the_code_step_sets_the_entry_up_again(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake_cloud: FakeCloud,
    built_clients: list[EufySecurity],
    seed_warm_cache: Callable[..., None],
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    two_step(monkeypatch, fake_cloud)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    assert result["step_id"] == "verify_code"
    await hass.async_block_till_done()
    assert _state(entry) is ConfigEntryState.NOT_LOADED

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    cache = await _cached(hass)
    assert cache.password == SYNTHETIC.password  # the unchecked new password is not kept

    await _unload_all(hass)
