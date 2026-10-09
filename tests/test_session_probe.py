"""
The session probe, on the real library and FakeCloud; every scenario proves no login.
"""

import logging
import re
from collections.abc import Callable

import pytest
from conftest import (
    SYNTHETIC,
    add_entry,
    advance_to_poll,
    cloud_calls,
    set_up_warm,
    setup_entry,
)
from eufy_home_security import (
    AuthenticationError,
    RateLimitedError,
    SessionCache,
    SessionReplacedError,
)
from eufy_home_security.testing.cloud import FakeCloud
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.eufy_home_security import runtime
from custom_components.eufy_home_security.const import CONF_SESSION_PROBE, DOMAIN


async def test_the_first_probe_runs_a_minute_after_setup_and_a_kick_out_becomes_the_repair(
    hass: HomeAssistant,
    fake_station,
    seed_warm_cache: Callable[..., None],
    built_clients: list,
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.call_errors = [SessionReplacedError("kicked", code=26084)]
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    issue_id = f"session_replaced_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    assert SYNTHETIC.email not in str(issue.translation_placeholders)

    assert fake_cloud.calls.count("devices") == 1
    assert "login" not in fake_cloud.calls
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Session probe" in caplog.text


async def test_a_healthy_session_is_probed_at_a_minute_and_again_at_six_hours_without_a_login(
    hass: HomeAssistant,
    fake_station,
    built_clients,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()
    assert cloud_calls(fake_cloud) == ["devices"]

    await advance_to_poll(hass, 6 * 3600 + 61)
    await hass.async_block_till_done()
    assert cloud_calls(fake_cloud) == ["devices", "devices"]

    issue_id = f"session_replaced_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is None

    assert "login" not in fake_cloud.calls
    await advance_to_poll(hass, 2)  # let the poll's 1 s dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    ok_line_found = any(
        "Session probe" in record.message and "ok" in record.message for record in caplog.records
    )
    assert ok_line_found


async def test_a_lapsed_key_identity_is_re_keyed_without_a_login_and_the_probe_says_ok(
    hass: HomeAssistant,
    fake_station,
    seed_warm_cache: Callable[..., None],
    built_clients: list,
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway's lapsed-key answer (HTTP 463, code 4404), once: the library runs a
    new key exchange on the same token and retries, so the probe succeeds and no login
    is spent, and the probe reports a real fetch, not the cached list."""
    entry = await set_up_warm(hass, seed_warm_cache)
    exchanges = fake_cloud.key_exchanges
    fake_cloud.call_errors = [FakeCloud.refusal(463, 4404, "get identity error")]
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    assert fake_cloud.calls.count("devices") == 2  # refused, then re-keyed and served
    assert fake_cloud.key_exchanges == exchanges + 1
    assert "login" not in fake_cloud.calls
    assert "Session probe: ok" in caplog.text
    assert not ir.async_get(hass).issues
    await advance_to_poll(hass, 2)  # let the poll's 1 s dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_key_identity_a_new_exchange_does_not_restore_is_reported_not_hidden(
    hass: HomeAssistant,
    fake_station,
    seed_warm_cache: Callable[..., None],
    built_clients: list,
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Refused again after the re-key: ``KeyExchangeRefusedError``. The probe says it
    failed, signs nothing in, raises no issue (the library guide: carry on and retry
    on the next interval), and never claims the session is fine."""
    entry = await set_up_warm(hass, seed_warm_cache)
    refusal = FakeCloud.refusal(463, 4404, "get identity error")
    fake_cloud.call_errors = [refusal, FakeCloud.refusal(463, 4404, "get identity error")]
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    assert "Session probe: failed (KeyExchangeRefusedError), routed" in caplog.text
    assert "Session probe: ok" not in caplog.text
    assert "login" not in fake_cloud.calls
    assert not ir.async_get(hass).issues
    assert not [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"].get("source") == SOURCE_REAUTH
    ]
    await advance_to_poll(hass, 2)  # let the poll's 1 s dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_with_the_option_off_no_probe_runs(
    hass: HomeAssistant,
    fake_station,
    built_clients,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache, options={CONF_SESSION_PROBE: False})
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()
    assert cloud_calls(fake_cloud) == []

    await advance_to_poll(hass, 6 * 3600 + 61)
    await hass.async_block_till_done()
    assert cloud_calls(fake_cloud) == []

    assert "Session probe: off by option, not scheduled" in caplog.text

    issue_id = f"session_replaced_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is None


async def test_a_latched_store_is_not_probed_and_a_deleted_issue_comes_back(
    hass: HomeAssistant,
    fake_station,
    built_clients,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_warm_cache()

    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    cache.set_replaced()
    for key in ("auth_token", "key_ident", "shared_key", "expires_at"):
        cache.section("cloud").pop(key, None)
    await cache.async_save()

    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    issue_id = f"session_replaced_{entry.entry_id}"
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")
    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert cloud_calls(fake_cloud) == []
    assert "login" not in fake_cloud.calls
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    latch_line_found = any(
        "Session probe" in record.message and "latch" in record.message for record in caplog.records
    )
    assert latch_line_found


async def test_a_request_hold_off_from_the_probe_is_the_login_limited_issue_and_nothing_retries(
    hass: HomeAssistant,
    fake_station,
    seed_warm_cache: Callable[..., None],
    built_clients: list,
    fake_cloud: FakeCloud,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.call_errors = [RateLimitedError("throttled", retry_after=120.0, code=26145)]

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    issue_id = f"login_limited_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    placeholders = issue.translation_placeholders
    assert placeholders is not None
    assert re.fullmatch(r"(\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}", placeholders["time"])
    assert SYNTHETIC.email not in str(issue.translation_placeholders)

    assert fake_cloud.calls.count("devices") == 1

    session_replaced_issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"session_replaced_{entry.entry_id}"
    )
    assert session_replaced_issue is None

    assert "login" not in fake_cloud.calls
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_rejected_session_from_the_probe_starts_one_reauth_flow_only(
    hass: HomeAssistant,
    fake_station,
    seed_warm_cache: Callable[..., None],
    built_clients: list,
    fake_cloud: FakeCloud,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.call_errors = [AuthenticationError("rejected")]

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    flows = [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(flows) == 1

    await advance_to_poll(hass, 6 * 3600 + 61)
    await hass.async_block_till_done()

    flows2 = [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(flows2) == 1

    assert "login" not in fake_cloud.calls
    await advance_to_poll(hass, 2)  # let the poll's 1 s dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.flow.async_abort(flows[0]["flow_id"])


async def test_the_latch_mirror_recreates_an_issue_the_user_deleted(
    hass: HomeAssistant,
    fake_station,
    seed_warm_cache: Callable[..., None],
    built_clients: list,
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    fake_cloud.call_errors = [SessionReplacedError("kicked", code=26084)]
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    issue_id = f"session_replaced_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    # The real library latched the kick-out itself (no stand-in writes the cache).
    assert built_clients[-1].session_replaced

    ir.async_delete_issue(hass, DOMAIN, issue_id)

    await advance_to_poll(hass, 6 * 3600 + 61)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    assert fake_cloud.calls.count("devices") == 1
    assert "login" not in fake_cloud.calls
    await advance_to_poll(hass, 2)  # let the poll's 1 s dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_clears_the_latch_and_the_new_setup_re_arms_the_probe(
    hass: HomeAssistant,
    fake_station,
    built_clients,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
) -> None:
    seed_warm_cache()
    cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await cache.async_load()
    cache.set_replaced()
    for key in ("auth_token", "key_ident", "shared_key", "expires_at"):
        cache.section("cloud").pop(key, None)
    await cache.async_save()

    entry = add_entry(hass)
    assert await setup_entry(hass, entry)

    issue_id = f"session_replaced_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["step_id"] == "reauth_take_over"
    assert cloud_calls(fake_cloud) == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert cloud_calls(fake_cloud) == ["login"]

    fresh_cache = SessionCache(runtime.cache_store(hass, SYNTHETIC.email), SYNTHETIC.email)
    await fresh_cache.async_load()
    assert fresh_cache.replaced_at is None

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is None

    assert entry.state is ConfigEntryState.LOADED

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    assert cloud_calls(fake_cloud) == ["login", "devices"]

    await advance_to_poll(hass, 2)  # let the poll's 1 s dump-settle timer fire
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_probe_runs_after_the_entry_is_unloaded(
    hass: HomeAssistant,
    fake_station,
    built_clients,
    seed_warm_cache: Callable[..., None],
    fake_cloud: FakeCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up_warm(hass, seed_warm_cache)
    caplog.set_level(logging.DEBUG, logger="custom_components.eufy_home_security")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    caplog.clear()

    await advance_to_poll(hass, 61)
    await hass.async_block_till_done()

    await advance_to_poll(hass, 6 * 3600 + 61)
    await hass.async_block_till_done()

    assert cloud_calls(fake_cloud) == []

    probe_log_found = any(
        "Session probe" in record.message and ("ok" in record.message or "failed" in record.message)
        for record in caplog.records
    )
    assert not probe_log_found

    issue_id = f"session_replaced_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is None
