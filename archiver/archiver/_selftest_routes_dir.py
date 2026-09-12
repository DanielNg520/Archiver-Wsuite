"""
Focused validation for the routes_dir config split (Phase 5): ROUTES_DIR unset
falls back to output_dir (byte-identical behavior), set makes the chat_id
ingest scan an independent root while platform trees stay on output_dir.

Run: python archiver/archiver/_selftest_routes_dir.py

Standalone (no pytest). Config.load driven via env vars against a temp
config.toml; the orchestrator's ingest pass exercised unbound on a shim.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "archiver"))

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "archive"
    routes = tmp / "routes"          # stands in for the future D: root
    (out / "x" / "alice").mkdir(parents=True)
    (routes / "-1001234567890").mkdir(parents=True)
    (routes / "-1001234567890" / "drop.mp4").write_bytes(b"x" * 256)

    # Import FIRST: archiver.config runs load_dotenv() at import time, which
    # injects the real machine .env (which now sets ROUTES_DIR) into os.environ.
    # If we popped ROUTES_DIR before this import, the import would silently put
    # it back and defeat the isolation — so import, THEN scrub the env.
    from archiver.config import Config          # noqa: E402 (env scrub below)

    # Config resolution happens against env — point the suite config at a temp
    # toml so nothing in the real install is read or written.
    os.environ["ARCHIVER_SUITE_CONFIG"] = str(tmp / "config.toml")
    os.environ["OUTPUT_DIR"] = str(out)
    os.environ.pop("ROUTES_DIR", None)

    # ── unset ⇒ routes_dir == output_dir (legacy single-tree layout) ────────
    cfg = Config.load(load_platform_configs=False, require_platforms=False)
    check(cfg.routes_dir == cfg.output_dir == str(out),
          "ROUTES_DIR unset: routes_dir falls back to output_dir")

    # ── set ⇒ independent root ──────────────────────────────────────────────
    os.environ["ROUTES_DIR"] = str(routes)
    cfg = Config.load(load_platform_configs=False, require_platforms=False)
    check(cfg.routes_dir == str(routes) and cfg.output_dir == str(out),
          "ROUTES_DIR set: routes_dir independent of output_dir")

    # ── orchestrator ingest pass scans routes_dir, not output_dir ───────────
    from core import ItemStore, PolicyStore, DeletionGuard
    from archiver.orchestrator import Archiver

    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    shim = types.SimpleNamespace(
        config=types.SimpleNamespace(policy_store=store,
                                     output_dir=str(out),
                                     routes_dir=str(routes)),
        db=db,
        deletion_guard=DeletionGuard(store),
        _routes_warned=False,
    )
    # Bind the real _routes_dir_ready onto the shim (unbound-call style, same
    # as _maybe_ingest_orphaned below) so the routes-unmounted guard it added
    # sees this shim's own config instead of raising AttributeError.
    shim._routes_dir_ready = lambda: Archiver._routes_dir_ready(shim)
    Archiver._maybe_ingest_orphaned(shim, known_platform_names={"x"})

    rows = db.list_items(status="pending", limit=10)
    check(len(rows) == 1 and rows[0].chat_id == "-1001234567890",
          "chat_id folder under routes_dir found + enqueued")
    check(rows[0].file_path == str(routes / "-1001234567890" / "drop.mp4"),
          "enqueued path resolves under routes_dir")
    check((out / "x" / "alice").is_dir() and not list(db.list_items(limit=50))[1:],
          "platform tree under output_dir untouched by the route scan")

    # ── output_dir chat_id folders are NOT scanned once split ───────────────
    (out / "-1009999999999").mkdir()
    (out / "-1009999999999" / "stray.mp4").write_bytes(b"y" * 128)
    Archiver._maybe_ingest_orphaned(shim, known_platform_names={"x"})
    chats = {r.chat_id for r in db.list_items(limit=50)}
    check("-1009999999999" not in chats,
          "a chat_id folder left under output_dir is ignored after the split")

    # ── an unmounted (missing) separate ROUTES_DIR skips the scan, once ─────
    (routes / "-1002222222222").mkdir()
    (routes / "-1002222222222" / "new.mp4").write_bytes(b"z" * 64)
    shim.config.routes_dir = str(routes / "not-actually-mounted")
    check(Archiver._routes_dir_ready(shim) is False,
          "_routes_dir_ready() is False when the separate ROUTES_DIR is absent")
    Archiver._maybe_ingest_orphaned(shim, known_platform_names={"x"})
    chats = {r.chat_id for r in db.list_items(limit=50)}
    check("-1002222222222" not in chats,
          "an unmounted ROUTES_DIR is never walked — nothing new is enqueued")
    check(shim._routes_warned is True,
          "the one-warning-per-run flag is set after the skip")

    db.close()
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
