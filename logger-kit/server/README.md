# Always-on logger (catch deleted messages while your PC is off)

Discord really erases deleted messages. To keep a copy, something has to be online when
the message arrives. When your PC is off, run the small logger on a device that stays on.
It's free and needs only Python (no extra packages), and uses very little power.

| Device | How |
|---|---|
| Raspberry Pi / old laptop with Linux | `bash server/install_linux.sh` |
| Android phone (Termux + Termux:Boot from F-Droid) | `bash server/install_termux.sh` |
| Another Windows PC that stays on | Install the app there, log in with Remember me, turn on the logger and tick **Start with Windows** |

The scripts ask for your token once, check it, install the logger so it **starts by itself
after a reboot**, and write a safe copy of its database every 10 minutes
(`deleted_messages_export.db`).

## Getting the messages on your PC
Easiest: install [Syncthing](https://syncthing.net) (free) on both devices and share the
export folder. Then in the app: red ⊘ section → **Auto-import a synced file…** → pick the
synced `deleted_messages_export.db`. The app imports it by itself whenever it changes.
You can also copy the file by hand and use **Import from another device…**.

## Safety
Your token gives full access to your Discord account. Only put it on a device **you own**,
never on a shared computer or a public/cloud server. `token.txt` is only readable by your
user. Changing your Discord password makes the token stop working everywhere.

## Manual start (any OS with Python 3.8+)
```
python3 main.py --logger --token-file token.txt --data ./data --export ./export/deleted_messages_export.db
```
