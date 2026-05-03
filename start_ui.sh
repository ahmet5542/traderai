#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  Freqtrade Web Arayüzü Açıcı
#  Kullanım: ./start_ui.sh   ya da   bash start_ui.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail

PIDFILE="/home/ahmet/Desktop/freqtrade/user_data/multi_pids.json"
BASE_PORT=8080

open_url() {
    local url="$1"
    if command -v xdg-open &>/dev/null;        then xdg-open "$url"
    elif command -v sensible-browser &>/dev/null; then sensible-browser "$url"
    elif command -v firefox &>/dev/null;        then firefox "$url" &
    elif command -v chromium-browser &>/dev/null; then chromium-browser "$url" &
    fi
}

wait_for_port() {
    local port="$1"
    echo -n "  :${port} bekleniyor"
    for i in $(seq 1 30); do
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${port}" 2>/dev/null \
                | grep -q "200\|401\|403"; then
            echo " ✓"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo " (zaman asimi)"
}

# ── Strateji seçimi ───────────────────────────────────────────
echo "Strateji secim menusu aciliyor..."
python3 /home/ahmet/Desktop/freqtrade/select_strategy_and_start.py

# ── Multi-mode mi yoksa tek bot mu? ──────────────────────────
# Multi-mode: PIDFILE mevcutsa ve en az bir canlı PID varsa
MULTI_ACTIVE=false
if [ -f "$PIDFILE" ]; then
    PIDS=$(python3 - <<'PYEOF'
import json, os
from pathlib import Path
f = Path("/home/ahmet/Desktop/freqtrade/user_data/multi_pids.json")
if not f.exists():
    print(0)
else:
    data = json.loads(f.read_text())
    alive = 0
    for pid in data.values():
        try:
            os.kill(int(pid), 0)
            alive += 1
        except (ProcessLookupError, OSError):
            pass
    print(alive)
PYEOF
)
    if [ "$PIDS" -gt 0 ] 2>/dev/null; then
        MULTI_ACTIVE=true
    fi
fi

if $MULTI_ACTIVE; then
    # ── Multi-mode: portları oku, hepsini bekle ve aç ─────────
    echo ""
    echo "Coklu instance modu aktif ($PIDS instance calisiyor)"
    echo ""

    # Port listesini Python'dan al
    PORTS=$(python3 - <<'PYEOF'
import json
from pathlib import Path
f = Path("/home/ahmet/Desktop/freqtrade/user_data/multi_pids.json")
data = json.loads(f.read_text())
# config dosyalarından portları oku
cfg_dir = Path("/home/ahmet/Desktop/freqtrade/user_data/multi_configs")
base_cfg = json.loads(Path("/home/ahmet/Desktop/freqtrade/user_data/config.json").read_text())
base_port = base_cfg.get("api_server", {}).get("listen_port", 8080)
for i, strat in enumerate(data.keys()):
    print(base_port + i)
PYEOF
)

    echo "Web arayüzleri bekleniyor..."
    FIRST_PORT=""
    while IFS= read -r port; do
        wait_for_port "$port"
        [ -z "$FIRST_PORT" ] && FIRST_PORT="$port"
    done <<< "$PORTS"

    echo ""
    echo "══════════════════════════════════════════════════════"
    echo "  Kullanıcı adı : admin  |  Şifre : freqtrade2024"
    echo "══════════════════════════════════════════════════════"
    python3 - <<'PYEOF'
import json
from pathlib import Path
cfg_dir = Path("/home/ahmet/Desktop/freqtrade/user_data/multi_configs")
base_port = json.loads(Path("/home/ahmet/Desktop/freqtrade/user_data/config.json").read_text()).get("api_server", {}).get("listen_port", 8080)
pids = json.loads(Path("/home/ahmet/Desktop/freqtrade/user_data/multi_pids.json").read_text())
for i, strat in enumerate(pids.keys()):
    suffix = strat.split("RelaxedHTF")[-1] or "Base"
    print(f"  http://127.0.0.1:{base_port+i}  →  {suffix}")
PYEOF
    echo "══════════════════════════════════════════════════════"
    echo "  Durdurmak icin: ./start_ui.sh  →  's' secenegi"
    echo "══════════════════════════════════════════════════════"

    # İlk instance'ı tarayıcıda aç
    open_url "http://127.0.0.1:${FIRST_PORT:-$BASE_PORT}"

else
    # ── Tek bot modu ──────────────────────────────────────────
    URL="http://127.0.0.1:${BASE_PORT}"

    if ! systemctl --user is-active --quiet freqtrade; then
        echo "Bot servisi calismiyor, baslatiliyor..."
        systemctl --user start freqtrade
    fi

    echo "Web arayüzü bekleniyor"
    wait_for_port "$BASE_PORT"

    echo "Tarayıcı açılıyor → $URL"
    open_url "$URL"

    echo ""
    echo "══════════════════════════════════════════════"
    echo "  Kullanıcı adı : admin"
    echo "  Şifre         : freqtrade2024"
    echo "  Adres         : $URL"
    echo "══════════════════════════════════════════════"
    echo "  Bot durumu    : systemctl --user status freqtrade"
    echo "  Durdur        : systemctl --user stop freqtrade"
    echo "  Log izle      : journalctl --user -u freqtrade -f"
    echo "══════════════════════════════════════════════"
fi
