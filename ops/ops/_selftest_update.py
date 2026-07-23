"""
ops._selftest_update
────────────────────
Proves the `ops update` change-detection + reinstall-command contract without
touching pipx, the workers, or the real config dir:
  - source_fingerprint is content-based, stable, and CHANGES when a tracked
    source file changes (and NOT when an ignored build/cache file changes)
  - looks_like_repo_root recognizes only a tree with all four package dirs
  - the pipx reinstall steps target media-archiver (not 'archiver') for the
    inject, pass --force, and resolve package DIRS to absolute paths
  - the cooperative stop-flag round-trips: write on graceful stop, clear after

Run:  python3 -m ops._selftest_update
Style matches the other _selftest scripts: plain asserts, checkmark per
assertion, nonzero exit on first failure. Temp dir only.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import update as u

_checks = 0


def ok(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"✗ {label}")
    _checks += 1
    print(f"✓ {label}")


def _make_repo(root: Path) -> None:
    for pkg in ("core", "archiver", "recorder", "dispatcher"):
        d = root / pkg / pkg
        d.mkdir(parents=True)
        (root / pkg / "pyproject.toml").write_text(f"name='{pkg}'\n",
                                                    encoding="utf-8")
        (d / "__init__.py").write_text("x = 1\n", encoding="utf-8")


def main() -> int:
    print("ops update selftest")
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _make_repo(repo)

        ok(u.looks_like_repo_root(repo), "full four-package tree is a repo root")
        (repo / "core").rename(repo / "core_moved")
        ok(not u.looks_like_repo_root(repo), "missing a package dir → not a root")
        (repo / "core_moved").rename(repo / "core")

        fp1 = u.source_fingerprint(repo)
        ok(fp1 == u.source_fingerprint(repo), "fingerprint is deterministic")

        # An ignored build/cache artifact must NOT shift the fingerprint.
        junk = repo / "dispatcher" / "build" / "lib" / "old.py"
        junk.parent.mkdir(parents=True)
        junk.write_text("stale = 999\n", encoding="utf-8")
        (repo / "core" / "core" / "__pycache__").mkdir()
        (repo / "core" / "core" / "__pycache__" / "x.py").write_text("c=1\n",
                                                                      encoding="utf-8")
        ok(u.source_fingerprint(repo) == fp1,
           "build/ and __pycache__/ files are ignored by the fingerprint")

        # A real source edit MUST shift it.
        (repo / "dispatcher" / "dispatcher" / "__init__.py").write_text(
            "x = 2\n", encoding="utf-8")
        ok(u.source_fingerprint(repo) != fp1, "a tracked .py edit changes it")

        # pipx step construction: the inject targets media-archiver with --force,
        # and every package DIR is resolved to an absolute path.
        steps = [u._pipx_argv(s, repo) for s in u.REINSTALL_STEPS]
        inject = next(s for s in steps if "inject" in s)
        ok("media-archiver" in inject and "--force" in inject,
           "inject targets media-archiver with --force")
        ok(inject[-1] == str((repo / "core").resolve()),
           "inject's package path is the absolute core/ dir")
        installs = [s for s in steps if "install" in s]
        ok(len(installs) == 3 and all(s[-1].startswith(str(repo)) for s in installs),
           "three install steps, each an absolute package dir")

        # Stop-flag round-trip against a temp config home (never the real one).
        os.environ["ARCHIVER_CONFIG_HOME"] = str(repo / "cfg")
        try:
            from core import paths as cpaths
            flag = cpaths.dispatcher_stop_flag()
            # dispatcher_pid=None → "nothing running", returns True and writes flag
            ok(u.graceful_stop_dispatcher(None, timeout_s=0.0) is True,
               "graceful stop with no dispatcher returns True")
            ok(flag.exists(), "stop-flag written")
            u.clear_stop_flag()
            ok(not flag.exists(), "stop-flag cleared")
            u.clear_stop_flag()  # idempotent — no error when already gone
            ok(True, "clearing an absent flag is a no-op")
        finally:
            os.environ.pop("ARCHIVER_CONFIG_HOME", None)

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
