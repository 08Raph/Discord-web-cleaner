"""Optional "Remember me": store the token encrypted with Windows DPAPI.

DPAPI ties the encryption to your Windows user account: the file can't be read on
another PC or by another Windows user. Programs running as YOU could still ask
Windows to decrypt it, so only use this on your own, trusted PC.
"""
import ctypes
import os
import sys

_ENTROPY = b"DiscordMessageCleaner/token/v1"


def available():
    return sys.platform == "win32"


def _path():
    root = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(root, "DiscordMessageCleaner", "token.bin")


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _crypt(func, data):
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in, _keep1 = _blob(data)
        entropy, _keep2 = _blob(_ENTROPY)
        blob_out = _Blob()
        CRYPTPROTECT_UI_FORBIDDEN = 0x01
        ok = getattr(crypt32, func)(ctypes.byref(blob_in), None, ctypes.byref(entropy), None,
                                    None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise OSError("Windows could not encrypt/decrypt the saved token.")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)


def save_token(token):
    if not available():
        return False
    data = _crypt("CryptProtectData", token.encode("utf-8"))
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path(), "wb") as f:
        f.write(data)
    return True


def load_token():
    if not available() or not os.path.exists(_path()):
        return None
    try:
        with open(_path(), "rb") as f:
            return _crypt("CryptUnprotectData", f.read()).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def forget_token():
    try:
        os.remove(_path())
    except OSError:
        pass


def has_saved_token():
    return available() and os.path.exists(_path())


# ----------------------------------------------------------------- several accounts
def _accounts_path():
    return os.path.join(os.path.dirname(_path()), "accounts.bin")


def _read_accounts():
    if not available() or not os.path.exists(_accounts_path()):
        return {}
    try:
        import json
        with open(_accounts_path(), "rb") as f:
            return json.loads(_crypt("CryptUnprotectData", f.read()).decode("utf-8"))
    except (OSError, ValueError):
        return {}


def _write_accounts(accts):
    import json
    data = _crypt("CryptProtectData", json.dumps(accts).encode("utf-8"))
    os.makedirs(os.path.dirname(_accounts_path()), exist_ok=True)
    with open(_accounts_path(), "wb") as f:
        f.write(data)


def list_accounts():
    """Saved accounts, most recently used first: [{id, name, avatar}] (no tokens)."""
    accts = _read_accounts()
    if not accts:
        legacy = load_token()          # migrate the old single "Remember me" token
        if legacy:
            return [{"id": "legacy", "name": "Saved account", "avatar": None}]
    items = sorted(accts.items(), key=lambda kv: -kv[1].get("used", 0))
    return [{"id": k, "name": v.get("name", "Account"), "avatar": v.get("avatar")}
            for k, v in items]


def get_account_token(account_id):
    if account_id == "legacy":
        return load_token()
    return (_read_accounts().get(account_id) or {}).get("token")


def save_account(user, token, avatar=None):
    if not available():
        return False
    import time
    accts = _read_accounts()
    accts[user["id"]] = {"name": user.get("global_name") or user.get("username", "Account"),
                         "avatar": avatar, "token": token, "used": time.time()}
    _write_accounts(accts)
    forget_token()                     # the old single-token file isn't needed any more
    return True


def remove_account(account_id):
    if account_id == "legacy":
        forget_token()
        return
    accts = _read_accounts()
    if accts.pop(account_id, None) is not None:
        if accts:
            _write_accounts(accts)
        else:
            try:
                os.remove(_accounts_path())
            except OSError:
                pass


def has_accounts():
    return bool(list_accounts())
