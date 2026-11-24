// Discord Message Cleaner – relay (Cloudflare Worker, free plan)
//
// Why: Discord's API refuses requests that come straight from a web page on another
// site (CORS), so the web version on GitHub Pages needs a tiny relay. The relay only
// forwards requests to Discord and adds the header the browser needs. It stores and
// logs nothing.
//
// Setup (about 3 minutes, free):
//  1. dash.cloudflare.com → Workers & Pages → Create → Create Worker → name it
//     "dmc-relay" → Deploy → Edit code → replace everything with this file → Deploy.
//  2. Copy the worker address, e.g. https://dmc-relay.yourname.workers.dev
//  3. Put it in config.js (RELAY) so every visitor uses it, or paste it in the app:
//     Settings → Connection → Relay address.
//
// Only pages listed in ALLOWED_ORIGINS can use your relay. Add your own site.

const ALLOWED_ORIGINS = [
  "https://08raph.github.io",
  "http://localhost:8000",
  "http://127.0.0.1:8000",
];

// Only these Discord hosts can be reached through the relay.
const HOSTS = {
  "discord.com": p => p.startsWith("/api/"),
  "cdn.discordapp.com": () => true,
  "media.discordapp.net": () => true,
  "gateway.discord.gg": () => true, // live updates (websocket), only used if the browser can't reach it directly
};

const PASS_REQ = ["authorization", "content-type", "x-audit-log-reason", "x-super-properties", "x-discord-locale"];
const PASS_RES = ["content-type", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
  "x-ratelimit-reset-after", "x-ratelimit-bucket", "x-ratelimit-global", "x-ratelimit-scope"];

function cors(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Audit-Log-Reason, X-Discord-Locale",
    "Access-Control-Expose-Headers": PASS_RES.join(", "),
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

export default {
  async fetch(request) {
    const origin = request.headers.get("Origin") || "";
    const okOrigin = ALLOWED_ORIGINS.includes(origin);
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({ ok: true, app: "dmc-relay", allowed: okOrigin }),
        { headers: { "content-type": "application/json", ...(okOrigin ? cors(origin) : {}) } });
    }
    if (!okOrigin) return new Response("Origin not allowed. Add it to ALLOWED_ORIGINS in the worker.", { status: 403 });
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(origin) });

    // /discord.com/api/v9/users/@me  →  https://discord.com/api/v9/users/@me
    const m = url.pathname.match(/^\/([^/]+)(\/.*)$/);
    if (!m || !HOSTS[m[1]] || !HOSTS[m[1]](m[2])) return new Response("Not allowed", { status: 400, headers: cors(origin) });
    const target = "https://" + m[1] + m[2] + url.search;

    if ((request.headers.get("Upgrade") || "").toLowerCase() === "websocket") {
      if (m[1] !== "gateway.discord.gg") return new Response("Not allowed", { status: 400 });
      return fetch(target, { headers: { Upgrade: "websocket" } });
    }
    if (m[1] === "gateway.discord.gg") return new Response("Websocket only", { status: 400, headers: cors(origin) });

    const headers = new Headers();
    for (const h of PASS_REQ) { const v = request.headers.get(h); if (v) headers.set(h, v); }
    headers.set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36");

    const init = { method: request.method, headers, redirect: "follow" };
    if (!["GET", "HEAD"].includes(request.method)) init.body = await request.arrayBuffer();

    let res;
    try { res = await fetch(target, init); }
    catch (e) { return new Response(JSON.stringify({ message: "Relay could not reach Discord" }), { status: 502, headers: { "content-type": "application/json", ...cors(origin) } }); }

    const out = new Headers(cors(origin));
    for (const h of PASS_RES) { const v = res.headers.get(h); if (v) out.set(h, v); }
    if (m[1] !== "discord.com") { const ct = res.headers.get("content-type"); if (ct) out.set("content-type", ct); out.set("Cache-Control", "private, max-age=3600"); }
    return new Response(res.body, { status: res.status, headers: out });
  },
};
