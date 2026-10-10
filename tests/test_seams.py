"""
tests.test_seams
────────────────
Cross-WORKER integration tests. The per-package `core/core/_selftest*.py`
suites prove each module in isolation; this proves the SEAMS where the four
workers actually meet — the places a refactor in one package can silently break
another:

  Seam 1  recorder.lock  ←→  archiver.lock_reader        (the TikTok soft-lock)
  Seam 2  every producer  →  one items table              (priority + content_hash)
  Seam 3  add_item        →  dispatcher.claim_batch        (album/bucket grouping)
  Seam 4  core.ingest     →  dispatcher dedup guarantee    (global content_hash)
  Seam 5  BatchPolicy     →  claim_batch min-batch gate    (defer + flush-age)
  Seam 6  recorder.startup_sweep over the shared table     (sent/failed/new/dup)
  Seam 7  archiver.reconcile_recordings identifier scheme  (matches live enqueue)
  Seam 8  dispatcher.tg_router resolution chain            (env + explicit chat_id)
  Seam 9  PolicyStore banned roster ↔ active user list     (mutual exclusivity)
  Seam 10 the FULL dispatcher drain loop, fake Telegram    (claim→send→delete)

Run (from repo root):
    PYTHONPATH="core:archiver:recorder:dispatcher:ops" python3 tests/test_seams.py

Style matches the project's `_selftest` scripts: plain asserts, a printed
checkmark per assertion, nonzero exit on first failure. No pytest dependency.
Everything runs against temp dirs / a temp DB / a temp config.toml and a fake
Telegram sender — no network, no real Telegram, no touching the user's config.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from seams import _harness
from seams._harness import _reset_config
from seams.locks import (
    test_dispatcher_instance_lock_seam,
    test_live_recording_protection_seam,
    test_lock_cwd_independence_seam,
    test_lock_seam,
)
from seams.claim import (
    test_album_batching_seam,
    test_album_byte_cap_seam,
    test_content_hash_dedup_seam,
    test_local_platform_discovery_seam,
    test_min_batch_gate_seam,
    test_name_cluster_batch_seam,
    test_name_cluster_threshold_seam,
    test_producer_table_seam,
    test_send_order_clustering_seam,
)
from seams.recorder import (
    test_recorder_enqueue_ingest_seam,
    test_recording_roots_seam,
    test_recordings_reconcile_seam,
    test_startup_sweep_seam,
)
from seams.routing import (
    test_banned_roster_seam,
    test_banned_word_sanitizer_seam,
    test_burner_account_seam,
    test_identity_ig_pk_dedup_seam,
    test_labeled_route_folder_seam,
    test_routing_seam,
    test_topic_routing_seam,
)
from seams.drain import (
    test_circuit_breaker_seam,
    test_drain_backoff_seam,
    test_failed_housekeeping_seam,
    test_full_drain_seam,
    test_in_batch_dedup_integrity_seam,
    test_media_empty_quarantine_seam,
)
from seams.send import (
    test_fast_album_native_fallback_seam,
    test_send_stall_watchdog_seam,
    test_send_streamable_net_seam,
    test_split_album_seam,
    test_upload_progress_seam,
    test_video_metadata_backend_seam,
)
from seams.orphaned import (
    test_hashtag_root_seam,
    test_keep_original_document_seam,
    test_noname_folder_seam,
    test_orphaned_mixed_album_seam,
    test_orphaned_no_trace_and_pseudo_platform_seam,
)
from seams.archiver import (
    test_concurrent_platform_loops_seam,
    test_full_history_gate_seam,
    test_stories_fast_lane_seam,
)


def main() -> int:
    print("cross-worker seam integration tests")
    # Each test gets an isolated temp config.toml so the real user config is
    # never read or written.
    cfgfd, cfgpath = tempfile.mkstemp(suffix=".toml")
    os.close(cfgfd)
    os.environ["ARCHIVER_SUITE_CONFIG"] = cfgpath

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_lock_seam(tmp / "s1")
        test_live_recording_protection_seam(tmp / "s32")
        test_producer_table_seam(tmp / "s2")
        test_local_platform_discovery_seam(tmp / "s13")
        test_dispatcher_instance_lock_seam(tmp / "s14")
        test_album_batching_seam(tmp / "s3")
        test_album_byte_cap_seam(tmp / "s3b")
        test_content_hash_dedup_seam(tmp / "s4")
        test_min_batch_gate_seam(tmp / "s5")
        # fresh config per config-touching test so rosters don't bleed across
        for sub, fn in (("s6", test_startup_sweep_seam),
                        ("s7", test_recordings_reconcile_seam)):
            fn(tmp / sub)
        test_routing_seam()
        test_identity_ig_pk_dedup_seam(tmp / "s11")
        # banned-roster test wants a clean config
        _reset_config()
        test_banned_roster_seam()
        _reset_config()
        test_full_history_gate_seam()
        test_full_drain_seam(tmp / "s10")
        test_media_empty_quarantine_seam(tmp / "s11")
        test_circuit_breaker_seam(tmp / "s11b")
        test_drain_backoff_seam(tmp / "s11c")
        _reset_config()
        test_in_batch_dedup_integrity_seam(tmp / "s15")
        test_lock_cwd_independence_seam(tmp / "s16")
        test_recorder_enqueue_ingest_seam(tmp / "s17")
        test_send_stall_watchdog_seam()
        test_upload_progress_seam(tmp / "s19")
        test_send_streamable_net_seam(tmp / "s20")
        test_keep_original_document_seam(tmp / "s21")
        test_topic_routing_seam(tmp / "s22")
        test_split_album_seam(tmp / "s23")
        test_banned_word_sanitizer_seam(tmp / "s24")
        test_failed_housekeeping_seam(tmp / "s25")
        test_send_order_clustering_seam(tmp / "s26")
        test_video_metadata_backend_seam(tmp / "s27")
        test_hashtag_root_seam(tmp / "s28")
        test_noname_folder_seam(tmp / "s29")
        test_orphaned_mixed_album_seam(tmp / "s30")
        test_orphaned_no_trace_and_pseudo_platform_seam(tmp / "s31")
        test_labeled_route_folder_seam(tmp / "s31b")
        test_name_cluster_batch_seam(tmp / "s32")
        test_name_cluster_threshold_seam(tmp / "s33")
        test_fast_album_native_fallback_seam()
        _reset_config()
        test_burner_account_seam()
        test_concurrent_platform_loops_seam()
        test_stories_fast_lane_seam()
        test_recording_roots_seam(tmp / "s36")

    print(f"\nALL PASS ({_harness._checks} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
