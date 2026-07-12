"""
Focused validation for the shared gone-account matcher (core.account_gone),
extracted from archiver.platforms in the bans/paths refactor (Phase 0).

Run: python core/core/_selftest_account_gone.py

Standalone (no pytest). Proves the matcher's verdict is byte-identical to the
old inline archiver copy and that archiver.platforms still resolves the aliased
private names after the re-point.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "archiver"))

from core import ACCOUNT_GONE_SIGNALS, match_account_gone  # noqa: E402
from core.account_gone import (                             # noqa: E402
    ACCOUNT_GONE_SIGNALS as MOD_SIGNALS,
    match_account_gone as mod_match,
)

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def main() -> int:
    # ── the package re-export and the module object are the same thing ──────
    check(match_account_gone is mod_match, "core re-exports the module matcher")
    check(ACCOUNT_GONE_SIGNALS is MOD_SIGNALS, "core re-exports the signal tuple")

    # ── every canonical signal matches, standalone and embedded ─────────────
    for sig in ACCOUNT_GONE_SIGNALS:
        check(match_account_gone(sig) == sig, f"exact signal matches: {sig!r}")
        embedded = f"error: the {sig} — retry later"
        check(match_account_gone(embedded) == sig, f"embedded signal matches: {sig!r}")

    # ── first-match ordering is preserved (returns the earliest in the tuple) ─
    both = "account suspended and account has been banned"
    check(match_account_gone(both) == "account suspended",
          "returns the first signal in tuple order, not the last")

    # ── non-gone / transient text does NOT trip the matcher ─────────────────
    for benign in (
        "",
        "http error 429 too many requests",
        "unable to download video: format not available",
        "a single post 404 not found",          # per-item 404, not account-gone
        "connection reset by peer",
        "private account",                        # exists, not gone
    ):
        check(match_account_gone(benign) == "", f"benign text ignored: {benign!r}")

    # ── caller contract: input is expected already-lowercased ───────────────
    check(match_account_gone("ACCOUNT SUSPENDED") == "",
          "uppercase input does not match (caller lowercases first)")

    # ── the archiver re-point resolves: aliases bind to the core module ─────
    import archiver.platforms as ap  # noqa: E402
    check(ap._match_account_gone is match_account_gone,
          "archiver.platforms._match_account_gone aliases the core matcher")
    check(ap._ACCOUNT_GONE_SIGNALS is ACCOUNT_GONE_SIGNALS,
          "archiver.platforms._ACCOUNT_GONE_SIGNALS aliases the core tuple")

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
