#!/usr/bin/env bash
# Phase 3, Schritt 2: livesync-bridge von der Replik auf den ECHTEN Vault umstellen.
#
# Ab hier wirkt alles, was in /mnt/data/llm/vault geaendert oder geloescht wird,
# auf ALLEN Geraeten. Deshalb: Vorpruefungen ohne Schreiben, dann Snapshot,
# dann erst Umstellung. Jede angefasste Datei wird vorher gesichert.
#
# Aufruf als homeserver, in einem echten Terminal (sudo fragt nach dem Passwort):
#   ~/docker/llm-stack/setup/03-vault-bridge-prod.sh              # umstellen
#   ~/docker/llm-stack/setup/03-vault-bridge-prod.sh schreibtest  # Testzeile -> am Handy?
#   ~/docker/llm-stack/setup/03-vault-bridge-prod.sh aufraeumen   # Replik + Testordner loeschen
#   ~/docker/llm-stack/setup/03-vault-bridge-prod.sh zurueck      # Bridge stoppen, Dateien zurueck
set -euo pipefail
# Nie wieder stumm sterben: jeder unerwartete Fehler nennt seine Zeile.
trap 'echo "ABBRUCH: unerwarteter Fehler in Zeile $LINENO (Befehl: $BASH_COMMAND)" >&2' ERR
cd "$(dirname "$0")/.."

COMPOSE_SHA_ERWARTET=6040c0fdb2f0abb889c70f460d189eb996be7ba696522c920f188fb01b025942
COMPOSE_NEU=setup/03-compose.yml.neu
VAULT_DIR=/mnt/data/llm/vault
STATE_DIR=/mnt/data/llm/livesync-bridge-state
SNAP_DIR=/mnt/data/llm/snapshots
OBS_DIR=$HOME/docker/obsidian
DB=obsidian-vault
BRIDGE_USER=livesync-bridge
SECURITY_ALT='{"members":{"roles":["_admin"]},"admins":{"roles":["_admin"]}}'
TESTNOTE="99 - bridge-test.md"
TS=$(date +%Y%m%d-%H%M%S)

# Admin-Zugang der echten CouchDB (nur fuer dieses Skript, nicht fuer die Bridge)
COUCHDB_USER=$(grep -E '^COUCHDB_USER=' "$OBS_DIR/.env" | cut -d= -f2-)
COUCHDB_PASSWORD=$(grep -E '^COUCHDB_PASSWORD=' "$OBS_DIR/.env" | cut -d= -f2-)
P=http://127.0.0.1:5984
pc() { curl -s -u "$COUCHDB_USER:$COUCHDB_PASSWORD" "$@"; }
die() { echo "ABBRUCH: $*" >&2; exit 1; }

# ============================================================================
if [ "${1:-}" = "schreibtest" ]; then
    grep -q '^VAULT_COUCHDB_URL=http://obsidian-livesync:5984$' vault.env \
        || die "Umstellung noch nicht erfolgt (vault.env zeigt nicht auf den echten Vault) - erst ohne Argument ausfuehren."
    docker ps --format '{{.Names}}' | grep -qx livesync-bridge || die "livesync-bridge laeuft nicht."
    f="$VAULT_DIR/$TESTNOTE"
    enc=$(printf '%s' "$TESTNOTE" | tr '[:upper:]' '[:lower:]' | sed 's/ /%20/g')
    before=$(pc "$P/$DB/$enc" | grep -o '"_rev":"[^"]*"' || echo "(neu)")
    echo ">> Haenge an: $f"
    echo "- [ ] bridge-schreibtest $(date '+%F %T')" | sudo tee -a "$f" >/dev/null
    sudo chown 1993:1993 "$f"
    for i in $(seq 30); do
        after=$(pc "$P/$DB/$enc" | grep -o '"_rev":"[^"]*"' || true)
        if [ -n "$after" ] && [ "$after" != "$before" ]; then
            echo "OK: echte DB hat neue Revision nach $((i*2)) s ($before -> $after)."
            echo
            echo "Jetzt am Handy: Notiz '$TESTNOTE' im Vault-Hauptordner - taucht sie"
            echo "innerhalb einer Minute auf? Das ist das Fertig-Kriterium von Phase 3."
            echo "Danach die Notiz in Obsidian loeschen (Loeschung laeuft ueber alle Geraete)."
            exit 0
        fi
        sleep 2
    done
    die "nach 60 s keine neue Revision in der echten DB. Logs: docker logs --tail 50 livesync-bridge"
fi

# ============================================================================
if [ "${1:-}" = "aufraeumen" ]; then
    docker ps --format '{{.Names}}' | grep -qx livesync-bridge || die "livesync-bridge laeuft nicht - erst umstellen."
    grep -q 'couchdb-vault-test' compose.yml && die "compose.yml enthaelt noch die Replik - erst umstellen."
    echo ">> Loesche Container couchdb-vault-test und die Testordner:"
    echo "   /mnt/data/llm/vault-test-couchdb  /mnt/data/llm/vault-test"
    read -r -p "Weiter? (ja) " a; [ "$a" = "ja" ] || die "nicht bestaetigt."
    docker rm -f couchdb-vault-test 2>/dev/null || true
    sudo rm -rf --one-file-system /mnt/data/llm/vault-test-couchdb /mnt/data/llm/vault-test
    sed -i '/^# Phase 3: Admin der Wegwerf-Replik/d; /^VAULT_TEST_COUCHDB_PASSWORD=/d' .env
    echo "OK. Snapshot bleibt in $SNAP_DIR - loeschen, wenn alles eine Woche stabil lief."
    exit 0
fi

# ============================================================================
if [ "${1:-}" = "zurueck" ]; then
    echo ">> Stoppe livesync-bridge (danach wirkt nichts mehr auf den Vault)"
    docker rm -f livesync-bridge 2>/dev/null || true
    cb=$(ls -t compose.yml.bak-03-* 2>/dev/null | head -1 || true)
    vb=$(ls -t vault.env.bak-03-* 2>/dev/null | head -1 || true)
    [ -n "$cb" ] && cp -p "$cb" compose.yml && echo "   compose.yml <- $cb"
    [ -n "$vb" ] && cp -p "$vb" vault.env && echo "   vault.env   <- $vb"
    echo
    echo "Die Bridge laeuft nicht mehr. Der CouchDB-User '$BRIDGE_USER' bleibt bestehen (harmlos)."
    echo "Nur falls der Vault selbst beschaedigt ist, den Snapshot zurueckspielen:"
    echo "  docker stop obsidian-livesync"
    echo "  sudo mv $OBS_DIR/data $OBS_DIR/data.kaputt-\$(date +%s)"
    echo "  sudo tar -C $OBS_DIR -I zstd -xpf $(ls -t $SNAP_DIR/obsidian-data-*.tar.zst 2>/dev/null | head -1 || echo '<snapshot>')"
    echo "  docker start obsidian-livesync"
    exit 0
fi

[ -z "${1:-}" ] || die "unbekanntes Argument '$1' (umstellen ohne Argument | schreibtest | aufraeumen | zurueck)"

# ============================================================================
# 1) Vorpruefungen - hier wird noch NICHTS geschrieben
# ============================================================================
echo ">> Vorpruefungen"
[ "$(sha256sum compose.yml | cut -d' ' -f1)" = "$COMPOSE_SHA_ERWARTET" ] \
    || die "compose.yml wurde seit der Vorbereitung geaendert - $COMPOSE_NEU passt nicht mehr dazu."
docker compose -f "$COMPOSE_NEU" --project-directory . config -q || die "$COMPOSE_NEU ist ungueltig."
[ "$(grep -c '^VAULT_COUCHDB_URL=http://couchdb-vault-test:5984$' vault.env)" = 1 ] || die "vault.env: URL-Zeile nicht wie erwartet."
[ "$(grep -c '^VAULT_COUCHDB_USER=admin$' vault.env)" = 1 ] || die "vault.env: USER-Zeile nicht wie erwartet."
[ "$(grep -c '^VAULT_COUCHDB_PASSWORD=' vault.env)" = 1 ] || die "vault.env: PASSWORD-Zeile nicht wie erwartet."
grep -qE '^VAULT_PASSPHRASE=.+' vault.env || die "vault.env: VAULT_PASSPHRASE leer."
[ -n "$COUCHDB_USER" ] && [ -n "$COUCHDB_PASSWORD" ] || die "Admin-Zugang aus $OBS_DIR/.env nicht lesbar."
docker image inspect localhost/livesync-bridge:1c25230 >/dev/null 2>&1 || die "Image fehlt - erst setup/02-vault-bridge-test.sh."
pc "$P/$DB" | grep -q '"doc_count"' || die "echte CouchDB nicht erreichbar oder Login falsch."
sec=$(pc "$P/$DB/_security")
[ "$sec" = "$SECURITY_ALT" ] || die "_security von $DB ist nicht wie erwartet: $sec"
if [ -d "$VAULT_DIR" ] && [ -n "$(sudo find "$VAULT_DIR" -mindepth 1 -print -quit)" ]; then
    die "$VAULT_DIR ist nicht leer - eine leere Bridge-Zielablage ist Pflicht (sonst Upload)."
fi
frei=$(df --output=avail -BG /mnt/data | tail -1 | tr -dc 0-9)
[ "$frei" -ge 10 ] || die "zu wenig Platz auf /mnt/data fuer den Snapshot (${frei} GB)."
echo "   alles wie erwartet."
echo
echo "Jetzt passiert:"
echo "  1. Test-Bridge + Replik stoppen"
echo "  2. obsidian-livesync ~1 min stoppen, Snapshot nach $SNAP_DIR (Geraete synchronisieren kurz nicht)"
echo "  3. CouchDB: _users anlegen, Nicht-Admin '$BRIDGE_USER' anlegen, als Mitglied von $DB eintragen"
echo "  4. vault.env + compose.yml umstellen (Sicherungen *.bak-03-$TS)"
echo "  5. Bridge gegen den echten Vault starten, Ordner $VAULT_DIR fuellen lassen"
read -r -p "Weiter? (ja) " a; [ "$a" = "ja" ] || die "nicht bestaetigt."

# ============================================================================
# 2) Test-Bridge und Replik stoppen
# ============================================================================
echo ">> Stoppe Test-Bridge und Replik"
docker rm -f livesync-bridge >/dev/null 2>&1 || true
docker stop couchdb-vault-test >/dev/null 2>&1 || true

# ============================================================================
# 3) Snapshot (kalt: CouchDB gestoppt, damit die Dateien konsistent sind)
# ============================================================================
SNAP="$SNAP_DIR/obsidian-data-$TS.tar.zst"
echo ">> Snapshot -> $SNAP"
sudo mkdir -p "$SNAP_DIR"
trap 'docker start obsidian-livesync >/dev/null; echo "(obsidian-livesync wieder gestartet)"' EXIT
docker stop obsidian-livesync >/dev/null
sudo tar -C "$OBS_DIR" -I 'zstd -T0 -3' -cpf "$SNAP" data
docker start obsidian-livesync >/dev/null
trap - EXIT
sudo zstd -tq "$SNAP" || die "Snapshot defekt: $SNAP"
n=$(sudo tar -I zstd -tf "$SNAP" | wc -l)
[ "$n" -gt 3 ] || die "Snapshot enthaelt nur $n Eintraege."
echo "   OK: $(sudo du -h "$SNAP" | cut -f1), $n Eintraege."
for i in $(seq 30); do pc "$P/_up" | grep -q ok && break; sleep 2; done
pc "$P/_up" | grep -q ok || die "obsidian-livesync kommt nach dem Snapshot nicht hoch!"

# ============================================================================
# 4) CouchDB-Konto fuer die Bridge (kein Admin)
# ============================================================================
echo ">> CouchDB-Konto '$BRIDGE_USER'"
code=$(pc -o /dev/null -w '%{http_code}' -X PUT "$P/_users")
case "$code" in 201|202|412) ;; *) die "_users anlegen: HTTP $code";; esac
# Kein "tr </dev/urandom | head": tr stirbt an SIGPIPE, mit pipefail beendet
# set -e dann das Skript stumm (genau so am 2026-11-13 passiert).
BPW=$(head -c 20 /dev/urandom | od -An -tx1 | tr -d ' \n')
[ "${#BPW}" = 40 ] || die "Passwort-Erzeugung fehlgeschlagen."
code=$(pc -o /dev/null -w '%{http_code}' -X PUT "$P/_users/org.couchdb.user:$BRIDGE_USER" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$BRIDGE_USER\",\"password\":\"$BPW\",\"roles\":[],\"type\":\"user\"}")
[ "$code" = 201 ] || [ "$code" = 202 ] || die "User anlegen: HTTP $code (existiert er schon? dann in _users loeschen und neu starten)"
# _admin-Regeln bleiben unveraendert - Geraete (Admin-Login) merken nichts.
code=$(pc -o /dev/null -w '%{http_code}' -X PUT "$P/$DB/_security" -H 'Content-Type: application/json' \
    -d "{\"members\":{\"names\":[\"$BRIDGE_USER\"],\"roles\":[\"_admin\"]},\"admins\":{\"roles\":[\"_admin\"]}}")
[ "$code" = 200 ] || die "_security setzen: HTTP $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -u "$BRIDGE_USER:$BPW" "$P/$DB")
[ "$code" = 200 ] || die "Bridge-User kann $DB nicht lesen (HTTP $code)."
code=$(curl -s -o /dev/null -w '%{http_code}' -u "$BRIDGE_USER:$BPW" "$P/_node/_local/_config")
[ "$code" = 401 ] || [ "$code" = 403 ] || die "Bridge-User hat Admin-Rechte?! (_config: HTTP $code)"
echo "   OK: liest $DB, kein Zugriff auf _config."

# ============================================================================
# 5) vault.env + compose.yml umstellen
# ============================================================================
echo ">> vault.env und compose.yml umstellen"
cp -p vault.env "vault.env.bak-03-$TS"
cp -p compose.yml "compose.yml.bak-03-$TS"
sed -i \
    -e 's|^VAULT_COUCHDB_URL=http://couchdb-vault-test:5984$|VAULT_COUCHDB_URL=http://obsidian-livesync:5984|' \
    -e "s|^VAULT_COUCHDB_USER=admin\$|VAULT_COUCHDB_USER=$BRIDGE_USER|" \
    -e "s|^VAULT_COUCHDB_PASSWORD=.*\$|VAULT_COUCHDB_PASSWORD=$BPW|" \
    -e 's|^# TESTPHASE:.*|# ECHTER VAULT seit '"$TS"' (setup/03-vault-bridge-prod.sh).|' \
    vault.env
grep -q '^VAULT_COUCHDB_URL=http://obsidian-livesync:5984$' vault.env || die "vault.env-Patch fehlgeschlagen (Sicherung: vault.env.bak-03-$TS)."
cp "$COMPOSE_NEU" compose.yml

# ============================================================================
# 6) Ordner vorbereiten und Bridge starten
# ============================================================================
echo ">> Ordner vorbereiten"
sudo mkdir -p "$VAULT_DIR" "$STATE_DIR"
# Resume-Stand der Replik gilt nicht fuer die echte DB -> leeren.
[ "$STATE_DIR" = /mnt/data/llm/livesync-bridge-state ] && sudo find "$STATE_DIR" -mindepth 1 -delete
sudo chown 1993:1993 "$VAULT_DIR" "$STATE_DIR"

seq_vorher=$(pc "$P/$DB" | grep -o '"update_seq":"[0-9]*' | grep -o '[0-9]*$')
echo ">> Starte Bridge gegen den echten Vault"
docker compose up -d livesync-bridge
echo ">> Warte, bis die Dateien da sind (max. 15 min) ..."
prev=-1
for i in $(seq 90); do
    sleep 10
    c=$(sudo find "$VAULT_DIR" -type f | wc -l)
    echo "   $(date +%T)  $c Dateien"
    if [ "$c" -gt 0 ] && [ "$c" = "$prev" ]; then break; fi
    prev=$c
done
seq_nachher=$(pc "$P/$DB" | grep -o '"update_seq":"[0-9]*' | grep -o '[0-9]*$')
aktiv=$(pc "$P/$DB/_all_docs?include_docs=true" | nix-shell -p jq --run \
    "jq '[.rows[].doc | select((.type==\"plain\" or .type==\"newnote\") and .deleted!=true)] | length'" 2>/dev/null)
fehler=$(docker logs livesync-bridge 2>&1 | grep -iE 'corrupt|decrypt|failed|error' \
    | grep -vcE 'delete failed|No such file or directory \(os error 2\): remove' || true)

echo
echo ">> Ergebnis"
echo "   Dateien im Ordner:        $(sudo find "$VAULT_DIR" -type f | wc -l)  (aktive Eintraege in der DB: $aktiv)"
echo "   Wochenplanungen:          $(sudo find "$VAULT_DIR" -type f -iname 'wochenplanung kw *.md' | wc -l)"
echo "   Echte Fehler im Log:      $fehler  (geloeschte Dateien 'delete failed' sind herausgefiltert)"
echo "   Neue DB-Aenderungen beim Erstabgleich: $((seq_nachher - seq_vorher))  (erwartet: 0 bis wenige; viele = Echo-Writes!)"
echo "   Container:                $(docker ps --filter name=livesync-bridge --format '{{.Status}}')"
echo
echo "Sicherungen: compose.yml.bak-03-$TS, vault.env.bak-03-$TS, $SNAP"
echo "Naechster Schritt: $0 schreibtest   (danach: $0 aufraeumen)"
echo "Notbremse:         $0 zurueck"
