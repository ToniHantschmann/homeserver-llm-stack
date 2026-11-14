#!/usr/bin/env bash
# Phase 3, Schritt 1: livesync-bridge gegen die REPLIK der Vault-DB testen.
# Der echte Vault (obsidian-livesync) wird hier NICHT angefasst.
#
# Voraussetzung (einmalig, vom Operator):
#   VAULT_PASSPHRASE in ~/docker/llm-stack/vault.env eintragen
#   (Obsidian -> Self-hosted LiveSync -> Remote Configuration -> E2EE-Passphrase;
#   bei Sonderzeichen in 'einfache Anfuehrungszeichen').
#
# Aufruf als homeserver (fragt einmal nach dem sudo-Passwort fuer chown):
#   ~/docker/llm-stack/setup/02-vault-bridge-test.sh             # bauen, starten, Lesetest
#   ~/docker/llm-stack/setup/02-vault-bridge-test.sh schreibtest # Testzeile -> landet in der Replik?
#   ~/docker/llm-stack/setup/02-vault-bridge-test.sh replik-neu  # Replik frisch von der echten DB ziehen
set -euo pipefail
cd "$(dirname "$0")/.."

VAULT_DIR=/mnt/data/llm/vault-test
STATE_DIR=/mnt/data/llm/livesync-bridge-state
PIN=1c252305bbddcacd200b8560d3bb786afe0906e9
TESTNOTE="99 - bridge-test.md"

set -a; . ./.env; set +a
tdb() { docker exec couchdb-vault-test curl -s -u "admin:$VAULT_TEST_COUCHDB_PASSWORD" "$@"; }

if [ "${1:-}" = "replik-neu" ]; then
    echo ">> Bridge stoppen, Replik verwerfen und neu von obsidian-livesync ziehen"
    docker compose --profile vault stop livesync-bridge 2>/dev/null || true
    set -a; . ../obsidian/.env; set +a
    tdb -X DELETE http://127.0.0.1:5984/obsidian-vault; echo
    tdb -X POST http://127.0.0.1:5984/_replicate -H 'Content-Type: application/json' --max-time 3600 -d "{
      \"source\":{\"url\":\"http://obsidian-livesync:5984/obsidian-vault\",\"auth\":{\"basic\":{\"username\":\"$COUCHDB_USER\",\"password\":\"$COUCHDB_PASSWORD\"}}},
      \"target\":{\"url\":\"http://127.0.0.1:5984/obsidian-vault\",\"auth\":{\"basic\":{\"username\":\"admin\",\"password\":\"$VAULT_TEST_COUCHDB_PASSWORD\"}}},
      \"create_target\":true}" | head -c 300; echo
    # _local-Dokumente repliziert CouchDB nie - ohne sie: "Corrupted document".
    for d in obsydian_livesync_milestone obsidian_livesync_sync_parameters; do
        curl -s -u "$COUCHDB_USER:$COUCHDB_PASSWORD" "http://127.0.0.1:5984/obsidian-vault/_local/$d" \
          | sed 's/"_rev":"[^"]*",//' \
          | docker exec -i couchdb-vault-test curl -s -u "admin:$VAULT_TEST_COUCHDB_PASSWORD" \
              -H 'Content-Type: application/json' -X PUT "http://127.0.0.1:5984/obsidian-vault/_local/$d" --data-binary @-
        echo
    done
    echo ">> Bridge-Zustand und Testordner muessen danach leer neu starten:"
    echo "   sudo rm -rf $VAULT_DIR/* $VAULT_DIR/.[!.]* $STATE_DIR/*   und dieses Skript ohne Argument erneut"
    exit 0
fi

if [ "${1:-}" = "schreibtest" ]; then
    f="$VAULT_DIR/$TESTNOTE"
    id=$(echo "$TESTNOTE" | tr '[:upper:]' '[:lower:]')
    enc=$(printf '%s' "$id" | sed 's/ /%20/g')
    before=$(tdb "http://127.0.0.1:5984/obsidian-vault/$enc" | grep -o '"_rev":"[^"]*"' || echo "(neu)")
    line="- [ ] bridge-schreibtest $(date '+%F %T')"
    echo ">> Haenge an: $f"
    echo "$line" | sudo tee -a "$f" >/dev/null
    sudo chown 1993:1993 "$f"
    for i in $(seq 30); do
        after=$(tdb "http://127.0.0.1:5984/obsidian-vault/$enc" | grep -o '"_rev":"[^"]*"' || true)
        if [ -n "$after" ] && [ "$after" != "$before" ]; then
            echo "OK: Replik hat neue Revision nach ${i}x2 s ($before -> $after)."
            exit 0
        fi
        sleep 2
    done
    echo "FEHLER: nach 60 s keine neue Revision in der Replik. Logs: docker logs --tail 50 livesync-bridge" >&2
    exit 1
fi

# --- Vorpruefungen (alles VOR dem ersten Schreiben) -------------------------
grep -qE '^VAULT_PASSPHRASE=.+' vault.env || { echo "VAULT_PASSPHRASE in vault.env ist leer - erst eintragen." >&2; exit 1; }
grep -q '^VAULT_COUCHDB_URL=http://couchdb-vault-test:5984$' vault.env \
    || { echo "vault.env zeigt NICHT auf die Replik - dieses Skript ist nur fuer den Test." >&2; exit 1; }
[ "$(git -C livesync-bridge/src rev-parse HEAD)" = "$PIN" ] \
    || { echo "livesync-bridge/src steht nicht auf $PIN - Abbruch." >&2; exit 1; }
[ -z "$(git -C livesync-bridge/src status --porcelain)" ] \
    || { echo "livesync-bridge/src hat lokale Aenderungen - Abbruch." >&2; exit 1; }
docker ps --format '{{.Names}}' | grep -qx couchdb-vault-test \
    || { echo "couchdb-vault-test laeuft nicht: docker compose --profile vault up -d couchdb-vault-test" >&2; exit 1; }
n=$(tdb http://127.0.0.1:5984/obsidian-vault | grep -o '"doc_count":[0-9]*' | cut -d: -f2)
echo ">> Replik: $n Dokumente"
tdb http://127.0.0.1:5984/obsidian-vault/_local/obsidian_livesync_sync_parameters | grep -q pbkdf2salt \
    || { echo "Replik ohne _local/obsidian_livesync_sync_parameters -> erst '$0 replik-neu'." >&2; exit 1; }

# --- Bauen -------------------------------------------------------------------
echo ">> Baue localhost/livesync-bridge:1c25230 aus dem gepinnten Commit"
docker compose --profile vault build livesync-bridge

# --- Rechte: Container laeuft als deno (1993:1993) ---------------------------
echo ">> chown 1993:1993 fuer $VAULT_DIR und $STATE_DIR (sudo)"
sudo chown 1993:1993 "$VAULT_DIR" "$STATE_DIR"

# --- Start + Lesetest --------------------------------------------------------
docker compose --profile vault up -d livesync-bridge
echo ">> Warte, bis Dateien ankommen (max. 15 min, 157k Dokumente) ..."
prev=-1
for i in $(seq 90); do
    sleep 10
    c=$(sudo find "$VAULT_DIR" -type f | wc -l)
    echo "   $(date +%T)  $c Dateien"
    if [ "$c" -gt 0 ] && [ "$c" = "$prev" ]; then break; fi
    prev=$c
done

echo
echo ">> Ergebnis"
echo "   Dateien gesamt:  $(sudo find "$VAULT_DIR" -type f | wc -l)"
echo "   Markdown:        $(sudo find "$VAULT_DIR" -type f -name '*.md' | wc -l)"
echo "   Wochenplanungen: $(sudo find "$VAULT_DIR" -type f -iname 'wochenplanung kw *.md' | wc -l)"
w=$(sudo find "$VAULT_DIR" -type f -iname 'wochenplanung kw 38.md' | head -1)
if [ -n "$w" ]; then
    echo "   Anfang von $w:"; sudo head -n 8 "$w" | sed 's/^/     | /'
fi
echo "   Fehler im Log:   $(docker logs livesync-bridge 2>&1 | grep -ciE 'corrupt|decrypt|failed|error')"
echo
echo "Pruefen: Ist der Text oben lesbar (nicht verschluesselt)? Dann ggf. '$0 schreibtest'."
echo "Logs:    docker logs -f livesync-bridge"
