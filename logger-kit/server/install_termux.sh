#!/data/data/com.termux/files/usr/bin/bash
# Discord Message Cleaner - always-on logger on an Android phone (Termux)
# 1. Install Termux AND Termux:Boot from F-Droid (https://f-droid.org) - both free.
# 2. Open Termux:Boot once, then in Termux:  termux-setup-storage
# 3. Copy the app's folder to the phone and run:  bash server/install_termux.sh
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/discord-logger"
EXPORT="$HOME/storage/shared/DiscordLogger"
FILES="main.py logger_service.py gateway.py msglog.py applog.py cleaner_core.py securestore.py"

pkg install -y python >/dev/null
mkdir -p "$DEST/data" "$EXPORT" "$HOME/.termux/boot"
for f in $FILES; do cp "$SRC/$f" "$DEST/"; done
if [ ! -s "$DEST/token.txt" ]; then
  echo "Paste your Discord token (hidden), then Enter:"
  read -rs TOKEN; echo
  printf '%s' "$TOKEN" > "$DEST/token.txt"
fi
chmod 600 "$DEST/token.txt"

cat > "$HOME/.termux/boot/discord-logger.sh" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$DEST"
while true; do
  python main.py --logger --token-file token.txt --data data --export "$EXPORT/deleted_messages_export.db"
  sleep 30
done
BOOT
chmod +x "$HOME/.termux/boot/discord-logger.sh"
nohup sh "$HOME/.termux/boot/discord-logger.sh" >/dev/null 2>&1 &

echo
echo "Done! The logger runs now and starts again when the phone restarts."
echo "Its file is in your phone's storage: DiscordLogger/deleted_messages_export.db"
echo "Sync that folder to your PC (Syncthing is free), then in the app:"
echo "  red ⊘ section > 'Auto-import a synced file...'"
echo "Tip: in Android settings, turn off battery optimization for Termux so it isn't killed."
