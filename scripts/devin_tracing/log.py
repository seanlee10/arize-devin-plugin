"""Append-only file logging. Devin discards hook stderr, and stdout is
reserved for hook control JSON, so the log file is the only output channel.

Never log environment variables or credentials."""

import os
import time

_path = None
_verbose = False


def configure(path, verbose):
    global _path, _verbose
    _path = path
    _verbose = verbose


def _write(level, message):
    if not _path:
        return
    try:
        with open(_path, "a") as f:
            f.write("[%s] [pid %d] %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), level, message))
    except OSError:
        pass


def debug(message):
    if _verbose:
        _write("DEBUG", message)


def info(message):
    _write("INFO", message)


def warn(message):
    _write("WARN", message)


def error(message):
    _write("ERROR", message)
