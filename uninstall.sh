#!/usr/bin/env bash
# Remove the Proxmox VM Power Control plugin from a local PegaProx instance.
set -euo pipefail
PLUGIN_ID="proxmox-power"
PEGAPROX_DIR="${PEGAPROX_DIR:-/opt/PegaProx}"
DEST="$PEGAPROX_DIR/plugins/$PLUGIN_ID"
DB="$PEGAPROX_DIR/config/pegaprox.db"

read -rp "Remove $PLUGIN_ID and its config? [y/N] " ans
[ "${ans:-N}" = "y" ] || { echo "Aborted."; exit 0; }

rm -rf "$DEST"
if command -v sqlite3 >/dev/null 2>&1 && [ -f "$DB" ]; then
  sqlite3 "$DB" "DELETE FROM plugin_state WHERE plugin_id = '$PLUGIN_ID';" || true
fi
systemctl restart pegaprox || echo "!! restart manually: systemctl restart pegaprox"
echo "==> Removed $PLUGIN_ID"
