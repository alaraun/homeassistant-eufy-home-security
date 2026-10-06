"""Library-boundary proofs.

The dev environment imports the local ``eufy-home-security`` library checkout,
not a copy in site-packages, and the dependencies that library drags in still
satisfy the pins of the installed Home Assistant.

The integration neither parses eufy payloads itself nor feeds pushes through the
poll coordinator. ``EufySecurity`` is built in exactly one place, ``runtime.py``,
which nothing reaches through an imported ``build_client`` name.

Every detector is a pure function, silent on the real tree and proven to flag a
staged break: a boundary check that cannot go red is worthless.
"""

from __future__ import annotations

import ast
import copy
import importlib.metadata
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import homeassistant
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_ROOT = REPO_ROOT / "custom_components" / "eufy_home_security"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
# The sibling checkout the editable uv path source points at.
LIBRARY_CHECKOUT = REPO_ROOT.parent / "python-eufy_home_security"
LIBRARY_DIST = "eufy-home-security"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _resolves_under(module_file: Path, root: Path) -> bool:
    return module_file.resolve().is_relative_to(root.resolve())


# ---------------------------------------------------------------------------
# The dev venv imports the local library checkout
# ---------------------------------------------------------------------------


def _checkout_skip_reason(checkout: Path) -> str | None:
    """Why the local-checkout check does not apply, or None when it does."""
    if (checkout / "pyproject.toml").is_file():
        return None
    return f"no sibling library checkout at {checkout}: the venv runs the library release from PyPI"


def test_the_dev_environment_imports_the_local_checkout(tmp_path: Path) -> None:
    """With the sibling checkout present, the venv must import it, not a copy."""
    import pytest

    if reason := _checkout_skip_reason(LIBRARY_CHECKOUT):
        pytest.skip(reason)

    # Fail-first: the skip applies only to a directory without the checkout.
    assert _checkout_skip_reason(tmp_path / "absent") is not None
    assert _checkout_skip_reason(LIBRARY_CHECKOUT) is None

    import eufy_home_security

    module_file = Path(eufy_home_security.__file__)
    assert _resolves_under(module_file, LIBRARY_CHECKOUT / "src"), (
        f"eufy_home_security imports from {module_file}, not from "
        f"{LIBRARY_CHECKOUT / 'src'}: tests would run against a stale copy "
        f"instead of the checkout under development. Remedy: run `uv sync`; the "
        f"dev group must carry eufy-home-security with the editable path source "
        f"in [tool.uv.sources]"
    )

    assert eufy_home_security.__version__ == importlib.metadata.version(LIBRARY_DIST), (
        f"eufy_home_security.__version__ ({eufy_home_security.__version__}) does "
        f"not match the installed {LIBRARY_DIST} distribution "
        f"({importlib.metadata.version(LIBRARY_DIST)}): the imported module and "
        f"the recorded install have diverged — re-run `uv sync`"
    )

    # Fail-first: a module sitting in a site-packages copy must not count as
    # the checkout, or this guard is decorative.
    staged = tmp_path / "site-packages" / "eufy_home_security" / "__init__.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("")
    assert not _resolves_under(staged, LIBRARY_CHECKOUT / "src"), (
        "the local-checkout detector accepted a staged site-packages copy — the "
        "detector stopped detecting"
    )


# ---------------------------------------------------------------------------
# Installed versions hold Home Assistant's pins
# ---------------------------------------------------------------------------

# The Home Assistant pins that the library's lower bounds (aiohttp>=3.11,
# cryptography>=44, protobuf>=5) could otherwise drag past. A Home Assistant install
# applies the same package_constraints.txt, so holding them here holds them there.
_PINNED = ("protobuf", "cryptography", "aiohttp")


def _constraints_path() -> Path:
    # Located through the installed package, never a hardcoded venv path, so it
    # follows whichever Home Assistant the test harness actually imports.
    return Path(homeassistant.__file__).parent / "package_constraints.txt"


def _parse_constraints(text: str) -> dict[str, Requirement]:
    pins: dict[str, Requirement] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        # Skip blanks, comments and pip options such as `-c other.txt`.
        if not line or line.startswith("-"):
            continue
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate():
            continue
        # PEP 503: `PROTOBUF` and `protobuf`, `a_b.c` and `a-b-c` are one name.
        pins[canonicalize_name(req.name)] = req
    return pins


def _pin_violations(pins: Mapping[str, Requirement], installed: Mapping[str, str]) -> list[str]:
    violations: list[str] = []
    for name in _PINNED:
        pin = pins.get(name)
        if pin is None:
            # Fail closed: a pin that vanished is not a pin that holds.
            violations.append(f"{name}: no pin in Home Assistant's package_constraints.txt")
        elif not pin.specifier.contains(installed[name], prereleases=True):
            violations.append(f"{name} {installed[name]} does not satisfy {pin.specifier}")
    return violations


def test_installed_versions_satisfy_home_assistant_pins() -> None:
    path = _constraints_path()
    assert path.is_file(), (
        f"Home Assistant's package_constraints.txt is missing at {path}: without "
        f"it the drift check has nothing to compare against and would pass "
        f"vacuously"
    )

    installed = {name: importlib.metadata.version(name) for name in _PINNED}
    violations = _pin_violations(_parse_constraints(path.read_text()), installed)
    assert violations == [], (
        f"installed dependencies drifted past Home Assistant's pins: {violations}. "
        f"Home Assistant installs its own pinned versions, so tests run against "
        f"anything else prove nothing about a real install. Remedy: hold the pin "
        f"through [tool.uv] constraint-dependencies, re-run `uv lock` and "
        f"`uv sync`, and never upgrade past Home Assistant's pin"
    )

    # Fail-first: a pin the installed protobuf cannot satisfy names protobuf,
    # and nothing else.
    staged = _parse_constraints("protobuf==0.0.1\ncryptography==48.0.1\naiohttp==3.14.3\n")
    staged_installed = {"protobuf": "6.32.0", "cryptography": "48.0.1", "aiohttp": "3.14.3"}
    flagged = _pin_violations(staged, staged_installed)
    assert len(flagged) == 1 and flagged[0].startswith("protobuf"), (
        f"a staged protobuf==0.0.1 pin must flag protobuf alone, got {flagged}"
    )

    real_pins = _parse_constraints("protobuf==6.32.0\ncryptography==48.0.1\naiohttp==3.14.3\n")
    upgraded = _pin_violations(real_pins, {**staged_installed, "protobuf": "7.36.1"})
    assert len(upgraded) == 1 and "protobuf" in upgraded[0], (
        f"protobuf 7.36.1 against ==6.32.0 must flag protobuf alone, got {upgraded}"
    )

    # Fail-first, empty input: a missing pin fails closed, one entry per name.
    empty = _pin_violations(_parse_constraints(""), staged_installed)
    assert len(empty) == len(_PINNED), (
        f"an empty constraints file must yield one violation per pinned name, got {empty}"
    )
    for name in _PINNED:
        assert any(v.startswith(name) and "no pin" in v for v in empty), (
            f"a missing {name} pin must fail closed with a 'no pin' message, got {empty}"
        )

    # Parser: comments, blanks and pip options are skipped.
    options = _parse_constraints("aiohttp==3.14.3\n# comment\n\n-c other.txt\n")
    assert list(options) == ["aiohttp"] and str(options["aiohttp"].specifier) == "==3.14.3", (
        f"comment, blank and pip-option lines must be skipped, got {options}"
    )

    # Parser: names are PEP 503 canonical.
    canonical = _parse_constraints("PROTOBUF==6.32.0\neufy_home.security==0.1.0\n")
    assert set(canonical) == {"protobuf", "eufy-home-security"}, (
        f"constraint names must be PEP 503 canonical, got {sorted(canonical)}"
    )

    # Parser: a marker that evaluates False skips the line.
    marked = _parse_constraints('foo==1.0 ; python_version < "3.0"\n')
    assert "foo" not in marked, (
        f"a constraint whose marker evaluates False must be skipped, got {marked}"
    )


# ---------------------------------------------------------------------------
# The uv project's shape
# ---------------------------------------------------------------------------


_PROJECT_NAME = "homeassistant-eufy_home_security"
_LIBRARY_SOURCE = {"path": "../python-eufy_home_security", "editable": True}


def _requirement_name(entry: object) -> str | None:
    # Dependency-group entries may be `{include-group = ...}` tables.
    return canonicalize_name(Requirement(entry).name) if isinstance(entry, str) else None


def _uv_project_violations(pyproject: Mapping[str, Any], ha_protobuf: Requirement) -> list[str]:
    violations: list[str] = []
    # Missing tables and keys are violations, never a KeyError.
    project = pyproject.get("project", {})
    tool_uv = pyproject.get("tool", {}).get("uv", {})

    name = project.get("name")
    if name != _PROJECT_NAME:
        violations.append(f"project.name is {name!r}, expected {_PROJECT_NAME!r}")
    if isinstance(name, str) and canonicalize_name(name) == canonicalize_name(LIBRARY_DIST):
        violations.append(
            f"project.name {name!r} normalises to the library's own name {LIBRARY_DIST!r}, "
            f"so uv refuses the editable library source"
        )

    if tool_uv.get("package") is not False:
        violations.append(
            f"tool.uv.package is {tool_uv.get('package')!r}, expected false: this repo is "
            f"loaded by Home Assistant, never built or installed"
        )

    protobuf = [
        Requirement(entry)
        for entry in tool_uv.get("constraint-dependencies", [])
        if _requirement_name(entry) == "protobuf"
    ]
    if len(protobuf) != 1:
        violations.append(
            f"tool.uv.constraint-dependencies must hold exactly one protobuf entry, "
            f"found {[str(r) for r in protobuf]}"
        )
    elif str(protobuf[0].specifier) != str(ha_protobuf.specifier):
        violations.append(
            f"tool.uv.constraint-dependencies pins protobuf{protobuf[0].specifier}, but "
            f"Home Assistant pins protobuf{ha_protobuf.specifier}"
        )

    dev = pyproject.get("dependency-groups", {}).get("dev", [])
    if not any(_requirement_name(entry) == canonicalize_name(LIBRARY_DIST) for entry in dev):
        violations.append(f"dependency-groups.dev does not carry {LIBRARY_DIST}")

    source = tool_uv.get("sources", {}).get(LIBRARY_DIST)
    if source != _LIBRARY_SOURCE:
        violations.append(
            f"tool.uv.sources[{LIBRARY_DIST!r}] is {source!r}, expected {_LIBRARY_SOURCE!r}"
        )
    return violations


def test_the_uv_project_constrains_protobuf_to_the_home_assistant_pin() -> None:
    real = tomllib.loads(PYPROJECT_PATH.read_text())

    ha_pins = _parse_constraints(_constraints_path().read_text())
    assert "protobuf" in ha_pins, (
        "Home Assistant's package_constraints.txt carries no protobuf pin, so the "
        "uv constraint has nothing to be checked against"
    )
    ha_protobuf = ha_pins["protobuf"]

    violations = _uv_project_violations(real, ha_protobuf)
    assert violations == [], (
        f"pyproject.toml no longer has the shape the library boundary depends on: "
        f"{violations}. A plain `uv lock` keeps an already-locked protobuf even "
        f"with the constraint gone, so this test is the only thing that notices. "
        f"Remedy: restore the project name, the editable library source and the "
        f"protobuf constraint, then re-run `uv lock` and `uv sync`"
    )

    # Fail-first: each staged mutation is flagged, naming the mutated field.
    def staged(mutate: Any) -> dict[str, Any]:
        doc = copy.deepcopy(real)
        mutate(doc)
        return doc

    mutations: list[tuple[str, dict[str, Any]]] = [
        (
            "constraint-dependencies",
            staged(lambda d: d["tool"]["uv"].__setitem__("constraint-dependencies", [])),
        ),
        (
            "constraint-dependencies",
            staged(
                lambda d: d["tool"]["uv"].__setitem__(
                    "constraint-dependencies", ["protobuf==7.36.1"]
                )
            ),
        ),
        (
            "project.name",
            staged(lambda d: d["project"].__setitem__("name", "eufy-home-security")),
        ),
        (
            "tool.uv.sources",
            staged(
                lambda d: d["tool"]["uv"]["sources"]["eufy-home-security"].__setitem__(
                    "editable", False
                )
            ),
        ),
        (
            "tool.uv.package",
            staged(lambda d: d["tool"]["uv"].__setitem__("package", True)),
        ),
        (
            "dependency-groups.dev",
            staged(
                lambda d: d["dependency-groups"].__setitem__(
                    "dev",
                    [
                        e
                        for e in d["dependency-groups"]["dev"]
                        if canonicalize_name(Requirement(e).name) != LIBRARY_DIST
                    ],
                )
            ),
        ),
    ]
    for field, doc in mutations:
        flagged = _uv_project_violations(doc, ha_protobuf)
        assert flagged and any(field in v for v in flagged), (
            f"a staged break of {field} was not flagged (got {flagged}) — the "
            f"detector stopped detecting"
        )

    # Missing tables count as violations and never raise.
    assert _uv_project_violations({}, ha_protobuf), (
        "an empty pyproject document must be flagged, not pass or raise KeyError"
    )


# ---------------------------------------------------------------------------
# One package-wide file discovery for both gates
# ---------------------------------------------------------------------------


# One discovery for both gates below. Discovered rather than listed, so a module
# added later is scanned without anybody remembering to add it.
def _package_files(root: Path = INTEGRATION_ROOT) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path, root: Path = INTEGRATION_ROOT) -> str:
    return path.relative_to(root).as_posix()


# Asserted as a SUBSET of the discovered set, so a path mistake or a rename
# cannot make either gate pass vacuously over an empty or wrong file list.
_KNOWN_PACKAGE_MODULES = frozenset({"__init__.py", "config_flow.py", "coordinator.py"})


# ---------------------------------------------------------------------------
# No module feeds the poll coordinator a push
# ---------------------------------------------------------------------------


_PUSH_FEED_NAME = "async_set_updated_data"


def _find_coordinator_push_feeds(tree: ast.AST) -> list[int]:
    # Any read of the attribute, called or not: a bound method handed over as a
    # callback (`listener.subscribe(c.async_set_updated_data)`), aliased or
    # wrapped in functools.partial feeds the coordinator just the same. A string
    # constant equal to the name catches `getattr(c, "async_set_updated_data")`;
    # a docstring that merely mentions it is a longer string and stays silent.
    return [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr == _PUSH_FEED_NAME
        )
        or (isinstance(node, ast.Constant) and node.value == _PUSH_FEED_NAME)
    ]


def test_no_module_in_the_package_feeds_the_poll_coordinator_a_push(tmp_path: Path) -> None:
    """Pushes go through a background task and the dispatcher, never here.

    In Home Assistant 2026.9.2 ``async_set_updated_data`` cancels and
    reschedules the coordinator's poll timer, so routing detection traffic
    through it would postpone the guard-mode poll for as long as a street is
    busy. An AST scan, not a grep: a docstring may name the call in order to
    forbid it.

    It scans the whole package. There is no exemption for any module, class or
    function, not even ``_async_update_data``.
    """
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    assert hasattr(DataUpdateCoordinator, "async_set_updated_data"), (
        "Home Assistant renamed async_set_updated_data — this gate now scans "
        "for a name nothing can call; point it at the replacement"
    )

    files = _package_files()
    discovered = {_relative(p) for p in files}
    assert _KNOWN_PACKAGE_MODULES <= discovered, (
        f"the package scan no longer finds {sorted(_KNOWN_PACKAGE_MODULES - discovered)} "
        f"under {INTEGRATION_ROOT}: a path mistake or rename would let this gate "
        f"pass over the wrong files"
    )

    violations = [
        f"{_relative(path)}:{lineno}"
        for path in files
        for lineno in _find_coordinator_push_feeds(_parse(path))
    ]
    assert not violations, (
        "a module in the package calls async_set_updated_data, which reschedules "
        "the guard-mode poll on every call; deliver pushes through the dispatcher "
        "instead:\n" + "\n".join(violations)
    )

    # Fail-first, stage 1: a push handler feeding the coordinator.
    broken = tmp_path / "broken_push_feed.py"
    broken.write_text("def on_push(coordinator, ev):\n    coordinator.async_set_updated_data(ev)\n")
    assert _find_coordinator_push_feeds(_parse(broken)) == [2], (
        "detector failed to flag a staged async_set_updated_data call"
    )

    # Fail-first, stage 2: no exemption, even inside the poll callback.
    in_update = tmp_path / "broken_update_feed.py"
    in_update.write_text(
        "from homeassistant.helpers.update_coordinator import DataUpdateCoordinator\n"
        "\n"
        "class C(DataUpdateCoordinator):\n"
        "    async def _async_update_data(self):\n"
        "        self.async_set_updated_data({})\n"
    )
    assert len(_find_coordinator_push_feeds(_parse(in_update))) == 1, (
        "detector exempted an async_set_updated_data call inside "
        "_async_update_data, which has no exemption"
    )

    # Fail-first, stage 3: indirect uses feed the coordinator just the same.
    indirect: list[tuple[str, str]] = [
        ("alias", "def on_push(c, x):\n    f = c.async_set_updated_data\n    f(x)\n"),
        (
            "partial",
            (
                "from functools import partial\n\n"
                "def on_push(c, x):\n    partial(c.async_set_updated_data, x)()\n"
            ),
        ),
        ("getattr", 'def on_push(c, x):\n    getattr(c, "async_set_updated_data")(x)\n'),
        ("callback", "def wire(listener, c):\n    listener.subscribe(c.async_set_updated_data)\n"),
    ]
    for stem, source in indirect:
        staged = tmp_path / f"indirect_{stem}.py"
        staged.write_text(source)
        assert _find_coordinator_push_feeds(_parse(staged)), (
            f"detector missed an indirect async_set_updated_data use ({stem}): {source!r}"
        )

    # Negative: a docstring naming the call in order to forbid it is not a use.
    doc_only = tmp_path / "doc_only.py"
    doc_only.write_text('"""Never call async_set_updated_data from a push handler."""\n')
    assert _find_coordinator_push_feeds(_parse(doc_only)) == [], (
        "a docstring that mentions async_set_updated_data must not be flagged"
    )


# ---------------------------------------------------------------------------
# No eufy payload parsing in the integration
# ---------------------------------------------------------------------------

# Eufy payload keys: the push-notification fields, the flat event fields built
# from them, and the parameter-dump fields. Taken from eufy's own code, never
# invented.
#
# Integer param-id literals such as ``ev.get(1210)`` are deliberately not
# flagged. The generic keys (name, channel, cmd, payload, raw, account, params)
# are kept on purpose: a hit on one of them fails loudly and a reviewer decides,
# instead of it being silently missed. Key reads are flagged as a Load subscript,
# ``.get``/``.pop``/``.setdefault`` with the key as first argument, an
# ``in``/``not in`` test and a ``match`` mapping pattern. Known limits: a key
# reached through a named constant, ``ev.get(KEY)``, ``operator.itemgetter``,
# ``dict(**ev)`` unpacking and a ``TypedDict``/dataclass field access are
# invisible to this scan.
_EUFY_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        # push-notification fields
        "cmd",
        "payload",
        "msg_type",
        "event_type",
        "device_sn",
        "name",
        "channel",
        "trigger_time",
        "create_time",
        "file_path",
        "pic_filepath",
        "pic_url",
        "notification_style",
        "rec_content",
        "thumb_path",
        "storage_path",
        "station_sn",
        "account",
        "pic_content",
        "crop_path",
        "detection_type",
        # flat event fields
        "msg_type_name",
        "event_type_name",
        "video_path",
        "pic_path",
        "raw",
        "record_video_path",
        "account_id",
        # parameter-dump fields
        "params",
        "param_type",
        "dev_type",
        "param_value",
    }
)


def _file_package(path: Path) -> tuple[str, ...]:
    return path.parent.relative_to(REPO_ROOT).parts


def _resolve_import_from(node: ast.ImportFrom, package: tuple[str, ...]) -> tuple[str, ...]:
    module = tuple(node.module.split(".")) if node.module else ()
    if node.level == 0:
        return module
    return package[: len(package) - (node.level - 1)] + module


# Mapping methods whose first argument is the key read (or removed) from a
# payload; each is parsing the payload exactly as a .get() is.
_KEY_READ_METHODS = frozenset({"get", "pop", "setdefault"})


def _is_payload_key(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in _EUFY_PAYLOAD_KEYS
    )


def _find_eufy_parsing(tree: ast.AST) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name.startswith("decode_"):
                    hits.append((node.lineno, "decode-name-import"))
        elif isinstance(node, ast.Subscript):
            if (
                isinstance(node.ctx, ast.Load)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value in _EUFY_PAYLOAD_KEYS
            ):
                hits.append((node.lineno, "payload-key-subscript"))
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _KEY_READ_METHODS
                and node.args
                and _is_payload_key(node.args[0])
            ):
                tag = "payload-key-get" if node.func.attr == "get" else "payload-key-method"
                hits.append((node.lineno, tag))
        elif isinstance(node, ast.Compare):
            # `"msg_type" in ev` / `"msg_type" not in ev`: a membership test on a
            # eufy payload key is parsing the payload just as a .get() is.
            if _is_payload_key(node.left) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
            ):
                hits.append((node.lineno, "payload-key-in"))
        elif isinstance(node, ast.MatchMapping):
            if any(_is_payload_key(key) for key in node.keys):
                hits.append((node.lineno, "payload-key-match"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "decode_"
        ):
            # A locally defined decoder is parsing just as an imported one is.
            hits.append((node.lineno, "decode-definition"))
    return hits


_INTEGRATION_PACKAGE = ("custom_components", "eufy_home_security")


def _eufy_parsing_report(root: Path) -> dict[str, list[tuple[int, str]]]:
    """The gate's own control flow over ``root``: sanity subset, then scan.

    Returns the hits by file. The sanity assertion runs first, exactly as in the
    gate, so a tree that trips it is never scanned.
    """
    files = _package_files(root)
    discovered = {_relative(p, root) for p in files}
    assert _KNOWN_PACKAGE_MODULES <= discovered, (
        f"the package scan no longer finds {sorted(_KNOWN_PACKAGE_MODULES - discovered)} "
        f"under {root}: a path mistake or rename would let this gate "
        f"pass over the wrong files"
    )
    hits_by_file: dict[str, list[tuple[int, str]]] = {}
    for path in files:
        if hits := _find_eufy_parsing(_parse(path)):
            hits_by_file[_relative(path, root)] = hits
    return hits_by_file


def test_the_integration_parses_no_eufy_payload(tmp_path: Path) -> None:
    """Eufy payloads are parsed by the library only.

    Flags an imported or defined ``decode_*`` name and a eufy payload key read by
    a Load subscript, a key-reading mapping method, a membership test or a
    ``match`` mapping pattern. No module of the integration may have a hit.
    """
    hits_by_file = _eufy_parsing_report(INTEGRATION_ROOT)

    assert not hits_by_file, (
        "eufy payload parsing found in the integration; it belongs in the "
        "eufy-home-security library (file a change request there):\n"
        + "\n".join(
            f"{rel}:{line}:{tag}"
            for rel in sorted(hits_by_file)
            for line, tag in sorted(hits_by_file[rel])[:5]
        )
    )

    import shutil

    import pytest

    # Fail-first for the gate's ordering: a tree missing a sanity-set module
    # trips the sanity assertion instead of being scanned.
    broken_tree = tmp_path / "broken_tree" / "eufy_home_security"
    shutil.copytree(INTEGRATION_ROOT, broken_tree, ignore=shutil.ignore_patterns("__pycache__"))
    (broken_tree / "coordinator.py").unlink()
    with pytest.raises(AssertionError, match="path mistake"):
        _eufy_parsing_report(broken_tree)

    # Fail-first: each rule flags its staged snippet, with its own tag only.
    positives: list[tuple[str, str, str]] = [
        (
            "decode_name",
            "from .coordinator import decode_detection_types\n",
            "decode-name-import",
        ),
        ("key_get", 'def f(ev):\n    return ev.get("msg_type")\n', "payload-key-get"),
        (
            "key_subscript",
            'def f(payload):\n    return payload["device_sn"]\n',
            "payload-key-subscript",
        ),
        ("decode_def", "def decode_thing(x):\n    return x\n", "decode-definition"),
        (
            "key_pop",
            'def f(ev):\n    return ev.pop("msg_type")\n',
            "payload-key-method",
        ),
        (
            "key_setdefault",
            'def f(ev):\n    return ev.setdefault("device_sn", 1)\n',
            "payload-key-method",
        ),
        ("key_in", 'def f(ev):\n    return "msg_type" in ev\n', "payload-key-in"),
        (
            "key_not_in",
            'def f(ev):\n    return "trigger_time" not in ev\n',
            "payload-key-in",
        ),
        (
            "key_match",
            'def f(ev):\n    match ev:\n        case {"msg_type": m}:\n            return m\n',
            "payload-key-match",
        ),
    ]
    for stem, source, tag in positives:
        staged = tmp_path / f"{stem}.py"
        staged.write_text(source)
        tags = {found for _, found in _find_eufy_parsing(_parse(staged))}
        assert tags == {tag}, (
            f"staged {stem} ({source.strip()!r}) must be flagged {tag!r} alone, "
            f"got {sorted(tags)} — the detector stopped detecting"
        )

    # Negatives: none of these is eufy parsing.
    negatives: list[tuple[str, str]] = [
        ("store_subscript", 'attributes = {}\nattributes["channel"] = 1\n'),
        ("int_param_id", "def f(ev):\n    x = ev.get(1210)\n    return x\n"),
        ("docstring_only", '"""Mentions msg_type and decode_push_notify, nothing more."""\n'),
        ("user_input", 'def f(user_input):\n    return user_input.get("password")\n'),
        ("user_input_pop", 'def f(user_input):\n    return user_input.pop("password")\n'),
        ("user_input_in", 'def f(user_input):\n    return "password" in user_input\n'),
        ("key_in_string", 'def f(text):\n    return text in "msg_type"\n'),
        (
            "match_other_key",
            'def f(d):\n    match d:\n        case {"password": p}:\n            return p\n',
        ),
    ]
    for stem, source in negatives:
        staged = tmp_path / f"{stem}.py"
        staged.write_text(source)
        assert _find_eufy_parsing(_parse(staged)) == [], (
            f"staged {stem} ({source.strip()!r}) is not eufy parsing but was flagged"
        )


# ---------------------------------------------------------------------------
# EufySecurity is built only in runtime.py
# ---------------------------------------------------------------------------

_RUNTIME_MODULE = (*_INTEGRATION_PACKAGE, "runtime")
_CONSTRUCTION_TAGS = frozenset({"import", "attribute", "construct"})


def _find_eufy_security_uses(tree: ast.AST, package: tuple[str, ...]) -> list[tuple[int, str]]:
    """(line, tag) for each way a module could build a second ``EufySecurity``.

    ``import``: an imported ``EufySecurity`` name, ``TYPE_CHECKING`` blocks
    included. ``attribute``: a read of ``.EufySecurity``. ``construct``: a call
    of ``EufySecurity`` by name or attribute. ``build-client-by-name``: an
    import of ``build_client`` from ``runtime``, which the tests' patch of the
    module attribute would miss, building a real client.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if "EufySecurity" in names:
                hits.append((node.lineno, "import"))
            if "build_client" in names and _resolve_import_from(node, package) == _RUNTIME_MODULE:
                hits.append((node.lineno, "build-client-by-name"))
        elif isinstance(node, ast.Attribute):
            if node.attr == "EufySecurity" and isinstance(node.ctx, ast.Load):
                hits.append((node.lineno, "attribute"))
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "EufySecurity") or (
                isinstance(func, ast.Attribute) and func.attr == "EufySecurity"
            ):
                hits.append((node.lineno, "construct"))
    return hits


def test_eufy_security_is_built_only_in_runtime(tmp_path: Path) -> None:
    """Two live clients on one account store would overwrite each other's session."""
    package_files = _package_files()
    runtime_file = INTEGRATION_ROOT / "runtime.py"
    assert runtime_file in package_files
    runtime_tags = {
        tag for _, tag in _find_eufy_security_uses(_parse(runtime_file), _INTEGRATION_PACKAGE)
    }
    assert "construct" in runtime_tags, (
        "runtime.py no longer constructs EufySecurity: the construction site moved or "
        "was renamed, and this gate would pass vacuously over a package with none"
    )

    outside_runtime = [
        f"{_relative(path)}:{lineno}:{tag}"
        for path in package_files
        if path != runtime_file
        for lineno, tag in _find_eufy_security_uses(_parse(path), _file_package(path))
        if tag in _CONSTRUCTION_TAGS
    ]
    assert not outside_runtime, (
        "EufySecurity is imported, read or built outside runtime.py; build every "
        "client through runtime.build_client:\n" + "\n".join(outside_runtime)
    )

    by_name_roots = (REPO_ROOT / "custom_components", REPO_ROOT / "tests")
    by_name = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}"
        for root in by_name_roots
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
        for lineno, tag in _find_eufy_security_uses(_parse(path), _file_package(path))
        if tag == "build-client-by-name"
    ]
    assert not by_name, (
        "build_client is imported by name; call it as runtime.build_client(...) so "
        "the tests' replacement applies:\n" + "\n".join(by_name)
    )

    positives: list[tuple[str, str, tuple[str, ...], set[str]]] = [
        (
            "import",
            "from eufy_home_security import EufySecurity\n",
            _INTEGRATION_PACKAGE,
            {"import"},
        ),
        (
            "type_checking",
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from eufy_home_security.client import EufySecurity\n"
            ),
            _INTEGRATION_PACKAGE,
            {"import"},
        ),
        (
            "attribute",
            "import eufy_home_security\nX = eufy_home_security.EufySecurity\n",
            _INTEGRATION_PACKAGE,
            {"attribute"},
        ),
        (
            "construct_name",
            "def f(EufySecurity, s):\n    return EufySecurity(s)\n",
            _INTEGRATION_PACKAGE,
            {"construct"},
        ),
        (
            "construct_attribute",
            "import eufy_home_security as e\n\ndef f(s):\n    return e.EufySecurity(s)\n",
            _INTEGRATION_PACKAGE,
            {"attribute", "construct"},
        ),
        (
            "build_client_relative",
            "from .runtime import build_client\n",
            _INTEGRATION_PACKAGE,
            {"build-client-by-name"},
        ),
        (
            "build_client_absolute",
            "from custom_components.eufy_home_security.runtime import build_client as b\n",
            ("tests",),
            {"build-client-by-name"},
        ),
    ]
    for stem, source, package, expected in positives:
        staged = tmp_path / f"client_{stem}.py"
        staged.write_text(source)
        tags = {tag for _, tag in _find_eufy_security_uses(_parse(staged), package)}
        assert tags == expected, (
            f"staged {stem} ({source.strip()!r}) must be flagged {sorted(expected)}, got "
            f"{sorted(tags)} — the detector stopped detecting"
        )

    negatives: list[tuple[str, str]] = [
        ("forget_account", "from eufy_home_security import async_forget_account\n"),
        (
            "runtime_attribute",
            "from . import runtime\n\ndef f(h):\n    return runtime.build_client(h, 'e', None)\n",
        ),
    ]
    for stem, source in negatives:
        staged = tmp_path / f"client_ok_{stem}.py"
        staged.write_text(source)
        assert _find_eufy_security_uses(_parse(staged), _INTEGRATION_PACKAGE) == [], (
            f"staged {stem} ({source.strip()!r}) builds no client but was flagged"
        )


# ---------------------------------------------------------------------------
# The tests run on eufy_home_security.testing, never on a stand-in
# ---------------------------------------------------------------------------

# The library objects a test could replace with a mock and still look green.
_LIBRARY_SEAMS = frozenset(
    {"EufySecurity", "Station", "StationSession", "EufyCloudApi", "build_client"}
)
_MOCK_FACTORIES = frozenset(
    {"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "create_autospec"}
)
# What conftest.py must import from the library's testing package.
_TESTING_IMPORTS = frozenset(
    {"SYNTHETIC", "FakeCloud", "FakeStation", "build_eufy_security", "warm_store"}
)
_FAKE_FIXTURES = frozenset({"fake_station", "fake_cloud", "built_clients", "seed_warm_cache"})
# Setup, flow and entity test modules: each must run at least one test on the fakes.
_LIBRARY_TEST_MODULES = (
    "test_init.py",
    "test_config_flow.py",
    "test_alarm_control_panel.py",
    "test_coordinator.py",
    "test_repairs.py",
    # The diagnostics, settings and options modules.
    "test_number.py",
    "test_select.py",
    "test_switch.py",
    "test_sensor.py",
    "test_binary_sensor.py",
    "test_update.py",
    "test_options_flow.py",
    "test_settings.py",
    # Events, alarm and diagnostics download.
    "test_event.py",
    "test_diagnostics.py",
    # Camera snapshots.
    "test_camera.py",
    # Camera buttons.
    "test_button.py",
    # Live video streaming.
    "test_streaming.py",
)


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _names_seam(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id in _LIBRARY_SEAMS) or (
        isinstance(node, ast.Attribute) and node.attr in _LIBRARY_SEAMS
    )


def _is_mock_factory_call(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node.func) in _MOCK_FACTORIES


def _is_patch_call(func: ast.expr) -> bool:
    """``patch(...)``, ``mock.patch(...)`` or ``patch.object(...)``."""
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        if func.attr == "patch":
            return True
        return func.attr == "object" and _callee_name(func.value) == "patch"
    return False


def _string_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) > index:
        arg = call.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _find_library_stand_ins(tree: ast.AST, library_names: frozenset[str]) -> list[tuple[int, str]]:
    """(line, tag) for each way a test could run against something other than the library.

    ``mock-of-library``: a mock factory given a library seam (``MagicMock(spec=EufySecurity)``).
    ``patch-of-library``: ``patch``/``patch.object`` of a seam, by its dotted target's last
    segment or by attribute name. ``setattr-mock``: ``monkeypatch.setattr`` whose new value
    is a mock factory call. ``library-named-class``: a class named like a library export
    or fake, which would shadow it. A mock hidden behind a helper in another module is
    out of this scan's reach.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if node.name in library_names:
                hits.append((node.lineno, "library-named-class"))
            continue
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node.func) in _MOCK_FACTORIES:
            values = [*node.args, *(kw.value for kw in node.keywords)]
            if any(_names_seam(value) for value in values):
                hits.append((node.lineno, "mock-of-library"))
        elif _is_patch_call(node.func):
            target = _string_arg(node, 0)
            attribute = _string_arg(node, 1)
            if (target is not None and target.rsplit(".", 1)[-1] in _LIBRARY_SEAMS) or (
                attribute in _LIBRARY_SEAMS
            ):
                hits.append((node.lineno, "patch-of-library"))
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and _callee_name(node.func.value) == "monkeypatch"
        ):
            # monkeypatch.setattr(target, name, value) or setattr("dotted.path", value).
            value: ast.expr | None = next(
                (kw.value for kw in node.keywords if kw.arg == "value"),
                node.args[-1] if len(node.args) >= 2 else None,
            )
            if _is_mock_factory_call(value):
                hits.append((node.lineno, "setattr-mock"))
    return hits


def _test_functions_taking(tree: ast.AST, fixtures: frozenset[str]) -> list[str]:
    """Names of the test functions that take at least one of ``fixtures``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            params = {arg.arg for arg in (*node.args.args, *node.args.kwonlyargs)}
            if params & fixtures:
                found.append(node.name)
    return found


def test_integration_tests_run_on_the_library_testing_package(tmp_path: Path) -> None:
    """No setup, flow or entity test replaces the library with a mock or a stand-in."""
    import eufy_home_security

    library_names = frozenset(eufy_home_security.__all__) | {"FakeStation", "FakeCloud"}
    tests_root = REPO_ROOT / "tests"
    conftest = _parse(tests_root / "conftest.py")

    imported = {
        alias.name
        for node in ast.walk(conftest)
        if isinstance(node, ast.ImportFrom) and node.module == "eufy_home_security.testing"
        for alias in node.names
    }
    assert _TESTING_IMPORTS <= imported, (
        f"tests/conftest.py no longer imports {sorted(_TESTING_IMPORTS - imported)} from "
        f"eufy_home_security.testing: the fixtures must build on the library's own fakes"
    )

    built_clients = [
        node
        for node in ast.walk(conftest)
        if isinstance(node, ast.FunctionDef) and node.name == "built_clients"
    ]
    assert len(built_clients) == 1, "tests/conftest.py no longer defines the built_clients fixture"
    assert any(
        isinstance(call, ast.Call) and _callee_name(call.func) == "build_eufy_security"
        for call in ast.walk(built_clients[0])
    ), "built_clients no longer builds its clients with build_eufy_security"

    for name in _LIBRARY_TEST_MODULES:
        path = tests_root / name
        assert path.is_file(), f"tests/{name} is missing: this gate would pass over nothing"
        assert _test_functions_taking(_parse(path), _FAKE_FIXTURES), (
            f"tests/{name} has no test taking one of {sorted(_FAKE_FIXTURES)}: its tests "
            f"no longer run on the library's fakes"
        )

    test_files = sorted(p for p in tests_root.glob("*.py") if "__pycache__" not in p.parts)
    stand_ins = [
        f"tests/{path.name}:{lineno}:{tag}"
        for path in test_files
        for lineno, tag in _find_library_stand_ins(_parse(path), library_names)
    ]
    assert not stand_ins, (
        "a test replaces the library with a mock or a stand-in; run it on "
        "eufy_home_security.testing instead:\n" + "\n".join(stand_ins)
    )

    # Fail-first: each rule flags its staged snippet, with its own tag only.
    positives: list[tuple[str, str, str]] = [
        (
            "mock",
            (
                "from unittest.mock import MagicMock\nfrom eufy_home_security import EufySecurity\n"
                "client = MagicMock(spec=EufySecurity)\n"
            ),
            "mock-of-library",
        ),
        (
            "patch",
            (
                "from unittest.mock import patch\n"
                'p = patch("custom_components.eufy_home_security.runtime.build_client")\n'
            ),
            "patch-of-library",
        ),
        (
            "setattr",
            (
                "from unittest.mock import AsyncMock\n\n"
                "def test_x(monkeypatch, runtime):\n"
                '    monkeypatch.setattr(runtime, "build_client", AsyncMock())\n'
            ),
            "setattr-mock",
        ),
        ("class", "class FakeStation:\n    ...\n", "library-named-class"),
    ]
    for stem, source, tag in positives:
        staged = tmp_path / f"stand_in_{stem}.py"
        staged.write_text(source)
        tags = {found for _, found in _find_library_stand_ins(_parse(staged), library_names)}
        assert tags == {tag}, (
            f"staged {stem} ({source.strip()!r}) must be flagged {tag!r} alone, got "
            f"{sorted(tags)}: the detector stopped detecting"
        )

    # Negative: replacing the construction site with a real builder is how conftest works.
    negative = tmp_path / "stand_in_ok_builder.py"
    negative.write_text(
        "def test_x(monkeypatch, runtime):\n"
        "    def build(hass, email, password, *, claims=None):\n"
        "        return None\n"
        '    monkeypatch.setattr(runtime, "build_client", build)\n'
    )
    assert _find_library_stand_ins(_parse(negative), library_names) == [], (
        "a monkeypatch.setattr to a real function is not a mock but was flagged"
    )
    # Fail-first for the fixture check: a module with no fake fixture has no such test.
    no_fakes = tmp_path / "stand_in_no_fakes.py"
    no_fakes.write_text("def test_x(hass):\n    assert hass\n")
    assert _test_functions_taking(_parse(no_fakes), _FAKE_FIXTURES) == []


# Home Assistant's entity platforms of this integration: none may import another.
_PLATFORM_MODULES = frozenset(
    {
        "alarm_control_panel",
        "binary_sensor",
        "button",
        "camera",
        "event",
        "image",
        "number",
        "select",
        "sensor",
        "switch",
        "text",
        "update",
    }
)


def test_no_platform_module_imports_another_platform_module() -> None:
    """Loading one platform must not import another platform's entities."""
    offenders: list[str] = []
    for name in sorted(_PLATFORM_MODULES):
        path = INTEGRATION_ROOT / f"{name}.py"
        package = _file_package(path)
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _resolve_import_from(node, package)
            if target == _INTEGRATION_PACKAGE:
                modules = [alias.name for alias in node.names]
            elif target[:2] == _INTEGRATION_PACKAGE:
                modules = [target[2]]
            else:
                modules = []
            offenders.extend(
                f"{name}.py:{node.lineno} imports {module}"
                for module in modules
                if module in _PLATFORM_MODULES and module != name
            )
    assert not offenders, "\n".join(offenders)
