"""Expose the changelog to the client.

An MCP server is upgraded underneath its user: the client reconnects one day
and the tool list has changed, with nothing to say why. `whats_new` answers
that from the shipped CHANGELOG.md, so the release notes live in one file
rather than being duplicated into a docstring that then drifts.

The file is parsed, not just printed, so a client can ask for one version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__

# Keep-a-Changelog style heading: "## [0.3.0] - 2026-09-17" or "## 0.3.0".
_HEADING = re.compile(r"^##\s+\[?v?(?P<ver>\d+\.\d+\.\d+)\]?\s*(?:-\s*(?P<date>.+))?$")


def changelog_path() -> Optional[Path]:
    """The shipped CHANGELOG.md, whether running from a clone or a wheel."""
    here = Path(__file__).resolve().parent
    for cand in (here / "CHANGELOG.md", here.parent / "CHANGELOG.md"):
        if cand.is_file():
            return cand
    return None


def parse_changelog(text: str) -> List[Tuple[str, str, str]]:
    """[(version, date, body)] in file order (newest first by convention)."""
    entries: List[Tuple[str, str, str]] = []
    ver = date = None
    body: List[str] = []
    for line in text.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            if ver is not None:
                entries.append((ver, date or "", "\n".join(body).strip()))
            ver = m.group("ver")
            date = (m.group("date") or "").strip()
            body = []
        elif ver is not None:
            body.append(line)
    if ver is not None:
        entries.append((ver, date or "", "\n".join(body).strip()))
    return entries


def load_entries() -> List[Tuple[str, str, str]]:
    p = changelog_path()
    if p is None:
        return []
    return parse_changelog(p.read_text(encoding="utf-8", errors="replace"))


def _fmt(ver: str, date: str, body: str, current: str) -> str:
    head = "Version %s" % ver
    if date:
        head += "  (%s)" % date
    if ver == current:
        head += "   <- you are running this"
    return "%s\n%s\n%s" % (head, "-" * len(head), body)


def render_changelog(version: Optional[str] = None) -> str:
    """Format the changelog for a client.

    ``None`` gives the running version, "all" the whole history, otherwise a
    specific version.
    """
    entries = load_entries()
    if not entries:
        return ("No CHANGELOG.md shipped with this install (running %s)."
                % __version__)

    known = [e[0] for e in entries]
    if version and version.lower() == "all":
        return ("abaqus-mcp changelog (running %s)\n\n" % __version__
                + "\n\n".join(_fmt(v, d, b, __version__) for v, d, b in entries))

    want = version or __version__
    for v, d, b in entries:
        if v == want:
            out = _fmt(v, d, b, __version__)
            if version is None and known and known[0] != __version__:
                # Running something older than the newest entry in the file.
                out += ("\n\nNote: this changelog also describes %s, which is "
                        "newer than the %s you are running."
                        % (known[0], __version__))
            return out

    return ("No changelog entry for %r. Known versions: %s\n"
            "Pass 'all' for the full history." % (want, ", ".join(known)))
