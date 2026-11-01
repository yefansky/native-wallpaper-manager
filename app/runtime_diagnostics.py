"""Local, bounded lifecycle diagnostics; never changes desktop behaviour.

Exception values and locals are deliberately omitted: resource URLs and login
credentials can occur in them. Fatal dumps contain Python stacks only.
"""
from __future__ import annotations

import ctypes
from datetime import datetime
import faulthandler
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import traceback

_logger = logging.getLogger("desktop_gallery.runtime")
_logger.propagate = False
_logger.addHandler(logging.NullHandler())
_fault_file = None
_initialized = False
MAX_LOG_BYTES = 512 * 1024


def record(event: str, **fields) -> None:
    """Only callers with fixed event/field names may write this private log."""
    try:
        _logger.info("%s %s", event, " ".join(f"{key}={value}" for key, value in fields.items()))
    except Exception:
        pass  # Diagnostics must never prevent a wallpaper operation or shutdown.


def record_exception(event: str, exc_type, _value, tb) -> None:
    try:
        frames = ";".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(tb)
        )
        record(event, exception=exc_type.__name__, frames=frames)
    except Exception:
        pass


def record_desktop(event: str, *, tray_thread=None, target=None) -> None:
    try:
        from wallpaper_backend import _current_wallpaper_from_registry, _current_wallpaper_from_system_api

        def identity(path):
            if path is None:
                return "unknown"
            return hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]

        record(event, spi=identity(_current_wallpaper_from_system_api()),
               registry=identity(_current_wallpaper_from_registry()),
               target=identity(target),
               tray_alive=tray_thread.is_alive() if tray_thread is not None else "unknown")
    except Exception as exc:
        record(event, observation_error=type(exc).__name__)


def initialize(directory: Path, version: str) -> None:
    global _initialized, _fault_file
    if _initialized:
        return
    _initialized = True
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / "runtime.log", maxBytes=MAX_LOG_BYTES,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s pid=%(process)d thread=%(threadName)s %(message)s"))
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
        session = ctypes.c_ulong()
        found = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
        record("startup", version=version, session=session.value if found else "unknown")
        # Keep the fd open for faulthandler. No periodic traceback dumps: at most
        # one fatal dump per process; rotate old data at startup, not under its fd.
        if faulthandler.is_enabled():
            record("fault_handler_existing")  # Respect a host/debugger's handler.
        else:
            fault_path = directory / "fault.log"
            if fault_path.exists() and fault_path.stat().st_size >= MAX_LOG_BYTES:
                fault_path.replace(directory / "fault.previous.log")
            _fault_file = fault_path.open("ab", buffering=0)
            _fault_file.write(f"\nstartup={datetime.now().isoformat()} pid={os.getpid()} session={session.value}\n".encode("ascii"))
            faulthandler.enable(file=_fault_file, all_threads=True)
            record("fault_handler_ready")
    except Exception as exc:
        record("diagnostics_unavailable", exception=type(exc).__name__)

    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def unhandled(exc_type, value, tb):
        record_exception("unhandled_main", exc_type, value, tb)
        previous_sys(exc_type, value, tb)

    def thread_unhandled(args):
        record_exception("unhandled_thread", args.exc_type, args.exc_value, args.exc_traceback)
        previous_thread(args)

    sys.excepthook = unhandled
    threading.excepthook = thread_unhandled


def observe_tk(root) -> None:
    if getattr(root, "_runtime_diagnostics_installed", False):
        return
    root._runtime_diagnostics_installed = True
    previous = root.report_callback_exception

    def callback_exception(exc_type, value, tb):
        record_exception("unhandled_tk", exc_type, value, tb)
        previous(exc_type, value, tb)

    root.report_callback_exception = callback_exception
