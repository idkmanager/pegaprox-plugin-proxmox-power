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

# Enable the plugin in the SQLite plugin_state table (idempotent).
if command -v sqlite3 >/dev/null 2>&1 && [ -f "$DB" ]; then
  sqlite3 "$DB" "INSERT INTO plugin_state (plugin_id, enabled) VALUES ('$PLUGIN_ID', 1)
    ON CONFLICT(plugin_id) DO UPDATE SET enabled=1;" || \
  sqlite3 "$DB" "INSERT OR REPLACE INTO plugin_state (plugin_id, enabled) VALUES ('$PLUGIN_ID', 1);"
  echo "==> Enabled in plugin_state"
else
  echo "!! sqlite3 or DB missing — enable '$PLUGIN_ID' from Settings > Plugins"
fi

# Best-effort ownership match to the rest of the install.
OWNER="$(stat -c '%U:%G' "$PEGAPROX_DIR" 2>/dev/null || echo root:root)"
chown -R "$OWNER" "$DEST" 2>/dev/null || true

echo "==> Restarting pegaprox"
systemctl restart pegaprox || echo "!! restart manually: systemctl restart pegaprox"
echo "==> Done. Open the 'Proxmox VM Power Control' tab in PegaProx."
