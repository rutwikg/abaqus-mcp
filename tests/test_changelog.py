"""Tests for the changelog / whats_new machinery.

The point of `whats_new` is that a client can find out why the tool list
changed under it. That only works if the changelog actually ships inside the
wheel and actually describes the version being run, so those are the things
asserted here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp import __version__
from abaqus_mcp.changelog import (changelog_path, load_entries,
                                  parse_changelog, render_changelog)

ROOT = Path(__file__).resolve().parents[1]


def test_changelog_is_found():
    p = changelog_path()
    assert p is not None and p.is_file(), "no CHANGELOG.md discoverable"
    print("OK changelog found at", p.name)


def test_package_copy_matches_the_root_copy():
    # The root copy is what GitHub renders; the package copy is what ships in
    # the wheel. Two files means two chances to drift, so pin them together.
    root = ROOT / "CHANGELOG.md"
    pkg = ROOT / "abaqus_mcp" / "CHANGELOG.md"
    assert root.is_file() and pkg.is_file(), "both copies must exist"
    a = root.read_text(encoding="utf-8").replace("\r\n", "\n")
    b = pkg.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert a == b, "CHANGELOG.md and abaqus_mcp/CHANGELOG.md have drifted"
    print("OK root and package copies identical")


def test_current_version_has_an_entry():
    versions = [v for v, _, _ in load_entries()]
    assert __version__ in versions, (
        "no changelog entry for the running version %s; known: %s"
        % (__version__, versions))
    print("OK entry present for running version", __version__)


def test_newest_entry_is_the_current_version():
    # Catches the reverse mistake: writing the notes and forgetting the bump.
    entries = load_entries()
    assert entries, "changelog parsed to nothing"
    assert entries[0][0] == __version__, (
        "newest changelog entry is %s but the package is %s -- bump one of them"
        % (entries[0][0], __version__))
    print("OK newest entry matches the package version")


def test_parses_headings_with_and_without_dates():
    text = ("# Changelog\n\n"
            "## [0.9.1] - 2026-01-02\nfixed a thing\n\n"
            "## 0.9.0\nfirst\n")
    got = parse_changelog(text)
    assert [v for v, _, _ in got] == ["0.9.1", "0.9.0"], got
    assert got[0][1] == "2026-01-02"
    assert "fixed a thing" in got[0][2]
    assert got[1][1] == ""
    print("OK parses both heading styles")


def test_render_defaults_to_the_running_version():
    out = render_changelog(None)
    assert __version__ in out
    assert "you are running this" in out
    print("OK default render marks the running version")


def test_render_all_and_unknown():
    everything = render_changelog("all")
    for v, _, _ in load_entries():
        assert v in everything, "version %s missing from 'all'" % v
    missing = render_changelog("99.99.99")
    assert "No changelog entry" in missing and "Known versions" in missing
    print("OK 'all' lists every version; unknown version explains itself")


if __name__ == "__main__":
    test_changelog_is_found()
    test_package_copy_matches_the_root_copy()
    test_current_version_has_an_entry()
    test_newest_entry_is_the_current_version()
    test_parses_headings_with_and_without_dates()
    test_render_defaults_to_the_running_version()
    test_render_all_and_unknown()
    print("\nAll changelog tests passed.")
