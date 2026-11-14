#!/usr/bin/env bash
# LLM-Stack, Phase 2: LiteLLM ins Tailnet (https://<host>.<tailnet>.ts.net:8454)
#
#   sudo bash ~/docker/llm-stack/setup/01-tailscale-serve-litellm.sh
#
# Fuegt in /etc/nixos/configuration.nix einen tailscale-serve-litellm-Block
# direkt nach dem Navidrome-Block ein (Marker "# NAVIDROME-ENDE"), sichert die
# Datei vorher und fuehrt nixos-rebuild switch aus.
# Bricht ab, BEVOR etwas geschrieben wird, wenn eine Annahme nicht stimmt.
set -euo pipefail

CFG=/etc/nixos/configuration.nix
MARKER='  # NAVIDROME-ENDE'
PORT=8454

[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausfuehren."; exit 1; }

n=$(grep -cxF "$MARKER" "$CFG" || true)
[[ "$n" == "1" ]] || { echo "ABBRUCH: Marker '$MARKER' kommt ${n}x vor (erwartet 1)."; exit 1; }
if grep -q 'tailscale-serve-litellm' "$CFG"; then echo "ABBRUCH: tailscale-serve-litellm existiert bereits."; exit 1; fi
if grep -q -- "--https=$PORT " "$CFG"; then echo "ABBRUCH: Port $PORT ist in $CFG schon vergeben."; exit 1; fi
if tailscale serve status 2>/dev/null | grep -q ":$PORT "; then echo "ABBRUCH: Port $PORT ist in tailscale serve schon aktiv."; exit 1; fi
curl -fsS -o /dev/null http://127.0.0.1:4000/health/liveliness || { echo "ABBRUCH: LiteLLM antwortet nicht auf 127.0.0.1:4000."; exit 1; }

BAK="$CFG.bak-llm-litellm-$(date +%Y%m%d-%H%M%S)"
cp -a "$CFG" "$BAK"
echo "Backup: $BAK"

BLOCK=$(cat <<'EOF'

  # LLM-STACK: LiteLLM-Gateway (OpenAI-kompatibel, Master-Key-geschuetzt) -> nur Tailnet
  systemd.services.tailscale-serve-litellm = {
    description = "Tailscale Serve for LiteLLM";
    after = [ "tailscaled.service" ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.bash}/bin/bash -c 'n=0; until ${pkgs.tailscale}/bin/tailscale status >/dev/null 2>&1 || [ $n -ge 40 ]; do n=$((n+1)); sleep 2; done; for v in 1 2 3 4 5 6 7 8 9 10; do ${pkgs.tailscale}/bin/tailscale serve --bg --https=8454 http://localhost:4000 && exit 0; sleep 3; done; exit 1'";
    };
  };
  # LLM-STACK-ENDE
EOF
)

TMP=$(mktemp)
awk -v marker="$MARKER" -v block="$BLOCK" '{ print } $0 == marker { print block }' "$CFG" > "$TMP"
grep -q 'tailscale-serve-litellm' "$TMP" || { echo "ABBRUCH: Einfuegen fehlgeschlagen."; rm -f "$TMP"; exit 1; }
cat "$TMP" > "$CFG"; rm -f "$TMP"
echo "configuration.nix gepatcht."

if ! nixos-rebuild switch; then
  echo "nixos-rebuild FEHLGESCHLAGEN - stelle Backup wieder her."
  cp -a "$BAK" "$CFG"
  exit 1
fi

sleep 3
if tailscale serve status | grep -q ":$PORT"; then
  echo "OK: https://<host>.<tailnet>.ts.net:$PORT -> http://localhost:4000"
else
  echo "WARNUNG: Port $PORT taucht in 'tailscale serve status' nicht auf - systemctl status tailscale-serve-litellm"
fi
