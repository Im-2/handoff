"""Windows compatibility patch for the installed `almanak` SDK.

Almanak's local DB file-locking (framework/local_paths.py) uses `fcntl`
unconditionally, which does not exist on Windows. This is a pure
environment-portability fix (swaps fcntl.flock for msvcrt.locking on
win32) -- it is NOT part of the KeeperHub adapter and changes no Almanak
business logic. Re-run this after any `pip install/reinstall almanak`
inside the venv, since pip reinstalls overwrite it.

Usage:
    python scripts/patch_almanak_windows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "Windows compatibility shim (local dev patch"

SHIM = '''
# --- Windows compatibility shim (local dev patch, not upstream Almanak code) ---
# Upstream only supports POSIX (uses fcntl.flock unconditionally). This repo's
# baseline is exercised on Windows, so file locking is redirected to
# msvcrt.locking here. Not part of the KeeperHub adapter; purely an
# environment-portability fix so `almanak strat run` / `almanak gateway` can
# boot locally. See README "Windows compatibility patch" note.
if sys.platform == "win32":
    import msvcrt

    _LOCK_CONTENTION_ERRNOS = (errno.EACCES,)

    def _flock_exclusive_nonblocking(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _funlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    _LOCK_CONTENTION_ERRNOS = (errno.EWOULDBLOCK, errno.EAGAIN)

    def _flock_exclusive_nonblocking(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _funlock(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
'''


SIGNAL_MARKER = "ProactorEventLoop.add_signal_handler is POSIX-only"


def patch_server_signal_handler(venv: Path) -> None:
    target = venv / "Lib" / "site-packages" / "almanak" / "gateway" / "server.py"
    if not target.is_file():
        print(f"{target} not found; skipping gateway/server.py signal-handler patch.")
        return
    text = target.read_text(encoding="utf-8")
    if SIGNAL_MARKER in text:
        print("gateway/server.py already patched.")
        return
    text = text.replace(
        "import asyncio\nimport inspect\nimport logging\nimport signal\nfrom concurrent import futures",
        "import asyncio\nimport inspect\nimport logging\nimport signal\nimport sys\nfrom concurrent import futures",
        1,
    )
    text = text.replace(
        '''    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)''',
        '''    # Windows compatibility patch (local dev patch, not upstream Almanak code):
    # ProactorEventLoop.add_signal_handler is POSIX-only (NotImplementedError
    # on win32). signal.signal() + call_soon_threadsafe is the standard
    # cross-platform substitute; SIGTERM isn't deliverable on Windows so only
    # SIGINT (Ctrl+C) is registered there.
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(handle_signal))
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_signal)''',
        1,
    )
    if SIGNAL_MARKER not in text:
        print("gateway/server.py patch failed to apply (source text did not match expected pattern).")
        return
    target.write_text(text, encoding="utf-8")
    print(f"Patched {target}")


def main() -> int:
    venv = Path(sys.prefix)
    target = venv / "Lib" / "site-packages" / "almanak" / "framework" / "local_paths.py"
    if not target.is_file():
        print(f"almanak not found at {target}; run inside the project venv after `pip install almanak`.")
        return 1

    patch_server_signal_handler(venv)

    text = target.read_text(encoding="utf-8")
    if MARKER in text:
        print("Already patched (local_paths.py).")
        return 0

    text = text.replace(
        "import shutil\nimport time\nfrom pathlib import Path",
        "import shutil\nimport sys\nimport time\nfrom pathlib import Path\n" + SHIM,
        1,
    )

    # acquire_local_db_lock: drop local `import fcntl` + inline flock call
    text = text.replace(
        '''    try:
        # Local import: ``fcntl`` is POSIX-only, but every supported
        # Almanak environment is POSIX. Importing here keeps the module
        # importable on Windows for completeness (e.g., test harnesses
        # that don't actually call this function).
        import fcntl

        # ``timeout <= 0`` (the strategy-DB default) is strictly''',
        '''    try:
        # ``timeout <= 0`` (the strategy-DB default) is strictly''',
        1,
    )
    text = text.replace(
        '''            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise''',
        '''            try:
                _flock_exclusive_nonblocking(fd)
                break
            except OSError as exc:
                if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                    raise''',
        1,
    )

    # _sweep_stale_utility_sessions: drop local `import fcntl` + inline flock call
    text = text.replace(
        '''    if not root.is_dir():
        return
    import fcntl

    try:''',
        '''    if not root.is_dir():
        return

    try:''',
        1,
    )
    text = text.replace(
        '''            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # live owner — leave it alone
            shutil.rmtree(entry, ignore_errors=True)''',
        '''            try:
                _flock_exclusive_nonblocking(fd)
            except OSError:
                continue  # live owner — leave it alone
            shutil.rmtree(entry, ignore_errors=True)''',
        1,
    )

    # release_local_db_lock: drop local `import fcntl` + inline unlock call
    text = text.replace(
        '''    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
    except Exception:
        pass''',
        '''    try:
        _funlock(handle)
    except Exception:
        pass''',
        1,
    )

    if MARKER not in text:
        print("Patch failed to apply (source text did not match expected pattern).")
        return 1

    target.write_text(text, encoding="utf-8")
    print(f"Patched {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
