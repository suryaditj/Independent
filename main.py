"""
Bot sinyal XAUUSD -> Telegram.
Alur: ambil candle -> hitung indikator -> tentukan sinyal -> (opsional) alasan dari AI -> kirim Telegram.
Hanya butuh library `requests`.
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

# ---------- KONFIGURASI (dari GitHub Secrets / environment) ----------
TWELVE_KEY = os.environ.get("TWELVE_DATA_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GROQ_KEY = os.environ.get("GROQ_KEY", "")  # opsional

SYMBOL = "XAU/USD"
INTERVAL = "5min"
COOLDOWN_MENIT = 60      # jangan kirim arah yang sama lagi dalam rentang ini
MAX_UMUR_DATA_MENIT = 15  # data lebih tua dari ini dianggap pasar tutup
STATE_FILE = "state.json"


# ---------- 1. AMBIL DATA ----------
def get_candles(symbol=SYMBOL, interval=INTERVAL, n=200):
    """Ambil candle OHLC dari Twelve Data, urut dari lama ke baru."""
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": n,
            "timezone": "UTC",
            "apikey": TWELVE_KEY,
        },
        timeout=20,
    )
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Gagal ambil data: {data}")
    candles = [
        {
            "t": datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
            "o": float(v["open"]),
            "h": float(v["high"]),
            "l": float(v["low"]),
            "c": float(v["close"]),
        }
        for v in data["values"]
    ]
    candles.reverse()  # API mengirim terbaru dulu
    return candles[:-1]  # buang candle yang masih berjalan, pakai candle yang sudah close


# ---------- 2. INDIKATOR ----------
def ema(values, period):
    """Exponential Moving Average."""
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=14):
    """RSI metode Wilder."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(candles, period=14):
    """Average True Range = ukuran rata-rata gerak harga, dipakai untuk SL/TP."""
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


# ---------- 3. LOGIKA SINYAL ----------
def analyze(candles):
    """
    Aturan sederhana (trend following):
    BUY  : EMA20 > EMA50, harga di atas EMA20, RSI 50-70, candle terakhir hijau.
    SELL : kebalikannya (RSI 30-50, candle merah).
    Return dict sinyal, atau None kalau tidak ada setup.
    """
    closes = [c["c"] for c in candles]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    r = rsi(closes)
    a = atr(candles)
    last = candles[-1]
    price = last["c"]

    side = None
    if e20 > e50 and price > e20 and 50 <= r <= 70 and last["c"] > last["o"]:
        side = "BUY"
    elif e20 < e50 and price < e20 and 30 <= r <= 50 and last["c"] < last["o"]:
        side = "SELL"
    if not side:
        return None

    sl_dist = a * 1.2          # jarak SL = 1.2 x ATR
    d = 1 if side == "BUY" else -1
    return {
        "side": side,
        "entry": price,
        "sl": price - d * sl_dist,
        "tp1": price + d * sl_dist * 1.5,  # TP kelipatan 1.5R, 3R, 4.5R
        "tp2": price + d * sl_dist * 3.0,
        "tp3": price + d * sl_dist * 4.5,
        "ema20": e20, "ema50": e50, "rsi": r, "atr": a,
    }


# ---------- 4. ALASAN DARI AI (OPSIONAL) ----------
def ai_reason(sig):
    """Minta LLM gratis (Groq) menulis alasan singkat. Arah sinyal tetap dari aturan di atas."""
    if not GROQ_KEY:
        return ""
    try:
        prompt = (
            f"Sinyal {sig['side']} XAUUSD timeframe 5 menit. EMA20={sig['ema20']:.2f}, "
            f"EMA50={sig['ema50']:.2f}, RSI={sig['rsi']:.1f}, ATR={sig['atr']:.2f}. "
            "Tulis alasan singkat 1-2 kalimat dalam bahasa Indonesia. Jangan menjanjikan profit."
        )
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
            },
            timeout=20,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


# ---------- 5. ANTI SINYAL DOBEL ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(side):
    with open(STATE_FILE, "w") as f:
        json.dump({"side": side, "ts": time.time()}, f)


def boleh_kirim(side):
    s = load_state()
    if s.get("side") == side and time.time() - s.get("ts", 0) < COOLDOWN_MENIT * 60:
        return False
    return True


# ---------- 6. KIRIM TELEGRAM ----------
def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.text}")


def format_pesan(sig, alasan):
    ikon = "🟢" if sig["side"] == "BUY" else "🔴"
    teks = (
        f"{ikon} <b>[SCALPING] XAUUSD {sig['side']}</b>\n\n"
        f"Entry : <code>{sig['entry']:.2f}</code>\n"
        f"SL    : <code>{sig['sl']:.2f}</code>\n"
        f"TP1   : <code>{sig['tp1']:.2f}</code>\n"
        f"TP2   : <code>{sig['tp2']:.2f}</code>\n"
        f"TP3   : <code>{sig['tp3']:.2f}</code>\n\n"
        f"RSI {sig['rsi']:.1f} | ATR {sig['atr']:.2f}\n"
    )
    if alasan:
        teks += f"\n💡 {alasan}\n"
    teks += "\n⚠️ Bukan saran finansial. Uji di akun demo dulu."
    return teks


# ---------- MAIN ----------
def main():
    candles = get_candles()

    umur = (datetime.now(timezone.utc) - candles[-1]["t"]).total_seconds() / 60
    if umur > MAX_UMUR_DATA_MENIT:
        print(f"Data sudah {umur:.0f} menit, pasar kemungkinan tutup. Lewati.")
        return

    sig = analyze(candles)
    if not sig:
        print("Tidak ada setup.")
        return
    if not boleh_kirim(sig["side"]):
        print("Masih cooldown, lewati.")
        return

    send_telegram(format_pesan(sig, ai_reason(sig)))
    save_state(sig["side"])
    print("Sinyal terkirim:", sig["side"], sig["entry"])


if __name__ == "__main__":
    main()
