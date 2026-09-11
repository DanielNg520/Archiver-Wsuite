"""
tools.setup_test_venv
──────────────────────
Creates a throwaway venv (`.venv-test` at the repo root) containing the UNION
of all five packages' dependencies, via `uv` — the only installer on this box
(no pipx, no plain pip/venv module). Needed because each package's own
`uv tool` venv only carries its own deps: the dispatcher's venv lacks
`gallery_dl`, the archiver's lacks `telethon`, and the system python lacks
everything — so `tests/test_seams.py` (a cross-package integration test) has
had no single interpreter it could run under end-to-end.

Run (from anywhere in the repo):

    python tools/setup_test_venv.py

Then run the seams test with the printed command. Re-runnable: `uv venv`
recreates `.venv-test` from scratch each time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run(command: list[str], description: str) -> None:
    result = subprocess.run(command)
    if result.returncode != 0:
        print(
            f"error: {description} failed; uv exited with status {result.returncode}",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def main() -> None:
    repo_root = _repo_root()
    venv_dir = repo_root / ".venv-test"
    venv_python = venv_dir / "bin" / "python"

    _run(["uv", "venv", str(venv_dir)], "creating the test virtualenv")

    packages = [
        repo_root / "core",
        repo_root / "archiver",
        repo_root / "recorder",
        repo_root / "dispatcher",
        repo_root / "ops",
    ]

    install_command = ["uv", "pip", "install", "--python", str(venv_python)]
    for package in packages:
        install_command.extend(["-e", str(package)])

    _run(install_command, "installing the five packages into the test virtualenv")

    print(
        'PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 '
        f"{venv_dir}/bin/python3 tests/test_seams.py"
    )


if __name__ == "__main__":
    main()
