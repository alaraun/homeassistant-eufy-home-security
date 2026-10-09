"""Static checks of the packaging: manifest, hacs.json, release version, translations, workflows.

hassfest and the HACS action (``.github/workflows/``) check much of this in CI; these
tests catch a break locally, at the commit that makes it. Each guard is a pure detector
over data, asserted silent on the real tree and loud on a staged break, so a guard that
stops detecting fails instead of passing.
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import homeassistant
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPONENT_ROOT = _REPO_ROOT / "custom_components" / "eufy_home_security"
_MANIFEST_PATH = _COMPONENT_ROOT / "manifest.json"
_HACS_PATH = _REPO_ROOT / "hacs.json"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

# Home Assistant's loader accepts CALVER, SEMVER, SIMPLEVER, BUILDVER and PEP440; this
# project ships SemVer. A version that does not parse keeps the integration from loading,
# with only a log line.
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# HACS requires domain, documentation, issue_tracker, codeowners, name and
# version for an integration (hacs.xyz/docs/publish/integration); the rest are
# what this project adds.
_REQUIRED_MANIFEST_KEYS = (
    "codeowners",
    "config_flow",
    "documentation",
    "domain",
    "integration_type",
    "iot_class",
    "issue_tracker",
    "loggers",
    "name",
    "requirements",
    "version",
)

# The integration's one runtime dependency: the eufy-home-security library,
# which owns all eufy logic. cryptography is not declared: Home Assistant core
# pins it, and hassfest rejects a custom integration re-declaring a core
# requirement. These tests hold the rule locally, including the PEP 440 shape.
# An exact pin: Home Assistant installs a requirement only when the installed
# version does not satisfy it, so a wildcard never upgrades an existing install.
_LIBRARY_REQUIREMENT = "eufy-home-security==0.3.3"
_SANCTIONED_REQUIREMENTS = [_LIBRARY_REQUIREMENT]


def _load_manifest() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text())


def _load_hacs() -> dict[str, Any]:
    return json.loads(_HACS_PATH.read_text())


def _requirement_violations(requirements: list[str]) -> list[str]:
    """Names every unsanctioned, missing and duplicated requirement.

    Compared by exact string, never by package name: `eufy-home-security>=0.1`
    names the right package with the wrong specifier and is refused.
    """
    violations = [entry for entry in requirements if entry not in _SANCTIONED_REQUIREMENTS]
    violations.extend(
        f"missing: {entry}" for entry in _SANCTIONED_REQUIREMENTS if entry not in requirements
    )
    seen: set[str] = set()
    reported: set[str] = set()
    for entry in requirements:
        if entry in seen and entry not in reported:
            violations.append(f"duplicate: {entry}")
            reported.add(entry)
        seen.add(entry)
    return violations


# The library's tree, and the opt-in cloud push stack it runs (firebase-messaging).
_LIBRARY_LOGGERS = ["eufy_home_security", "firebase_messaging"]


def _invalid_requirements(requirements: list[str]) -> list[str]:
    """Every entry that does not parse as a PEP 508 requirement.

    hassfest's PACKAGE_REGEX accepts `==0.1.x`; `packaging` does not,
    and neither does Home Assistant's `is_installed` at setup.
    """
    invalid: list[str] = []
    for entry in requirements:
        try:
            Requirement(entry)
        except InvalidRequirement:
            invalid.append(entry)
    return invalid


def _home_assistant_dependency_names() -> frozenset[str]:
    """PEP 503 canonical names Home Assistant pins or declares as its own dependency.

    Reads the installed core's package_constraints.txt and its Requires-Dist,
    never a hardcoded list, so raising the HA harness moves this set with it.
    """
    constraints = Path(homeassistant.__file__).parent / "package_constraints.txt"
    assert constraints.is_file(), (
        f"{constraints} is missing; without it the HA-pin guard would pass vacuously"
    )

    names: set[str] = set()
    for raw in constraints.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(canonicalize_name(Requirement(line).name))
    for entry in importlib.metadata.requires("homeassistant") or []:
        names.add(canonicalize_name(Requirement(entry).name))

    assert {"cryptography", "protobuf"} <= names, (
        f"Home Assistant's pinned names lack cryptography or protobuf, which it is "
        f"known to pin; the constraints parse is broken and the guard would pass "
        f"vacuously (read {len(names)} names from {constraints})"
    )
    return frozenset(names)


def _home_assistant_pinned_requirements(
    requirements: list[str], ha_names: frozenset[str]
) -> list[str]:
    """Every parsable entry naming a package Home Assistant already pins.

    Unparsable entries are `_invalid_requirements`' job and are skipped here.
    """
    pinned: list[str] = []
    for entry in requirements:
        try:
            name = Requirement(entry).name
        except InvalidRequirement:
            continue
        if canonicalize_name(name) in ha_names:
            pinned.append(entry)
    return pinned


def _logger_violations(loggers: object) -> list[str]:
    """Every way the manifest loggers differ from exactly the library's loggers."""
    if not isinstance(loggers, list):
        return ["loggers must be a list"]
    if loggers == _LIBRARY_LOGGERS:
        return []
    extra = [name for name in loggers if name not in _LIBRARY_LOGGERS]
    missing = [name for name in _LIBRARY_LOGGERS if name not in loggers]
    return [
        (
            f"loggers must be exactly {_LIBRARY_LOGGERS}, got {loggers!r} "
            f"(extra: {extra}, missing: {missing})"
        )
    ]


def _parser_constructions(tree: ast.AST) -> list[ast.Call]:
    """Every ``argparse.ArgumentParser(...)`` construction in the module.

    Walks the AST rather than grepping, so a docstring or comment naming the
    class (this module's own docstring would otherwise trip it) is invisible.
    """
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name == "ArgumentParser":
            found.append(node)
    return found


def _defines_argument_parser(source: str) -> bool:
    """True when the module constructs an ``argparse.ArgumentParser`` anywhere."""
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a syntax error is a louder failure
        return False
    return bool(_parser_constructions(tree))


def _integration_only_violations(paths: list[Path]) -> list[str]:
    """Names every shipped module that is a CLI entry point.

    Any ``argparse.ArgumentParser`` construction is a violation, ``__main__``
    guard or not.
    """
    violations: list[str] = []
    for path in paths:
        if _defines_argument_parser(path.read_text()):
            violations.append(f"{path}: constructs an argparse.ArgumentParser")
    return violations


def _ha_release_series(version: str) -> tuple[int, int]:
    """(year, month) of a Home Assistant CalVer string such as '2026.9.0'."""
    year, month = version.split(".")[:2]
    return int(year), int(month)


def test_manifest_and_hacs_json() -> None:
    manifest = _load_manifest()
    hacs = _load_hacs()

    missing = [key for key in _REQUIRED_MANIFEST_KEYS if key not in manifest]
    assert not missing, (
        f"manifest.json is missing {missing}; Home Assistant and HACS both read "
        f"this file before any of this integration's code runs"
    )

    assert manifest["domain"] == "eufy_home_security", manifest["domain"]
    assert manifest["iot_class"] == "local_push", (
        f"iot_class must be local_push, got {manifest['iot_class']!r}: detections, "
        f"doorbell presses and alarm state arrive by local push from the base, and the "
        f"45 s poll stays authoritative for guard mode. Core precedent: Reolink and "
        f"UniFi Protect are local_push with a polling fallback"
    )
    assert manifest["integration_type"] == "hub", (
        f"integration_type must be hub (a station that fans out to sub-devices), "
        f"got {manifest['integration_type']!r}"
    )
    assert manifest["config_flow"] is True, (
        "config_flow must be true — a HACS hub integration is config-entry-only "
        "and YAML platform setup is not an option"
    )
    assert _SEMVER.match(str(manifest["version"])), (
        f"manifest version {manifest['version']!r} does not parse as SemVer; "
        f"homeassistant/loader.py blocks a custom integration whose version does "
        f"not parse from loading at all, logging an error and returning None — "
        f"the integration then simply never appears in Add Integration"
    )

    assert hacs["name"], "hacs.json must carry a name — it is the only required key"
    assert "zip_release" not in hacs, (
        "hacs.json must not declare zip_release: HACS installs this repository "
        "from the release tag's source tree, and no release builds an archive"
    )


def test_manifest_key_order() -> None:
    # Ordering is the property under test, so read the file text with an
    # order-preserving parse rather than comparing a plain dict.
    keys = list(json.loads(_MANIFEST_PATH.read_text()).keys())

    assert keys[:2] == ["domain", "name"], (
        f"manifest.json must open with domain then name, got {keys[:2]}; hassfest "
        f"requires this order"
    )
    rest = keys[2:]
    assert rest == sorted(rest), (
        f"manifest.json keys after domain/name must be alphabetical, got {rest}"
    )


def test_requirements_declare_only_the_library() -> None:
    requirements = _load_manifest()["requirements"]

    assert _requirement_violations(requirements) == [], (
        f"manifest.json requirements must be exactly {_SANCTIONED_REQUIREMENTS}, "
        f"got {requirements}: Home Assistant installs every entry here into its "
        f"own venv at setup, so an unreviewed entry is an unreviewed dependency "
        f"on every user's instance"
    )
    assert requirements == _SANCTIONED_REQUIREMENTS, (
        f"manifest.json requirements must equal {_SANCTIONED_REQUIREMENTS} as an "
        f"ordered list, got {requirements}"
    )

    # Fail-first: every staged break must be flagged, or this guard is decorative.
    staged = [*_SANCTIONED_REQUIREMENTS, "totally-not-a-typosquat>=1.0"]
    assert _requirement_violations(staged) == ["totally-not-a-typosquat>=1.0"], (
        "the requirement guard failed to flag a staged extra dependency — the "
        "detector stopped detecting"
    )
    assert _requirement_violations([]) == [f"missing: {_LIBRARY_REQUIREMENT}"], (
        "an empty requirements list must be refused as missing the library"
    )
    assert _requirement_violations([_LIBRARY_REQUIREMENT, _LIBRARY_REQUIREMENT]) == [
        f"duplicate: {_LIBRARY_REQUIREMENT}"
    ], "a duplicated entry must be refused even though every element is sanctioned"
    assert _requirement_violations(["eufy-home-security>=0.1"]) == [
        "eufy-home-security>=0.1",
        f"missing: {_LIBRARY_REQUIREMENT}",
    ], (
        "the guard compares exact strings, not package names: the right package "
        "with the wrong specifier must be refused"
    )
    assert _requirement_violations(["cryptography>=44.0.0", _LIBRARY_REQUIREMENT]) == [
        "cryptography>=44.0.0"
    ], "re-adding cryptography, which Home Assistant core pins, must be refused"


def test_requirements_are_valid_pep_440_and_not_pinned_by_home_assistant() -> None:
    requirements = _load_manifest()["requirements"]
    ha_names = _home_assistant_dependency_names()

    assert _invalid_requirements(requirements) == [], (
        f"manifest.json requirements {requirements} carry an invalid PEP 440 "
        f"specifier: Home Assistant's is_installed parses each entry at setup and "
        f"raises on one that does not parse, so the integration fails to load"
    )
    assert _home_assistant_pinned_requirements(requirements, ha_names) == [], (
        f"manifest.json requirements {requirements} re-declare a package Home "
        f"Assistant core already pins, which fights HA's own pin. hassfest "
        f"rejects this in CI; this test catches it locally"
    )

    # Fail-first: each detector must flag its staged break.
    assert _invalid_requirements(["eufy-home-security==0.1.*"]) == [], (
        "the PEP 440 guard flags the valid wildcard specifier"
    )
    assert _invalid_requirements(["eufy-home-security==0.1.x", "bad req"]) == [
        "eufy-home-security==0.1.x",
        "bad req",
    ], "the PEP 440 guard failed to flag ==0.1.x and a spaced name, both invalid"
    assert _home_assistant_pinned_requirements(["cryptography>=44.0.0"], ha_names) == [
        "cryptography>=44.0.0"
    ], "the HA-pin guard failed to flag cryptography, which Home Assistant pins"
    assert _home_assistant_pinned_requirements(["CRYPTOGRAPHY>=44"], ha_names) == [
        "CRYPTOGRAPHY>=44"
    ], "the HA-pin guard must compare PEP 503 canonical names, not raw strings"
    assert _home_assistant_pinned_requirements(["eufy-home-security==0.1.*"], ha_names) == [], (
        "the HA-pin guard flags the library, which Home Assistant does not pin"
    )


def test_manifest_loggers_name_the_library() -> None:
    # `eufy_home_security` covers the library's `.wire`, `.secrets` and every
    # other child logger. Home Assistant adds
    # `custom_components.eufy_home_security` for the integration itself
    # (components/logger/helpers.py), so listing it here would be redundant.
    # `firebase_messaging` is the cloud push client, which logs outside the
    # library's tree.
    loggers = _load_manifest()["loggers"]
    assert _logger_violations(loggers) == [], (
        f"manifest.json loggers must be exactly {_LIBRARY_LOGGERS}, got {loggers!r}"
    )

    # Fail-first: every staged break must be flagged.
    assert _logger_violations(["custom_components.eufy_home_security"]), (
        "the logger guard failed to flag the integration's own logger in place of the library's"
    )
    assert _logger_violations([]), "the logger guard failed to flag an empty logger list"
    assert _logger_violations(["eufy_home_security"]), (
        "the logger guard failed to flag a list without the push client's logger"
    )
    assert _logger_violations("eufy_home_security"), (
        "the logger guard failed to flag a string where a list is required"
    )


def test_component_tree_is_integration_only(tmp_path: Path) -> None:
    # Assert against the file listing, not an import: a stale .pyc can satisfy an import.
    shipped = sorted(_COMPONENT_ROOT.rglob("*.py"))
    assert shipped, f"no python modules found under {_COMPONENT_ROOT}"

    violations = _integration_only_violations(shipped)
    assert violations == [], (
        f"the shipped custom_components/eufy_home_security/ tree must carry only "
        f"integration code: an argument parser has no business inside a Home "
        f"Assistant process. The library ships its "
        f"own CLI. Offending modules: {violations}"
    )

    # Fail-first, every arm of the detector, on files staged under tmp_path.
    staged_parser = tmp_path / "entry.py"
    staged_parser.write_text(
        'import argparse\n\nif __name__ == "__main__":\n    argparse.ArgumentParser()\n'
    )
    assert _integration_only_violations([staged_parser]), (
        "the tree guard failed to flag a staged argparse.ArgumentParser construction "
        "behind a __main__ guard — no such exemption exists any more"
    )
    assert _defines_argument_parser("import argparse\np = argparse.ArgumentParser()\n"), (
        "the tree guard failed to flag a staged argparse.ArgumentParser construction"
    )
    assert not _defines_argument_parser(
        '"""A docstring merely naming ArgumentParser must not trip the guard."""\n'
    ), "the tree guard flags a mention in a docstring — it is grepping, not parsing"


def test_hacs_and_test_harness_pin_the_same_core() -> None:
    # pytest-homeassistant-custom-component hard-pins ONE Home Assistant version
    # per release. Raising hacs.json's floor without bumping the pin means the
    # test suite imports a different core than the one HACS gates installs on,
    # so every assertion in this repository would be made against the wrong
    # platform. The mapping lives here, in one place, for that reason.
    _PHCC_RELEASE_TO_HA_SERIES = {
        "0.13.355": (2026, 8),
        "0.13.365": (2026, 9),
    }

    hacs_floor = _load_hacs()["homeassistant"]
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text())
    dev_group = pyproject["dependency-groups"]["dev"]

    pins = [
        entry.split("==", 1)[1]
        for entry in dev_group
        if entry.startswith("pytest-homeassistant-custom-component==")
    ]
    assert len(pins) == 1, (
        f"expected exactly one pinned pytest-homeassistant-custom-component in "
        f"the dev dependency group, found {pins}"
    )
    phcc_version = pins[0]

    assert phcc_version in _PHCC_RELEASE_TO_HA_SERIES, (
        f"pytest-homeassistant-custom-component was bumped to {phcc_version} "
        f"without recording which Home Assistant release it pins; add it to "
        f"_PHCC_RELEASE_TO_HA_SERIES and check hacs.json's floor against it"
    )

    assert _ha_release_series(hacs_floor) == _PHCC_RELEASE_TO_HA_SERIES[phcc_version], (
        f"hacs.json declares Home Assistant {hacs_floor} but the test harness "
        f"pins pytest-homeassistant-custom-component=={phcc_version}, which "
        f"carries {_PHCC_RELEASE_TO_HA_SERIES[phcc_version]}. These must name "
        f"the same release: bump them in the same commit or the suite tests a "
        f"different core than HACS gates on"
    )


# ---------------------------------------------------------------------------
# Translations
#
# Home Assistant reads `<integration>/translations/<lang>.json` at runtime
# (helpers/translation.py). A custom integration never runs Core's translation
# build script, so a `[%key:common::...%]` reference written into that file is
# shown to the user verbatim as raw bracketed text instead of a sentence.
# `strings.json` stays the authoring source and
# is what hassfest reads; the runtime file is authoritative for what a user
# actually sees, and the two must agree key for key.
# ---------------------------------------------------------------------------

_STRINGS_PATH = _COMPONENT_ROOT / "strings.json"
_EN_PATH = _COMPONENT_ROOT / "translations" / "en.json"

# The prefix of a Core translation reference, assembled rather than written so
# this file can be grepped for the literal without matching itself.
_CORE_REFERENCE_PREFIX = "[" + "%key:"

# An error string this short is a label ("Cannot connect"), not a remedy.
_MIN_REMEDY_LENGTH = 40


def _remedy_error_keys() -> set[str]:
    """Every error key the flow can show: the value of each ``const`` name ``ERROR_*``.

    Read from the shipped constants rather than a hand list, so an error added by
    a later change is held to the remedy bar without anybody remembering to list it.
    """
    from custom_components.eufy_home_security import const

    return {
        value
        for name, value in vars(const).items()
        if name.startswith("ERROR_") and isinstance(value, str)
    }


def _label_like(errors: Mapping[str, str], keys: set[str]) -> dict[str, str]:
    """The keys whose string is missing or at most ``_MIN_REMEDY_LENGTH`` characters."""
    return {
        key: errors.get(key, "") for key in keys if len(errors.get(key, "")) <= _MIN_REMEDY_LENGTH
    }


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf of a nested JSON object, keyed by its dotted path."""
    if not isinstance(node, dict):
        return {prefix: node}
    flat: dict[str, Any] = {}
    for key, value in node.items():
        flat.update(_flatten(value, f"{prefix}.{key}" if prefix else key))
    return flat


def test_translations_are_literal_and_complete() -> None:
    """Both files parse, agree key for key, and carry literal English only."""
    strings = json.loads(_STRINGS_PATH.read_text())
    runtime = json.loads(_EN_PATH.read_text())

    # The WHOLE document, not just `config`: the `issues`, `entity` and
    # `exceptions` subtrees count too, and a subtree the
    # parity check does not look at is a subtree that can drift.
    authored = _flatten(strings)
    served = _flatten(runtime)

    missing = sorted(set(authored) - set(served))
    assert not missing, (
        f"{missing} exist in strings.json but not in translations/en.json. "
        f"The runtime file is the one Home Assistant actually reads — a key "
        f"only in strings.json is a string no user will ever see."
    )
    extra = sorted(set(served) - set(authored))
    assert not extra, (
        f"{extra} exist in translations/en.json but not in strings.json. The "
        f"two must agree key for key or hassfest and the UI disagree."
    )

    empty = sorted(k for k, v in served.items() if not isinstance(v, str) or not v.strip())
    assert not empty, f"empty or non-string translation values: {empty}"

    referenced = sorted(
        k
        for flat in (authored, served)
        for k, v in flat.items()
        if isinstance(v, str) and v.startswith(_CORE_REFERENCE_PREFIX)
    )
    assert not referenced, (
        f"{referenced} use a Core translation reference. Custom integrations "
        f"never run Core's translation build script, so the user is shown the "
        f"raw bracketed key instead of a sentence."
    )


def test_every_error_key_carries_a_remedy_not_a_label() -> None:
    """A user told 'Cannot connect' learns nothing they did not already know."""
    runtime = json.loads(_EN_PATH.read_text())
    # A missing subtree is read as empty, so a constant without its string fails.
    errors = runtime.get("config", {}).get("error", {})

    short = _label_like(errors, _remedy_error_keys())
    assert short == {}, (
        f"these error strings are missing or are labels rather than remedies: "
        f"{short}. Each failure has a different thing the user must DO about it, "
        f"and the message is where that is said."
    )

    # Fail-first: a label and a missing string are both flagged, a remedy is not.
    assert _label_like({"x": "Cannot connect"}, {"x"}) == {"x": "Cannot connect"}
    assert _label_like({}, {"gone"}) == {"gone": ""}
    remedy = "Home Assistant could not reach the station; check it is powered on."
    assert _label_like({"ok": remedy}, {"ok"}) == {}


def test_every_step_and_error_the_flow_can_show_has_a_translation() -> None:
    """The flow, const.py and both JSON files, held to one vocabulary.

    Reads the shipped constants rather than a hand-copied list, so a step id
    or error key added later without its string is caught here rather than by
    a user staring at an untranslated form.
    """
    from custom_components.eufy_home_security import const

    document = json.loads(_EN_PATH.read_text())
    runtime = document.get("config", {})

    def constants(prefix: str) -> set[str]:
        return {
            value
            for name, value in vars(const).items()
            if name.startswith(prefix) and isinstance(value, str)
        }

    step_ids = constants("STEP_")
    assert step_ids, "const.py defines no STEP_ constant; this check would pass vacuously"
    missing_steps = sorted(step_ids - set(runtime.get("step", {})))
    assert not missing_steps, (
        f"config-flow steps {missing_steps} have no translated section; Home "
        f"Assistant would render the form with no title and no field labels"
    )

    missing_errors = sorted(constants("ERROR_") - set(runtime.get("error", {})))
    assert not missing_errors, (
        f"error keys {missing_errors} have no translated string; the user "
        f"would be shown the bare key"
    )

    # Missing top-level subtrees count as empty, so a constant without its
    # string fails here instead of reaching a user as a bare key.
    missing_issues = sorted(constants("ISSUE_") - set(document.get("issues", {})))
    assert not missing_issues, (
        f"repair issues {missing_issues} have no entry under the top-level issues "
        f"key; the repair would show with no title and no description"
    )

    missing_exceptions = sorted(constants("EXC_") - set(document.get("exceptions", {})))
    assert not missing_exceptions, (
        f"translated exceptions {missing_exceptions} have no entry under the top-level "
        f"exceptions key; the user would see the bare translation key"
    )


# release-please bumps manifest.json's version in each release PR, together with its own
# manifest; the release workflow checks a pushed tag against manifest.json.
_RELEASE_PLEASE_CONFIG_PATH = _REPO_ROOT / "release-please-config.json"
_RELEASE_PLEASE_MANIFEST_PATH = _REPO_ROOT / ".release-please-manifest.json"
_MANIFEST_VERSION_FILE = {
    "type": "json",
    "path": "custom_components/eufy_home_security/manifest.json",
    "jsonpath": "$.version",
}


def _release_version_violations(
    manifest_version: object, release_manifest: Mapping[str, Any], config: Mapping[str, Any]
) -> list[str]:
    """Every way manifest.json's version and the release-please setup disagree."""
    violations: list[str] = []
    released = release_manifest.get(".")
    if manifest_version != released:
        violations.append(
            f"manifest.json version {manifest_version!r} != release-please version {released!r}"
        )
    package = config.get("packages", {}).get(".", {})
    if _MANIFEST_VERSION_FILE not in package.get("extra-files", []):
        violations.append("release-please extra-files do not bump manifest.json $.version")
    if package.get("include-component-in-tag") is not False:
        violations.append("include-component-in-tag is not false: tags would not be vX.Y.Z")
    return violations


def test_the_config_entry_is_version_2_2_and_the_manifest_carries_the_release_version() -> None:
    """The flow creates 2.2 entries; manifest.json carries the version release-please releases."""
    from custom_components.eufy_home_security.config_flow import EufyHomeSecurityConfigFlow

    release_manifest = json.loads(_RELEASE_PLEASE_MANIFEST_PATH.read_text())
    config = json.loads(_RELEASE_PLEASE_CONFIG_PATH.read_text())
    version = _load_manifest()["version"]
    assert _release_version_violations(version, release_manifest, config) == []
    assert (EufyHomeSecurityConfigFlow.VERSION, EufyHomeSecurityConfigFlow.MINOR_VERSION) == (
        2,
        2,
    ), (
        f"the config flow creates version {EufyHomeSecurityConfigFlow.VERSION}."
        f"{EufyHomeSecurityConfigFlow.MINOR_VERSION} entries; stored entries are 2.2 "
        f"and nothing migrates them"
    )

    # Fail-first: a version mismatch, a dropped extra file and a component tag are flagged.
    assert _release_version_violations("9.9.9", release_manifest, config) == [
        f"manifest.json version '9.9.9' != release-please version {release_manifest['.']!r}"
    ]
    package = config["packages"]["."]
    unbumped = {"packages": {".": {**package, "extra-files": []}}}
    assert _release_version_violations(version, release_manifest, unbumped) == [
        "release-please extra-files do not bump manifest.json $.version"
    ]
    component = {"packages": {".": {**package, "include-component-in-tag": True}}}
    assert _release_version_violations(version, release_manifest, component) == [
        "include-component-in-tag is not false: tags would not be vX.Y.Z"
    ]


# The config-entry update-listener registration. Home Assistant raises
# ValueError when an options flow inheriting OptionsFlowWithReload finishes on an
# entry that has one, so a listener added later
# would break the options flow rather than merely duplicating its reload.
_UPDATE_LISTENER_METHOD = "add_update_listener"


def _update_listener_uses(tree: ast.AST) -> list[int]:
    """The line of every reference to the update-listener registration method.

    Any attribute access, not only a call: binding the method without calling it
    is the same mistake one step later. Walks the AST rather than grepping, so a
    docstring or comment naming the method (this module's own does) is invisible.
    """
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == _UPDATE_LISTENER_METHOD
    )


def _option_texts(step: dict[str, Any], part: str) -> dict[str, str]:
    """The options step's ``part`` (data or data_description) of every section, merged."""
    return {key: text for texts in step["sections"].values() for key, text in texts[part].items()}


def test_the_options_flow_offers_exactly_its_thirteen_options() -> None:
    """The entry's options are exactly these thirteen; a new one needs a deliberate change here.

    The detection hold (seconds) and the alarm safety-net timeout (minutes) default
    to 10. The live snapshot is off by default: a live keyframe wakes a battery
    camera. The camera image defaults to hd, the library's suggested configuration.
    Cloud push is off by default: it uses eufy's cloud. The login country is empty by
    default, which means Home Assistant's country, and there is no extra country (each
    costs a sign-in). The event history keeps 7
    days by default: every shown still is also kept as a dated file in the media
    folder. Event videos are off by default (storage) and
    the recording length defaults to 30 s, HA's own camera.record default. The
    sessions per HomeBase take the library's range and default, applied to running
    stations. No config entry update listener is registered beside the reloading
    options flow.
    """
    from eufy_home_security import DEFAULT_STATION_SESSIONS
    from homeassistant.config_entries import OptionsFlowWithReload

    from custom_components.eufy_home_security.config_flow import (
        OPTIONS_SCHEMA,
        OPTIONS_SECTIONS,
        EufyHomeSecurityConfigFlow,
        EufyHomeSecurityOptionsFlow,
    )
    from custom_components.eufy_home_security.const import (
        CONF_ALARM_TIMEOUT,
        CONF_CAMERA_IMAGE,
        CONF_CLOUD_PUSH,
        CONF_COUNTRY,
        CONF_DETECTION_HOLD,
        CONF_EVENT_HISTORY_DAYS,
        CONF_EVENT_VIDEOS,
        CONF_EXTRA_COUNTRIES,
        CONF_LIVE_SNAPSHOT,
        CONF_RECORD_LENGTH,
        CONF_SCAN_REGIONS,
        CONF_SESSION_PROBE,
        CONF_STATION_SESSIONS,
        DEFAULT_EVENT_HISTORY_DAYS,
        DEFAULT_RECORD_LENGTH_SECONDS,
        OPTIONS_SECTION_MORE_COUNTRIES,
        OPTIONS_STEP_INIT,
    )

    assert "async_get_options_flow" in EufyHomeSecurityConfigFlow.__dict__, (
        "the config flow offers no options flow"
    )
    assert issubclass(EufyHomeSecurityOptionsFlow, OptionsFlowWithReload), (
        "the options flow does not reload the entry when the option changes. The "
        "option is read at platform setup, so without the reload nothing a user "
        "can see would change"
    )

    expected = [
        CONF_DETECTION_HOLD,
        CONF_ALARM_TIMEOUT,
        CONF_CAMERA_IMAGE,
        CONF_LIVE_SNAPSHOT,
        CONF_STATION_SESSIONS,
        CONF_RECORD_LENGTH,
        CONF_EVENT_HISTORY_DAYS,
        CONF_EVENT_VIDEOS,
        CONF_COUNTRY,
        CONF_SESSION_PROBE,
        CONF_CLOUD_PUSH,
        CONF_EXTRA_COUNTRIES,
        CONF_SCAN_REGIONS,
    ]
    keys = [str(key) for key in OPTIONS_SCHEMA.schema]
    assert keys == expected, (
        f"the options schema offers {keys}; this entry has exactly thirteen options, and "
        f"another one added here without a decision would ship unannounced"
    )
    assert [key for keys in OPTIONS_SECTIONS.values() for key in keys] == expected, (
        "the options sections do not hold each option exactly once, in schema order"
    )
    (
        hold,
        timeout,
        camera_image,
        live,
        sessions,
        length,
        history_days,
        videos,
        country,
        probe,
        push,
        extra_countries,
        scan_regions,
    ) = OPTIONS_SCHEMA.schema
    assert push.default() is False, "cloud push uses eufy's cloud: opt-in only"
    assert country.default() == "", "an empty login country means Home Assistant's"
    assert extra_countries.default() == [], "each extra country costs a sign-in: none by default"
    assert scan_regions.default() is False, (
        "asking every region may cost a sign-in on each fetch: opt-in only"
    )
    assert history_days.default() == DEFAULT_EVENT_HISTORY_DAYS
    assert videos.default() is False, "event videos take storage: opt-in only"
    assert length.default() == DEFAULT_RECORD_LENGTH_SECONDS == 30
    assert sessions.default() == DEFAULT_STATION_SESSIONS
    assert hold.default() == 10, "the detection hold does not default to 10 s"
    assert timeout.default() == 10, "the alarm timeout does not default to 10 min"
    assert camera_image.default() == "hd", (
        "the camera image does not default to hd, the library's suggested configuration"
    )
    assert live.default() is False, "the live snapshot must be opt-in: it wakes battery cameras"
    assert probe.default() is True, (
        "the session probe is on by default: a kick-out by another eufy client must "
        "surface as a repair without a user action"
    )

    for path in (_STRINGS_PATH, _EN_PATH):
        document = json.loads(path.read_text())
        step = document["options"]["step"][OPTIONS_STEP_INIT]
        assert list(step["sections"]) == list(OPTIONS_SECTIONS), (
            f"{path.name} names {list(step['sections'])} as the options sections, not "
            f"the form's {list(OPTIONS_SECTIONS)}"
        )
        for name, section_keys in OPTIONS_SECTIONS.items():
            texts = step["sections"][name]
            assert texts["name"], f"{path.name} options section {name} has no name"
            for part in ("data", "data_description"):
                assert set(texts[part]) == set(section_keys), (
                    f"{path.name} {name} {part} covers {sorted(texts[part])}, not the "
                    f"section's fields {sorted(section_keys)}"
                )
        more_countries = step["sections"][OPTIONS_SECTION_MORE_COUNTRIES]
        assert "experimental" in more_countries["name"].lower(), (
            f"{path.name}: the extra countries section is not named experimental"
        )
        assert "session" in more_countries["description"], (
            f"{path.name}: the extra countries section does not warn that it can end a session"
        )
        assert set(document["selector"][CONF_CAMERA_IMAGE]["options"]) == {
            "hd",
            "thumbnail",
            "hd_only",
        }, f"{path.name} does not label exactly the three camera image choices"
        camera_image_text = _option_texts(step, "data_description")[CONF_CAMERA_IMAGE]
        for placeholder in ("{thumbnail_seconds}", "{hd_seconds}"):
            assert placeholder in camera_image_text, (
                f"{path.name} camera image description lost {placeholder}: its timing "
                f"must come from the library's IMAGE_SOURCES"
            )

    registrations = [
        f"{path.relative_to(_COMPONENT_ROOT).as_posix()}:{lineno}"
        for path in sorted(_COMPONENT_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
        for lineno in _update_listener_uses(ast.parse(path.read_text()))
    ]
    assert registrations == [], (
        f"{registrations} register a config entry update listener. Home Assistant "
        f"forbids one beside OptionsFlowWithReload and raises when the options flow "
        f"finishes, and two reload paths for one change is a reload loop."
    )

    # Fail-first: a staged registration is flagged, an unrelated call is not.
    assert _update_listener_uses(ast.parse("entry.add_update_listener(_async_updated)\n")) == [1]
    assert _update_listener_uses(ast.parse("entry.async_on_unload(_async_updated)\n")) == []


def test_image_labels_say_whether_an_image_is_from_an_event_or_live() -> None:
    """The button names, the option label and its choice labels say event or live.

    Refresh event image shows the newest recorded event's image, not a live picture.
    """
    from custom_components.eufy_home_security.const import (
        CAPTURE_LIVE_IMAGE_KEY,
        CONF_CAMERA_IMAGE,
        OPTIONS_STEP_INIT,
        REFRESH_IMAGE_KEY,
    )

    for path in (_STRINGS_PATH, _EN_PATH):
        doc = json.loads(path.read_text())
        step = doc["options"]["step"][OPTIONS_STEP_INIT]
        assert _option_texts(step, "data")[CONF_CAMERA_IMAGE] == "Event image", (
            f"{path.name}: the camera image option label is not Event image"
        )
        assert doc["selector"][CONF_CAMERA_IMAGE]["options"] == {
            "hd": "Thumbnail, then HD",
            "thumbnail": "Thumbnail only",
            "hd_only": "HD only",
        }, f"{path.name}: the Event image choices are not the three short labels"
        buttons = doc["entity"]["button"]
        assert buttons[REFRESH_IMAGE_KEY]["name"] == "Refresh event image", (
            f"{path.name}: the no-wake button is not named Refresh event image"
        )
        assert buttons[CAPTURE_LIVE_IMAGE_KEY]["name"] == "Capture live image", (
            f"{path.name}: the live button is not named Capture live image"
        )
        description = _option_texts(step, "data_description")[CONF_CAMERA_IMAGE]
        for needle in (
            "Refresh event image",
            "Capture live image",
            "Thumbnail, then HD",
            "Thumbnail only",
            "HD only",
            "{thumbnail_seconds}",
            "{hd_seconds}",
        ):
            assert needle in description, (
                f"{path.name}: the Event image description does not mention {needle}"
            )


# ---------------------------------------------------------------------------
# Every third-party GitHub Action is pinned to a full commit SHA, never a branch
# or tag. First-party ``actions/*`` and local ``./`` actions are exempt.
# ---------------------------------------------------------------------------

_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_USES = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^\s'\"#]+)", re.MULTILINE)
_FULL_SHA = re.compile(r"@[0-9a-f]{40}$")


def _unpinned_third_party_actions(workflow_text: str) -> list[str]:
    """Every third-party ``uses:`` reference not pinned to a full commit SHA."""
    unpinned = []
    for ref in _USES.findall(workflow_text):
        if ref.startswith(("actions/", "./", "docker://")):
            continue
        if not _FULL_SHA.search(ref):
            unpinned.append(ref)
    return unpinned


def test_third_party_workflow_actions_are_pinned_to_a_commit_sha() -> None:
    workflows = sorted(_WORKFLOWS_DIR.glob("*.yml"))
    assert workflows, f"no workflow found under {_WORKFLOWS_DIR}"
    third_party_seen = 0
    for workflow in workflows:
        text = workflow.read_text()
        third_party_seen += sum(
            1 for ref in _USES.findall(text) if not ref.startswith(("actions/", "./", "docker://"))
        )
        assert _unpinned_third_party_actions(text) == [], (
            f"{workflow.name} references a third-party action by a moving ref"
        )
    assert third_party_seen >= 2, (
        "the hassfest and HACS actions must both be present for this guard to mean anything"
    )

    # Fail-first: the detector must flag a moving branch and a short SHA.
    staged = (
        "      - uses: hacs/action@main\n"
        "      - uses: home-assistant/actions/hassfest@58bff37\n"
        "      - uses: actions/checkout@v4\n"
    )
    assert _unpinned_third_party_actions(staged) == [
        "hacs/action@main",
        "home-assistant/actions/hassfest@58bff37",
    ]
