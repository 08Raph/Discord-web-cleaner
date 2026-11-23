"""Storage for the deleted-messages logger (SQLite, on this PC only).

- `cache`:   recent messages OTHER people sent you in DMs (kept N days, then removed)
- `deleted`: messages that were deleted after you received them
- `events`:  ghost pings, friends you lost, things that happened while your PC was off
- `meta`:    logger status (pid, heartbeat, stop flag)
"""
import json
import os
import sqlite3
import threading
import time


def data_dir():
    root = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(root, "DiscordMessageCleaner")


def db_path():
    return os.path.join(data_dir(), "deleted_messages.db")


def files_dir():
    return os.path.join(data_dir(), "logger_files")


class Store:
    def __init__(self, path=None):
        self.path = path or db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                id TEXT PRIMARY KEY, account TEXT, channel_id TEXT, channel_name TEXT,
                author_id TEXT, author_name TEXT, author_avatar TEXT, content TEXT,
                attachments TEXT, created REAL, edited TEXT);
            CREATE INDEX IF NOT EXISTS cache_created ON cache(created);
            CREATE TABLE IF NOT EXISTS deleted (
                id TEXT PRIMARY KEY, account TEXT, channel_id TEXT, channel_name TEXT,
                author_id TEXT, author_name TEXT, author_avatar TEXT, content TEXT,
                attachments TEXT, created REAL, deleted_at REAL, edited TEXT);
            CREATE INDEX IF NOT EXISTS deleted_channel ON deleted(account, channel_id);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS edits (
                message_id TEXT, account TEXT, channel_id TEXT, old TEXT, at REAL);
            CREATE INDEX IF NOT EXISTS edits_msg ON edits(message_id);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, kind TEXT, at REAL,
                user_id TEXT, name TEXT, avatar TEXT, info TEXT);
        """)
        for table, col in (("deleted", "note"), ("cache", "note")):
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass                                  # already there
        self.db.commit()

    # ---------------------------------------------------------- meta
    def set_meta(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---------------------------------------------------------- logger side
    COLS = ("id, account, channel_id, channel_name, author_id, author_name, author_avatar, "
            "content, attachments, created, edited, note")

    def remember(self, account, m, channel_name, attachments, note=None, created=None):
        a = m.get("author") or {}
        with self.lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO cache ({self.COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (m["id"], account, m["channel_id"], channel_name, a.get("id"),
                 a.get("global_name") or a.get("username", "?"), a.get("avatar"),
                 m.get("content") or "", json.dumps(attachments), created or time.time(),
                 None, note))
            self.db.commit()

    def cached(self, account, channel_id):
        """{message id: content} of the remembered messages of one chat."""
        with self.lock:
            rows = self.db.execute("SELECT id, content FROM cache WHERE account=? AND "
                                   "channel_id=?", (account, channel_id)).fetchall()
        return dict(rows)

    def cached_channels(self, account):
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT DISTINCT channel_id FROM cache WHERE account=?", (account,))]

    def edited(self, message_id, new_content):
        """Keep the text history when someone edits (so a deleted edit shows everything)."""
        with self.lock:
            row = self.db.execute("SELECT content, edited FROM cache WHERE id=?",
                                  (message_id,)).fetchone()
            if not row or row[0] == new_content:
                return
            history = json.loads(row[1] or "[]") + [row[0]]
            self.db.execute("UPDATE cache SET content=?, edited=? WHERE id=?",
                            (new_content, json.dumps(history[-10:]), message_id))
            meta = self.db.execute("SELECT account, channel_id FROM cache WHERE id=?",
                                   (message_id,)).fetchone()
            self.db.execute("INSERT INTO edits VALUES (?,?,?,?,?)",
                            (message_id, meta[0], meta[1], row[0], time.time()))
            self.db.commit()

    # ---------------------------------------------------------- edit history
    def add_edit(self, account, channel_id, message_id, old):
        """Remember the text a message had before it was edited (seen by the app)."""
        if old is None:
            return
        with self.lock:
            last = self.db.execute("SELECT old FROM edits WHERE message_id=? ORDER BY at DESC "
                                   "LIMIT 1", (message_id,)).fetchone()
            if last and last[0] == old:
                return
            self.db.execute("INSERT INTO edits VALUES (?,?,?,?,?)",
                            (message_id, account, channel_id, old, time.time()))
            self.db.commit()

    def edit_history(self, ids):
        """{message id: [older texts, oldest first]} for the given messages."""
        ids = [i for i in ids if i]
        out = {}
        with self.lock:
            for k in range(0, len(ids), 400):
                part = ids[k:k + 400]
                q = ",".join("?" * len(part))
                for mid, old in self.db.execute(
                        f"SELECT message_id, old FROM edits WHERE message_id IN ({q}) "
                        "ORDER BY at", part):
                    out.setdefault(mid, []).append(old)
        return out

    def search_deleted(self, account, text, limit=500):
        like = f"%{text}%"
        with self.lock:
            rows = self.db.execute(
                "SELECT id, author_id, author_name, author_avatar, content, attachments, "
                "created, deleted_at, edited, note, channel_id, channel_name FROM deleted "
                "WHERE account=? AND (content LIKE ? OR author_name LIKE ? OR edited LIKE ?) "
                "ORDER BY deleted_at DESC LIMIT ?",
                (account, like, like, like, limit)).fetchall()
        return [self._row(r) for r in rows]

    def all_deleted(self, account):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, author_id, author_name, author_avatar, content, attachments, "
                "created, deleted_at, edited, note, channel_id, channel_name FROM deleted "
                "WHERE account=? ORDER BY channel_name, CAST(id AS INTEGER)",
                (account,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r):
        return {"id": r[0], "author_id": r[1], "author_name": r[2], "avatar": r[3],
                "content": r[4], "attachments": json.loads(r[5] or "[]"), "created": r[6],
                "deleted_at": r[7], "edits": json.loads(r[8] or "[]"), "note": r[9],
                "channel_id": r[10], "channel_name": r[11]}

    def snapshot(self, path):
        """Consistent copy of the database (for syncing to another PC)."""
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        dst = sqlite3.connect(tmp)
        with self.lock:
            self.db.backup(dst)
        dst.close()
        os.replace(tmp, path)

    def deleted_now(self, message_id, note=None):
        """Move a cached message to `deleted`. Returns the saved row or None.
        note: e.g. 'offline' when it was deleted while the logger wasn't running."""
        with self.lock:
            row = self.db.execute(f"SELECT {self.COLS} FROM cache WHERE id=?",
                                  (message_id,)).fetchone()
            if not row:
                return None
            self.db.execute(
                f"INSERT OR REPLACE INTO deleted ({self.COLS}, deleted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row[:11] + (note or row[11], time.time()))
            self.db.execute("DELETE FROM cache WHERE id=?", (message_id,))
            self.db.commit()
            return row

    # ---------------------------------------------------------- events
    def add_event(self, account, kind, user=None, info=None):
        user = user or {}
        with self.lock:
            self.db.execute(
                "INSERT INTO events (account, kind, at, user_id, name, avatar, info) "
                "VALUES (?,?,?,?,?,?,?)",
                (account, kind, time.time(), user.get("id"),
                 user.get("global_name") or user.get("username") or user.get("name"),
                 user.get("avatar"), json.dumps(info or {})))
            self.db.commit()

    def events(self, account, limit=300):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, kind, at, user_id, name, avatar, info FROM events WHERE account=? "
                "ORDER BY at DESC LIMIT ?", (account, limit)).fetchall()
        return [{"id": r[0], "kind": r[1], "at": r[2], "user_id": r[3], "name": r[4],
                 "avatar": r[5], "info": json.loads(r[6] or "{}")} for r in rows]

    def event_count(self, account):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE account=?",
                                   (account,)).fetchone()[0]

    def forget_events(self, account, event_id=None):
        with self.lock:
            if event_id:
                self.db.execute("DELETE FROM events WHERE account=? AND id=?",
                                (account, event_id))
            else:
                self.db.execute("DELETE FROM events WHERE account=?", (account,))
            self.db.commit()

    # ---------------------------------------------------------- another device
    def merge_from(self, path):
        """Add the deleted messages + events of a database copied from another device
        (the logger running on an always-on PC, server or Raspberry Pi).
        Returns (messages added, events added)."""
        src = sqlite3.connect(path)
        try:
            cols = [r[1] for r in src.execute("PRAGMA table_info(deleted)")]
            want = [c for c in self.COLS.split(", ") + ["deleted_at"] if c in cols]
            rows = src.execute(f"SELECT {', '.join(want)} FROM deleted").fetchall()
            try:
                evs = src.execute("SELECT account, kind, at, user_id, name, avatar, info "
                                  "FROM events").fetchall()
            except sqlite3.OperationalError:
                evs = []
            try:
                eds = src.execute("SELECT message_id, account, channel_id, old, at "
                                  "FROM edits").fetchall()
            except sqlite3.OperationalError:
                eds = []
        finally:
            src.close()
        added = ev_added = 0
        with self.lock:
            for r in rows:
                cur = self.db.execute(
                    f"INSERT OR IGNORE INTO deleted ({', '.join(want)}) VALUES "
                    f"({', '.join('?' * len(want))})", r)
                added += cur.rowcount
            for e in evs:
                if not self.db.execute("SELECT 1 FROM events WHERE account=? AND kind=? AND "
                                       "at=? AND IFNULL(user_id,'')=IFNULL(?,'')",
                                       (e[0], e[1], e[2], e[3])).fetchone():
                    self.db.execute("INSERT INTO events (account, kind, at, user_id, name, "
                                    "avatar, info) VALUES (?,?,?,?,?,?,?)", e)
                    ev_added += 1
            for e in eds:
                if not self.db.execute("SELECT 1 FROM edits WHERE message_id=? AND at=?",
                                       (e[0], e[4])).fetchone():
                    self.db.execute("INSERT INTO edits VALUES (?,?,?,?,?)", e)
            self.db.commit()
        return added, ev_added

    def prune(self, keep_days):
        """Forget cached (not deleted) messages older than keep_days. Returns removed rows."""
        cutoff = time.time() - keep_days * 86400
        with self.lock:
            rows = self.db.execute("SELECT attachments FROM cache WHERE created < ?",
                                   (cutoff,)).fetchall()
            self.db.execute("DELETE FROM cache WHERE created < ?", (cutoff,))
            self.db.commit()
        for (atts,) in rows:      # remove saved pictures of messages nobody deleted
            for a in json.loads(atts or "[]"):
                if a.get("local"):
                    try:
                        os.remove(os.path.join(files_dir(), os.path.basename(a["local"])))
                    except OSError:
                        pass
        return len(rows)

    # ---------------------------------------------------------- app side
    def chats(self, account):
        with self.lock:
            return self.db.execute(
                "SELECT channel_id, MAX(channel_name), COUNT(*), MAX(deleted_at) FROM deleted "
                "WHERE account=? GROUP BY channel_id ORDER BY MAX(deleted_at) DESC",
                (account,)).fetchall()

    def messages(self, account, channel_id):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, author_id, author_name, author_avatar, content, attachments, "
                "created, deleted_at, edited, note FROM deleted WHERE account=? AND "
                "channel_id=? ORDER BY CAST(id AS INTEGER)", (account, channel_id)).fetchall()
        return [{"id": r[0], "author_id": r[1], "author_name": r[2], "avatar": r[3],
                 "content": r[4], "attachments": json.loads(r[5] or "[]"), "created": r[6],
                 "deleted_at": r[7], "edits": json.loads(r[8] or "[]"), "note": r[9]}
                for r in rows]

    def total(self, account):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM deleted WHERE account=?",
                                   (account,)).fetchone()[0]

    def cached_count(self, account):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM cache WHERE account=?",
                                   (account,)).fetchone()[0]

    def forget(self, account, message_id=None, channel_id=None):
        with self.lock:
            if message_id:
                self.db.execute("DELETE FROM deleted WHERE account=? AND id=?",
                                (account, message_id))
            elif channel_id:
                self.db.execute("DELETE FROM deleted WHERE account=? AND channel_id=?",
                                (account, channel_id))
            else:
                self.db.execute("DELETE FROM deleted WHERE account=?", (account,))
                self.db.execute("DELETE FROM cache WHERE account=?", (account,))
                self.db.execute("DELETE FROM events WHERE account=?", (account,))
            self.db.commit()

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass
