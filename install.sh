#!/usr/bin/env bash
# Install the Proxmox VM Power Control plugin into a local PegaProx instance.
# Run as root on the PegaProx host (e.g. LXC 119).
set -euo pipefail

PLUGIN_ID="proxmox-power"
PEGAPROX_DIR="${PEGAPROX_DIR:-/opt/PegaProx}"
PLUGINS_DIR="$PEGAPROX_DIR/plugins"
DEST="$PLUGINS_DIR/$PLUGIN_ID"
DB="$PEGAPROX_DIR/config/pegaprox.db"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing $PLUGIN_ID into $DEST"
[ -d "$PEGAPROX_DIR" ] || { echo "PegaProx not found at $PEGAPROX_DIR"; exit 1; }

mkdir -p "$DEST"
for f in __init__.py manifest.json power.html; do
  cp -f "$SRC/$f" "$DEST/$f"
done

# Seed config.json on first install only (never clobber operator config).
if [ ! -f "$DEST/config.json" ]; then
  echo '{ "groups": [] }' > "$DEST/config.json"
fi
chmod 600 "$DEST/config.json"

# Try to enable the plugin in plugin_state — but only if the DB is a *plain*
# SQLite file. Newer PegaProx encrypts the DB via dbcrypto/SQLCipher, where an
# external sqlite3 fails with "file is not a database (26)". In that case (and
# any other), we never touch the DB and just tell the operator to flip the
# toggle in the UI. This step must never abort the install.
ENABLED_VIA_DB=0
if command -v sqlite3 >/dev/null 2>&1 && [ -f "$DB" ] \
   && sqlite3 "$DB" "PRAGMA schema_version;" >/dev/null 2>&1; then
  if sqlite3 "$DB" "INSERT OR REPLACE INTO plugin_state (plugin_id, enabled) VALUES ('$PLUGIN_ID', 1);" 2>/dev/null; then
    ENABLED_VIA_DB=1
    echo "==> Enabled in plugin_state (plain SQLite)"
  fi
fi
if [ "$ENABLED_VIA_DB" -eq 0 ]; then
  echo "!! Could not auto-enable via the DB (it is encrypted or locked — normal)."
  echo "   Files are installed. Enable it from the web UI:"
  echo "     PegaProx > Settings > Plugins > 'Proxmox VM Power Control' > Enable"
fi

# Best-effort ownership match to the rest of the install.
OWNER="$(stat -c '%U:%G' "$PEGAPROX_DIR" 2>/dev/null || echo root:root)"
chown -R "$OWNER" "$DEST" 2>/dev/null || true

echo "==> Restarting pegaprox"
systemctl restart pegaprox || echo "!! restart manually: systemctl restart pegaprox"
echo "==> Done."
if [ "$ENABLED_VIA_DB" -eq 1 ]; then
  echo "    Open the 'Proxmox VM Power Control' tab in PegaProx."
else
  echo "    Now enable it: PegaProx > Settings > Plugins > 'Proxmox VM Power Control' > Enable."
fi
