"""Guards on the TMP feature's two declared contracts: the pinned schema path, and
the discovery route path.

Both exist so a contract has ONE definition; both were previously re-typed at call
sites with nothing pinning the copies equal. A declaration only ends drift if
declining to use it fails, which is what these guards make true (#1197 review).

Guard 1 — every ``adcp/_schemas/...`` citation names a file that actually exists.

A citation is only worth writing if it can be checked. Comments and docstrings
across the TMP surfaces cited ``dist/schemas/3.1.1/...`` — a path that resolves
to nothing in this tree (``dist/`` is gitignored and absent from the repo), so
three separate review rounds each had to re-grep by hand and each one missed
sites, including an operator-facing log line telling an operator to open a path
they cannot open (#1197 review).

The fix is to cite the pinned tree the SDK actually ships
(``adcp/_schemas/<major.minor>/...``, which ``tests/helpers/pinned_schema``
reads). This guard makes that form self-checking: a typo, a renamed schema, or a
spec bump that moves a file fails here instead of misleading the next reader.

Scope: only the resolvable ``adcp/_schemas/`` form is graded. Full upstream
GitHub URLs (the repo's other citation convention) are left alone — they are
checkable by following the link, and pinning a remote fetch into the unit suite
would make it network-dependent.
"""

from __future__ import annotations

import pathlib
import re

from tests.helpers.pinned_schema import schema_root
from tests.unit._architecture_helpers import REPO_ROOT, iter_git_tracked_files

# e.g. adcp/_schemas/3.1/trusted-match/provider-registration.json
_CITATION = re.compile(r"adcp/_schemas/(?P<version>\d+\.\d+)/(?P<ref>[\w./-]+\.json)")

_TEXT_SUFFIXES = {".py", ".md", ".feature", ".html", ".yaml", ".yml", ".txt"}


def _citations():
    """Yield (relative_path, line_number, version, ref) for every citation found."""
    for path in iter_git_tracked_files(REPO_ROOT):
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        rel = path.relative_to(REPO_ROOT)
        if rel.parts and rel.parts[0] not in ("src", "tests", "docs", "alembic", "templates"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _CITATION.finditer(line):
                yield rel, lineno, match.group("version"), match.group("ref")


def test_every_pinned_schema_citation_resolves():
    """Each cited schema file exists in the installed SDK's pinned tree."""
    root = schema_root()
    # schema_root() is already version-scoped (``adcp/_schemas/<major.minor>``),
    # so a citation naming a DIFFERENT minor is itself a drift to report.
    pinned_version = root.name

    offenders: list[str] = []
    for rel, lineno, version, ref in _citations():
        if version != pinned_version:
            offenders.append(f"{rel}:{lineno} cites spec {version}, but the pin is {pinned_version}")
            continue
        if not (root / ref).is_file():
            offenders.append(f"{rel}:{lineno} cites {ref}, which does not exist under {root}")

    assert not offenders, "Unresolvable pinned-schema citations:\n  " + "\n  ".join(offenders)


def test_the_guard_actually_finds_citations():
    """A guard that matched nothing would pass vacuously forever."""
    found = list(_citations())
    assert found, "no adcp/_schemas/... citations found — the citation regex or the scan roots regressed"


# The one place the discovery path may be spelled: the module that declares it and
# registers the route from that declaration.
_DISCOVERY_PATH_FRAGMENT = "tmp-providers/discovery"
_DISCOVERY_PATH_OWNER = "src/routes/tmp_providers.py"
# src/app.py's include_router comment names the mounted path for an operator
# reading the app wiring; it is a comment beside the mount, not a second consumer.
_DISCOVERY_PATH_ALLOWED = frozenset({_DISCOVERY_PATH_OWNER, "src/app.py"})


def test_discovery_path_is_spelled_only_where_it_is_declared():
    """No hand-typed discovery path outside the route module.

    ``DISCOVERY_ROUTE`` is declared in ``src/routes/tmp_providers.py`` and the route
    is registered from it. Every other site — tests, prose, other modules — must
    reference the constant (``DISCOVERY_ROUTE.format(tenant_id=...)``), so editing
    the path cannot leave a stale copy behind. Two suites previously carried 15
    executable literals plus prose restatements of it (#1197 review).
    """
    offenders: list[str] = []
    for path in iter_git_tracked_files(REPO_ROOT):
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        rel = path.relative_to(REPO_ROOT)
        if str(rel) in _DISCOVERY_PATH_ALLOWED:
            continue
        if rel.parts and rel.parts[0] not in ("src", "tests"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if str(rel) == str(pathlib.Path(__file__).relative_to(REPO_ROOT)):
            continue  # this guard names the fragment on purpose
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _DISCOVERY_PATH_FRAGMENT in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")

    assert not offenders, (
        "The discovery path is spelled outside "
        f"{_DISCOVERY_PATH_OWNER}. Reference DISCOVERY_ROUTE instead:\n  " + "\n  ".join(offenders)
    )
