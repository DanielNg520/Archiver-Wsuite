"""
Self-test for the capture-termination + remux/handoff integrity path.

Regression cover for an observed production data-loss bug: yt-dlp runs the real
download via a CHILD ffmpeg (`--downloader ffmpeg`). Terminating only the yt-dlp
pid orphaned that ffmpeg, which kept the recording file open and writing; a later
remux then unlinked the source, draining live footage into a deleted inode (and
leaving the segment "No such file" at remux time). The fixes verified here:

  - StreamCapture launches each capture in its OWN process group and
    _terminate() signals the whole group, so no child survives (no orphan).
  - _remux_for_telegram never trades a present source for a missing/empty
    output, and treats a vanished/empty source as nothing-to-convert.
  - _enqueue_job reports a file that vanished before enqueue as lost instead of
    enqueuing a phantom row.

No network, no yt-dlp: a shell parent that forks a writer child stands in for the
yt-dlp→ffmpeg pair; real temp files stand in for segments.

Run: PYTHONPATH=core:recorder python3 -m recorder._selftest_capture
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recorder.state as st                              # noqa: E402
import recorder.capture as cap_mod                        # noqa: E402
from recorder.capture import StreamCapture               # noqa: E402
from core.platform import process as _process            # noqa: E402
from core.platform import procgroup as _procgroup        # noqa: E402

_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        print(f"✗ FAIL: {label}")
        raise SystemExit(1)
    print(f"✓ {label}")


# pid-liveness via core.platform.process: `os.kill(pid, 0)` is POSIX-only —
# on Windows it TerminateProcess-es (or errors) instead of merely probing.
_pid_alive = _process.pid_alive


def test_terminate_kills_whole_group(tmp: Path) -> None:
    print("\n── _terminate kills the child writer too (no orphan) ──")
    tmp.mkdir(parents=True, exist_ok=True)
    childfile = tmp / "child.out"
    pidfile = tmp / "childpid"
    # Parent (stands in for yt-dlp) spawns a writer child (stands in for the
    # ffmpeg downloader) that appends forever. We record the child's pid so we
    # can prove it dies with the group rather than being orphaned. Both levels
    # are Python: an `sh` parent would report MSYS pids on Windows (useless to
    # taskkill/OpenProcess), and $!-style forking doesn't exist there at all.
    writer_py = tmp / "writer.py"
    writer_py.write_text(
        "import sys, time\n"
        "while True:\n"
        "    with open(sys.argv[1], 'a') as f:\n"
        "        f.write('x\\n')\n"
        "    time.sleep(0.1)\n"
    )
    parent_py = tmp / "parent.py"
    parent_py.write_text(
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "pathlib.Path(sys.argv[3]).write_text(str(child.pid))\n"
        "child.wait()\n"
    )
    # Launch exactly the way StreamCapture launches yt-dlp (same popen kwargs:
    # its own process group / session), so _terminate() is exercised on the
    # same shape of process tree it manages in production.
    proc = subprocess.Popen(
        [sys.executable, str(parent_py), str(writer_py),
         str(childfile), str(pidfile)],
        **_procgroup.popen_kwargs())

    cap = StreamCapture(str(tmp), None)
    cap._proc = proc                       # inject the live group as the capture
    deadline = time.time() + 10            # let the child spin up + write its pid
    while time.time() < deadline and not pidfile.exists():
        time.sleep(0.1)
    time.sleep(0.3)                        # pid written → let the writer start
    child_pid = int(pidfile.read_text().strip())
    check(_pid_alive(child_pid), "writer child is running before terminate")

    cap._terminate()

    check(proc.poll() is not None, "parent (yt-dlp stand-in) terminated")
    time.sleep(0.4)
    check(not _pid_alive(child_pid),
          "writer child (ffmpeg stand-in) killed with the group — NO orphan")
    s1 = childfile.stat().st_size
    time.sleep(0.4)
    s2 = childfile.stat().st_size
    check(s1 == s2, "the orphan is not still writing the file after terminate")


def test_remux_missing_or_empty_source(tmp: Path) -> None:
    print("\n── remux: vanished / empty source → nothing-to-convert, no loss ──")
    tmp.mkdir(parents=True, exist_ok=True)
    missing = tmp / "gone.flv"
    out = st._remux_for_telegram(missing)
    check(out == missing and not missing.exists(),
          "missing source returns unchanged path, never raises")

    empty = tmp / "empty.flv"
    empty.write_bytes(b"")
    out = st._remux_for_telegram(empty)
    check(out == empty and empty.exists(),
          "empty source is kept, not converted, not deleted")


def test_remux_keeps_source_on_empty_output(tmp: Path, monkeypatch_run) -> None:
    print("\n── remux: ffmpeg rc=0 but no output → keep the original ──")
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "real.flv"
    src.write_bytes(b"\0" * 4096)          # passes the present+non-empty guard

    # Simulate ffmpeg returning success WITHOUT producing the .mp4.
    class _R:
        returncode = 0
        stderr = b""
    monkeypatch_run(lambda *a, **k: _R())

    out = st._remux_for_telegram(src)
    check(out == src and src.exists(),
          "source preserved when remux yields no real output (no silent loss)")
    check(not (tmp / "real.mp4").exists(),
          "no empty .mp4 left masquerading as the recording")


def test_inplace_remux_temp_is_hidden(tmp: Path, monkeypatch_run) -> None:
    print("\n── remux: in-place .mp4 temp is a dotfile (no phantom on kill) ──")
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "clip_1234.mp4"
    src.write_bytes(b"\0" * 4096)
    seen: dict = {}

    class _R:
        returncode = 0
        stderr = b""

    def _fake_run(cmd, **k):
        # ffmpeg's last positional arg is the output path; record + create it so
        # the remux "succeeds" and we can inspect the temp name it chose.
        out = Path(cmd[-1])
        seen["out"] = out
        out.write_bytes(b"\0" * 2048)
        return _R()

    monkeypatch_run(_fake_run)
    st._remux_for_telegram(src)
    # A hidden temp is excluded by both output_files() and the startup sweep,
    # so an interrupted remux can never resurrect as a phantom recording.
    check(seen["out"].name.startswith("."),
          f"in-place remux temp is hidden ({seen['out'].name})")


def test_enqueue_skips_vanished_file(tmp: Path) -> None:
    print("\n── handoff: a file gone before enqueue is reported lost, not phantomed ──")
    tmp.mkdir(parents=True, exist_ok=True)
    calls: list[tuple] = []

    # Minimal StateMachine stand-in: only _enqueue_job + its enqueue dep matter.
    sm = st.StateMachine.__new__(st.StateMachine)
    sm.enqueue = lambda *a, **k: calls.append((a, k))

    job = st._Job(username="alice", file_path=tmp / "never_existed.flv")
    sm._enqueue_job(job)
    check(calls == [], "vanished file is NOT enqueued (no phantom queue row)")


def test_manifest_is_authoritative(tmp: Path) -> None:
    print("\n── output_files: manifest wins over concurrent prep scratch ──")
    run = tmp / "alice"
    run.mkdir(parents=True, exist_ok=True)
    rec = run / "alice_111.mp4"
    rec.write_bytes(b"\0" * 4096)
    # A concurrent media_prep of this same recording litters scratch into the
    # SAME run dir — exactly what the old mtime scan swept up as phantom rows.
    (run / "alice_111.tgprep.mp4").write_bytes(b"\0" * 4096)
    (run / "alice_111.tgprep_part000.mp4").write_bytes(b"\0" * 4096)
    (run / "alice_111_segments.txt").write_text("scratch\n")
    manifest = run / "alice_111_files.txt"
    manifest.write_text(str(rec) + "\n")           # yt-dlp reported only this

    cap = StreamCapture(str(tmp), None)
    cap._run_dir = run
    cap._started_at = time.time()
    cap._manifest_path = manifest

    out = cap.output_files()
    check(out == [rec],
          f"only the recording is returned, no prep scratch ({[p.name for p in out]})")


def test_manifest_drops_vanished_and_reanchors(tmp: Path) -> None:
    print("\n── output_files: manifest drops vanished + re-anchors relative ──")
    run = tmp / "dave"
    run.mkdir(parents=True, exist_ok=True)
    rec = run / "dave_333.mp4"
    rec.write_bytes(b"\0" * 4096)
    manifest = run / "dave_333_files.txt"
    manifest.write_text("\n".join([
        str(run / "dave_333_GONE.mp4"),   # listed but never on disk → dropped
        "dave_333.mp4",                   # CWD-relative basename → re-anchored
        "",                               # blank line ignored
    ]) + "\n")

    cap = StreamCapture(str(tmp), None)
    cap._run_dir = run
    cap._started_at = time.time()
    cap._manifest_path = manifest

    out = cap.output_files()
    check(out == [rec],
          f"vanished entry dropped, relative path re-anchored ({[p.name for p in out]})")


def test_scan_fallback_excludes_scratch(tmp: Path) -> None:
    print("\n── output_files: manifest-less fallback still excludes scratch ──")
    run = tmp / "carol"
    run.mkdir(parents=True, exist_ok=True)
    rec = run / "carol_222.mp4"
    rec.write_bytes(b"\0" * 4096)
    # No manifest (hard kill / Ctrl-C: after_move never fired). The partial must
    # still be recovered, but concurrent prep scratch must NOT be resurrected.
    (run / "carol_222.tgprep.mp4").write_bytes(b"\0" * 4096)
    (run / "carol_222_segments.txt").write_text("scratch\n")
    (run / "carol_222_files.txt").write_text("")     # a stray manifest sidecar

    cap = StreamCapture(str(tmp), None)
    cap._run_dir = run
    cap._started_at = time.time() - 1
    cap._manifest_path = None                         # force the scan fallback

    out = cap.output_files()
    check(out == [rec],
          f"scan returns the partial, excludes tgprep/_segments/_files ({[p.name for p in out]})")


def test_finalize_removes_manifest(tmp: Path) -> None:
    print("\n── finalize: the consumed manifest sidecar is dropped ──")
    run = tmp / "erin"
    run.mkdir(parents=True, exist_ok=True)
    rec = run / "erin_444.mp4"
    rec.write_bytes(b"\0" * 4096)
    manifest = run / "erin_444_files.txt"
    manifest.write_text(str(rec) + "\n")
    logf = run / "erin_444_ytdlp.log"
    logf.write_text("log\n")

    cap = StreamCapture(str(tmp), None)
    cap._run_dir = run
    cap._started_at = time.time()
    cap._manifest_path = manifest
    cap._log_path = logf

    cap.finalize()
    check(not manifest.exists(), "finalize deletes the run's output manifest")
    check(cap._manifest_path is None, "manifest handle cleared after finalize")


def test_start_wires_manifest(tmp: Path) -> None:
    print("\n── start: wires --print-to-file and resets a stale manifest ──")
    run = tmp / "bob"
    run.mkdir(parents=True, exist_ok=True)

    class _FakeProc:
        def poll(self):
            return None

    seen: dict = {}
    orig_popen = cap_mod.subprocess.Popen
    orig_time = cap_mod.time.time
    # Pin the clock so the epoch-keyed manifest name is deterministic, and stub
    # Popen so no real yt-dlp launches — we only inspect the assembled command.
    cap_mod.time.time = lambda: 1_000_000.0
    cap_mod.subprocess.Popen = (
        lambda cmd, **k: (seen.__setitem__("cmd", cmd) or _FakeProc()))
    # A leftover manifest from a crashed prior run on the same second.
    stale = run / "bob_1000000_files.txt"
    stale.write_text("D:/old/phantom.mp4\n")
    try:
        cap = StreamCapture(str(tmp), None)
        cap.start("https://example/live", "bob")
        cap._close_log()                             # release the log fd we opened
    finally:
        cap_mod.subprocess.Popen = orig_popen
        cap_mod.time.time = orig_time

    cmd = seen["cmd"]
    i = cmd.index("--print-to-file")
    check(cmd[i + 1] == "after_move:%(filepath)s",
          "print template is after_move:%(filepath)s")
    check(cmd[i + 2] == str(cap._manifest_path),
          "print-to-file targets this run's manifest")
    check(cap._manifest_path.name == "bob_1000000_files.txt",
          "manifest is named by user + epoch")
    check(not stale.exists(),
          "a stale manifest at the same path is cleared on start")


def main() -> int:
    print("recorder capture-termination + remux integrity self-test")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        test_terminate_kills_whole_group(root / "grp")
        test_remux_missing_or_empty_source(root / "rx")

        # tiny monkeypatch helper scoped to one test
        orig_run = st.subprocess.run

        def _set(fn):
            st.subprocess.run = fn
        try:
            test_remux_keeps_source_on_empty_output(root / "rx2", _set)
            test_inplace_remux_temp_is_hidden(root / "rx3", _set)
        finally:
            st.subprocess.run = orig_run

        test_enqueue_skips_vanished_file(root / "hq")

        test_manifest_is_authoritative(root / "m1")
        test_manifest_drops_vanished_and_reanchors(root / "m2")
        test_scan_fallback_excludes_scratch(root / "m3")
        test_finalize_removes_manifest(root / "m4")
        test_start_wires_manifest(root / "m5")
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
