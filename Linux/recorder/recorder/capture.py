"""
recorder.capture
────────────────
yt-dlp subprocess wrapper for recording one live stream.

FLAG CHOICES (corrected vs the guide — verified against yt-dlp 2026):
  --downloader ffmpeg   TikTok live HLS is NOT supported by yt-dlp's
                        native downloader; ffmpeg is required for live
                        HLS. Without this you get a few seconds then a
                        "Live HLS not supported by native downloader"
                        bail.
  --downloader-args ffmpeg:-reconnect …
                        ffmpeg does the actual live-HLS download and, unlike
                        yt-dlp's own --retries, does NOT reconnect the HTTP
                        transport by default. A socket stall / rotated m3u8 /
                        mid-stream EOF then kills ffmpeg, ends the recording
                        file, and the state machine relaunches into a NEW file —
                        one broadcast becomes many short clips. These reconnect
                        flags keep one ffmpeg process alive across blips so the
                        broadcast stays in a single file.
  --hls-use-mpegts      MPEG-TS container survives mid-stream disconnects
                        (TikTok lives drop often). NOTE: yt-dlp already
                        enables this by default for live, but we set it
                        explicitly so behavior is pinned regardless of
                        yt-dlp default changes.
  --no-part             Write directly to the final filename, no .part
                        rename at the end. We want a usable file even if
                        the recorder is killed mid-stream — the partial
                        TS is still playable/uploadable.
  --retries infinite
  --fragment-retries infinite
                        A live stream that blips shouldn't end the
                        recording. Keep retrying fragments until the
                        stream genuinely ends (yt-dlp then exits 0).

  We deliberately DROP --live-from-start: it's a YouTube DVR-rewind
  feature, has caused live regressions (yt-dlp #15751), and isn't
  meaningful for TikTok where you record from join point forward.

PROCESS CONTROL (guide lesson, kept):
  Never call proc.wait() with no timeout — it's uninterruptible and the
  recorder could never be shut down mid-recording. wait() polls in a loop
  against a threading.Event so the main thread can terminate cleanly.

OUTPUT DISCOVERY:
  yt-dlp's final filename isn't perfectly predictable (the %(epoch)s and the
  extension are chosen by yt-dlp at runtime). Rather than guess from the
  directory, we have yt-dlp report the FINAL path of every completed file into
  a per-run manifest (`--print-to-file after_move:filepath`) and read that back
  authoritatively. This matters because a concurrent media_prep of the same
  recording litters .tgprep/_segments.txt scratch into the same run dir; an
  mtime-window directory scan can't tell that scratch from the recording and
  used to sweep it in, enqueuing phantom "lost recording" rows. The manifest
  lists only what THIS capture wrote, so scratch and prior-run leftovers can
  never be mistaken for a recording.

  A directory scan survives only as a scoped fallback: while a download is in
  progress (byte measurement for the dead-stream guard) and on a hard kill /
  Ctrl-C (after_move never fires, but --no-part leaves a usable partial), the
  manifest is empty. The fallback is newer-than-start and excludes sidecars +
  media_prep scratch so it recovers the partial without resurrecting byproducts.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.platform import procgroup as _procgroup

log = logging.getLogger(__name__)


class StreamCapture:
    def __init__(self, output_dir: str, cookies_file: str | None,
                 start_timeout_s: float = 120.0):
        self.output_dir = Path(output_dir).expanduser()
        self.cookies_file = cookies_file
        # Dead-stream guard: terminate yt-dlp if it produces ZERO bytes
        # within this many seconds of starting. Zero bytes means there is no
        # recording to lose, so this can never drop captured data; set to 0
        # to disable.
        self.start_timeout_s = start_timeout_s
        self._proc: subprocess.Popen | None = None
        self._run_dir: Path | None = None
        self._started_at: float = 0.0
        self._log_fh = None  # yt-dlp stdout/stderr sink; closed in wait()
        self._log_path: Path | None = None  # the sink's path, for finalize()
        # yt-dlp --print-to-file sink: the exact final path of each completed
        # output. The authoritative source for output_files() (see module doc).
        self._manifest_path: Path | None = None

    def start(self, stream_url: str, username: str) -> None:
        """Launch yt-dlp. Files land in output_dir/<username>/."""
        self._run_dir = self.output_dir / username
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._started_at = time.time()

        # Fresh output manifest per run. Unlink any leftover at this exact path
        # first: a crashed prior run that landed on the same wall-clock second
        # must not seed this run's discovery with stale phantom paths.
        self._manifest_path = (
            self._run_dir / f"{username}_{int(self._started_at)}_files.txt")
        try:
            self._manifest_path.unlink()
        except OSError:
            pass

        # %(epoch)s keeps successive recordings of the same user from
        # colliding; the dispatcher later sends each file individually.
        out_template = str(
            self._run_dir / f"{username}_%(epoch)s.%(ext)s"
        )

        cmd = [
            # Invoke yt-dlp via OUR OWN interpreter (`sys.executable -m yt_dlp`),
            # never the bare `yt-dlp` command. yt_dlp is a dependency inside the
            # recorder's venv, so this always runs a working copy. A bare
            # `yt-dlp` resolves through PATH, and on the target box PATH's first
            # match was a stale `pip install --user` shim in an unrelated app's
            # sandboxed Python whose yt_dlp module had been removed — it exited
            # instantly with `ModuleNotFoundError: No module named 'yt_dlp'`,
            # producing zero bytes that the state machine read as a dead stream
            # (the "capture exited rc=1 but still LIVE" loop). Pinning to
            # sys.executable makes the recorder immune to whatever else is on
            # PATH. (ffmpeg is still resolved via PATH by yt-dlp's downloader —
            # it is not a Python module — but no broken ffmpeg shadowed it.)
            sys.executable, "-m", "yt_dlp",
            # --socket-timeout: cap a hung read at 10s instead of yt-dlp's 20s
            # default. TikTok's pull-HLS edges (pull-hls-*) sometimes complete
            # the TLS handshake but never return a body — the real fix is to pull
            # FLV instead of HLS (see tiktok._extract_pull_url), but if an FLV
            # edge ever stalls too this bounds the dead time before the reconnect
            # loop re-resolves a fresh edge. The UA + Referer are defensive (a
            # player-like request never hurts and forwards into the child
            # ffmpeg); they did NOT fix the HLS stall on their own — edge choice
            # did — but are cheap to keep.
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "--referer", "https://www.tiktok.com/",
            "--socket-timeout", "10",
            "--downloader", "ffmpeg",
            # ffmpeg (our live-HLS downloader) does NOT retry the HTTP transport
            # on its own — any socket stall, rotated m3u8, or mid-stream EOF ends
            # the ffmpeg process, yt-dlp exits, and that output file is closed. On
            # TikTok live those blips happen constantly, so without this the state
            # machine relaunches yt-dlp on every drop and one broadcast lands as a
            # pile of short clips. These flags keep a SINGLE ffmpeg process alive
            # across blips by re-opening the connection internally, so a whole
            # broadcast stays in one file. yt-dlp's --retries/--fragment-retries
            # are its own knobs and don't reach the child ffmpeg — hence this.
            # (reconnect_* apply to http/https inputs, exactly the HLS case.)
            "--downloader-args",
            "ffmpeg:-reconnect 1 -reconnect_streamed 1 "
            "-reconnect_at_eof 1 -reconnect_delay_max 30",
            "--hls-use-mpegts",
            "--no-part",
            "--retries", "infinite",
            "--fragment-retries", "infinite",
            # Authoritative output discovery: append the FINAL path of each
            # completed file (after any move/merge) to our per-run manifest.
            # output_files() reads this instead of scanning the dir, so a
            # concurrent media_prep's .tgprep/_segments.txt scratch is never
            # mistaken for a recording. Writes only to the file, not stdout, so
            # it never pollutes the merged yt-dlp log.
            "--print-to-file", "after_move:%(filepath)s",
            str(self._manifest_path),
            "-o", out_template,
        ]
        if self.cookies_file:
            cmd += ["--cookies", self.cookies_file]
        cmd.append(stream_url)

        log.debug("capture: starting yt-dlp for @%s → %s", username, self._run_dir)
        log.debug("capture cmd: %s", " ".join(cmd))
        # yt-dlp/ffmpeg emit continuous progress on stderr while recording.
        # Capturing with subprocess.PIPE and never reading it fills the OS
        # pipe buffer (~16-64KB) within minutes, blocking the child's next
        # write() and silently freezing the recording. Redirect to a regular
        # file fd instead: kernel appends never block the writer. stderr is
        # merged into stdout so one file holds the full diagnostic stream.
        self._log_path = self._run_dir / f"{username}_{int(self._started_at)}_ytdlp.log"
        self._log_fh = open(self._log_path, "ab", buffering=0)
        # Put yt-dlp in its OWN process group so we can later signal the whole
        # group. yt-dlp does the actual download via a child ffmpeg (--downloader
        # ffmpeg); without the group, terminating only the yt-dlp pid orphans that
        # ffmpeg — it keeps the recording file open and writing, and a remux that
        # then unlinks the source drains live footage into a deleted inode (silent
        # data loss, observed in prod). The OS-specific spawn flag (POSIX
        # start_new_session / Windows CREATE_NEW_PROCESS_GROUP) comes from the
        # core.platform.procgroup adapter.
        # Pin the child's working directory to this run's own output dir (always
        # under output_dir on the internal drive, and just created above) rather
        # than inheriting the launcher's cwd. A stale inherited cwd — e.g. a
        # shell or service started on a drive that was later removed/formatted
        # (D:\ post-migration) — makes CreateProcess fail with
        # `[WinError 3] cannot find the path specified: 'D:\\'` before yt-dlp
        # ever runs. _run_dir is guaranteed to exist, so this can't reintroduce
        # the same failure.
        self._proc = subprocess.Popen(
            cmd, stdout=self._log_fh, stderr=subprocess.STDOUT,
            cwd=self._run_dir,
            **_procgroup.popen_kwargs(),
        )

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def elapsed_s(self) -> float:
        """Wall-clock seconds since this run's yt-dlp was launched (0 if it
        never started). Used for the recording-ended summary line."""
        return max(0.0, time.time() - self._started_at) if self._started_at else 0.0

    def wait(self, stop_event: threading.Event) -> int:
        """Block until yt-dlp exits OR stop_event is set.

        Returns the process exit code, or -1 if we terminated it because
        stop_event fired. Polls every 2s so a shutdown request is honored
        within ~2s rather than hanging on an uninterruptible wait()."""
        if self._proc is None:
            return -1
        while self.is_running():
            if stop_event.wait(timeout=2.0):
                log.debug("capture: stop requested — terminating yt-dlp")
                self._terminate()
                self._close_log()
                return -1
            # Dead-stream guard. After a live ends, the recorder's handoff
            # re-scan can re-detect the just-ended user as live (TikTok lag)
            # and we relaunch yt-dlp on a dead URL. With --retries infinite
            # that call would hang here forever and the recorder would never
            # return to LISTENING. If no bytes have arrived within the
            # startup window the stream is dead — bail. Safe: zero bytes
            # means there is no recording to lose.
            if (self.start_timeout_s > 0
                    and time.time() - self._started_at > self.start_timeout_s
                    and self._recorded_bytes() == 0):
                log.warning("capture: no data after %.0fs — assuming dead "
                            "stream, terminating yt-dlp", self.start_timeout_s)
                self._terminate()
                self._close_log()
                return -2
        rc = self._proc.returncode
        log.debug("capture: yt-dlp exited rc=%d", rc)
        self._close_log()
        return rc

    def _terminate(self) -> None:
        """Stop the capture AND every subprocess it spawned.

        Graceful-stop the whole process group (yt-dlp + its child ffmpeg),
        escalating to a forceful group/tree kill if it ignores us. Group-signalling
        is what guarantees the ffmpeg downloader dies with yt-dlp instead of being
        orphaned and left writing the recording file (see start()). Falls back to
        acting on the lone pid if the group can't be reached (already gone). The
        OS-specific signalling (POSIX SIGTERM/SIGKILL to the group, Windows
        CTRL_BREAK then taskkill /T /F) lives in core.platform.procgroup."""
        if self._proc is None:
            return
        if not _procgroup.terminate(self._proc):
            self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("capture: yt-dlp group ignored graceful stop — killing")
            if not _procgroup.kill(self._proc):
                self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("capture: yt-dlp survived kill — possible orphan")

    def _recorded_bytes(self) -> int:
        """Total bytes written so far for this run. output_files() already
        excludes logs/sidecars, so this measures only the recording."""
        total = 0
        for p in self.output_files():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def _close_log(self) -> None:
        """Release the yt-dlp log fd. Idempotent — safe to call twice."""
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            finally:
                self._log_fh = None

    def finalize(self) -> None:
        """Settle this run's yt-dlp log so it never orphans. Call once after
        wait() returns.

          recording produced → rename the log to share the media's stem
            (<user>_<epoch>_ytdlp.log) so cleanup_sidecars deletes it when the
            recording is deleted post-upload. The log is named off our wall
            clock but the media off yt-dlp's own %(epoch)s; the two can drift a
            second, so we re-key the log to the actual file rather than trust
            the names to match.
          no recording (dead stream) → there is no record to ever pair with or
            delete, so the log would pile up forever. Remove it now.

        The media stem survives the later -c copy remux (suffix-only change),
        so the pairing still holds for the uploaded .mp4."""
        media = self.output_files()
        # The manifest's paths are now consumed (the record loop snapshotted this
        # run's files before calling finalize). Drop the sidecar so it doesn't
        # accumulate next to recordings — it's keyed by wall clock, not the media
        # stem, so cleanup_sidecars wouldn't pair it later.
        self._unlink_manifest()
        if self._log_path is None:
            return
        if not media:
            # TEMP DIAGNOSTIC (remove once the intermittent zero-byte failures
            # are understood): a dead-stream run is exactly the case we can't
            # otherwise inspect — normally the log is deleted here, so the real
            # yt-dlp/ffmpeg error is lost. Instead RENAME it to a distinctive
            # *_DEADSTREAM.log kept in the run dir so a failed capture leaves its
            # diagnostics behind. These won't be cleaned by cleanup_sidecars (no
            # media to pair with) — delete them manually after debugging.
            try:
                keep = self._log_path.with_name(
                    self._log_path.stem.replace("_ytdlp", "") + "_DEADSTREAM.log")
                if self._log_path.exists():
                    self._log_path.rename(keep)
                    log.warning("capture: dead-stream diagnostics kept at %s", keep)
            except OSError as e:
                log.debug("capture: could not keep dead-stream log: %s", e)
            self._log_path = None
            return
        target = media[0].with_name(media[0].stem + "_ytdlp.log")
        if target != self._log_path and self._log_path.exists():
            try:
                self._log_path.rename(target)
                self._log_path = target
            except OSError as e:
                log.debug("capture: could not pair log with recording: %s", e)

    def output_files(self) -> list[Path]:
        """Files this run's yt-dlp actually produced.

        Authoritative source: the --print-to-file manifest, into which yt-dlp
        writes the final path of every completed download. Reading it (not the
        directory) means files this capture did NOT write — chiefly a
        concurrent media_prep's .tgprep/_segments.txt scratch, or leftovers
        from a prior run — can never be swept in and enqueued as phantom "lost
        recordings" (the failure this replaced).

        Fallback: before any download completes (in-progress byte measurement
        for the dead-stream guard) and on a hard kill / Ctrl-C (after_move
        never fires, but --no-part leaves a usable partial), the manifest is
        empty. Only then do we scan the run dir — scoped to files newer than
        our start and excluding sidecars + media_prep scratch."""
        if self._run_dir is None or not self._run_dir.exists():
            return []
        manifest = self._manifest_files()
        if manifest:
            return manifest
        return self._scan_run_dir()

    def _manifest_files(self) -> list[Path]:
        """Parse the yt-dlp output manifest into existing files. Tolerates
        absolute or CWD-relative paths (re-anchored by basename into the run
        dir) and drops entries that no longer exist."""
        if self._manifest_path is None or not self._manifest_path.exists():
            return []
        try:
            lines = self._manifest_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[Path] = []
        seen: set[Path] = set()
        for line in lines:
            name = line.strip()
            if not name:
                continue
            p = Path(name)
            if not p.is_absolute() and self._run_dir is not None:
                # yt-dlp may print relative to its CWD; re-anchor by basename
                # into our run dir so discovery is CWD-independent.
                cand = self._run_dir / p.name
                if cand.exists():
                    p = cand
            if p in seen:
                continue
            seen.add(p)
            if p.is_file():
                out.append(p)
        return sorted(out)

    def _scan_run_dir(self) -> list[Path]:
        """mtime-window directory scan (fallback only, see output_files).
        Kept deliberately narrow: it excludes media_prep scratch so an
        interrupted-capture partial is recovered without also resurrecting a
        concurrent prep's byproducts as phantom recordings."""
        assert self._run_dir is not None
        out: list[Path] = []
        for p in self._run_dir.iterdir():
            if not p.is_file():
                continue
            # Skip dotfiles — chiefly macOS AppleDouble companions (._name)
            # the OS auto-creates next to every file on exFAT/FAT volumes.
            # They carry the recording's extension but are 4KB metadata stubs,
            # not recordings; enqueuing them spawns phantom rows that vanish
            # (→ "file missing on disk") or upload as junk.
            if p.name.startswith("."):
                continue
            if p.suffix in (".part", ".ytdl", ".temp", ".log"):
                continue
            # media_prep byproducts + our own manifest sidecar are never
            # recordings. A concurrent prep of this same file litters
            # <stem>.tgprep.mp4 / <stem>.tgprep_part*.mp4 / <stem>_segments.txt
            # into the run dir; the mtime window can't tell them from the
            # capture, so exclude them by name here too (defense in depth for
            # the manifest-less path).
            if ".tgprep" in p.name or p.name.endswith(
                    ("_segments.txt", "_files.txt")):
                continue
            try:
                if p.stat().st_mtime >= self._started_at - 1:
                    out.append(p)
            except OSError:
                continue
        return sorted(out)

    def _unlink_manifest(self) -> None:
        """Drop this run's output manifest once its paths are consumed, so the
        sidecar doesn't accumulate next to recordings after upload."""
        if self._manifest_path is None:
            return
        try:
            self._manifest_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._manifest_path = None
