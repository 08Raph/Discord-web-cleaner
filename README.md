# Discord Message Cleaner (web)

Static site: no build step. Upload everything in this folder to a public GitHub repo
(root, or a `/docs` folder), then Settings → Pages → Deploy from a branch.

## Make it feel like the Windows app
Open the page in Chrome or Edge and click **Install as app** (or the install icon in the address bar).
It then has its own window, taskbar icon, Windows notifications and an unread badge.
To start it with Windows: Edge `edge://apps` / Chrome `chrome://apps` → app details → start on sign-in.
It only works while the app window is open (minimised is fine); a web app cannot run when closed.
For 24/7 logging run the Raspberry Pi / Termux logger from the exe kit and use **Logger → Import .db file**.

Automating a user account is against Discord's Terms and may get it limited or banned. Use at your own risk.

## v8 additions
Data-package cleaning (closed DMs are skipped once Discord denies access), whole-server cleaning with a count preview, passphrase-encrypted saved tokens, logger picture copies + 7-day retention + muted list + activity screen, richer text formatting, full translations (tour, pop-ups, prompts), settings screen and a phone layout with bottom tabs.

## v9
Add by link for closed DMs, chat filter box, Clear / Save chats, show-token, Help, named and hourly rules with Run now, Auto speed, input checks, media gallery tabs, Latest, Copy message ID, app-behaviour toggles, auto sign-in, demo banner.
`logger-kit/` holds the always-on logger from the exe kit (Raspberry Pi / Linux / Termux). It is not part of the website; see `logger-kit/server/README.md`.

## v10
Chat list sections (Direct messages / Group DMs) and refresh, quick switcher for chats and servers with arrow keys, exe-style statistics, Load older messages, KEPT and pinned labels, [system message], save and jump for saved messages, recent emojis, tour Skip, My account section (switch account, remove saved account, report a problem), notification / ghost-ping / lost-friend toggles.

## v11
Separate Deleted messages view (⊘ in the server bar), find bar (Ctrl+F) with counter and prev/next, selected chats as chips, "Clean this chat" box in the chat header, rules with enable switch / days / last run, guided tour that points at the real controls, APP / you / mention markers, activity entries can be removed. Fixed a startup error that could stop the logger and rules code from loading.
