"""
tests.test_dispatcher_stall_backoff
────────────────────────────────────
Regression tests for the 2026-09-05 connection_fix.md fix (dispatcher
post-outage upload-stall infinite loop). Every call in this file matches
the actual, current signatures in core/core/store.py, core/core/models.py,
dispatcher/dispatcher/send.py and dispatcher/dispatcher/config.py — read
directly from those files, not guessed.

Run (from repo root):
    PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 \
        python3 tests/test_dispatcher_stall_backoff.py

No pytest (this repo's convention — see CLAUDE.md). Requires the real
`telethon`/`tomli_w` deps, so run it with this repo's installed venv, e.g.
~/.local/share/uv/tools/dispatcher/bin/python3.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# ── 1. retry_after gates claim_next() / claim_batch() ──────────────────────

class RetryAfterClaimTests(unittest.TestCase):
    def _fresh_store(self):
        from core.store import ItemStore
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.unlink(path))
        return ItemStore.open(path)

    def test_claim_next_skips_future_retry_after(self):
        db = self._fresh_store()
        db.add_item(source="archiver", platform="x", username="u",
                    identifier="a", file_path="/tmp/a.bin", priority=10)
        db.add_item(source="archiver", platform="x", username="u",
                    identifier="b", file_path="/tmp/b.bin", priority=10)

        it = db.claim_next()
        self.assertIsNotNone(it)
        # Fail it with a backoff so it goes back to 'pending' with a future
        # retry_after, then confirm claim_next skips straight past it to the
        # other row instead of re-claiming the blocked one.
        db.mark_failed(it.id, error="stall", max_retries=4, backoff_s=3600)
        blocked_id = it.id

        other = db.claim_next()
        self.assertIsNotNone(other)
        self.assertNotEqual(other.id, blocked_id,
                            "claim_next must not reclaim a row with a "
                            "future retry_after")

        # Nothing else pending (the blocked row is the only one left) —
        # claim_next must return None, not the blocked row.
        self.assertIsNone(db.claim_next(),
                          "future retry_after must hide the row from "
                          "claim_next entirely")

    def test_claim_next_reclaims_past_retry_after(self):
        db = self._fresh_store()
        db.add_item(source="archiver", platform="x", username="u",
                    identifier="a", file_path="/tmp/a.bin", priority=10)
        it = db.claim_next()
        db.mark_failed(it.id, error="stall", max_retries=4, backoff_s=3600)

        # Manually rewind retry_after into the past (simulating time having
        # passed) — the row must become claimable again.
        db.conn.execute("UPDATE items SET retry_after=? WHERE id=?",
                        ("2000-01-01T00:00:00Z", it.id))
        db.conn.commit()

        reclaimed = db.claim_next()
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.id, it.id)

    def test_claim_batch_skips_future_retry_after(self):
        db = self._fresh_store()
        ids = []
        for i in range(3):
            db.add_item(source="archiver", platform="x", username="u",
                        identifier=f"p{i}", file_path=f"/tmp/p{i}.bin",
                        priority=10)
            ids.append(db.id_of(f"/tmp/p{i}.bin"))

        # Claim and fail the first with a long backoff.
        first = db.claim_next()
        db.mark_failed(first.id, error="stall", max_retries=4, backoff_s=3600)

        batch = db.claim_batch()
        batch_ids = {it.id for it in batch}
        self.assertNotIn(first.id, batch_ids,
                         "claim_batch must exclude a future-retry_after row")
        self.assertTrue(batch_ids,
                        "the other rows (NULL retry_after) must still be "
                        "claimable via claim_batch")


# ── 2. mark_failed() sets retry_after only on the pending transition ──────

class MarkFailedBackoffTests(unittest.TestCase):
    def _fresh_store(self):
        from core.store import ItemStore
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.unlink(path))
        return ItemStore.open(path)

    def test_backoff_s_stamps_retry_after_on_pending_transition(self):
        db = self._fresh_store()
        db.add_item(source="archiver", platform="x", username="u",
                    identifier="a", file_path="/tmp/a.bin", priority=10)
        it = db.claim_next()

        status = db.mark_failed(it.id, error="stall", max_retries=4,
                                backoff_s=300)
        self.assertEqual(status, "pending")

        row = db.get(it.id)
        self.assertEqual(row.status, "pending")
        self.assertIsNotNone(row.retry_after,
                             "backoff_s given + pending transition -> "
                             "retry_after must be stamped")

    def test_no_backoff_s_leaves_retry_after_unset(self):
        db = self._fresh_store()
        db.add_item(source="archiver", platform="x", username="u",
                    identifier="a", file_path="/tmp/a.bin", priority=10)
        it = db.claim_next()

        # backoff_s omitted entirely (defaults to None) -> no stamp, even
        # though the row still goes back to 'pending'.
        status = db.mark_failed(it.id, error="net error", max_retries=4)
        self.assertEqual(status, "pending")
        row = db.get(it.id)
        self.assertIsNone(row.retry_after)

    def test_terminal_failed_never_gets_retry_after(self):
        db = self._fresh_store()
        db.add_item(source="archiver", platform="x", username="u",
                    identifier="a", file_path="/tmp/a.bin", priority=10)
        it = db.claim_next()  # attempts=1

        # max_retries=1 means this single attempt already exhausts the
        # budget -> terminal 'failed', even though backoff_s is given.
        status = db.mark_failed(it.id, error="stall", max_retries=1,
                                backoff_s=300)
        self.assertEqual(status, "failed")
        row = db.get(it.id)
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.retry_after,
                          "a terminal 'failed' row must never carry a "
                          "future retry_after")


# ── 3. SendResult.stalled — typed, not error-string matching ──────────────

class SendResultStalledTests(unittest.TestCase):
    def test_stalled_defaults_false(self):
        from dispatcher.send import SendResult
        r = SendResult(ok=False, error="some error")
        self.assertFalse(r.stalled)

    def test_stalled_is_a_plain_bool_field(self):
        from dispatcher.send import SendResult
        r = SendResult(ok=False, error="stalled: no upload progress for 600s",
                      stalled=True)
        self.assertTrue(r.stalled)
        # image_process_failed / media_empty are independent flags — setting
        # stalled must not implicitly flip them.
        self.assertFalse(r.image_process_failed)
        self.assertFalse(r.media_empty)


class SendWithRetriesStallIntegrationTests(unittest.TestCase):
    """Drives the real _send_with_retries envelope end to end (no network)
    with a tiny stall timeout so a no-progress send_fn trips the watchdog
    fast, confirming SendResult.stalled is set by the TIMEOUT PATH itself
    rather than by matching error text."""

    def _make_strategy(self, **overrides):
        from dispatcher.send import TelethonSendStrategy
        kwargs = dict(
            api_id=1, api_hash="x", phone="+10000000000",
            session_name="/tmp/not-a-real-session",
            max_retries=1,
            stall_base_timeout_s=0.05,   # trips almost immediately
            stall_min_rate_kib_s=1_000_000.0,  # keep the ceiling tiny too
        )
        kwargs.update(overrides)
        strat = TelethonSendStrategy(**kwargs)
        # _force_reconnect touches self._sender -> self._client; give it a
        # harmless stand-in so the reconnect attempt inside the stall branch
        # doesn't blow up on None (it's caught/logged either way, but this
        # keeps the test's own assertions the only signal).
        strat._client = _DummyClient()
        return strat

    def test_stall_watchdog_timeout_sets_stalled_true(self):
        strat = self._make_strategy()

        async def never_progresses():
            await asyncio.sleep(10)  # never reaches this under the watchdog

        result = asyncio.run(
            strat._send_with_retries(never_progresses, what="test upload")
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.stalled,
                        "a stall-watchdog TimeoutError exhausting retries "
                        "must set SendResult.stalled=True")

    def test_non_timeout_failure_leaves_stalled_false(self):
        strat = self._make_strategy()

        async def raises_immediately():
            raise RuntimeError("some non-timeout send error")

        result = asyncio.run(
            strat._send_with_retries(raises_immediately, what="test upload")
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.stalled,
                         "a non-timeout failure must leave "
                         "SendResult.stalled=False")


class _DummyClient:
    async def disconnect(self):
        return None

    async def connect(self):
        return None


# ── 4. DispatcherConfig — five new fields, exact defaults when unset ──────

class DispatcherConfigDefaultsTests(unittest.TestCase):
    _ENV_KEYS = (
        "USE_IPV6", "FAST_UPLOAD_CONNECT_TIMEOUT_S",
        "FAST_UPLOAD_CONNECT_RETRIES", "FAST_UPLOAD_CONNECT_STAGGER_S",
        "STALL_BACKOFF_S", "ARCHIVER_SUITE_CONFIG", "ARCHIVER_DB",
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
        tmp = tempfile.mkdtemp()
        os.environ["ARCHIVER_SUITE_CONFIG"] = str(Path(tmp) / "config.toml")
        os.environ["ARCHIVER_DB"] = str(Path(tmp) / "suite.db")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_when_env_unset(self):
        from dispatcher.config import DispatcherConfig
        cfg = DispatcherConfig.load(require_telegram=False)
        self.assertEqual(cfg.use_ipv6, False)
        self.assertEqual(cfg.fast_upload_connect_timeout_s, 8.0)
        self.assertEqual(cfg.fast_upload_connect_retries, 2)
        self.assertEqual(cfg.fast_upload_connect_stagger_s, 0.1)
        self.assertEqual(cfg.stall_backoff_s, 300.0)

    def test_explicit_env_values_override_defaults(self):
        os.environ["USE_IPV6"] = "1"
        os.environ["FAST_UPLOAD_CONNECT_TIMEOUT_S"] = "12.5"
        os.environ["FAST_UPLOAD_CONNECT_RETRIES"] = "5"
        os.environ["FAST_UPLOAD_CONNECT_STAGGER_S"] = "0.25"
        os.environ["STALL_BACKOFF_S"] = "60"

        from dispatcher.config import DispatcherConfig
        cfg = DispatcherConfig.load(require_telegram=False)
        self.assertEqual(cfg.use_ipv6, True)
        self.assertEqual(cfg.fast_upload_connect_timeout_s, 12.5)
        self.assertEqual(cfg.fast_upload_connect_retries, 5)
        self.assertEqual(cfg.fast_upload_connect_stagger_s, 0.25)
        self.assertEqual(cfg.stall_backoff_s, 60.0)


# ── 5. TelethonSendStrategy._build_client forwards use_ipv6 ───────────────

class BuildClientUseIpv6Tests(unittest.TestCase):
    def test_use_ipv6_true_reaches_telegram_client(self):
        from dispatcher.send import TelethonSendStrategy
        with patch("dispatcher.send.TelegramClient") as mock_client_cls:
            strat = TelethonSendStrategy(
                api_id=1, api_hash="x", phone="+10000000000",
                session_name="/tmp/not-a-real-session", use_ipv6=True,
            )
            strat._build_client("/tmp/not-a-real-session", 1, "x")
            mock_client_cls.assert_called_once()
            self.assertTrue(
                mock_client_cls.call_args.kwargs.get("use_ipv6") is True,
                "use_ipv6=True must reach the TelegramClient(...) call",
            )

    def test_use_ipv6_false_reaches_telegram_client(self):
        from dispatcher.send import TelethonSendStrategy
        with patch("dispatcher.send.TelegramClient") as mock_client_cls:
            strat = TelethonSendStrategy(
                api_id=1, api_hash="x", phone="+10000000000",
                session_name="/tmp/not-a-real-session", use_ipv6=False,
            )
            strat._build_client("/tmp/not-a-real-session", 1, "x")
            mock_client_cls.assert_called_once()
            self.assertFalse(
                mock_client_cls.call_args.kwargs.get("use_ipv6") is not False,
                "use_ipv6=False must NOT silently default to True",
            )
            self.assertIs(mock_client_cls.call_args.kwargs.get("use_ipv6"),
                          False)


if __name__ == "__main__":
    unittest.main()
