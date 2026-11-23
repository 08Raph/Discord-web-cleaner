"""Log file + crash reports. Tokens are always masked before anything is written."""
import os
import platform
import re
import sys
import threading
import time
import traceback

_lock = threading.Lock()
_TOKEN_RE = re.compile(r"(mfa\.[\w-]{20,})|([\w-]{23,28}\.[\w-]{6,7}\.[\w-]{25,110})")


def log_dir():
    root = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(root, "DiscordMessageCleaner", "logs")


def log_path():
    return os.path.join(log_dir(), time.strftime("log_%Y-%m-%d.txt"))


def mask(text):
    return _TOKEN_RE.sub("[token hidden]", str(text))


def write(line):
    try:
        with _lock:
            os.makedirs(log_dir(), exist_ok=True)
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + mask(line) + "\n")
    except OSError:
        pass


def cleanup(keep_days=14):
    try:
        cutoff = time.time() - keep_days * 86400
        for f in os.listdir(log_dir()):
            p = os.path.join(log_dir(), f)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def crash_text(exc_type, exc, tb):
    return mask("".join(traceback.format_exception(exc_type, exc, tb)))


def system_info(version):
    return (f"Discord Message Cleaner {version}\n"
            f"Windows: {platform.platform()}\n"
            f"Python: {sys.version.split()[0]}  ({'app' if getattr(sys, 'frozen', False) else 'source'})\n")


def recent_lines(n=200):
    try:
        with open(log_path(), encoding="utf-8") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no log yet today)"
