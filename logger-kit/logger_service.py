"""Background deleted-messages logger:  DiscordMessageCleaner.exe --logger

Runs without a window, keeps one real-time connection to Discord and remembers the
messages people send you in DMs for a few days. If one of them gets deleted, it's saved
so you can still read it in the app ("Deleted messages").

Low usage by design: it sleeps until Discord sends something (heartbeat every ~41 s),
writes small rows to a local database, and only downloads pictures if you turned that on.
"""
import json
import os
import sys
import time

import applog
import cleaner_core as core
import gateway
import msglog
import securestore

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DiscordMessageCleanerLogger"
HEARTBEAT_S = 30


def settings():
    try:
        with open(os.path.join(msglog.data_dir(), "settings.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --logger'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{main}" --logger'


# ------------------------------------------------------------------ start with Windows
def set_autostart(on):
    if sys.platform != "win32":
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
    return True


def autostart_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ start / stop / status
def start_process():
    """Start the logger in the background (no window)."""
    import shlex
    import subprocess
    args = shlex.split(command(), posix=False)
    args = [a.strip('"') for a in args]
    flags = 0
    if sys.platform == "win32":
        flags = 0x08000000 | 0x00000008     # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(args, creationflags=flags, close_fds=True)


def status(store=None):
    """('running'|'starting'|'off'|'error: …', info dict)."""
    s = store or msglog.Store()
    beat = s.get_meta("heartbeat", 0) or 0
    st = s.get_meta("status", "off")
    info = {"account": s.get_meta("account"), "since": s.get_meta("started", 0),
            "dms": s.get_meta("dm_count", 0)}
    if time.time() - beat > HEARTBEAT_S * 3:
        return ("off" if not str(st).startswith("error") else st), info
    return st, info


def request_stop(store=None):
    (store or msglog.Store()).set_meta("stop", True)


# ------------------------------------------------------------------ the service itself
class Logger:
    """What it watches:
    - DMs: every message people send you (kept a few days) -> saved if they delete it
    - servers: ONLY messages that @mention you -> a "ghost ping" if they delete it
    - your friends list -> tells you when someone is no longer your friend
    When it starts again after your PC was off, it checks your DMs and finds what was
    deleted or edited while it wasn't running (for messages it had already seen)."""

    def __init__(self, store, account_id, token, save_pictures=False, keep_days=7,
                 ghost_pings=True, friends=True, catch_up=True, api=None, ignore=(),
                 export_path=None):
        self.store = store
        self.account = account_id
        self.token = token
        self.save_pictures = save_pictures
        self.keep_days = keep_days
        self.ghost_pings = ghost_pings
        self.track_friends = friends
        self.catch_up = catch_up
        self.ignore = set(str(i) for i in ignore or ())     # muted chats / people
        self.export_path = export_path
        self.last_export = 0
        self.me = None
        self.dms = {}            # channel id -> display name
        self.guild_names = {}    # guild id -> name
        self.channel_names = {}  # channel id -> name (servers)
        self.friends = {}        # user id -> user
        self.last_beat = 0
        self.last_prune = 0
        self._stopping = False
        self.api = api or core.DiscordApi(token, applog.write, lambda: self._stopping,
                                          time.sleep)

    def should_stop(self):
        now = time.time()
        if now - self.last_beat >= HEARTBEAT_S:
            self.last_beat = now
            self.store.set_meta("heartbeat", now)
            if self.store.get_meta("stop"):
                self._stopping = True
                return True
            if now - self.last_prune > 3600:
                self.last_prune = now
                self.store.prune(self.keep_days)
            if self.export_path and now - self.last_export > 600:
                self.last_export = now
                try:
                    self.store.snapshot(self.export_path)
                except Exception as e:
                    applog.write(f"Logger export failed: {e}")
        return False

    def on_status(self, s):
        self.store.set_meta("status", s)

    # ---------------------------------------------------------------- events
    def on_event(self, t, d):
        if t == "READY":
            self.on_ready(d)
        elif t == "CHANNEL_CREATE" and d.get("type") in (1, 3):
            self.dms[d["id"]] = self._dm_name(d, {})
        elif t == "GUILD_CREATE":
            self._guild(d)
        elif t == "MESSAGE_CREATE":
            self.on_message(d)
        elif t == "MESSAGE_UPDATE" and "content" in d and d.get("id"):
            self.store.edited(d["id"], d.get("content") or "")
        elif t == "MESSAGE_DELETE":
            row = self.store.deleted_now(d["id"])
            if row:
                if d.get("guild_id"):
                    self.store.add_event(self.account, "ghost_ping",
                                         {"id": row[4], "name": row[5], "avatar": row[6]},
                                         {"where": row[3], "content": row[7][:300],
                                          "channel_id": row[2], "guild_id": d.get("guild_id")})
                applog.write("Logger: saved a deleted message")
        elif t == "MESSAGE_DELETE_BULK":
            for mid in d.get("ids") or []:
                self.store.deleted_now(mid)
        elif t in ("RELATIONSHIP_ADD", "RELATIONSHIP_UPDATE") and self.track_friends:
            if d.get("type") == 1:
                u = d.get("user") or self.friends.get(d.get("id")) or {"id": d.get("id")}
                self.friends[d["id"]] = _small_user(u)
                self._save_friends()
        elif t == "RELATIONSHIP_REMOVE" and self.track_friends:
            if d.get("type") == 1 or d.get("id") in self.friends:
                u = self.friends.pop(d.get("id"), {"id": d.get("id")})
                self.store.add_event(self.account, "friend_removed", u, {"offline": False})
                self._save_friends()

    def on_ready(self, d):
        self.me = d.get("user") or {}
        users = {u["id"]: u for u in d.get("users") or []}
        chans = d.get("private_channels") or []
        for ch in chans:
            self.dms[ch["id"]] = self._dm_name(ch, users)
        for g in d.get("guilds") or []:
            self._guild(g)
        self.store.set_meta("dm_count", len(self.dms))
        if self.track_friends:
            self._check_friends(d.get("relationships") or [], users)
        applog.write(f"Logger ready: watching {len(self.dms)} DMs")
        if self.catch_up:
            import threading
            threading.Thread(target=self._catch_up, args=(chans,), daemon=True).start()

    def on_message(self, d):
        author = d.get("author") or {}
        mine = self.me and author.get("id") == self.me.get("id")
        cid = d.get("channel_id")
        if cid in self.ignore or author.get("id") in self.ignore \
                or d.get("guild_id") in self.ignore:
            return
        if d.get("guild_id"):
            # servers: only keep messages that ping YOU (for ghost pings)
            if mine or not self.ghost_pings or not self.me:
                return
            if not any(u.get("id") == self.me.get("id") for u in d.get("mentions") or []):
                return
            where = (f"#{self.channel_names.get(cid, 'channel')} · "
                     f"{self.guild_names.get(d['guild_id'], 'server')}")
            self.store.remember(self.account, d, where, _atts(d), note="ghost")
            return
        self.store.set_meta(f"seen:{cid}", d.get("id"))
        if mine:
            return                                  # your own messages: not needed
        if cid not in self.dms:
            self.dms[cid] = author.get("global_name") or author.get("username", "DM")
        atts = _atts(d)
        if self.save_pictures:
            self._save_pictures(d["id"], atts)
        self.store.remember(self.account, d, self.dms[cid], atts)

    # ---------------------------------------------------------------- friends
    def _check_friends(self, rels, users):
        now = {}
        for r in rels:
            if r.get("type") != 1:
                continue
            u = r.get("user") or users.get(r.get("user_id") or r.get("id")) or {
                "id": r.get("user_id") or r.get("id")}
            now[u.get("id")] = _small_user(u)
        before = self.store.get_meta(f"friends:{self.account}")
        if before:
            for uid, u in before.items():
                if uid not in now:
                    self.store.add_event(self.account, "friend_removed", u, {"offline": True})
        self.friends = now
        self._save_friends()

    def _save_friends(self):
        self.store.set_meta(f"friends:{self.account}", self.friends)

    # ---------------------------------------------------------------- catch-up
    def _catch_up(self, chans):
        """After the logger was off: pick up the DM messages that arrived meanwhile, and
        find which remembered messages were deleted or edited while it was off."""
        try:
            checked = 0
            for ch in chans:
                if self._stopping:
                    return
                cid = ch.get("id")
                last = ch.get("last_message_id")
                seen = self.store.get_meta(f"seen:{cid}")
                cached = self.store.cached(self.account, cid)
                if not cached and (not seen or not last or int(last) <= int(seen)):
                    if last and not seen:
                        self.store.set_meta(f"seen:{cid}", last)
                    continue
                oldest = min((int(i) for i in cached), default=None)
                floor = min(x for x in (oldest, int(seen) if seen else None) if x is not None) \
                    if (oldest or seen) else None
                if floor is None:
                    continue
                found = self._history_after(cid, floor - 1)
                if found is None:
                    continue
                checked += 1
                ids = {m["id"]: m for m in found}
                for mid, content in cached.items():
                    m = ids.get(mid)
                    if m is None:
                        self.store.deleted_now(mid, note="offline")
                    elif (m.get("content") or "") != (content or ""):
                        self.store.edited(mid, m.get("content") or "")
                newest = seen
                for m in found:
                    if seen and int(m["id"]) <= int(seen):
                        continue
                    if not (self.me and m["author"]["id"] == self.me.get("id")) \
                            and m["id"] not in cached:
                        self.store.remember(self.account, dict(m, channel_id=cid),
                                            self.dms.get(cid, "DM"), _atts(m),
                                            created=_created(m))
                    if not newest or int(m["id"]) > int(newest):
                        newest = m["id"]
                if newest:
                    self.store.set_meta(f"seen:{cid}", newest)
            self.store.set_meta("last_catch_up", time.time())
            applog.write(f"Logger: caught up on {checked} DMs")
        except Exception as e:
            applog.write(f"Logger catch-up problem: {e}")

    def _history_after(self, cid, after_id, max_pages=6):
        """Messages newer than after_id (oldest first), at most max_pages*100."""
        out, after = [], str(after_id)
        for _ in range(max_pages):
            st, page = self.api.request("GET", f"/channels/{cid}/messages?after={after}&limit=100")
            if st != 200 or not isinstance(page, list):
                return None if not out else out
            if not page:
                break
            page.sort(key=lambda m: int(m["id"]))
            out += page
            after = page[-1]["id"]
            if len(page) < 100:
                break
            time.sleep(1.0)
        time.sleep(0.6)
        return out

    # ---------------------------------------------------------------- helpers
    def _guild(self, g):
        if g.get("id"):
            self.guild_names[g["id"]] = (g.get("properties") or {}).get("name") or g.get(
                "name") or self.guild_names.get(g["id"], "server")
            for c in g.get("channels") or []:
                self.channel_names[c["id"]] = c.get("name")
            for c in g.get("threads") or []:
                self.channel_names[c["id"]] = c.get("name")

    def _dm_name(self, ch, users):
        if ch.get("name"):
            return ch["name"]
        rec = ch.get("recipients") or [users.get(i, {"username": "?"})
                                        for i in ch.get("recipient_ids") or []]
        return ", ".join((r.get("global_name") or r.get("username", "?")) for r in rec) or "DM"

    def _save_pictures(self, mid, atts):
        os.makedirs(msglog.files_dir(), exist_ok=True)
        for a in atts:
            if not core.is_image(a) or (a.get("size") or 0) > 8 * 1024 * 1024 or not a.get("url"):
                continue
            name = f"{mid}_{core.safe_name(a.get('filename') or 'picture', 60)}"
            try:
                data = core.download(a["url"], limit=8 * 1024 * 1024)
                with open(os.path.join(msglog.files_dir(), name), "wb") as f:
                    f.write(data)
                a["local"] = name
            except Exception:
                pass

    def run(self, url=gateway.GATEWAY):
        self.store.set_meta("stop", False)
        self.store.set_meta("account", self.account)
        self.store.set_meta("started", time.time())
        self.store.set_meta("pid", os.getpid())
        self.store.set_meta("heartbeat", time.time())
        self.on_status("starting")
        sess = gateway.GatewaySession(self.token, self.on_event, self.should_stop,
                                      log=applog.write, url=url, on_status=self.on_status)
        sess.run_forever()
        self._stopping = True
        if not sess.fatal:
            self.on_status("off")
        self.store.set_meta("heartbeat", 0)


def _atts(m):
    return [{"filename": a.get("filename"), "url": a.get("url"),
             "content_type": a.get("content_type"), "size": a.get("size")}
            for a in m.get("attachments") or []]


def _small_user(u):
    return {"id": u.get("id"), "username": u.get("username"),
            "global_name": u.get("global_name"), "avatar": u.get("avatar")}


def _created(m):
    try:
        return ((int(m["id"]) >> 22) + core.DISCORD_EPOCH_MS) / 1000
    except (KeyError, ValueError):
        return None


def single_instance():
    """Only one logger at a time (Windows named mutex)."""
    if sys.platform != "win32":
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "DiscordMessageCleanerLogger")
    return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS


def arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    """Normal use: started by the app (saved account).
    Another always-on device (old PC, server, Raspberry Pi, Android with Termux):
        python main.py --logger --token-file token.txt [--data folder] [--export file.db]
    --export writes a safe copy every 10 minutes (sync it to your PC with Syncthing etc.)
    then copy deleted_messages.db back and use 'Import from another device' in the app."""
    if arg("--data"):
        os.environ["APPDATA"] = os.path.abspath(arg("--data"))
    applog.write("=== Deleted-messages logger starting ===")
    if not single_instance():
        applog.write("Logger already running.")
        return
    st = settings()
    store = msglog.Store()
    token = None
    account = st.get("logger_account") or st.get("last_account")
    if arg("--token-file") or os.environ.get("DMC_TOKEN"):
        try:
            token = core.clean_token(open(arg("--token-file"), encoding="utf-8").read()
                                     if arg("--token-file") else os.environ["DMC_TOKEN"])
            me, _ = core.get_me(core.DiscordApi(token, applog.write, lambda: False, time.sleep))
            account = (me or {}).get("id")
            if not account:
                print("That token doesn't work (Discord said no).")
                return
            print(f"Logged in as {me.get('username')}. Logger running - press Ctrl+C to stop.")
            print(f"Data: {msglog.db_path()}")
        except OSError as e:
            print(f"Can't read the token file: {e}")
            return
    elif account:
        token = securestore.get_account_token(account)
    if not token:
        store.set_meta("status", "error: no saved account (log in with Remember me)")
        applog.write("Logger: no saved account.")
        return
    try:
        Logger(store, account, token, save_pictures=bool(st.get("logger_pictures")),
               keep_days=int(st.get("logger_days", 7) or 7),
               ghost_pings=st.get("logger_ghost", True) is not False,
               friends=st.get("logger_friends", True) is not False,
               catch_up=st.get("logger_catchup", True) is not False,
               ignore=st.get("logger_ignore") or [],
               export_path=arg("--export")).run()
    except KeyboardInterrupt:
        store.set_meta("heartbeat", 0)
    applog.write("=== Logger stopped ===")


if __name__ == "__main__":
    main()
