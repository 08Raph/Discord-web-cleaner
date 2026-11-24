"""Core logic for Discord Message Cleaner (no GUI code, so it can be tested on its own).

Talks only to discord.com. The token is never saved or sent anywhere else.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

API = "https://discord.com/api/v9"
LANG = "en"   # set by the app ("en" or "fr"), used for dates
DISCORD_EPOCH_MS = 1420070400000
# Normal user messages that can be deleted: default, reply, slash command, context menu command
DELETABLE_TYPES = {0, 19, 20, 23}


# ----------------------------------------------------------------- helpers
def clean_token(raw):
    """Strip things people often paste by accident ("authorization:", quotes, spaces)."""
    t = (raw or "").strip()
    if t.lower().startswith("authorization"):
        t = t.split(":", 1)[-1] if ":" in t else t[len("authorization"):]
    return t.strip().strip('"').strip("'").strip()


def token_problem(token):
    """Return a friendly message if the token is obviously wrong, else None."""
    if not token:
        return "Please paste your token."
    if token.startswith("eyJ"):
        return ("That looks like the 'x-super-properties' header, not your token.\n"
                "Copy the value next to 'authorization' instead.")
    if token.count(".") < 2 or len(token) < 50:
        return ("That doesn't look like a Discord token.\n"
                "It should be around 70 characters with two dots in it.")
    return None


def parse_channel_id(text):
    """Accept a raw ID or a discord.com/channels/<guild or @me>/<channel> URL."""
    text = (text or "").strip()
    m = re.search(r"channels/(?:@me|\d+)/(\d+)", text)
    if m:
        return m.group(1)
    return text if text.isdigit() else None


def parse_channel_list(text):
    """Many links/IDs separated by new lines, commas or spaces.
    Returns (unique_ids_in_order, list_of_bad_entries)."""
    ids, bad = [], []
    for part in re.split(r"[\s,;]+", text or ""):
        if not part:
            continue
        cid = parse_channel_id(part)
        if cid is None:
            bad.append(part)
        elif cid not in ids:
            ids.append(cid)
    return ids, bad


def channel_display_name(ch):
    """Readable name for a channel object: '#general' or 'DM with bob'."""
    if not ch:
        return ""
    if ch.get("name"):
        return ("#" + ch["name"]) if ch.get("guild_id") else ch["name"]
    people = [r.get("global_name") or r.get("username", "?") for r in ch.get("recipients", [])]
    return ("DM with " + ", ".join(people)) if people else ""


def parse_date(text):
    """'YYYY-MM-DD' -> datetime (local time) or None. Raises ValueError if invalid."""
    text = (text or "").strip()
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d")


def date_to_snowflake(dt):
    return max(0, (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22)


def parse_keywords(text):
    return [k.strip() for k in (text or "").split(",") if k.strip()]


# ----------------------------------------------------------------- settings
@dataclass
class Options:
    channel_id: str
    delay: float = 1.5
    dry_run: bool = True
    max_delete: int = 0              # 0 = no limit
    keywords: list = field(default_factory=list)
    case_sensitive: bool = False
    whole_word: bool = True
    match_mode: str = None           # "whole" | "starts" | "anywhere" (None = from whole_word)
    from_date: datetime = None       # inclusive, start of day
    to_date: datetime = None         # inclusive, end of day
    skip_pinned: bool = True
    backup_dir: str = None           # folder to save deleted messages in, or None
    use_search: bool = True          # use Discord search to find your messages (faster)
    keep_ids: set = field(default_factory=set)   # message ids never to delete
    pacer: object = None             # shared Pacer (auto speed)
    remove_reactions: bool = False   # also remove YOUR reactions on everyone's messages
    has_files: bool = False          # only messages with attachments
    has_images: bool = False         # only messages with pictures
    has_links: bool = False          # only messages with links
    only_replies: bool = False       # only replies
    min_len: int = 0                 # only messages with at least N characters (0 = off)
    max_len: int = 0                 # only messages with at most N characters (0 = off)
    older_than_days: int = 0         # only messages older than N days (0 = off)


MATCH_MODES = {
    "whole": "Whole word",       # hello -> "hello", "hello!"         (not "hellos")
    "starts": "Starts with",     # hello -> "hello", "hellos", "helloooo" (not "othello")
    "anywhere": "Anywhere",      # hello -> also "othello", "xhellox"
}


def match_mode(opts):
    if opts.match_mode in MATCH_MODES:
        return opts.match_mode
    return "whole" if opts.whole_word else "anywhere"


def keyword_pattern(keyword, mode, flags=re.IGNORECASE):
    p = re.escape(keyword)
    if mode == "whole":
        p = r"(?<!\w)" + p + r"(?!\w)"
    elif mode == "starts":
        p = r"(?<!\w)" + p
    return re.compile(p, flags)


LINK_RE = re.compile(r"https?://\S+", re.I)


class MessageFilter:
    def __init__(self, opts):
        self.opts = opts
        flags = 0 if opts.case_sensitive else re.IGNORECASE
        self.patterns = [keyword_pattern(k, match_mode(opts), flags) for k in opts.keywords]
        self.after_sf = date_to_snowflake(opts.from_date) if opts.from_date else None
        self.before_sf = (date_to_snowflake(opts.to_date + timedelta(days=1))
                          if opts.to_date else None)
        if opts.older_than_days:
            cut = date_to_snowflake(datetime.now() - timedelta(days=int(opts.older_than_days)))
            self.before_sf = min(self.before_sf, cut) if self.before_sf else cut

    def matches(self, msg, my_id):
        if msg.get("author", {}).get("id") != my_id:
            return False
        if msg.get("type", 0) not in DELETABLE_TYPES:
            return False
        if self.opts.skip_pinned and msg.get("pinned"):
            return False
        if msg.get("id") in self.opts.keep_ids:
            return False
        o = self.opts
        content = msg.get("content") or ""
        if o.min_len and len(content.strip()) < o.min_len:
            return False
        if o.max_len and len(content.strip()) > o.max_len:
            return False
        atts = msg.get("attachments") or []
        if isinstance(atts, str):   # data package format: space separated urls
            atts = [{"url": u, "filename": u.split("?")[0].rsplit("/", 1)[-1]}
                    for u in atts.split()]
        if o.has_files and not atts:
            return False
        if o.has_images and not any(is_image(a) for a in atts):
            return False
        if o.has_links and not (LINK_RE.search(content) or msg.get("embeds")):
            return False
        if o.only_replies and not (msg.get("type") == 19 or msg.get("message_reference")):
            return False
        if self.patterns:
            content = msg.get("content") or ""
            if not any(p.search(content) for p in self.patterns):
                return False
        return True

    def in_dates(self, msg):
        mid = int(msg["id"])
        if self.after_sf and mid < self.after_sf:
            return False
        if self.before_sf and mid >= self.before_sf:
            return False
        return True


class Pacer:
    """Delay between deletes. With auto on, it slows down when Discord rate-limits us
    and slowly speeds back up (never below the delay you chose)."""

    def __init__(self, base, auto=True, max_delay=30.0):
        import threading
        self.base = max(0.5, float(base))
        self.auto = auto
        self.max = max_delay
        self.delay = self.base
        self.ok_streak = 0
        self.lock = threading.Lock()
        self.jitter = 0.0          # safe mode: random extra wait (fraction of the delay)
        self.on_delete = None      # safe mode: counts deletes for the daily limit

    def on_rate_limit(self, retry_after):
        if not self.auto:
            return
        with self.lock:
            self.delay = min(self.max, max(self.delay * 1.5, self.delay + 0.5,
                                           float(retry_after or 0) / 2))
            self.ok_streak = 0

    def after_delete(self, sleep):
        with self.lock:
            self.ok_streak += 1
            if self.auto and self.ok_streak >= 15 and self.delay > self.base:
                self.delay = max(self.base, self.delay * 0.85)
                self.ok_streak = 0
            d = self.delay
        if self.on_delete:
            self.on_delete()
        if self.jitter:
            import random
            d *= 1 + random.uniform(0, self.jitter)
        sleep(d)


# ----------------------------------------------------------------- API
class DiscordApi:
    def __init__(self, token, log, should_stop, sleep, pacer=None, paused=None):
        self.token = token
        self.log = log
        self.should_stop = should_stop
        self.sleep = sleep
        self.pacer = pacer
        self.paused = paused or (lambda: False)

    def request(self, method, path, body=None, files=None):
        """Return (status, data). Waits and retries on rate limits / network errors.
        body: JSON to send. files: [(filename, bytes)] sent as a normal upload.
        Returns (None, None) if stopped."""
        while not self.should_stop():
            if self.paused():
                self.sleep(0.5)
                continue
            headers = {"Authorization": self.token,
                       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            data = None
            if files:
                data, ctype = multipart(body or {}, files)
                headers["Content-Type"] = ctype
            elif body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(API + path, data=data, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60 if files else 30) as r:
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read() or b"null")
                except Exception:
                    data = None
                if e.code == 429:
                    wait = float((data or {}).get("retry_after", 5)) + 0.5
                    if self.pacer:
                        self.pacer.on_rate_limit(wait)
                    self.log(f"Discord asked us to slow down, waiting {wait:.1f}s...")
                    self.sleep(wait)
                    continue
                return e.code, data
            except Exception as e:
                self.log(f"Network problem: {e}. Retrying in 5s...")
                self.sleep(5)
        return None, None


def multipart(payload, files):
    """Build a multipart/form-data body (payload_json + files[n]) for uploads."""
    import uuid
    boundary = "----dmc" + uuid.uuid4().hex
    parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
              f'Content-Type: application/json\r\n\r\n').encode() + json.dumps(payload).encode()
             + b"\r\n"]
    for i, (name, blob) in enumerate(files):
        safe = name.replace('"', "'")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; '
                      f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                      ).encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------- pictures
CDN = "https://cdn.discordapp.com"


def user_avatar_url(user, size=64):
    if user.get("avatar"):
        return f"{CDN}/avatars/{user['id']}/{user['avatar']}.png?size={size}"
    disc = user.get("discriminator") or "0"
    try:
        idx = (int(user["id"]) >> 22) % 6 if disc == "0" else int(disc) % 5
    except (KeyError, ValueError):
        idx = 0
    return f"{CDN}/embed/avatars/{idx}.png"


def guild_icon_url(guild, size=96):
    if guild.get("icon"):
        return f"{CDN}/icons/{guild['id']}/{guild['icon']}.png?size={size}"
    return None


def initials(name):
    """'My Cool Server' -> 'MCS' (like Discord does for servers without an icon)."""
    words = re.findall(r"\w+", name or "") or ["?"]
    return "".join(w[0] for w in words)[:3]


def fetch_image(url, timeout=15):
    """Download a picture from Discord's CDN (no token is sent)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def circle_png_b64(data, size):
    """Turn image bytes into a round PNG (base64), like Discord avatars.
    Falls back to the original picture if Pillow isn't installed."""
    import base64
    import io
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return base64.b64encode(data).decode()
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img = ImageOps.fit(img, (size, size), Image.LANCZOS)
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ----------------------------------------------------------------- messages preview
def fetch_messages(api, channel_id, before=None, limit=50):
    """Latest messages of a chat (newest first). Returns (status, list)."""
    path = f"/channels/{channel_id}/messages?limit={limit}"
    if before:
        path += f"&before={before}"
    st, data = api.request("GET", path)
    return st, (data if st == 200 else [])


def get_channel(api, channel_id):
    st, ch = api.request("GET", f"/channels/{channel_id}")
    return ch if st == 200 else None


def render_content(msg):
    """Make message text readable: mentions, custom emojis."""
    text = msg.get("content") or ""
    names = {u["id"]: (u.get("global_name") or u.get("username", "user"))
             for u in msg.get("mentions", [])}
    text = re.sub(r"<@!?(\d+)>", lambda m: "@" + names.get(m.group(1), "user"), text)
    text = re.sub(r"<@&\d+>", "@role", text)
    text = re.sub(r"<#\d+>", "#channel", text)
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    text = re.sub(r"<t:(\d+)(?::\w)?>",
                  lambda m: datetime.fromtimestamp(int(m.group(1))).strftime("%Y-%m-%d %H:%M"),
                  text)
    return text


CLOCK_24 = False      # English times as 20:15 instead of 8:15 PM (setting "clock_24h")

# Discord's system messages (calls, pins, people added…) as readable lines, like Discord.
_SYS = {
    "en": {1: "{a} added {t} to the group.", 2: "{a} removed {t} from the group.",
           "left": "{a} left the group.", "call": "{a} started a call.",
           "call_len": "{a} started a call that lasted {n}.", "missed": "You missed a call from {a}.",
           "missed_len": "You missed a call from {a} that lasted {n}.",
           4: "{a} changed the channel name: {c}", 5: "{a} changed the channel icon.",
           6: "{a} pinned a message to this channel.", 7: "{a} joined the server.",
           8: "{a} just boosted the server!", 9: "{a} just boosted the server! It reached Level 1!",
           10: "{a} just boosted the server! It reached Level 2!",
           11: "{a} just boosted the server! It reached Level 3!",
           12: "{a} added {c} to this channel.", 18: "{a} started a thread: {c}",
           21: "Thread started from this message.", 24: "AutoMod blocked or flagged a message.",
           46: "The poll has ended.", "other": "{a} did something in this chat.",
           "min": "{n} minutes", "1min": "a minute", "hour": "{n} hours", "1hour": "an hour",
           "sec": "a few seconds"},
    "fr": {1: "{a} a ajouté {t} au groupe.", 2: "{a} a retiré {t} du groupe.",
           "left": "{a} a quitté le groupe.", "call": "{a} a lancé un appel.",
           "call_len": "{a} a lancé un appel qui a duré {n}.", "missed": "Vous avez manqué un appel de {a}.",
           "missed_len": "Vous avez manqué un appel de {a} qui a duré {n}.",
           4: "{a} a changé le nom du salon : {c}", 5: "{a} a changé l'icône du salon.",
           6: "{a} a épinglé un message dans ce salon.", 7: "{a} a rejoint le serveur.",
           8: "{a} vient de booster le serveur !", 9: "{a} a boosté le serveur ! Niveau 1 atteint !",
           10: "{a} a boosté le serveur ! Niveau 2 atteint !", 11: "{a} a boosté le serveur ! Niveau 3 atteint !",
           12: "{a} a ajouté {c} à ce salon.", 18: "{a} a créé un fil : {c}",
           21: "Fil créé à partir de ce message.", 24: "AutoMod a bloqué ou signalé un message.",
           46: "Le sondage est terminé.", "other": "{a} a fait quelque chose dans ce chat.",
           "min": "{n} minutes", "1min": "une minute", "hour": "{n} heures", "1hour": "une heure",
           "sec": "quelques secondes"},
    "es": {1: "{a} añadió a {t} al grupo.", 2: "{a} quitó a {t} del grupo.",
           "left": "{a} salió del grupo.", "call": "{a} inició una llamada.",
           "call_len": "{a} inició una llamada que duró {n}.", "missed": "Tienes una llamada perdida de {a}.",
           "missed_len": "Tienes una llamada perdida de {a} que duró {n}.",
           4: "{a} cambió el nombre del canal: {c}", 5: "{a} cambió el icono del canal.",
           6: "{a} fijó un mensaje en este canal.", 7: "{a} se unió al servidor.",
           8: "¡{a} mejoró el servidor!", 9: "¡{a} mejoró el servidor! ¡Nivel 1!",
           10: "¡{a} mejoró el servidor! ¡Nivel 2!", 11: "¡{a} mejoró el servidor! ¡Nivel 3!",
           12: "{a} añadió {c} a este canal.", 18: "{a} empezó un hilo: {c}",
           21: "Hilo creado a partir de este mensaje.", 24: "AutoMod bloqueó o marcó un mensaje.",
           46: "La encuesta terminó.", "other": "{a} hizo algo en este chat.",
           "min": "{n} minutos", "1min": "un minuto", "hour": "{n} horas", "1hour": "una hora",
           "sec": "unos segundos"},
    "pt": {1: "{a} adicionou {t} ao grupo.", 2: "{a} removeu {t} do grupo.",
           "left": "{a} saiu do grupo.", "call": "{a} iniciou uma chamada.",
           "call_len": "{a} iniciou uma chamada que durou {n}.", "missed": "Você perdeu uma chamada de {a}.",
           "missed_len": "Você perdeu uma chamada de {a} que durou {n}.",
           4: "{a} mudou o nome do canal: {c}", 5: "{a} mudou o ícone do canal.",
           6: "{a} fixou uma mensagem neste canal.", 7: "{a} entrou no servidor.",
           8: "{a} impulsionou o servidor!", 9: "{a} impulsionou o servidor! Nível 1!",
           10: "{a} impulsionou o servidor! Nível 2!", 11: "{a} impulsionou o servidor! Nível 3!",
           12: "{a} adicionou {c} a este canal.", 18: "{a} começou um tópico: {c}",
           21: "Tópico criado a partir desta mensagem.", 24: "O AutoMod bloqueou ou sinalizou uma mensagem.",
           46: "A enquete terminou.", "other": "{a} fez algo neste chat.",
           "min": "{n} minutos", "1min": "um minuto", "hour": "{n} horas", "1hour": "uma hora",
           "sec": "alguns segundos"},
    "de": {1: "{a} hat {t} zur Gruppe hinzugefügt.", 2: "{a} hat {t} aus der Gruppe entfernt.",
           "left": "{a} hat die Gruppe verlassen.", "call": "{a} hat einen Anruf gestartet.",
           "call_len": "{a} hat einen Anruf gestartet, der {n} dauerte.", "missed": "Verpasster Anruf von {a}.",
           "missed_len": "Verpasster Anruf von {a} ({n}).",
           4: "{a} hat den Kanalnamen geändert: {c}", 5: "{a} hat das Kanalsymbol geändert.",
           6: "{a} hat eine Nachricht in diesem Kanal angepinnt.", 7: "{a} ist dem Server beigetreten.",
           8: "{a} hat den Server geboostet!", 9: "{a} hat den Server geboostet! Level 1!",
           10: "{a} hat den Server geboostet! Level 2!", 11: "{a} hat den Server geboostet! Level 3!",
           12: "{a} hat {c} zu diesem Kanal hinzugefügt.", 18: "{a} hat einen Thread gestartet: {c}",
           21: "Thread aus dieser Nachricht gestartet.", 24: "AutoMod hat eine Nachricht blockiert oder markiert.",
           46: "Die Umfrage ist beendet.", "other": "{a} hat etwas in diesem Chat gemacht.",
           "min": "{n} Minuten", "1min": "eine Minute", "hour": "{n} Stunden", "1hour": "eine Stunde",
           "sec": "ein paar Sekunden"},
}
SYS_ICONS = {1: "➜", 2: "←", 3: "📞", 4: "✏", 5: "✏", 6: "📌", 7: "➜", 8: "🚀", 9: "🚀", 10: "🚀",
             11: "🚀", 12: "➜", 18: "🧵", 21: "🧵", 24: "🛡", 46: "📊"}


def is_system(m):
    return m.get("type") not in DELETABLE_TYPES and m.get("type") is not None


def system_text(m, my_id=None, lang=None):
    """(icon, text) for a Discord system message, e.g. ('📞', 'Alex started a call that
    lasted 5 minutes.'). Works for DMs and servers."""
    T = _SYS.get(lang or LANG, _SYS["en"])
    t = m.get("type")
    au = m.get("author") or {}
    a = au.get("global_name") or au.get("username") or "?"
    ment = m.get("mentions") or []
    target = (ment[0].get("global_name") or ment[0].get("username")) if ment else "?"
    c = m.get("content") or ""
    if t == 3:
        call = m.get("call") or {}
        n = None
        if call.get("ended_timestamp"):
            try:
                t0 = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(call["ended_timestamp"].replace("Z", "+00:00"))
                secs = max(0, (t1 - t0).total_seconds())
                n = (T["sec"] if secs < 60 else T["1min"] if secs < 120 else
                     T["min"].format(n=int(secs // 60)) if secs < 3600 else
                     T["1hour"] if secs < 7200 else T["hour"].format(n=int(secs // 3600)))
            except (KeyError, ValueError):
                n = None
        missed = my_id and au.get("id") != my_id and my_id not in (call.get("participants") or [])
        key = ("missed" if missed else "call") + ("_len" if n else "")
        return "📞", T[key].format(a=a, n=n)
    if t == 2 and ment and ment[0].get("id") == au.get("id"):
        return "←", T["left"].format(a=a)
    tpl = T.get(t, T["other"])
    return SYS_ICONS.get(t, "•"), tpl.format(a=a, t=target, c=c)


def format_time(iso):
    """Discord-like time: 'Today at 3:14 PM' or '11/21/2026 3:14 PM'."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return ""
    now = datetime.now().astimezone()
    words = {"fr": ("Aujourd'hui à", "Hier à"), "es": ("Hoy a las", "Ayer a las"),
             "pt": ("Hoje às", "Ontem às"), "de": ("Heute um", "Gestern um")}.get(LANG)
    if words:
        clock = dt.strftime("%H:%M")
        if dt.date() == now.date():
            return f"{words[0]} {clock}"
        if (now.date() - dt.date()).days == 1:
            return f"{words[1]} {clock}"
        sep = "." if LANG == "de" else "/"
        return f"{dt.day:02d}{sep}{dt.month:02d}{sep}{dt.year} {clock}"
    clock = dt.strftime("%H:%M") if CLOCK_24 else dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == now.date():
        return f"Today at {clock}"
    if (now.date() - dt.date()).days == 1:
        return f"Yesterday at {clock}"
    return f"{dt.month}/{dt.day}/{dt.year} {clock}"


# ----------------------------------------------------------------- listing
TEXT_CHANNEL_TYPES = {0, 5}   # text, announcement
FORUM_TYPES = {15, 16}         # forum, media (only posts/threads inside)
CATEGORY_TYPE = 4


def get_me(api):
    st, me = api.request("GET", "/users/@me")
    return (me if st == 200 else None), st


def list_dms(api):
    """All open DMs and group DMs, most recent first.
    Returns list of {id, name, kind:'dm'|'group', avatar, user_id} or raises RuntimeError."""
    st, chans = api.request("GET", "/users/@me/channels")
    if st != 200:
        raise RuntimeError(friendly_error(st, "load your DMs"))
    out = []
    for c in chans or []:
        if c.get("type") not in (1, 3):
            continue
        recips = c.get("recipients", [])
        name = c.get("name") or ", ".join(
            r.get("global_name") or r.get("username", "?") for r in recips)
        if c.get("type") == 1 and recips:
            avatar = user_avatar_url(recips[0])
        elif c.get("icon"):
            avatar = f"{CDN}/channel-icons/{c['id']}/{c['icon']}.png?size=64"
        else:
            avatar = None
        out.append({"id": c["id"], "name": name or "Unknown",
                    "kind": "group" if c.get("type") == 3 else "dm",
                    "avatar": avatar,
                    "user_id": (recips[0].get("id") if recips else None) or c["id"],
                    "recipients": [{"id": r.get("id"), "username": r.get("username"),
                                    "global_name": r.get("global_name"),
                                    "avatar": r.get("avatar")} for r in recips],
                    "last": int(c.get("last_message_id") or 0)})
    out.sort(key=lambda c: -c["last"])
    return out


def list_guilds(api):
    st, guilds = api.request("GET", "/users/@me/guilds")
    if st != 200:
        raise RuntimeError(friendly_error(st, "load your servers"))
    return [{"id": g["id"], "name": g.get("name", "Server"), "icon": guild_icon_url(g),
             "owner": bool(g.get("owner"))} for g in guilds or []]


def list_guild_roles(api, guild_id):
    """{role_id: (name, colour)} for a server; empty on any error."""
    try:
        st, roles = api.request("GET", f"/guilds/{guild_id}/roles")
    except Exception:
        return {}
    if st != 200 or not isinstance(roles, list):
        return {}
    return {r["id"]: (r.get("name", "role"), r.get("color", 0)) for r in roles if "id" in r}


def message_link(m, guild_id=None):
    """https://discord.com/channels/<guild or @me>/<channel>/<message>"""
    return f"https://discord.com/channels/{guild_id or m.get('guild_id') or '@me'}/{m.get('channel_id')}/{m['id']}"


def list_guild_channels(api, guild_id):
    """Text channels of a server, ordered like in Discord, with their category name."""
    st, chans = api.request("GET", f"/guilds/{guild_id}/channels")
    if st != 200:
        raise RuntimeError(friendly_error(st, "load this server's channels"))
    chans = chans or []
    cats = {c["id"]: c for c in chans if c.get("type") == CATEGORY_TYPE}

    def order(c):
        cat = cats.get(c.get("parent_id"))
        return ((cat or {}).get("position", -1), c.get("position", 0))

    out = []
    for c in sorted((c for c in chans if c.get("type") in TEXT_CHANNEL_TYPES | FORUM_TYPES),
                    key=order):
        cat = cats.get(c.get("parent_id"))
        out.append({"id": c["id"], "name": c.get("name", "channel"),
                    "category": (cat or {}).get("name", ""),
                    "forum": c.get("type") in FORUM_TYPES})
    return out


# ----------------------------------------------------------------- main job
def friendly_error(status, what):
    hints = {
        401: "Your token is wrong or expired. Copy it again from the browser.",
        403: "You don't have access to that.",
        404: "Not found. Use the Channel ID (not the User ID) or paste the chat link.",
    }
    return f"Could not {what} (HTTP {status}). " + hints.get(status, "")


def _new_stats(name):
    return {"scanned": 0, "matched": 0, "deleted": 0, "failed": 0, "total": 0,
            "user": "", "chat": name, "cursor": None, "reactions": 0}


def _describe(opts, log):
    if opts.keywords:
        log(f"Keywords: {', '.join(opts.keywords)}"
            f" ({'caps matter' if opts.case_sensitive else 'caps ignored'},"
            f" {MATCH_MODES[match_mode(opts)].lower()})")
    if opts.max_delete:
        log(f"Limit: {opts.max_delete} messages")
    extra = [t for flag, t in ((opts.has_files, "with files"), (opts.has_images, "with pictures"),
                               (opts.has_links, "with links"), (opts.only_replies, "replies"))
             if flag]
    if opts.min_len or opts.max_len:
        extra.append(f"length {opts.min_len or 0}-{opts.max_len or '∞'} characters")
    if opts.older_than_days:
        extra.append(f"older than {opts.older_than_days} days")
    if extra:
        log("Only messages: " + ", ".join(extra))
    log("Preview: nothing will be deleted." if opts.dry_run else "Deleting messages...")


def _preview(m):
    return ((m.get("content") or "[attachment/embed]").replace("\n", " ")[:60],
            (m.get("timestamp") or "")[:10])


def _pause_after_delete(opts, sleep):
    if opts.pacer:
        opts.pacer.after_delete(sleep)
    else:
        sleep(opts.delay)


def delete_message(api, channel_id, message_id):
    """Delete one message. Returns the HTTP status (None if stopped)."""
    st, _ = api.request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
    return st


def _delete_one(api, m, stats, backup, log):
    """Delete one message and update stats. Returns the HTTP status (None if stopped)."""
    preview, when = _preview(m)
    st = delete_message(api, m["channel_id"], m["id"])
    if st in (200, 204):
        stats["deleted"] += 1
        backup.append({"id": m["id"], "channel_id": m.get("channel_id"),
                       "timestamp": m.get("timestamp"), "content": m.get("content"),
                       "attachments": [a.get("url") for a in m.get("attachments", [])
                                       if isinstance(a, dict)]})
        log(f"Deleted #{stats['deleted']}  {when}  {preview}")
    elif st == 404:
        stats["deleted"] += 1   # already gone: counts as done
        log(f"Already gone  {when}  {preview}")
    elif st is not None:
        stats["failed"] += 1
        log(f"Failed (HTTP {st})  {preview}")
    return st


def _save_backup(opts, target_id, stats, backup, log):
    if not (backup and opts.backup_dir):
        return
    try:
        os.makedirs(opts.backup_dir, exist_ok=True)
        safe = re.sub(r"[^\w\- ]", "", stats["chat"] or "")[:40].strip() or target_id
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(opts.backup_dir, f"deleted_{stamp}_{safe}_{target_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"target": stats["chat"], "id": target_id, "messages": backup},
                      f, ensure_ascii=False, indent=2)
        log(f"Backup saved: {path}")
    except OSError as e:
        log(f"Could not save backup: {e}")


def _finish(opts, stats, log, progress, should_stop):
    if should_stop():
        log("Stopped.")
    verb = "would be deleted" if opts.dry_run else "deleted"
    count = stats["matched"] if opts.dry_run else stats["deleted"]
    log(f"Done. Checked {stats['scanned']} messages, {count} {verb}"
        + (f", {stats['failed']} failed" if stats["failed"] else "")
        + (f", {stats['reactions']} reactions" if stats.get("reactions") else "") + ".")
    progress(stats)
    return stats


def _login(api, stats, log):
    me, status = get_me(api)
    if not me:
        log(friendly_error(status, "log in"))
        return None
    stats["user"] = me.get("global_name") or me.get("username", "")
    return me["id"]


def _limit_reached(opts, stats, log):
    if opts.max_delete and stats["matched"] >= opts.max_delete:
        log(f"Reached the limit of {opts.max_delete} messages.")
        return True
    return False


def run_cleaner(api, opts, log, progress, should_stop, sleep, name=None, start_before=None):
    """Clean ONE chat by reading its history, newest first.
    start_before: resume from this message id (older messages only)."""
    stats = _new_stats(name or opts.channel_id)
    backup = []
    my_id = _login(api, stats, log)
    if not my_id:
        return stats
    if not name:
        ch = get_channel(api, opts.channel_id)
        if not ch:
            log("Could not open this chat. " + friendly_error(404, "open it"))
            return stats
        stats["chat"] = channel_display_name(ch) or opts.channel_id
    log(f"Started: {stats['chat']}" + (" (resuming)" if start_before else ""))
    progress(stats)

    flt = MessageFilter(opts)
    _describe(opts, log)
    before = start_before or (str(flt.before_sf) if flt.before_sf else None)
    finished = False
    try:
        while not finished and not should_stop():
            st, msgs = fetch_messages(api, opts.channel_id, before, 100)
            if st is None:
                break
            if st != 200:
                log(friendly_error(st, "read messages"))
                break
            if not msgs:
                break
            for m in msgs:
                if should_stop():
                    finished = True
                    break
                if flt.after_sf and int(m["id"]) < flt.after_sf:
                    finished = True  # older than the "from" date: nothing more to do
                    break
                stats["scanned"] += 1
                m.setdefault("channel_id", opts.channel_id)
                will_delete = flt.matches(m, my_id)
                if opts.remove_reactions and not will_delete and flt.in_dates(m):
                    if _remove_reactions(api, m, stats, log, opts, sleep) is None:
                        finished = True
                        break
                if will_delete:
                    stats["matched"] += 1
                    if opts.dry_run:
                        preview, when = _preview(m)
                        log(f"[preview] {when}  {preview}")
                    else:
                        if _delete_one(api, m, stats, backup, log) is None:
                            finished = True
                            break
                        _pause_after_delete(opts, sleep)
                    if _limit_reached(opts, stats, log):
                        finished = True
                stats["cursor"] = m["id"]
                progress(stats)
                if finished:
                    break
            before = msgs[-1]["id"]
            progress(stats)
    finally:
        _save_backup(opts, opts.channel_id, stats, backup, log)
    return _finish(opts, stats, log, progress, should_stop)


def emoji_param(emoji):
    """Reaction emoji for the URL: unicode emoji or name:id for custom ones."""
    from urllib.parse import quote
    if emoji.get("id"):
        return quote(f"{emoji.get('name') or 'emoji'}:{emoji['id']}")
    return quote(emoji.get("name") or "")


def _remove_reactions(api, m, stats, log, opts, sleep):
    """Remove YOUR reactions from one message. Returns False/True, None if stopped."""
    done = False
    for r in m.get("reactions") or []:
        burst = bool(r.get("me_burst"))
        if not (r.get("me") or burst):
            continue
        em = r.get("emoji") or {}
        label = f":{em.get('name')}:" if em.get("id") else em.get("name", "?")
        if opts.dry_run:
            stats["reactions"] += 1
            log(f"[preview] would remove your reaction {label}")
            continue
        path = (f"/channels/{m['channel_id']}/messages/{m['id']}/reactions/"
                f"{emoji_param(em)}/@me" + ("?type=1" if burst and not r.get("me") else ""))
        st, _ = api.request("DELETE", path)
        if st is None:
            return None
        if st in (200, 204, 404):
            stats["reactions"] += 1
            log(f"Removed your reaction {label}")
            done = True
        else:
            log(f"Could not remove reaction {label} (HTTP {st})")
        _pause_after_delete(opts, sleep)
    return done


def run_reaction_scan(api, opts, channel_id, log, progress, should_stop, sleep, stats,
                      my_id):
    """Read a channel's history only to remove your reactions (used for whole servers)."""
    flt = MessageFilter(opts)
    before = str(flt.before_sf) if flt.before_sf else None
    while not should_stop():
        st, msgs = fetch_messages(api, channel_id, before, 100)
        if st != 200 or not msgs:
            return
        for m in msgs:
            if should_stop():
                return
            if flt.after_sf and int(m["id"]) < flt.after_sf:
                return
            m.setdefault("channel_id", channel_id)
            if flt.in_dates(m) and _remove_reactions(api, m, stats, log, opts, sleep) is None:
                return
        progress(stats)
        before = msgs[-1]["id"]


def _hit(group):
    """Search results come as lists; pick the actual hit."""
    if isinstance(group, dict):
        return group
    for m in group:
        if m.get("hit"):
            return m
    return group[0] if group else None


def search_extra(opts):
    """Extra search parameters to make Discord return fewer, better-matching results."""
    has = [h for flag, h in ((opts.has_files, "file"), (opts.has_images, "image"),
                             (opts.has_links, "link")) if flag]
    return f"&has={has[0]}" if len(has) == 1 else ""


def search_base(kind, target_id, my_id, guild_id=None):
    """Search URL for your messages in a server, a server channel, or a DM."""
    if kind == "server":
        base = f"/guilds/{target_id}/messages/search?author_id={my_id}"
    elif guild_id:
        base = f"/guilds/{guild_id}/messages/search?author_id={my_id}&channel_id={target_id}"
    else:
        base = f"/channels/{target_id}/messages/search?author_id={my_id}"
    return base + "&include_nsfw=true&sort_by=timestamp&sort_order=desc"


def search_count(api, kind, target_id, my_id, guild_id=None):
    """How many of your messages Discord search finds (or None)."""
    for _ in range(3):
        st, data = api.request("GET", search_base(kind, target_id, my_id, guild_id) + "&offset=0")
        if st == 202:
            time.sleep(float((data or {}).get("retry_after", 2)) + 0.5)
            continue
        if st == 200:
            return int(data.get("total_results") or 0)
        return None
    return None


def run_search(api, opts, base_path, target_id, log, progress, should_stop, sleep, name,
               my_id=None, stats=None, fallback_ok=False):
    """Clean using Discord search. Returns stats, or None if search isn't available
    and fallback_ok is True (the caller then reads the history instead)."""
    stats = stats or _new_stats(name or target_id)
    backup = []
    if my_id is None:
        my_id = _login(api, stats, log)
        if not my_id:
            return stats
    flt = MessageFilter(opts)
    base = base_path + search_extra(opts)
    if flt.after_sf:
        base += f"&min_id={flt.after_sf}"
    if flt.before_sf:
        base += f"&max_id={flt.before_sf}"

    seen = set()
    finished = False
    first = True
    try:
        while not finished and not should_stop():       # one "pass" over the results
            offset = 0
            acted_this_pass = 0
            while not finished and not should_stop():
                st, data = api.request("GET", f"{base}&offset={offset}")
                if st == 202:  # Discord is still indexing
                    wait = float((data or {}).get("retry_after", 5)) + 1
                    log(f"Discord is indexing this chat, waiting {wait:.0f}s...")
                    sleep(wait)
                    continue
                if st is None:
                    finished = True
                    break
                if st != 200:
                    if first and fallback_ok:
                        return None
                    log(friendly_error(st, "search here"))
                    finished = True
                    break
                if first:
                    first = False
                    log(f"Started: {stats['chat']}")
                    _describe(opts, log)
                if not stats["total"]:
                    stats["total"] = int(data.get("total_results") or 0)
                    log(f"Discord found about {stats['total']} of your messages here.")
                    progress(stats)
                hits = [h for h in (_hit(g) for g in data.get("messages") or []) if h]
                if not hits:
                    break
                for m in hits:
                    if should_stop():
                        finished = True
                        break
                    if m["id"] in seen:
                        offset += 1          # still in results (index lag) or skipped
                        continue
                    seen.add(m["id"])
                    stats["scanned"] += 1
                    if not flt.matches(m, my_id):
                        offset += 1
                        continue
                    stats["matched"] += 1
                    acted_this_pass += 1
                    if opts.dry_run:
                        preview, when = _preview(m)
                        log(f"[preview] {when}  {preview}")
                        offset += 1
                    else:
                        if _delete_one(api, m, stats, backup, log) is None:
                            finished = True
                            break
                        _pause_after_delete(opts, sleep)
                    progress(stats)
                    if _limit_reached(opts, stats, log):
                        finished = True
                        break
                sleep(1)  # be gentle with search
            if opts.dry_run or acted_this_pass == 0:
                break
    finally:
        _save_backup(opts, target_id, stats, backup, log)
    return _finish(opts, stats, log, progress, should_stop)


def run_server_search(api, opts, guild_id, log, progress, should_stop, sleep, name=None):
    """Clean a WHOLE server using Discord search."""
    stats = _new_stats(name or guild_id)
    my_id = _login(api, stats, log)
    if not my_id:
        return stats
    progress(stats)
    return run_search(api, opts, search_base("server", guild_id, my_id), guild_id, log,
                      progress, should_stop, sleep, name, my_id=my_id, stats=stats)


def run_ids(api, opts, channel_id, messages, log, progress, should_stop, sleep, name=None,
            start_index=0):
    """Delete messages whose ids we already know (from the data package)."""
    stats = _new_stats(name or channel_id)
    backup = []
    my_id = _login(api, stats, log)
    if not my_id:
        return stats
    flt = MessageFilter(opts)
    stats["total"] = len(messages)
    log(f"Started: {stats['chat']} ({len(messages)} messages in your data package)"
        + (" (resuming)" if start_index else ""))
    _describe(opts, log)
    forbidden = 0
    try:
        for i in range(start_index, len(messages)):
            if should_stop():
                break
            m = dict(messages[i], author={"id": my_id}, type=0, channel_id=channel_id)
            stats["scanned"] += 1
            stats["cursor"] = i + 1
            if not flt.in_dates(m) or not flt.matches(m, my_id):
                if i % 200 == 0:
                    progress(stats)
                continue
            stats["matched"] += 1
            if opts.dry_run:
                preview, when = _preview(m)
                log(f"[preview] {when}  {preview}")
            else:
                st = _delete_one(api, m, stats, backup, log)
                if st is None:
                    break
                if st == 403:
                    forbidden += 1
                    if forbidden >= 3:
                        log("No access to this chat any more (left the server, blocked, or "
                            "chat removed). Skipping it.")
                        break
                else:
                    forbidden = 0
                _pause_after_delete(opts, sleep)
            progress(stats)
            if _limit_reached(opts, stats, log):
                break
    finally:
        _save_backup(opts, channel_id, stats, backup, log)
    return _finish(opts, stats, log, progress, should_stop)


def run_target(api, opts, t, log, progress, should_stop, sleep):
    """Run one target dict (see run_many)."""
    name = t.get("name")
    cursor = t.get("cursor")
    if t["kind"] == "server":
        stats = run_server_search(api, opts, t["id"], log, progress, should_stop, sleep, name)
        if opts.remove_reactions and not should_stop():
            my_id = _login(api, stats, lambda x: None)
            try:
                chans = list_guild_channels(api, t["id"])
            except RuntimeError as e:
                log(str(e))
                chans = []
            log(f"Removing your reactions in {len(chans)} channels…")
            for ch in chans:
                if should_stop():
                    break
                if ch.get("forum"):
                    continue
                run_reaction_scan(api, opts, ch["id"], log, progress, should_stop, sleep,
                                  stats, my_id)
            log(f"Reactions removed: {stats['reactions']}")
            progress(stats)
        return stats
    if t["kind"] == "ids":
        if opts.remove_reactions:
            log("Reactions can't be found from the data package (Discord doesn't include "
                "them). Clean the live chat to remove reactions.")
        if opts.only_replies:
            log("The data package doesn't say which messages are replies: "
                "'only replies' can't be used here.")
        return run_ids(api, opts, t["id"], t["messages"], log, progress, should_stop, sleep,
                       name, start_index=cursor or 0)
    # a single chat (DM or server channel)
    if opts.remove_reactions and opts.use_search and not cursor:
        log("Removing reactions needs to read the whole chat, so search is skipped here.")
    if opts.use_search and not cursor and not opts.remove_reactions:
        stats = _new_stats(name or t["id"])
        my_id = _login(api, stats, log)
        if not my_id:
            return stats
        guild_id = t.get("guild_id")
        is_dm = t.get("is_dm")
        if not guild_id and not is_dm:
            ch = get_channel(api, t["id"])
            if ch:
                guild_id = ch.get("guild_id")
                is_dm = ch.get("type") in (1, 3)
                if not name:
                    stats["chat"] = channel_display_name(ch) or t["id"]
        if guild_id or is_dm:
            res = run_search(api, opts, search_base("channel", t["id"], my_id, guild_id),
                             t["id"], log, progress, should_stop, sleep, stats["chat"],
                             my_id=my_id, stats=stats, fallback_ok=True)
            if res is not None:
                return res
            log(f"[{stats['chat']}] Search isn't available here, reading the history instead.")
        name = name or stats["chat"]
    return run_cleaner(api, opts, log, progress, should_stop, sleep, name=name,
                       start_before=cursor)


# ----------------------------------------------------------------- many targets
def run_many(make_api, opts, targets, parallel, log, progress, should_stop, sleep):
    """Clean several targets, `parallel` at the same time.

    targets: list of dicts:
      {"kind": "channel", "id", "name", "guild_id"?, "is_dm"?, "cursor"?}
      {"kind": "server", "id", "name"}
      {"kind": "ids", "id", "name", "messages": [...], "cursor"?}   (data package)
    Plain channel id strings are accepted too.
    progress(all_stats) gets one stats dict per target (with "state" and "cursor").
    """
    import copy
    import queue
    import threading

    targets = [t if isinstance(t, dict) else {"kind": "channel", "id": t, "name": None}
               for t in targets]
    all_stats = [dict(_new_stats(t.get("name") or t["id"]), state="waiting",
                      cursor=t.get("cursor")) for t in targets]
    jobs = queue.Queue()
    for i in range(len(targets)):
        jobs.put(i)
    lock = threading.Lock()

    def worker():
        api = make_api()
        while not should_stop():
            try:
                i = jobs.get_nowait()
            except queue.Empty:
                return
            t = targets[i]
            o = copy.copy(opts)
            o.channel_id = t["id"]

            def t_log(text, i=i):
                log(f"[{i + 1}: {all_stats[i]['chat']}] {text}")

            def t_progress(st, i=i):
                with lock:
                    for k in ("scanned", "matched", "deleted", "failed", "chat", "total",
                              "reactions",
                              "cursor"):
                        if k == "cursor" and st.get(k) is None:
                            continue
                        all_stats[i][k] = st[k]
                progress(all_stats)

            all_stats[i]["state"] = "running"
            progress(all_stats)
            try:
                run_target(api, o, t, t_log, t_progress, should_stop, sleep)
            except Exception as e:  # one broken chat shouldn't stop the others
                t_log(f"Unexpected error: {e}")
            all_stats[i]["state"] = "stopped" if should_stop() else "done"
            progress(all_stats)

    n = max(1, min(int(parallel), len(targets)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return all_stats


# ----------------------------------------------------------------- export
def fetch_all_messages(api, channel_id, on_page=None, should_stop=lambda: False):
    """Every message of a chat (all authors), oldest first."""
    out, before = [], None
    while not should_stop():
        st, msgs = fetch_messages(api, channel_id, before, 100)
        if st != 200 or not msgs:
            break
        out.extend(msgs)
        before = msgs[-1]["id"]
        if on_page:
            on_page(len(out))
    out.reverse()
    return out


def download(url, limit=50 * 1024 * 1024, timeout=30):
    """Download a file from Discord's CDN (no token is sent). Refuses files over `limit`."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        size = int(r.headers.get("Content-Length") or 0)
        if size > limit:
            raise ValueError("file too big")
        chunks, total = [], 0
        while True:
            c = r.read(65536)
            if not c:
                break
            total += len(c)
            if total > limit:
                raise ValueError("file too big")
            chunks.append(c)
        return b"".join(chunks)


def safe_name(text, max_len=60):
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text or "").strip(" .")
    return (t[:max_len].strip(" .") or "chat")


def save_conversation(api, channel_id, title, base_dir, log=lambda t: None,
                      progress=lambda t: None, should_stop=lambda: False, with_files=True,
                      messages=None):
    """Save a WHOLE conversation (everyone's messages) to its own folder:
        chat.html   - looks like Discord, works offline
        chat.txt    - plain text
        messages.json - raw data
        files/      - pictures, files and profile pictures
    Returns (folder, message_count, file_count)."""
    if messages is None:
        progress("Loading messages…")
        messages = fetch_all_messages(api, channel_id,
                                      lambda n: progress(f"Loading messages… {n:,}"),
                                      should_stop)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    folder = os.path.join(base_dir, f"{safe_name(title)}_{stamp}")
    n = 1
    while os.path.exists(folder):
        n += 1
        folder = os.path.join(base_dir, f"{safe_name(title)}_{stamp}_{n}")
    files_dir = os.path.join(folder, "files")
    os.makedirs(files_dir, exist_ok=True)
    saved = 0
    if with_files:
        avatars = {}
        todo = [(m, a) for m in messages for a in m.get("attachments", [])]
        for i, (m, a) in enumerate(todo, 1):
            if should_stop():
                break
            url = a.get("url") or a.get("proxy_url")
            if not url:
                continue
            progress(f"Downloading files… {i}/{len(todo)}")
            name = f"{m['id']}_{safe_name(a.get('filename') or 'file', 80)}"
            try:
                data = download(url)
                with open(os.path.join(files_dir, name), "wb") as f:
                    f.write(data)
                a["local"] = "files/" + name
                saved += 1
            except Exception as e:
                log(f"Could not download {a.get('filename')}: {e}")
        for m in messages:
            au = m.get("author") or {}
            uid = au.get("id")
            if not uid or should_stop():
                continue
            if uid not in avatars:
                avatars[uid] = None
                try:
                    data = download(user_avatar_url(au, 64))
                    name = f"avatar_{uid}.png"
                    with open(os.path.join(files_dir, name), "wb") as f:
                        f.write(data)
                    avatars[uid] = "files/" + name
                except Exception:
                    pass
            if avatars[uid]:
                au["local_avatar"] = avatars[uid]
    export_html(messages, title, os.path.join(folder, "chat.html"))
    export_txt(messages, title, os.path.join(folder, "chat.txt"))
    with open(os.path.join(folder, "messages.json"), "w", encoding="utf-8") as f:
        json.dump({"title": title, "channel_id": channel_id, "saved": stamp,
                   "messages": messages}, f, ensure_ascii=False, indent=1)
    return folder, len(messages), saved


def export_txt(messages, title, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{title}\nExported {datetime.now():%Y-%m-%d %H:%M}\n{'=' * 60}\n\n")
        for m in messages:
            a = m.get("author", {})
            name = a.get("global_name") or a.get("username", "?")
            f.write(f"[{format_time(m.get('timestamp'))}] {name}: {render_content(m)}\n")
            for att in m.get("attachments", []):
                f.write(f"    (file) {att.get('url', att.get('filename', ''))}\n")


def export_html(messages, title, path):
    import html as h
    rows = []
    prev = None
    for m in messages:
        a = m.get("author", {})
        name = a.get("global_name") or a.get("username", "?")
        head = prev is None or prev.get("author", {}).get("id") != a.get("id")
        body = h.escape(render_content(m)).replace("\n", "<br>")
        files = "".join(
            (f'<div><a href="{h.escape(x.get("local") or x.get("url", ""))}">'
             f'<img src="{h.escape(x.get("local") or x.get("url", ""))}" loading="lazy"></a></div>'
             if is_image(x) else
             f'<div class="file"><a href="{h.escape(x.get("local") or x.get("url", ""))}">'
             f'{h.escape(x.get("filename", "file"))}</a></div>') for x in m.get("attachments", []))
        if head:
            avatar = a.get("local_avatar") or user_avatar_url(a)
            rows.append(
                f'<div class="msg head"><img class="av" src="{h.escape(avatar)}">'
                f'<div><div><b>{h.escape(name)}</b> <span class="t">'
                f'{h.escape(format_time(m.get("timestamp")))}</span></div>'
                f'<div class="c">{body}</div>{files}</div></div>')
        else:
            rows.append(f'<div class="msg"><div class="sp"></div><div><div class="c">{body}'
                        f'</div>{files}</div></div>')
        prev = m
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{h.escape(title)}</title>
<style>
body{{background:#313338;color:#dbdee1;font-family:'Segoe UI',sans-serif;margin:0}}
header{{background:#2b2d31;padding:14px 20px;font-weight:600;color:#f2f3f5;
border-bottom:1px solid #26282c;position:sticky;top:0}}
header span{{color:#949ba4;font-weight:400;font-size:13px;margin-left:10px}}
.msg{{display:flex;gap:16px;padding:2px 20px}} .msg:hover{{background:#2e3035}}
.head{{margin-top:16px}} .av{{width:40px;height:40px;border-radius:50%}} .sp{{width:40px}}
b{{color:#f2f3f5}} .t{{color:#949ba4;font-size:12px}} .c{{white-space:pre-wrap;word-break:break-word}}
img:not(.av){{max-width:400px;max-height:300px;border-radius:8px;margin-top:4px}} a{{color:#00a8fc}}
.file{{background:#2b2d31;border:1px solid #1e1f22;border-radius:8px;padding:10px 14px;
display:inline-block;margin-top:4px}}
</style></head><body><header>{h.escape(title)}<span>{len(messages)} messages · exported
{datetime.now():%Y-%m-%d %H:%M}</span></header>{''.join(rows)}<div style="height:30px"></div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def thumb_png_b64(data, max_w, max_h):
    """Shrink a picture to fit max_w x max_h and return it as base64 PNG (for Tk)."""
    import base64
    import io
    try:
        from PIL import Image
    except ImportError:
        return base64.b64encode(data).decode()
    img = Image.open(io.BytesIO(data))
    try:
        img.seek(0)
    except EOFError:
        pass
    img = img.convert("RGBA")
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def is_image(att):
    ct = (att.get("content_type") or "").lower()
    if ct.startswith("image/"):
        return True
    return (att.get("filename") or "").lower().rsplit(".", 1)[-1] in (
        "png", "jpg", "jpeg", "gif", "webp")


def rounded_png_b64(data, size, radius_ratio=0.32):
    """Image as a rounded square PNG (base64), like hovered servers in Discord."""
    import base64
    import io
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return base64.b64encode(data).decode()
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img = ImageOps.fit(img, (size, size), Image.LANCZOS)
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1),
                                           radius=int(big * radius_ratio), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def split_blocks(text):
    """Split message text into Discord-style blocks: ('text'|'code'|'quote', content).
    Inline **bold**, *italics*, __underline__ and ~~strike~~ markers are removed."""
    blocks = []
    parts = re.split(r"```(?:[\w+-]*\n)?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            blocks.append(("code", part.strip("\n")))
            continue
        quote, normal = [], []

        def flush_normal():
            t = "\n".join(normal).strip("\n")
            if t:
                blocks.append(("text", _strip_inline(t)))
            normal.clear()

        def flush_quote():
            t = "\n".join(quote)
            if t.strip():
                blocks.append(("quote", _strip_inline(t)))
            quote.clear()

        for line in part.split("\n"):
            if line.startswith("> ") or line == ">":
                flush_normal()
                quote.append(line[2:])
            else:
                flush_quote()
                normal.append(line)
        flush_quote()
        flush_normal()
    return blocks


def _strip_inline(t):
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"__(.+?)__", r"\1", t)
    t = re.sub(r"~~(.+?)~~", r"\1", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"\|\|(.+?)\|\|", r"\1", t)
    return t



# ----------------------------------------------------------------- threads & forums
def list_threads(api, guild_id, channel_id, max_pages=4):
    """Threads (and forum posts) of a channel: active + archived. Returns [{id, name}]."""
    found = {}
    for page in range(max_pages):
        st, data = api.request(
            "GET", f"/channels/{channel_id}/threads/search?archived=true&sort_by=last_message_time"
                   f"&sort_order=desc&limit=25&offset={page * 25}")
        if st != 200 or not isinstance(data, dict):
            break
        for th in data.get("threads") or []:
            found[th["id"]] = th
        if not data.get("has_more"):
            break
    if not found:
        st, data = api.request("GET", f"/channels/{channel_id}/threads/archived/public?limit=50")
        if st == 200 and isinstance(data, dict):
            for th in data.get("threads") or []:
                found[th["id"]] = th
    st, data = api.request("GET", f"/guilds/{guild_id}/threads/active")
    if st == 200 and isinstance(data, dict):
        for th in data.get("threads") or []:
            if th.get("parent_id") == channel_id:
                found[th["id"]] = th
    out = [{"id": t["id"], "name": t.get("name") or "thread",
            "archived": bool((t.get("thread_metadata") or {}).get("archived"))}
           for t in found.values()]
    out.sort(key=lambda t: -int(t["id"]) if str(t["id"]).isdigit() else 0)
    return out


def close_channel(api, channel_id):
    """Close a DM (it disappears from your list) or leave a group DM."""
    st, _ = api.request("DELETE", f"/channels/{channel_id}")
    return st in (200, 204)


# ----------------------------------------------------------------- media
EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")


def emoji_url(emoji_id, animated=False, size=48):
    return f"{CDN}/emojis/{emoji_id}.{'gif' if animated else 'png'}?size={size}"


def jumbo_emojis(content):
    """If a message is only custom emojis (like Discord's big emojis), return them."""
    text = (content or "").strip()
    found = EMOJI_RE.findall(text)
    if not found or len(found) > 27 or EMOJI_RE.sub("", text).strip():
        return []
    return [(name, emoji_url(eid, anim == "a")) for anim, name, eid in found]


def sticker_url(st):
    fmt = st.get("format_type")
    if fmt == 3:          # lottie animations can't be shown
        return None
    ext = "gif" if fmt == 4 else "png"
    return f"https://media.discordapp.net/stickers/{st['id']}.{ext}?size=160"


def is_video(att):
    ct = (att.get("content_type") or "").lower()
    if ct.startswith("video/"):
        return True
    return (att.get("filename") or "").lower().rsplit(".", 1)[-1] in ("mp4", "webm", "mov")


def video_thumb_url(att):
    """Discord's media proxy returns the first frame of a video with format=jpeg."""
    url = att.get("proxy_url") or att.get("url") or ""
    if not url:
        return None
    return url + ("&" if "?" in url else "?") + "format=jpeg"


def is_animated(att):
    """GIFs, animated WebP/PNG (Discord marks those with the 'animated' flag)."""
    if is_gif(att) or (att.get("flags") or 0) & 32:
        return True
    ct = (att.get("content_type") or "").lower()
    return ct == "image/webp" or (att.get("filename") or "").lower().endswith(".webp")


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def is_gif(att):
    ct = (att.get("content_type") or "").lower()
    return ct == "image/gif" or (att.get("filename") or "").lower().endswith(".gif")


def gif_frames_b64(data, max_w, max_h, max_frames=150):
    """Frames of an animated GIF/WebP/PNG as base64 PNGs + durations (ms) for Tk.
    Long animations keep every 2nd/3rd frame (with longer durations) so they stay light."""
    import base64
    import io
    from PIL import Image, ImageSequence
    img = Image.open(io.BytesIO(data))
    total = getattr(img, "n_frames", 1) or 1
    step = max(1, -(-total // max_frames))
    frames, durations = [], []
    pending = 0
    for i, fr in enumerate(ImageSequence.Iterator(img)):
        pending += max(20, int(fr.info.get("duration", 80) or 80))
        if i % step:
            continue
        f = fr.convert("RGBA")
        f.thumbnail((max_w, max_h), Image.LANCZOS)
        buf = io.BytesIO()
        f.save(buf, "PNG", compress_level=1)
        frames.append(base64.b64encode(buf.getvalue()).decode())
        durations.append(pending)
        pending = 0
    if durations and pending:
        durations[0] += pending
    return frames, durations


# ----------------------------------------------------------------- statistics
STOPWORDS = set("""the and for you that this with have are was but not just what your can get
all like they its it's i'm dont don't was how out about one when there their them then than
too lol yeah yes really know will would could should from been were had has his her she him
our who why where which some more much very also only into over here well even any because
les des une est pas que qui pour dans sur avec mais vous nous elle ils sont""".split())


def chat_stats(messages, my_id):
    """Numbers for the stats window. messages: any order."""
    from collections import Counter
    mine = [m for m in messages if (m.get("author") or {}).get("id") == my_id]
    per_month, per_month_all = Counter(), Counter()
    hours, weekdays, words = Counter(), Counter(), Counter()
    first = last = None
    for m in messages:
        ts = m.get("timestamp") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        key = dt.strftime("%Y-%m")
        per_month_all[key] += 1
        if (m.get("author") or {}).get("id") == my_id:
            per_month[key] += 1
            hours[dt.hour] += 1
            weekdays[dt.strftime("%A")] += 1
            for w in re.findall(r"[a-zA-ZÀ-ÿ']{3,}", m.get("content") or ""):
                w = w.lower().strip("'")
                if w not in STOPWORDS and not w.startswith("http"):
                    words[w] += 1
            first = dt if first is None or dt < first else first
            last = dt if last is None or dt > last else last
    months = sorted(set(per_month_all) | set(per_month))
    return {"total": len(messages), "mine": len(mine),
            "months": [(k, per_month.get(k, 0), per_month_all.get(k, 0)) for k in months],
            "hours": [hours.get(h, 0) for h in range(24)],
            "weekdays": weekdays.most_common(), "top_words": words.most_common(20),
            "first": first, "last": last,
            "with_files": sum(1 for m in mine if m.get("attachments")),
            "chars": sum(len(m.get("content") or "") for m in mine)}


# ----------------------------------------------------------------- options from saved dict
def options_from_dict(d, keep_ids=(), backup_dir=None):
    """Build Options from the settings/job/rule dict (used by the app and auto-clean)."""
    def num(key, default=0, cast=int):
        try:
            return cast(d.get(key) or default)
        except (TypeError, ValueError):
            return default
    return Options(
        channel_id="", delay=max(0.5, num("delay", 1.5, float)), dry_run=bool(d.get("dry_run")),
        max_delete=num("max_delete"), keywords=parse_keywords(d.get("keywords", "")),
        case_sensitive=bool(d.get("case_sensitive")), whole_word=d.get("whole_word", True),
        match_mode=d.get("match_mode"), from_date=parse_date(d.get("from_date", "")),
        to_date=parse_date(d.get("to_date", "")), skip_pinned=d.get("skip_pinned", True),
        backup_dir=backup_dir if d.get("backup", True) else None,
        use_search=d.get("use_search", True), keep_ids=set(keep_ids),
        remove_reactions=bool(d.get("remove_reactions")), has_files=bool(d.get("has_files")),
        has_images=bool(d.get("has_images")), has_links=bool(d.get("has_links")),
        only_replies=bool(d.get("only_replies")), min_len=num("min_len"),
        max_len=num("max_len"), older_than_days=num("older_than_days"))


# ----------------------------------------------------------------- find text in a chat
SEARCH_HAS = {"link", "embed", "file", "video", "image", "sound", "sticker", "poll", "snapshot"}


def parse_search(text, people=(), me_id=None):
    """Discord-style search text -> (words, [(param, value)]).
    Understands from:name mentions:name has:link|image|file|video|embed|sound|sticker
    before:YYYY-MM-DD after:YYYY-MM-DD during:YYYY-MM-DD pinned:true|false.
    people: user dicts used to turn names into ids. Unknown filters stay as words."""
    from datetime import datetime, timedelta
    words, params = [], []

    def who(q):
        q = q.lower().lstrip("@")
        if q == "me" and me_id:
            return me_id
        for exact in (True, False):
            for u in people or ():
                names = [(u.get("global_name") or "").lower(), (u.get("username") or "").lower()]
                if any(n == q if exact else n.startswith(q) for n in names if n):
                    return u.get("id")
        return None
    for tok in re.findall(r'\S+:"[^"]*"|\S+', text or ""):
        m = re.match(r"(?i)^(from|mentions|has|before|after|during|pinned):(.+)$", tok)
        if not m:
            words.append(tok)
            continue
        key, val = m.group(1).lower(), m.group(2).strip('"')
        if key in ("from", "mentions"):
            uid = who(val)
            if uid:
                params.append(("author_id" if key == "from" else "mentions", uid))
            else:
                words.append(tok)
        elif key == "has":
            v = val.lower().rstrip("s")
            v = {"picture": "image", "pic": "image", "attachment": "file", "url": "link"}.get(v, v)
            params.append(("has", v)) if v in SEARCH_HAS else words.append(tok)
        elif key == "pinned":
            params.append(("pinned", "true" if val.lower() in ("true", "yes", "1") else "false"))
        else:
            try:
                day = datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                words.append(tok)
                continue
            nxt = day + timedelta(days=1)
            if key == "before":
                params.append(("max_id", str(date_to_snowflake(day))))
            elif key == "after":
                params.append(("min_id", str(date_to_snowflake(nxt))))
            else:
                params += [("min_id", str(date_to_snowflake(day))),
                           ("max_id", str(date_to_snowflake(nxt)))]
    return " ".join(words), params


def search_text(api, channel_id, text, guild_id=None, offset=0, people=None, me_id=None):
    """Discord search in one chat (everyone's messages). The text may contain filters
    (see parse_search). Returns (total, [messages]) or (None, []) if search isn't available."""
    from urllib.parse import quote, urlencode
    words, params = parse_search(text, people or (), me_id)
    if guild_id:
        base = f"/guilds/{guild_id}/messages/search?channel_id={channel_id}"
    else:
        base = f"/channels/{channel_id}/messages/search?"
    sep = "&" if "?" in base and not base.endswith("?") else ""
    extra = ("&" + urlencode(params)) if params else ""
    path = f"{base}{sep}" + (f"content={quote(words)}&" if words else "") + \
        f"offset={offset}{extra}"
    for _ in range(3):
        st, data = api.request("GET", path)
        if st == 202:
            time.sleep(float((data or {}).get("retry_after", 2)) + 0.5)
            continue
        if st != 200 or not isinstance(data, dict):
            return None, []
        hits = [h for h in (_hit(g) for g in data.get("messages") or []) if h]
        return int(data.get("total_results") or 0), hits
    return None, []


def fetch_around(api, channel_id, message_id, limit=50):
    """Messages around one message (to jump to a search result). Newest first."""
    st, data = api.request("GET", f"/channels/{channel_id}/messages?around={message_id}"
                                  f"&limit={limit}")
    return st, (data if st == 200 else [])


# ----------------------------------------------------------------- chatting
def make_nonce():
    """Same kind of number the Discord app sends with each message."""
    import random
    return str(((int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22) + random.randint(0, 4095))


def send_message(api, channel_id, content, reply_to=None, files=None):
    body = {"content": content, "nonce": make_nonce(), "tts": False, "flags": 0}
    if reply_to:
        body["message_reference"] = {"channel_id": channel_id, "message_id": reply_to}
        body["allowed_mentions"] = {"parse": ["users", "roles", "everyone"],
                                    "replied_user": True}
    if files:
        body["attachments"] = [{"id": str(i), "filename": n} for i, (n, _) in enumerate(files)]
    return api.request("POST", f"/channels/{channel_id}/messages", body, files)


def edit_message(api, channel_id, message_id, content):
    return api.request("PATCH", f"/channels/{channel_id}/messages/{message_id}",
                       {"content": content})


def react(api, channel_id, message_id, emoji, add=True):
    st, _ = api.request("PUT" if add else "DELETE",
                        f"/channels/{channel_id}/messages/{message_id}/reactions/"
                        f"{emoji_param(emoji)}/@me?location=Message&type=0")
    return st in (200, 204)


def reaction_users(api, channel_id, message_id, emoji, limit=25):
    st, users = api.request("GET", f"/channels/{channel_id}/messages/{message_id}/reactions/"
                                   f"{emoji_param(emoji)}?limit={limit}&type=0")
    return users if st == 200 and isinstance(users, list) else []


def typing(api, channel_id):
    st, _ = api.request("POST", f"/channels/{channel_id}/typing")
    return st in (200, 204)


def guild_emojis(api, guild_id):
    st, data = api.request("GET", f"/guilds/{guild_id}/emojis")
    return data if st == 200 and isinstance(data, list) else []


def apply_reaction(msg, emoji, add, me=True):
    """Update the reactions list of a message locally (Discord sends the same change
    through the live connection, so the screen updates instantly)."""
    reactions = msg.setdefault("reactions", [])
    key = (emoji.get("id") or None, None if emoji.get("id") else emoji.get("name"))
    for r in reactions:
        em = r.get("emoji") or {}
        if (em.get("id") or None, None if em.get("id") else em.get("name")) == key:
            if add:
                if me and r.get("me"):
                    return
                r["count"] = r.get("count", 0) + 1
                r["me"] = r.get("me") or me
            else:
                if me and not r.get("me"):
                    return
                r["count"] = r.get("count", 1) - 1
                if me:
                    r["me"] = False
                if r["count"] <= 0:
                    reactions.remove(r)
            return
    if add:
        reactions.append({"emoji": {"id": emoji.get("id"), "name": emoji.get("name"),
                                    "animated": emoji.get("animated", False)},
                          "count": 1, "me": me})


def emoji_image_url(emoji, size=48):
    """Picture for a reaction/emoji: Discord's file for custom ones, Twemoji (the
    same pictures Discord uses) for normal ones."""
    if emoji.get("id"):
        return emoji_url(emoji["id"], emoji.get("animated"), size)
    return twemoji_url(emoji.get("name") or "")


def twemoji_url(chars):
    cps = [ord(c) for c in chars]
    if 0x200D not in cps:
        cps = [c for c in cps if c != 0xFE0F]
    if not cps:
        return None
    return ("https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/"
            + "-".join(f"{c:x}" for c in cps) + ".png")


def relationships(api):
    st, data = api.request("GET", "/users/@me/relationships")
    return data if st == 200 and isinstance(data, list) else None


def search_has(api, channel_id, kind, guild_id=None, offset=0):
    """Messages of one chat that have pictures / videos / files / links (newest first).
    Returns (total, [messages]) or (None, []) if search isn't available."""
    if guild_id:
        base = f"/guilds/{guild_id}/messages/search?channel_id={channel_id}&"
    else:
        base = f"/channels/{channel_id}/messages/search?"
    path = f"{base}has={kind}&offset={offset}"
    for _ in range(4):
        st, data = api.request("GET", path)
        if st == 202:
            time.sleep(float((data or {}).get("retry_after", 2)) + 0.5)
            continue
        if st != 200 or not isinstance(data, dict):
            return None, []
        hits = [h for h in (_hit(g) for g in data.get("messages") or []) if h]
        return int(data.get("total_results") or 0), hits
    return None, []
