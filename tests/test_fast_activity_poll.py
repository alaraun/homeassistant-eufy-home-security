"""Pure policy tests for optional full-station activity polling."""

from __future__ import annotations

import pytest

from custom_components.eufy_home_security.const import (
    CONF_FAST_ACTIVITY_POLL,
    CONF_FAST_ACTIVITY_POLL_INTERVAL,
)
from custom_components.eufy_home_security.coordinator import activity_poll_interval


@pytest.mark.parametrize(
    ("enabled", "seconds", "on_demand", "expected"),
    [
        (False, None, False, 45),
        (True, None, False, 5),
        (True, 1, False, 1),
        (True, 5, False, 5),
        (True, 15, False, 15),
        (True, 5, True, 45),
        (True, 0, False, 45),
        (True, 45, False, 45),
        (True, "5", False, 45),
    ],
)
def test_activity_poll_interval(
    enabled: bool,
    seconds: object | None,
    on_demand: bool,
    expected: int,
) -> None:
    options: dict[str, object] = {CONF_FAST_ACTIVITY_POLL: enabled}
    if seconds is not None:
        options[CONF_FAST_ACTIVITY_POLL_INTERVAL] = seconds

    assert activity_poll_interval(options, on_demand=on_demand) == expected
