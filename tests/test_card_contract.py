"""The bundled card's contract with the integration and the library.

The card finds entities by translation key or library setting key, calls this
integration's actions, reads its state attributes and recognises one of its error
keys. Each name it uses must still exist here, so a change on either side that would
leave a card row or button silently empty fails this module.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
import yaml
from eufy_home_security.devices import Scope, SettingControl
from eufy_home_security.devices.model_settings import (
    bundled_codes,
    mode_table_settings,
    settings_of,
)

from custom_components.eufy_home_security import card, const

_ROOT: Final = Path(__file__).resolve().parent.parent / "custom_components" / "eufy_home_security"
_SOURCE: Final = card.CARD_FILE.read_text(encoding="utf-8")
_STRINGS: Final = json.loads((_ROOT / "strings.json").read_text(encoding="utf-8"))

# Attributes Home Assistant itself sets on states.
_HA_ATTRIBUTES: Final = frozenset(
    {
        "access_token",
        "assumed_state",
        "device_class",
        "entity_picture",
        "event_type",
        "friendly_name",
        "icon",
        "max",
        "min",
        "options",
        "restored",
        "supported_features",
        "unit_of_measurement",
    }
)
# Section ids inside the card's key tables, not entity keys.
_CARD_WORDS: Final = frozenset({"picture", "detection", "recording", "power", "ptz", "other"})
# The card's tables that name setting keys.
_KEY_TABLES: Final = (
    "GROUPS",
    "ROW_UNCATEGORISED",
    "ROW_ICONS",
    "ROW_LABELS",
    "DEP_ORDER",
    "PRESET_KEYS",
)


def _block(name: str) -> str:
    """The source of ``const <name> = …;`` in the card."""
    start = _SOURCE.index(f"const {name} = ")
    return _SOURCE[start : _SOURCE.index(";\n", start)]


def _names(block: str) -> set[str]:
    """Quoted identifiers and object keys in a block of the card's source."""
    quoted = set(re.findall(r"'([a-z][a-z0-9_]+)'", block))
    keys = set(re.findall(r"(?:^|[{,]\s*)([a-z][a-z0-9_]+):", block, re.MULTILINE))
    return {n for n in quoted | keys if not n.startswith("mdi")}


def _translation_keys() -> set[str]:
    return {key for platform in _STRINGS["entity"].values() for key in platform}


def _built_setting_keys() -> set[str]:
    """Every setting entity key (unique id after ``<serial>_``) the integration builds."""
    keys: set[str] = set()
    for code in bundled_codes():
        for setting in settings_of(code).values():
            keys.add(setting.key)
            if setting.control is SettingControl.TOGGLES:
                keys.update(f"{setting.key}_{member}" for member in setting.flags)
    for scope in Scope:
        keys.update(s.key for s in mode_table_settings(scope))
    return keys


def _built_option_labels() -> set[str]:
    """Every option text a built setting's select can offer (the library's label, else the value)."""
    settings = [s for code in bundled_codes() for s in settings_of(code).values()]
    settings += [s for scope in Scope for s in mode_table_settings(scope)]
    return {s.label(v) or str(v) for s in settings for v in s.values or ()}


def _description_keys() -> set[str]:
    """Entity keys named by constants (``*_KEY``) and descriptions (``key=``)."""
    keys = {v for k, v in vars(const).items() if k.endswith("_KEY") and isinstance(v, str)}
    for path in _ROOT.glob("*.py"):
        keys.update(re.findall(r'key="(\w+)"', path.read_text(encoding="utf-8")))
    return keys


def test_the_card_is_valid_javascript() -> None:
    """``node --check`` accepts the shipped file."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "--check", str(card.CARD_FILE)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_every_translation_key_the_card_looks_for_exists() -> None:
    """Roles and entity kinds the card finds by translation key are still translated here."""
    wanted = set(re.findall(r"(?:^|[{,]\s*)([a-z_]+): '", _block("ROLES"), re.MULTILINE))
    wanted |= set(re.findall(r"tk === '([a-z_]+)'", _SOURCE))
    assert wanted
    missing = sorted(wanted - _translation_keys())
    assert not missing, (
        f"the card looks for translation keys the integration does not have: {missing}"
    )


def test_every_power_reading_key_the_card_looks_for_is_built() -> None:
    """Power readings are found by translation key or entity key."""
    wanted = set(re.findall(r"([a-z_]+): '[a-zA-Z]+'", _block("STAT_KEYS")))
    assert wanted
    missing = sorted(wanted - _translation_keys() - _description_keys() - _built_setting_keys())
    assert not missing, missing


def test_every_action_the_card_calls_is_registered() -> None:
    """``eufy_home_security.<action>`` calls name actions in services.yaml."""
    called = set(re.findall(r"_(?:move|ptz)\([^,]+, '([a-z_]+)'", _SOURCE))
    assert called
    services = yaml.safe_load((_ROOT / "services.yaml").read_text(encoding="utf-8"))
    missing = sorted(called - set(services))
    assert not missing, missing


def test_the_record_action_the_card_calls_is_registered() -> None:
    """The card's record button calls ``eufy_home_security.record`` with a response."""
    called = set(re.findall(r"callService\(DOMAIN, '([a-z_]+)'", _SOURCE))
    assert "record" in called
    services = yaml.safe_load((_ROOT / "services.yaml").read_text(encoding="utf-8"))
    missing = sorted(called - set(services))
    assert not missing, missing


def test_the_station_recording_commands_the_card_sends_are_registered() -> None:
    """The Station sub-tab's websocket commands are registered by the integration.

    The card names each command type once as a constant (``STATION_WS``,
    ``STATION_FETCH_WS``); the integration must register that same type string.
    """
    sent = {_names_ws(name) for name in ("STATION_WS", "STATION_FETCH_WS")}
    assert sent == {"eufy_home_security/recordings", "eufy_home_security/recordings/fetch"}
    integration = "\n".join(p.read_text(encoding="utf-8") for p in _ROOT.glob("*.py"))
    registered = set(re.findall(r"[\"'](eufy_home_security/[a-z_/]+)[\"']", integration))
    registered |= {
        f"{const.DOMAIN}/{suffix}"
        for suffix in re.findall(r"f[\"']\{DOMAIN\}/([a-z_/]+)[\"']", integration)
    }
    missing = sorted(sent - registered)
    assert not missing, f"the card sends websocket commands nothing registers: {missing}"


def test_the_station_list_fields_the_card_sends_are_accepted() -> None:
    """Every field of the card's station list request is a key of the command's schema.

    The card builds the request as ``{ type: STATION_WS, <field>: …, … }`` and adds
    fields as ``req.<field> = …``.
    """
    literal = re.search(r"\{ type: STATION_WS, ([^}]*) \}", _SOURCE)
    assert literal, "the card's station list request"
    sent = set(re.findall(r"(\w+):", literal.group(1)))
    sent |= set(re.findall(r"\breq\.(\w+) =", _SOURCE))
    assert {"entity_id", "limit", "before"} <= sent
    source = (_ROOT / "station_recordings.py").read_text(encoding="utf-8")
    schema = re.search(r"vol\.Required\(\"type\"\): WS_LIST,(.*?)\n    \}\n\)", source, re.DOTALL)
    assert schema, "the list command's schema"
    accepted = set(
        re.findall(r"vol\.(?:Required|Optional)\((?:ATTR_ENTITY_ID|\"(\w+)\")", schema.group(1))
    )
    accepted = {name or "entity_id" for name in accepted}
    missing = sorted(sent - accepted)
    assert not missing, f"the card sends fields the list command refuses: {missing}"


def _names_ws(name: str) -> str:
    """The command type a ``const <name> = '<type>'`` line of the card holds."""
    match = re.search(rf"const {name} = '([^']+)';", _SOURCE)
    assert match, name
    return match.group(1)


def test_every_attribute_the_card_reads_is_published() -> None:
    """Attributes beyond Home Assistant's own are this integration's ``ATTR_*`` values."""
    read = set(re.findall(r"attributes\.([a-z_]+)", _SOURCE))
    ours = {v for k, v in vars(const).items() if k.startswith("ATTR_") and isinstance(v, str)}
    missing = sorted(read - _HA_ATTRIBUTES - ours)
    assert not missing, missing


def test_every_error_key_the_card_recognises_exists() -> None:
    """The card treats ``setting_unconfirmed`` apart from other failed writes."""
    for key in re.findall(r"translation_key === '([a-z_]+)'", _SOURCE):
        assert key in _STRINGS["exceptions"], key


def test_the_documented_settings_groups_are_the_cards() -> None:
    """``settings_groups`` ids in the user doc are the card's groups plus More, in the card's order."""
    ids = re.findall(r"^\s*\['([a-z]+)', '", _block("GROUPS"), re.MULTILINE)
    more = re.findall(r"'([a-z]+)'", _block("MORE_GROUP"))[0]
    doc = (_ROOT.parent.parent / "docs" / "card.md").read_text(encoding="utf-8")
    listed = doc[doc.index("`settings_groups` lists the tab ids") :]
    listed = listed[: listed.index("Without the key")]
    assert re.findall(r"`([a-z]+)`", listed) == [*ids, more]


def test_every_setting_key_the_card_names_is_built() -> None:
    """Group, icon, label, dependent-order and live-restart tables name keys the integration builds."""
    named: set[str] = set()
    for table in _KEY_TABLES:
        named |= _names(_block(table))
    named |= set(re.findall(r"[a-z][a-z0-9_]+", _block("RESTARTS_LIVE").split("=", 1)[1]))
    known = _built_setting_keys() | _translation_keys() | _CARD_WORDS
    unknown = sorted(named - known)
    assert not unknown, f"the card names keys nothing builds: {unknown}"


def test_every_option_the_card_shortens_or_draws_is_offered() -> None:
    """``OPT_SHORT`` and ``OPT_ICONS`` keys are option texts of settings the integration builds."""
    named: set[str] = set()
    for table in ("OPT_SHORT", "OPT_ICONS"):
        named |= set(re.findall(r"(?:^|[{,]\s*)'([^']+)':", _block(table), re.MULTILINE))
        named |= set(re.findall(r"(?:^|[{,]\s*)([A-Za-z_]\w*):", _block(table), re.MULTILINE))
    assert named
    unknown = sorted(named - _built_option_labels())
    assert not unknown, f"the card names options no setting offers: {unknown}"
