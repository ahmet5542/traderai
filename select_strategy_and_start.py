#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import time
from pathlib import Path


ROOT        = Path("/home/ahmet/Desktop/freqtrade")
CONFIG_PATH = ROOT / "user_data/config.json"
RESULTS_DIR = ROOT / "user_data/backtest_results"
MULTI_DIR   = ROOT / "user_data/multi_configs"   # per-strategy configs
PIDFILE     = ROOT / "user_data/multi_pids.json"  # {strategy: pid}
FREQTRADE   = ROOT / ".venv/bin/freqtrade"
BASE_PORT   = 8080   # tek bot buradan başlar; çoklu modda 8081, 8082 …

# Kabul kriterleri
MIN_PROFITABLE_FOLDS = 2   # en az 2/3 fold kârlı olsun
MAX_DRAWDOWN         = 20  # %20 üzeri DD olan stratejiler çıkarılsın
MIN_PROFIT           = 20  # 20 USDC altı kâr kabul edilmez


def short_name(strategy: str) -> str:
    """RelaxedHTF sonrasını döndür; boşsa 'Base' kullan."""
    suffix = strategy.split("RelaxedHTF")[-1]
    return suffix if suffix else "Base"


# ─── veri yükleme ─────────────────────────────────────────────────────────────

def load_candidates() -> list[dict]:
    """Tüm summary JSON'lardan birleştir, filtrele, sırala."""
    best: dict[str, dict] = {}

    for f in sorted(RESULTS_DIR.glob("*summary*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for row in data.get("summary", []):
            name   = row["strategy"]
            profit = row.get("profit_abs", 0)
            dd     = row.get("max_drawdown_pct", 999)
            folds  = row.get("profitable_folds", "0/0")
            try:
                ok_folds = int(str(folds).split("/")[0]) >= MIN_PROFITABLE_FOLDS
            except Exception:
                ok_folds = False

            # Filtrele
            if profit <= MIN_PROFIT:
                continue
            if dd > MAX_DRAWDOWN:
                continue
            if not ok_folds:
                continue

            # Aynı strateji birden fazla dosyada varsa en iyi kârı sakla
            if name not in best or profit > best[name]["profit_abs"]:
                best[name] = {
                    "strategy":         name,
                    "profit_abs":       profit,
                    "profit_pct":       row.get("profit_pct", 0),
                    "profitable_folds": folds,
                    "max_drawdown_pct": dd,
                    "avg_profit_factor":row.get("avg_profit_factor", row.get("profit_factor", 0)),
                    "trades":           row.get("trades", 0),
                    "avg_winrate":      row.get("avg_winrate", row.get("winrate", 0)),
                }

    rows = sorted(best.values(), key=lambda x: x["profit_abs"], reverse=True)

    candidates = []
    for row in rows:
        candidates.append({
            "strategy": row["strategy"],
            "label": (
                f"{row['strategy']} | tahmini toplam: {row['profit_pct']:.2f}% "
                f"({row['profit_abs']:.1f} USDC) | fold: {row['profitable_folds']} | "
                f"DD: {row['max_drawdown_pct']:.2f}% | trade: {row['trades']}"
            ),
        })
    return candidates


def current_strategy() -> str:
    with CONFIG_PATH.open() as f:
        return json.load(f).get("strategy", "")


# ─── seçim menüsü ─────────────────────────────────────────────────────────────

def choose_strategy(candidates: list[dict], active: str) -> str | None:
    """
    Döndürür:
      str  → tek strateji seçildi
      None → "hepsi birlikte" seçildi
    """
    running = _running_pids()

    print("")
    print("Strateji Secimi")
    print("================")
    print(f"Mevcut secili strateji: {active}")
    if running:
        print(f"Suanda calistirilmakta olan coklu instance: {len(running)} adet")
    print("")
    for idx, item in enumerate(candidates, start=1):
        marker = " [aktif]" if item["strategy"] == active else ""
        print(f"{idx}. {item['label']}{marker}")
    print("")
    print("0  = Hepsi ayni anda (coklu instance)")
    if running:
        print("s  = Tum coklu instance'lari durdur")
    print("Enter = aktif strateji ile devam et")
    raw = input("Secim numarasi: ").strip().lower()

    if not raw:
        return active
    if raw == "0":
        return None          # "hepsi" sinyali
    if raw == "s":
        stop_all_multi()
        return active        # durdurup ana stratejiyle devam et
    selected = int(raw)
    if selected < 1 or selected > len(candidates):
        raise ValueError("Gecersiz secim")
    return candidates[selected - 1]["strategy"]


# ─── tek bot (systemd) ────────────────────────────────────────────────────────

def write_strategy(strategy: str) -> None:
    with CONFIG_PATH.open() as f:
        config = json.load(f)
    config["strategy"] = strategy
    CONFIG_PATH.write_text(json.dumps(config, indent=4) + "\n")


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def start_single(strategy: str) -> None:
    write_strategy(strategy)
    print(f"\nSecilen strateji: {strategy}")
    print("Servis yeniden yukleniyor ve baslatiliyor...")
    systemctl("daemon-reload")
    systemctl("restart", "freqtrade")
    print("Tamam.")


# ─── çoklu instance ───────────────────────────────────────────────────────────

def _make_config(strategy: str, port: int) -> Path:
    """Base config'den kopyalayıp strategy/port/log'u güncelle, dosyaya yaz."""
    MULTI_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)

    cfg["strategy"]                       = strategy
    cfg["api_server"]["listen_port"]      = port
    cfg["api_server"]["jwt_secret_key"]   = f"multi_{strategy}_{port}"
    # Her instance kendi loguna yazar
    short = short_name(strategy)
    cfg.setdefault("api_server", {})
    log_path = ROOT / f"user_data/logs/multi_{short}.log"
    cfg["logfile"] = str(log_path)

    out = MULTI_DIR / f"{short}.json"
    out.write_text(json.dumps(cfg, indent=4) + "\n")
    return out


def _save_pids(pids: dict[str, int]) -> None:
    PIDFILE.write_text(json.dumps(pids, indent=2))


def _running_pids() -> dict[str, int]:
    if not PIDFILE.exists():
        return {}
    try:
        data: dict[str, int] = json.loads(PIDFILE.read_text())
    except Exception:
        return {}
    # Sadece gerçekten çalışan PID'leri döndür
    alive = {}
    for strat, pid in data.items():
        try:
            os.kill(pid, 0)   # sinyal 0 = sadece var mı diye kontrol
            alive[strat] = pid
        except ProcessLookupError:
            pass
    return alive


def start_all_multi(candidates: list[dict]) -> None:
    """Ana systemd servisini durdur, her strateji için ayrı süreç başlat."""
    running = _running_pids()
    if running:
        print(f"\n{len(running)} instance zaten calisiyor. Durduruluyor ve yeniden baslatiliyor...")
        stop_all_multi()
        time.sleep(2)

    print("\nAna freqtrade servisi durduruluyor...")
    try:
        systemctl("stop", "freqtrade")
    except subprocess.CalledProcessError:
        pass   # zaten duruyorsa önemli değil

    pids: dict[str, int] = {}
    for i, item in enumerate(candidates):
        strategy = item["strategy"]
        port     = BASE_PORT + i          # 8080, 8081, 8082 …
        cfg_file = _make_config(strategy, port)
        short    = short_name(strategy)

        cmd = [
            str(FREQTRADE), "trade",
            "--config", str(cfg_file),
        ]
        log_path = ROOT / f"user_data/logs/multi_{short}.log"
        log_fd   = open(log_path, "a")
        proc     = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,   # SIGHUP'tan bağımsız
        )
        pids[strategy] = proc.pid
        print(f"  [{i+1}/{len(candidates)}] {short:<30} port:{port}  PID:{proc.pid}")
        time.sleep(1)   # başlatmaları biraz sıraya sok

    _save_pids(pids)
    print(f"\n{len(pids)} instance baslatildi.")
    print("Web arayuzleri:")
    for i, item in enumerate(candidates):
        port = BASE_PORT + i
        print(f"  http://127.0.0.1:{port}  →  {item['strategy'].split('RelaxedHTF')[-1]}")
    print(f"\nTum loglar: user_data/logs/multi_*.log")
    print("Durdurmak icin: ./start_ui.sh  →  's' secenegi")


def stop_all_multi() -> None:
    running = _running_pids()
    if not running:
        print("Calisan coklu instance bulunamadi.")
    else:
        print(f"\n{len(running)} instance durduruluyor...")
        for strat, pid in running.items():
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"  SIGTERM → PID {pid}  ({strat.split('RelaxedHTF')[-1]})")
            except ProcessLookupError:
                pass
        PIDFILE.write_text("{}")
        time.sleep(2)
        print("Tum instance'lar durduruldu.")

    print("\nAna freqtrade servisi yeniden baslatiliyor...")
    systemctl("daemon-reload")
    systemctl("restart", "freqtrade")
    print("Ana servis hazir.")


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    candidates = load_candidates()
    active     = current_strategy()

    selected = choose_strategy(candidates, active)

    if selected is None:
        # Hepsi aynı anda
        start_all_multi(candidates)
    else:
        start_single(selected)


if __name__ == "__main__":
    main()
