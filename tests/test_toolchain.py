"""Self-test that the integration package is type-checked strictly by configuration,
not only by a command-line flag."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"

# The strict-mode flags the integration's override must set. `strict` itself
# cannot be set per module, so the flags are listed.
_PACKAGE_GLOB = "custom_components.eufy_home_security.*"
_STRICT_FLAGS = (
    "disallow_untyped_defs",
    "disallow_incomplete_defs",
    "disallow_any_generics",
    "check_untyped_defs",
    "warn_return_any",
    "strict_equality",
)


def _strict_override_violations(pyproject: dict[str, Any]) -> list[str]:
    """Every strict flag no override covering the integration package sets to true."""
    overrides = pyproject.get("tool", {}).get("mypy", {}).get("overrides", [])
    covering = []
    for override in overrides:
        modules = override.get("module", [])
        if isinstance(modules, str):
            modules = [modules]
        if _PACKAGE_GLOB in modules:
            covering.append(override)
    if not covering:
        return [f"no [[tool.mypy.overrides]] table covers {_PACKAGE_GLOB}"]
    return [
        f"{flag} is not true"
        for flag in _STRICT_FLAGS
        if not any(override.get(flag) is True for override in covering)
    ]


def test_the_integration_package_is_type_checked_strictly() -> None:
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text())
    violations = _strict_override_violations(pyproject)
    assert violations == [], (
        f"pyproject.toml no longer type-checks the integration strictly: {violations}. "
        f"Restore the [[tool.mypy.overrides]] table for {_PACKAGE_GLOB}"
    )

    # Fail-first: a dropped flag and a dropped table are both flagged.
    without_flag = copy.deepcopy(pyproject)
    for override in without_flag["tool"]["mypy"]["overrides"]:
        override.pop("strict_equality", None)
    assert _strict_override_violations(without_flag) == ["strict_equality is not true"]

    without_table = copy.deepcopy(pyproject)
    without_table["tool"]["mypy"]["overrides"] = []
    assert _strict_override_violations(without_table) == [
        f"no [[tool.mypy.overrides]] table covers {_PACKAGE_GLOB}"
    ]
