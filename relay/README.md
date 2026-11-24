# Free relay for the web version

Discord's API refuses requests that come straight from a web page on another site
(this is called CORS). The Windows app talks to Discord directly, but the website on
GitHub Pages needs a small relay in between. The relay below runs for free on
Cloudflare Workers (100,000 requests a day on the free plan). It only forwards
requests to Discord and adds the header the browser needs. It doesn't store or log
anything.

## Set it up (about 3 minutes)

1. Make a free account at <https://dash.cloudflare.com/sign-up> (no card needed).
2. In the dashboard, open **Workers & Pages** → **Create** → **Create Worker**.
   Name it `dmc-relay` and press **Deploy**.
3. Press **Edit code**. Delete everything in `worker.js` and paste in the contents
   of [`worker.js`](worker.js) from this folder. Press **Deploy**.
4. If your site isn't `https://08raph.github.io`, add its address to
   `ALLOWED_ORIGINS` at the top of the code and deploy again.
5. Copy the worker's address, for example `https://dmc-relay.yourname.workers.dev`.
   Open it in a browser. You should see `{"ok":true,...}`.
6. Use it:
   - **For every visitor of your site:** put the address in `config.js`
     (`RELAY: "https://dmc-relay.yourname.workers.dev"`) and push.
   - **Just for you:** open the web app → **Connection** (login screen) or
     **Settings → Connection**, paste the address and press **Test**.

## Good to know

- Requests pass through your worker, including the token of anyone using your site
  through it. The code only forwards to `discord.com/api`, `cdn.discordapp.com`,
  `media.discordapp.net` and the Discord gateway, and only for the sites in
  `ALLOWED_ORIGINS`. Anyone who prefers can run their own relay with this same file.
- Requests reach Discord from Cloudflare's network instead of your home internet.
  Discord may treat that as unusual and ask for a captcha or limit the account more
  quickly. Automating a user account is against Discord's Terms either way.
- Over 100,000 requests in a day, Cloudflare answers with an error until the next
  day. One deleted message is one request, so cleaning very large chats can reach it.
