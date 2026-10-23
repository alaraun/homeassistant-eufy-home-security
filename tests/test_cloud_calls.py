"""The ``cloud_calls`` test helper: what it leaves out of a fake cloud's call log."""

from __future__ import annotations

from conftest import cloud_calls
from eufy_home_security.testing import FakeCloud


def _calls(*calls: str) -> list[str]:
    cloud = FakeCloud()
    cloud.calls[:] = calls
    return cloud_calls(cloud)


def test_a_device_list_fetch_reads_as_its_devices_call() -> None:
    """The house list and per-house lists right after ``devices`` go, per region."""
    assert _calls(
        "login", "things", "devices", "houses", "house:h1", "devices@us", "houses@us"
    ) == ["login", "devices", "devices@us"]


def test_fetches_of_two_regions_may_interleave() -> None:
    assert _calls("devices", "devices@us", "houses", "houses@us", "house:h1", "house:h2@us") == [
        "devices",
        "devices@us",
    ]


def test_a_lone_house_lookup_stays_visible() -> None:
    """An arming-group lookup (``houses``, ``house:<id>``) without a device list shows."""
    assert _calls("login", "houses", "house:h1") == ["login", "houses", "house:h1"]


def test_a_house_lookup_after_a_fetch_and_another_call_stays_visible() -> None:
    assert _calls("devices", "houses", "house:h1", "push_token", "houses", "house:h1") == [
        "devices",
        "push_token",
        "houses",
        "house:h1",
    ]


def test_a_second_house_list_after_a_fetch_stays_visible() -> None:
    assert _calls("devices", "houses", "house:h1", "houses", "house:h1") == [
        "devices",
        "houses",
        "house:h1",
    ]


def test_invites_are_kept() -> None:
    assert _calls("devices", "houses", "house_invites", "device_invites") == [
        "devices",
        "house_invites",
        "device_invites",
    ]
