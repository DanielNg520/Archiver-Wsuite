"""
Focused validation for the recorder's two-stage auto-ban gate (Phase 3):
persistent escalation counter, stage-1 threshold/age gate, stage-2 profile
confirmation, roster write + quarantine, and the config-load banned filter.

Run: python recorder/recorder/_selftest_ban_escalation.py

Standalone (no pytest). Real UnstartableTracker/PolicyStore on temp paths;
StateMachine._maybe_ban_unstartable is exercised unbound on a duck-typed shim
with profile_check stubbed (the LIVE profile_check against a real banned
handle is a separate manual verification step — see the plan's Phase 3 gate).
"""
import sys
import tempfile
import types
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "recorder"))

from core import PolicyStore                                   # noqa: E402
import core.quarantine as q                                     # noqa: E402
import recorder.config as rconfig                               # noqa: E402
import recorder.ban_check as ban_check                          # noqa: E402
from recorder.ban_check import ProfileStatus, classify_profile_html  # noqa: E402
from recorder.state import StateMachine, _BAN_AFTER_COOLDOWNS   # noqa: E402
from recorder.unstartable import UnstartableTracker             # noqa: E402

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def _shim(tmp: Path, tracker: UnstartableTracker) -> types.SimpleNamespace:
    cfg = types.SimpleNamespace(tiktok_cookies_file=None,
                                output_dir=str(tmp / "records"))
    return types.SimpleNamespace(config=cfg, _unstartable=tracker,
                                 _banned=set(), _skipped={})


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    state_dir = tmp / "state"
    q.recorder_lock.live_recording_user = lambda: (False, None)

    # Point the recorder's config.toml at a temp file for the whole test.
    toml_path = tmp / "config.toml"
    rconfig.CONFIG_TOML = toml_path

    # ── counter increments + persists across a simulated restart ────────────
    t1 = UnstartableTracker(state_dir)
    t1.note_cooldown("chronic")
    t1.note_cooldown("chronic")
    check(t1.cycles("chronic") == 2, "cooldown cycles increment")
    t2 = UnstartableTracker(state_dir)              # "restart"
    check(t2.cycles("chronic") == 2, "cycles persist across restart")
    check(t2.age_seconds("chronic") >= 0.0, "age readable after restart")

    # successful start clears the entry
    t2.clear("chronic")
    check(UnstartableTracker(state_dir).cycles("chronic") == 0,
          "successful start evicts the entry (persisted)")

    # corrupt file degrades to empty, never raises
    (state_dir / "unstartable.json").write_text("{not json", encoding="utf-8")
    check(UnstartableTracker(state_dir).cycles("x") == 0,
          "corrupt tally file degrades to empty")

    # ── page classification (canned bodies) ─────────────────────────────────
    check(classify_profile_html("u1", "<p>Couldn't find this account</p>")
          is ProfileStatus.GONE, "TikTok 'couldn't find' marker → GONE")
    check(classify_profile_html("u1", "error: account was banned")
          is ProfileStatus.GONE, "core account-gone signal → GONE")
    check(classify_profile_html("u1", "This account is private")
          is ProfileStatus.PRIVATE, "private marker → PRIVATE")
    check(classify_profile_html("u1", '{"uniqueId":"u1","nickname":"U"}')
          is ProfileStatus.ALIVE, "hydration JSON with uniqueId → ALIVE")
    check(classify_profile_html("u1", "<html>captcha wall</html>")
          is ProfileStatus.UNKNOWN, "unrecognized page → UNKNOWN")

    # ── stage-1 gate: threshold AND age floor ───────────────────────────────
    calls: list[str] = []

    def _stub(status):
        def fake(username, cookies_file=None):
            calls.append(username)
            return status
        return fake

    tracker = UnstartableTracker(state_dir)
    shim = _shim(tmp, tracker)
    (tmp / "records" / "tiktok" / "goner").mkdir(parents=True)

    ban_check.profile_check = _stub(ProfileStatus.GONE)

    # below cycle threshold → no profile call, no ban
    for _ in range(_BAN_AFTER_COOLDOWNS - 1):
        tracker.note_cooldown("goner")
    StateMachine._maybe_ban_unstartable(shim, "goner")
    check(not calls and "goner" not in shim._banned,
          "below cycle threshold: no network call, no ban")

    # at threshold but first_seen too recent → still blocked by the age floor
    tracker.note_cooldown("goner")
    check(tracker.cycles("goner") >= _BAN_AFTER_COOLDOWNS,
          "precondition: cycle threshold met")
    StateMachine._maybe_ban_unstartable(shim, "goner")
    check(not calls and "goner" not in shim._banned,
          "age floor blocks a same-day burst")

    # ── stage-2: age satisfied, verdict decides ─────────────────────────────
    # Backdate first_seen instead of waiting 24h.
    tracker._data["goner"]["first_seen"] = "2020-01-01T00:00:00+00:00"
    tracker._save()

    for status in (ProfileStatus.ALIVE, ProfileStatus.PRIVATE,
                   ProfileStatus.UNKNOWN):
        calls.clear()
        ban_check.profile_check = _stub(status)
        StateMachine._maybe_ban_unstartable(shim, "goner")
        check(calls == ["goner"] and "goner" not in shim._banned,
              f"stage-2 {status.value}: profile checked, NO ban")

    calls.clear()
    ban_check.profile_check = _stub(ProfileStatus.GONE)
    shim._skipped["goner"] = 99.0
    StateMachine._maybe_ban_unstartable(shim, "goner")
    check("goner" in shim._banned, "stage-2 GONE: banned")
    check("goner" in PolicyStore(toml_path).list_banned("tiktok"),
          "ban lands on the config.toml roster")
    check((tmp / "records" / "tiktok" / ".deleted" / "goner").is_dir()
          and not (tmp / "records" / "tiktok" / "goner").exists(),
          "folder quarantined into .deleted/")
    check(UnstartableTracker(state_dir).cycles("goner") == 0,
          "escalation entry evicted after ban")
    check("goner" not in shim._skipped, "cooldown bench entry dropped")

    # ── config load filters the roster out of the poll list ─────────────────
    # The ban above wrote [platform.tiktok.banned] into config.toml; append a
    # priority list containing the banned user and reload.
    existing = toml_path.read_text(encoding="utf-8")
    toml_path.write_text(
        existing + '\n[recorder.tiktok]\nusers = ["alice", "goner", "bob"]\n',
        encoding="utf-8")
    cfg = rconfig.RecorderConfig.load()
    check(cfg.tiktok_users == ("alice", "bob"),
          "banned user filtered out of tiktok_users at load")

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
