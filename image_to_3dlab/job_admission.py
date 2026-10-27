"""Run one model worker while holding the configured Canopy admission lease.

This module is deliberately safe to import in the idle studio process: it imports no
Canopy, torch, HIP, or model modules. The canonical admission implementation is loaded
only by the short-lived job wrapper process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Sequence

PROFILE_ENV = "ASSETFURNACE_ADMISSION_PROFILE"


def command_prefix() -> list[str]:
    """Return the explicit argv prefix for a top-level GPU-capable job worker."""
    return [sys.executable, "-m", "image_to_3dlab.job_admission", "--"]


def _load_admission():
    profile_path = os.environ.get(PROFILE_ENV)
    if not profile_path:
        raise RuntimeError(f"{PROFILE_ENV} is required; refusing to start an unadmitted GPU job")
    path = Path(profile_path)
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read admission profile {path}: {exc}") from exc
    # Parent provides this module through PYTHONPATH. Import only after the worker wrapper
    # starts; importing any studio/API module remains independent of Canopy and HIP.
    from canopy_admission import Admission

    return Admission(profile)


def _lock_descriptors(admission) -> tuple[int, ...]:
    """Expose canonical held flock FDs to the direct model child.

    Admission has no public descriptor-export API. This narrowly depends on its private
    `_validate_held()` method and `_handles` list of `(lock, file-object)` pairs to verify
    each configured path/inode and pass the held FDs to the child. That inheritance keeps
    the flock held if this wrapper is SIGKILLed while the direct model worker survives.
    Normal completion cleans the owned process group before `Admission.release()` unlocks.
    """
    validate_held = getattr(admission, "_validate_held", None)
    if not callable(validate_held):
        raise RuntimeError("Canopy admission held-lock validation API changed")
    validate_held()
    handles = getattr(admission, "_handles", None)
    if not isinstance(handles, list) or not handles:
        raise RuntimeError("Canopy admission acquired no inspectable lock handles")
    descriptors = []
    for entry in handles:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise RuntimeError("Canopy admission lock handle shape changed")
        handle = entry[1]
        try:
            fd = handle.fileno()
            os.fstat(fd)
        except (AttributeError, OSError, ValueError) as exc:
            raise RuntimeError("Canopy admission returned an invalid lock descriptor") from exc
        descriptors.append(fd)
    return tuple(descriptors)


def _group_exists(pgid: int) -> bool:
    """Whether the owned process group still contains a live process (not a zombie)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    # Orphaned descendants are reparented to init. Ignore their zombie entries: they no
    # longer execute or hold GPU resources, and some minimal container inits never reap.
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="ascii")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return True
        _, separator, rest = stat_text.rpartition(")")
        fields = rest.split()
        if not separator or len(fields) < 3:
            return True
        if fields[2].isdecimal() and int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
            return True
    return False


def _stop_owned_group(pgid: int, grace: float = 1.0) -> None:
    """Stop/reap only the model child's private session, never unrelated processes."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        # Sending SIGKILL is not proof of cleanup; retain the lease until the kernel
        # confirms the owned group is gone, including any uninterruptible processes.
        while _group_exists(pgid):
            time.sleep(0.05)


def run(command: Sequence[str]) -> int:
    """Acquire admission, execute argv unchanged, clean its process group, then release."""
    if not command:
        raise ValueError("a worker command is required")
    admission = _load_admission()
    cancelled = False
    cancel_signal = signal.SIGTERM
    cancel_started = None
    child: subprocess.Popen | None = None
    cleanup_complete = True
    prior_handlers = {}

    def request_cancel(signum, _frame):
        nonlocal cancelled, cancel_signal, cancel_started
        cancelled = True
        cancel_signal = signum
        cancel_started = cancel_started or time.monotonic()
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        prior_handlers[sig] = signal.signal(sig, request_cancel)
    try:
        print("Waiting for Canopy GPU admission...", file=sys.stderr, flush=True)
        if not admission.acquire(lambda: cancelled):
            return 128 + cancel_signal if cancelled else 1
        if cancelled:
            return 128 + cancel_signal
        descriptors = _lock_descriptors(admission)
        # A distinct session gives cancellation a precise owned process group. The only
        # inherited FDs are canonical flock handles required for wrapper-SIGKILL safety.
        child = subprocess.Popen(
            list(command),
            start_new_session=True,
            close_fds=True,
            pass_fds=descriptors,
        )
        cleanup_complete = False
        if cancelled:
            request_cancel(cancel_signal, None)
        while True:
            try:
                code = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancelled and cancel_started is not None and time.monotonic() - cancel_started >= 2:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        # The worker may have left helpers in its own session. Do not release the lease
        # until that owned process group is gone; cleanup remains strictly group-scoped.
        _stop_owned_group(child.pid)
        cleanup_complete = True
        return code
    finally:
        if child is not None:
            if child.poll() is None:
                request_cancel(signal.SIGTERM, None)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    request_cancel(signal.SIGKILL, None)
                    child.wait()
            if not cleanup_complete:
                while True:
                    try:
                        _stop_owned_group(child.pid)
                        cleanup_complete = True
                        break
                    except Exception as exc:
                        print(
                            f"Cannot verify owned worker cleanup; keeping Canopy admission: {exc}",
                            file=sys.stderr, flush=True,
                        )
                        time.sleep(1)
        if admission.held:
            admission.release_after(lambda: None)
        for sig, handler in prior_handlers.items():
            signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--" or len(args) == 1:
        print("usage: python -m image_to_3dlab.job_admission -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    try:
        result = run(args[1:])
        if result < 0:
            sig = -result
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
            return 128 + sig
        return result
    except Exception as exc:
        print(f"Canopy GPU admission failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
