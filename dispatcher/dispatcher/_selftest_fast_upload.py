"""
Self-test for dispatcher.fast_upload — the parallel multi-connection uploader.

Drives the real chunking/dispatch logic against a fake Telethon client that
records SaveBigFilePart requests, with zero network. Pins the contract the
send path relies on: byte-perfect reassembly, every part sent exactly once,
borrowed senders always returned, progress monotonic to 100%, and a hard
fallback to the serial uploader on any fast-path failure.

Run: PYTHONPATH=core:dispatcher python3 -m dispatcher._selftest_fast_upload
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon.tl import types                                  # noqa: E402
from dispatcher import fast_upload                             # noqa: E402

_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        print(f"✗ FAIL: {label}")
        raise SystemExit(1)
    print(f"✓ {label}")


class _FakeSender:
    """Records each SaveBigFilePart it is asked to send."""
    def __init__(self, fail_on: int | None = None):
        self.parts: list[tuple[int, bytes]] = []
        self.fail_on = fail_on
        self.disconnected = False

    async def send(self, req):
        if self.fail_on is not None and req.file_part == self.fail_on:
            raise IOError("simulated transport failure")
        await asyncio.sleep(0)                # yield so workers truly interleave
        self.parts.append((req.file_part, bytes(req.bytes)))
        return True

    async def disconnect(self):
        self.disconnected = True


class _FakeClient:
    """Exposes the internals _internals_present checks; the actual sender
    creation is monkeypatched (see _patch_senders) so no real network/auth is
    needed."""
    def __init__(self, *, dc_id: int = 2):
        self.session = SimpleNamespace(dc_id=dc_id, auth_key=b"fake-auth-key")
        self._log = None
        self._proxy = None
        self.serial_calls: list[str] = []

    async def _get_dc(self, dc_id):                       # pragma: no cover
        return SimpleNamespace(ip_address="0.0.0.0", port=443, id=dc_id)

    def _connection(self, *a, **k):                       # pragma: no cover
        return SimpleNamespace()

    async def upload_file(self, path, *, file_name=None, progress_callback=None):
        self.serial_calls.append(str(path))
        if progress_callback:
            progress_callback(os.path.getsize(path), os.path.getsize(path))
        return ("SERIAL", str(path))


def _patch_senders(fail_on: int | None = None) -> list[_FakeSender]:
    """Replace fast_upload._connect_sender with one that yields fake senders,
    returning the list they're collected into for assertions."""
    created: list[_FakeSender] = []

    async def _fake_connect(_client, *, connect_timeout=None, retries=None):
        s = _FakeSender(fail_on=fail_on)
        created.append(s)
        return s

    fast_upload._connect_sender = _fake_connect
    return created


def _make_file(path: Path, size: int) -> bytes:
    data = os.urandom(size)
    path.write_bytes(data)
    return data


def _reassemble(senders: list[_FakeSender]) -> bytes:
    parts = [p for s in senders for p in s.parts]
    return b"".join(chunk for _, chunk in sorted(parts, key=lambda x: x[0]))


def test_big_file_parallel(tmp: Path) -> None:
    print("\n── big file → parallel, byte-perfect, senders returned ──")
    size = 11 * 1024 * 1024 + 777          # >10 MiB, not part-aligned
    data = _make_file(tmp / "big.bin", size)
    client = _FakeClient()
    senders = _patch_senders()

    progress: list[tuple[int, int]] = []
    handle = asyncio.run(fast_upload.upload_file(
        client, tmp / "big.bin", connections=4,
        progress_callback=lambda c, t: progress.append((c, t))))

    check(not client.serial_calls, "the serial uploader was NOT used")
    check(isinstance(handle, types.InputFileBig), "returns an InputFileBig handle")
    expected_parts = (size + fast_upload.PART_SIZE - 1) // fast_upload.PART_SIZE
    check(handle.parts == expected_parts, "handle part_count matches the file")

    all_parts = [p for s in senders for p in s.parts]
    indices = sorted(i for i, _ in all_parts)
    check(indices == list(range(expected_parts)),
          "every part 0..N-1 sent exactly once (no gaps, no dupes)")
    check(_reassemble(senders) == data,
          "reassembled bytes are identical to the source file")
    check(len(senders) == 4 and all(s.disconnected for s in senders),
          "all 4 home-DC senders were connected AND disconnected")
    check(progress and progress[-1] == (size, size),
          "progress callback ends at 100% (sent == total)")
    check([c for c, _ in progress] == sorted(c for c, _ in progress),
          "progress is monotonically non-decreasing")


def test_small_file_serial(tmp: Path) -> None:
    print("\n── small file → serial uploader (md5 path) ──")
    _make_file(tmp / "small.bin", 1024 * 1024)     # 1 MiB, under threshold
    client = _FakeClient()
    senders = _patch_senders()
    handle = asyncio.run(fast_upload.upload_file(
        client, tmp / "small.bin", connections=4))
    check(client.serial_calls == [str(tmp / "small.bin")],
          "delegated to client.upload_file")
    check(handle == ("SERIAL", str(tmp / "small.bin")),
          "returns the serial handle unchanged")
    check(not senders, "no senders created for a small file")


def test_connections_one_is_serial(tmp: Path) -> None:
    print("\n── connections=1 → serial even for a big file ──")
    _make_file(tmp / "b.bin", 11 * 1024 * 1024)
    client = _FakeClient()
    senders = _patch_senders()
    asyncio.run(fast_upload.upload_file(client, tmp / "b.bin", connections=1))
    check(client.serial_calls and not senders,
          "connections=1 opts out of the parallel path")


def test_missing_internals_serial(tmp: Path) -> None:
    print("\n── missing Telethon internals → serial fallback ──")
    _make_file(tmp / "b.bin", 11 * 1024 * 1024)

    class _Bare:
        # No _get_dc / _connection / session.auth_key ⇒ internals absent.
        session = SimpleNamespace(dc_id=2)
        def __init__(self): self.serial_calls = []
        async def upload_file(self, path, *, file_name=None, progress_callback=None):
            self.serial_calls.append(str(path)); return ("SERIAL", str(path))

    client = _Bare()
    handle = asyncio.run(fast_upload.upload_file(
        client, tmp / "b.bin", connections=4))
    check(client.serial_calls and handle[0] == "SERIAL",
          "absent Telethon internals ⇒ serial path, no crash")


def test_part_failure_falls_back(tmp: Path) -> None:
    print("\n── a rejected part → fallback to serial, senders disconnected ──")
    _make_file(tmp / "b.bin", 11 * 1024 * 1024)
    client = _FakeClient()
    senders = _patch_senders(fail_on=3)        # sender raises on part 3
    handle = asyncio.run(fast_upload.upload_file(
        client, tmp / "b.bin", connections=4))
    check(client.serial_calls and handle[0] == "SERIAL",
          "parallel failure transparently falls back to the serial uploader")
    check(senders and all(s.disconnected for s in senders),
          "every connected sender is disconnected even when a part fails")


def test_part_retry_transient(tmp: Path) -> None:
    print("\n── transient part failure → retried on new sender, succeeds ──")
    size = 11 * 1024 * 1024 + 777
    data = _make_file(tmp / "transient.bin", size)
    client = _FakeClient()

    failed = False
    senders: list[_FakeSender] = []

    class _TransientSender(_FakeSender):
        async def send(self, req):
            nonlocal failed
            if req.file_part == 2 and not failed:
                failed = True
                raise ConnectionError("simulated transient failure")
            return await super().send(req)

    orig_connect = fast_upload._connect_sender
    orig_sleep = fast_upload.asyncio.sleep

    async def _fake_connect(_client, *, connect_timeout=None, retries=None):
        s = _TransientSender()
        senders.append(s)
        return s

    async def _instant_sleep(*_a, **_kw):
        pass

    fast_upload._connect_sender = _fake_connect
    fast_upload.asyncio.sleep = _instant_sleep
    try:
        progress: list[tuple[int, int]] = []
        handle = asyncio.run(fast_upload.upload_file(
            client, tmp / "transient.bin", connections=4,
            progress_callback=lambda c, t: progress.append((c, t))))
    finally:
        fast_upload._connect_sender = orig_connect
        fast_upload.asyncio.sleep = orig_sleep

    check(failed, "transient ConnectionError was triggered")
    check(not client.serial_calls, "the serial uploader was NOT used")
    check(isinstance(handle, types.InputFileBig),
          "returns an InputFileBig handle")
    expected = (size + fast_upload.PART_SIZE - 1) // fast_upload.PART_SIZE
    all_parts = [p for s in senders for p in s.parts]
    indices = sorted(i for i, _ in all_parts)
    check(indices == list(range(expected)),
          "every part index sent exactly once")
    check(_reassemble(senders) == data,
          "reassembled bytes are identical to the source file")
    check(progress and progress[-1] == (size, size),
          "progress callback ends at 100% (sent == total)")
    check(senders and all(s.disconnected for s in senders),
          "every created sender disconnected")


def test_part_retry_persistent(tmp: Path) -> None:
    print("\n── persistent part failure → raises after 3 attempts ──")
    size = 11 * 1024 * 1024 + 777
    _make_file(tmp / "persistent.bin", size)
    client = _FakeClient()

    attempts = 0
    senders: list[_FakeSender] = []

    class _PersistentSender(_FakeSender):
        async def send(self, req):
            nonlocal attempts
            if req.file_part == 2:
                attempts += 1
                raise ConnectionError("simulated persistent failure")
            return await super().send(req)

    orig_connect = fast_upload._connect_sender
    orig_sleep = fast_upload.asyncio.sleep

    async def _fake_connect(_client, *, connect_timeout=None, retries=None):
        s = _PersistentSender()
        senders.append(s)
        return s

    async def _instant_sleep(*_a, **_kw):
        pass

    fast_upload._connect_sender = _fake_connect
    fast_upload.asyncio.sleep = _instant_sleep
    raised = False
    try:
        try:
            asyncio.run(fast_upload._parallel_upload(
                client, tmp / "persistent.bin", size, 4,
                fast_upload.PART_SIZE, "persistent.bin", None,
            ))
        except ConnectionError:
            raised = True
    finally:
        fast_upload._connect_sender = orig_connect
        fast_upload.asyncio.sleep = orig_sleep

    check(raised, "parallel upload raised ConnectionError on failure")
    check(attempts == 3, "failed part was attempted exactly 3 times")


def test_part_retry_hung_disconnect(tmp: Path) -> None:
    print("\n── hung disconnect on retry → timeout drops sender, succeeds ──")
    size = 11 * 1024 * 1024 + 777
    data = _make_file(tmp / "hung.bin", size)
    client = _FakeClient()

    failed = False
    senders: list[_FakeSender] = []

    class _HungSender(_FakeSender):
        def __init__(self):
            super().__init__()
            self.hung = False
            self.disconnect_calls = 0

        async def send(self, req):
            nonlocal failed
            if req.file_part == 2 and not failed:
                failed = True
                self.hung = True
                raise ConnectionError("simulated transient failure")
            return await super().send(req)

        async def disconnect(self):
            self.disconnect_calls += 1
            if self.hung and self.disconnect_calls == 1:
                await asyncio.Event().wait()
            await super().disconnect()

    orig_connect = fast_upload._connect_sender
    orig_timeout = fast_upload._DISCONNECT_TIMEOUT_S

    async def _fake_connect(_client, *, connect_timeout=None, retries=None):
        s = _HungSender()
        senders.append(s)
        return s

    fast_upload._connect_sender = _fake_connect
    fast_upload._DISCONNECT_TIMEOUT_S = 0.05
    try:
        handle = asyncio.run(
            asyncio.wait_for(
                fast_upload.upload_file(
                    client, tmp / "hung.bin", connections=4
                ),
                10,
            )
        )
    finally:
        fast_upload._connect_sender = orig_connect
        fast_upload._DISCONNECT_TIMEOUT_S = orig_timeout

    check(isinstance(handle, types.InputFileBig),
          "returns an InputFileBig handle")
    check(not client.serial_calls, "the serial uploader was NOT used")
    expected = (size + fast_upload.PART_SIZE - 1) // fast_upload.PART_SIZE
    all_parts = [p for s in senders for p in s.parts]
    indices = sorted(i for i, _ in all_parts)
    check(indices == list(range(expected)),
          "every part index sent exactly once")
    check(_reassemble(senders) == data,
          "reassembled bytes are identical to the source file")


def main() -> int:
    print("dispatcher.fast_upload self-test")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_big_file_parallel(tmp)
        test_small_file_serial(tmp)
        test_connections_one_is_serial(tmp)
        test_missing_internals_serial(tmp)
        test_part_failure_falls_back(tmp)
        test_part_retry_transient(tmp)
        test_part_retry_persistent(tmp)
        test_part_retry_hung_disconnect(tmp)
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
