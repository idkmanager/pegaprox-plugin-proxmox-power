#!/usr/bin/env bash
# Powvm Control — host-side maintenance (run by a systemd timer).
#
# Does two jobs, idempotently and quietly:
#   1. PERSISTENCE — if a PegaProx upgrade wiped or downgraded the plugin in
#      /opt/PegaProx/plugins/proxmox-power, restore it from the cache that lives
#      OUTSIDE /opt/PegaProx (so it survives PegaProx reinstalls), re-enable it
#      and restart pegaprox.
#   2. AUTO-UPDATE (opt-in) — if a newer version is published at $SOURCE and the
#      host can reach it, refresh the cache; persistence then rolls it out.
#
# It only restarts pegaprox when something actually changed.
set -uo pipefail

CONF=/etc/proxmox-power.conf
[ -f "$CONF" ] && . "$CONF"

PLUGIN_ID=proxmox-power
PEGAPROX_DIR="${PEGAPROX_DIR:-/opt/PegaProx}"
DEST="$PEGAPROX_DIR/plugins/$PLUGIN_ID"
DB="$PEGAPROX_DIR/config/pegaprox.db"
CACHE="${CACHE_DIR:-/usr/local/lib/proxmox-power}"
SOURCE="${SOURCE:-}"
# Fallback mirrors (different CDN/DNS path) tried after $SOURCE, so a host that
# can't resolve raw.githubusercontent.com still updates. Override via the conf.
MIRRORS_DEFAULT="https://cdn.jsdelivr.net/gh/alfonsokuen/pegaprox-plugin-proxmox-power@main https://fastly.jsdelivr.net/gh/alfonsokuen/pegaprox-plugin-proxmox-power@main"
MIRRORS="${MIRRORS:-$MIRRORS_DEFAULT}"
AUTO_UPDATE="${AUTO_UPDATE:-false}"
SVC_USER="${SVC_USER:-pegaprox}"
RUNTIME_FILES="__init__.py manifest.json power.html"

log(){ logger -t proxmox-power-maint "$*" 2>/dev/null || true; echo "[proxmox-power-maint] $*"; }
ver(){ python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('version','0'))" "$1" 2>/dev/null || echo 0; }
# vgt A B -> exit 0 if version A > B
vgt(){ python3 - "$1" "$2" <<'PY'
import sys
def t(v):
    o=[]
    for p in str(v).split('.'):
        d=''.join(c for c in p if c.isdigit()); o.append(int(d) if d else 0)
    return o
a,b=t(sys.argv[1]),t(sys.argv[2]); n=max(len(a),len(b)); a+=[0]*(n-len(a)); b+=[0]*(n-len(b))
sys.exit(0 if a>b else 1)
PY
}

changed=0

# --- 1. AUTO-UPDATE: refresh the cache from SOURCE if reachable and newer -----
if [ "$AUTO_UPDATE" = "true" ]; then
  TMP="$(mktemp -d)"
  # Pull manifest from the first reachable base (configured SOURCE, then mirrors).
  BASE_OK=""
  for BASE in $SOURCE $MIRRORS; do
    [ -n "$BASE" ] || continue
    if curl -fsSL --max-time 20 "$BASE/manifest.json" -o "$TMP/manifest.json" 2>/dev/null; then
      BASE_OK="$BASE"; break
    fi
  done
  if [ -n "$BASE_OK" ]; then
    RV="$(ver "$TMP/manifest.json")"; CV="$(ver "$CACHE/manifest.json")"
    if vgt "$RV" "$CV"; then
      ok=1
      for f in __init__.py power.html; do
        curl -fsSL --max-time 20 "$BASE_OK/$f" -o "$TMP/$f" 2>/dev/null || ok=0
      done
      if [ "$ok" = 1 ] && python3 -m py_compile "$TMP/__init__.py" 2>/dev/null && [ -s "$TMP/power.html" ]; then
        mkdir -p "$CACHE"
        cp -f "$TMP/manifest.json" "$TMP/__init__.py" "$TMP/power.html" "$CACHE/"
        log "cache updated $CV -> $RV from $BASE_OK"
      else
        log "remote $RV failed validation; kept cache $CV"
      fi
    fi
  else
    log "no update source reachable (tried: $SOURCE $MIRRORS)"
  fi
  rm -rf "$TMP"
fi

# --- 2. PERSISTENCE: restore DEST from cache if missing or older -------------
need_restore=0
if [ ! -f "$DEST/__init__.py" ]; then
  need_restore=1
elif [ -f "$CACHE/manifest.json" ] && vgt "$(ver "$CACHE/manifest.json")" "$(ver "$DEST/manifest.json")"; then
  need_restore=1
fi

if [ "$need_restore" = 1 ] && [ -f "$CACHE/__init__.py" ]; then
  mkdir -p "$DEST"
  for f in $RUNTIME_FILES; do cp -f "$CACHE/$f" "$DEST/$f"; done
  [ -f "$DEST/config.json" ] || echo '{ "groups": [] }' > "$DEST/config.json"
  GRP="$(id -gn "$SVC_USER" 2>/dev/null || echo "$SVC_USER")"
  chown -R "$SVC_USER:$GRP" "$DEST" 2>/dev/null || true
  chmod 600 "$DEST/config.json" 2>/dev/null || true
  # Best-effort re-enable (plain SQLite only; encrypted DBs keep their row).
  if command -v sqlite3 >/dev/null 2>&1 && sqlite3 "$DB" "PRAGMA schema_version;" >/dev/null 2>&1; then
    sqlite3 "$DB" "INSERT OR REPLACE INTO plugin_state (plugin_id, enabled) VALUES ('$PLUGIN_ID',1);" 2>/dev/null || true
  fi
  changed=1
  log "restored plugin into $DEST (v$(ver "$DEST/manifest.json"))"
fi

if [ "$changed" = 1 ]; then
  if systemctl restart pegaprox 2>/dev/null; then log "pegaprox restarted"; else log "WARN: could not restart pegaprox"; fi
fi
exit 0
