#!/usr/bin/env bash
# Discord Message Cleaner - always-on deleted-messages logger
# For a Raspberry Pi, an old laptop with Linux, or any Debian/Ubuntu machine that stays on.
# Run from the app's folder:   bash server/install_linux.sh
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/discord-logger"
FILES="main.py logger_service.py gateway.py msglog.py applog.py cleaner_core.py securestore.py"

echo "== Discord Message Cleaner: always-on logger =="
command -v python3 >/dev/null || { echo "Installing Python 3..."; sudo apt-get update && sudo apt-get install -y python3; }
python3 -c 'import sys; assert sys.version_info >= (3, 8)' || { echo "Python 3.8 or newer is needed."; exit 1; }

mkdir -p "$DEST/data" "$DEST/export"
for f in $FILES; do cp "$SRC/$f" "$DEST/"; done

if [ ! -s "$DEST/token.txt" ]; then
  echo
  echo "Paste your Discord token (it won't show while you type), then press Enter:"
  read -rs TOKEN
  echo
  printf '%s' "$TOKEN" > "$DEST/token.txt"
fi
chmod 600 "$DEST/token.txt"

echo "Checking the token..."
python3 - "$DEST" <<'PY'
import sys, time
sys.path.insert(0, sys.argv[1])
import cleaner_core as core
tok = core.clean_token(open(sys.argv[1] + "/token.txt").read())
me, st = core.get_me(core.DiscordApi(tok, print, lambda: False, time.sleep))
if not me:
    sys.exit(f"Discord refused the token ({st}). Run this script again with a new token.")
print("OK, logged in as", me.get("username"))
PY

CMD="/usr/bin/env python3 $DEST/main.py --logger --token-file $DEST/token.txt --data $DEST/data --export $DEST/export/deleted_messages_export.db"

if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/discord-logger.service" <<UNIT
[Unit]
Description=Discord Message Cleaner - deleted messages logger
After=network-online.target

[Service]
ExecStart=$CMD
Restart=always
RestartSec=30
Nice=10

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now discord-logger.service
  # keep it running when nobody is logged in, and start it at boot
  sudo loginctl enable-linger "$USER" 2>/dev/null || echo "(Couldn't enable start-at-boot without login; it still starts when you log in.)"
  echo "Installed as a service. Status:  systemctl --user status discord-logger"
else
  ( crontab -l 2>/dev/null | grep -v discord-logger; echo "@reboot $CMD >/dev/null 2>&1 # discord-logger" ) | crontab -
  nohup $CMD >/dev/null 2>&1 &
  echo "Installed with cron (starts at every boot) and started now."
fi

echo
echo "Done! The logger is running 24/7 on this device."
echo "Every 10 minutes it writes:  $DEST/export/deleted_messages_export.db"
echo "Get that file to your PC (Syncthing is free and automatic, or copy it), then in the app:"
echo "  red ⊘ section > 'Auto-import a synced file...' (or 'Import from another device...')"
echo "To stop it:  systemctl --user disable --now discord-logger   (or remove the line with: crontab -e)"
