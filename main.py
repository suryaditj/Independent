"""
Bot sinyal XAUUSD HIBRID (EMA + SMC) -> Telegram.
Alur: ambil candle -> filter tren (EMA) -> cari setup SMC (BOS + Order Block + FVG + retest)
      -> (opsional) alasan dari AI -> kirim Telegram.
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
CANDLES_N = 300

# Parameter strategi (silakan diutak-atik lalu backtest)
SWING_K = 3             # sebuah swing = high/low yang lebih ekstrem dari 3 candle kiri & kanan
LOOKBACK = 60           # swing yang dipakai maksimal berumur 60 candle
MAX_BOS_AGE = 30        # BOS lebih tua dari 30 candle dianggap basi
DISP_ATR = 1.0          # candle BOS harus punya range >= 1.0 x ATR (ada displacement)
REQUIRE_FVG = True      # wajib ada Fair Value Gap setelah order block
SL_BUFFER_ATR = 0.2     # SL = di luar order block + 0.2 x ATR
MAX_RISK_ATR = 3.0      # skip kalau jarak SL > 3 x ATR (terlalu lebar)
RR = (1.0, 2.0, 3.0)    # TP1, TP2, TP3 dalam kelipatan risiko (R)

SESSION_FILTER = True
SESSION_START_UTC = 7   # 07:00 UTC = 14:00 WIB (London buka)
SESSION_END_UTC = 20    # 20:00 UTC = 03:00 WIB (New York tutup)

COOLDOWN_MENIT = 30
MAX_UMUR_DATA_MENIT = 15  # data lebih tua dari ini dianggap pasar tutup
STATE_FILE = "state.json"


# ---------- 1. AMBIL DATA ----------
def get_candles(symbol=SYMBOL, interval=INTERVAL, n=CANDLES_N):
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
    """RSI metode Wilder (hanya ditampilkan sebagai info)."""
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
    """Average True Range = ukuran rata-rata gerak harga, dipakai untuk SL dan filter."""
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


# ---------- 3. KOMPONEN SMC ----------
def swings(candles, k=SWING_K):
    """
    Cari swing high & swing low (indeks candle).
    Swing hanya dianggap valid jika sudah terkonfirmasi k candle setelahnya.
    """
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        h, l = candles[i]["h"], candles[i]["l"]
        if all(h > candles[i - j]["h"] and h > candles[i + j]["h"] for j in range(1, k + 1)):
            highs.append(i)
        if all(l < candles[i - j]["l"] and l < candles[i + j]["l"] for j in range(1, k + 1)):
            lows.append(i)
    return highs, lows


def has_fvg(candles, start, end, side):
    """
    Fair Value Gap (imbalance) pada rentang candle start..end.
    BUY : high candle ke-1 < low candle ke-3 (celah naik).
    SELL: low candle ke-1 > high candle ke-3 (celah turun).
    """
    for k in range(max(start, 2), end + 1):
        if side == "BUY" and candles[k - 2]["h"] < candles[k]["l"]:
            return True
        if side == "SELL" and candles[k - 2]["l"] > candles[k]["h"]:
            return True
    return False


def find_setup(candles, side, a):
    """
    Cari satu setup SMC searah tren:
      1. BOS  : candle close menembus swing high (BUY) / swing low (SELL) terbaru, dengan displacement kuat.
      2. OB   : candle berlawanan terakhir sebelum BOS (zona order block).
      3. FVG  : ada imbalance di antara OB dan BOS (jika REQUIRE_FVG).
      4. Zona masih fresh (belum pernah disentuh sejak BOS).
      5. Retest: candle terakhir menyentuh zona lalu ditutup menolak (rejection).
    Return dict setup, atau None.
    """
    n = len(candles)
    last = candles[-1]
    highs, lows = swings(candles)
    pts = highs if side == "BUY" else lows

    for si in reversed(pts):
        if n - 1 - si > LOOKBACK:
            break
        level = candles[si]["h"] if side == "BUY" else candles[si]["l"]

        # 1. cari candle BOS (candle terakhir tidak dihitung, itu candle retest)
        bos = None
        for j in range(si + 1, n - 1):
            c = candles[j]["c"]
            if (side == "BUY" and c > level) or (side == "SELL" and c < level):
                bos = j
                break
        if bos is None or n - 1 - bos > MAX_BOS_AGE:
            continue
        if candles[bos]["h"] - candles[bos]["l"] < DISP_ATR * a:
            continue  # tembusan lemah, bukan displacement

        # 2. order block = candle berlawanan terakhir sebelum BOS
        ob = None
        for m in range(bos - 1, max(bos - 12, 0), -1):
            cm = candles[m]
            if (side == "BUY" and cm["c"] < cm["o"]) or (side == "SELL" and cm["c"] > cm["o"]):
                ob = m
                break
        if ob is None:
            continue
        ob_hi, ob_lo = candles[ob]["h"], candles[ob]["l"]

        # 3. FVG sebagai konfirmasi
        if REQUIRE_FVG and not has_fvg(candles, ob + 1, bos, side):
            continue

        # 4. zona harus fresh: belum disentuh antara BOS dan candle terakhir
        touched = False
        for m in range(bos + 1, n - 1):
            if side == "BUY" and candles[m]["l"] <= ob_hi:
                touched = True
                break
            if side == "SELL" and candles[m]["h"] >= ob_lo:
                touched = True
                break
        if touched:
            continue

        # 5. retest + rejection pada candle terakhir
        if side == "BUY":
            retest = last["l"] <= ob_hi and last["c"] > ob_lo and last["c"] > last["o"]
        else:
            retest = last["h"] >= ob_lo and last["c"] < ob_hi and last["c"] < last["o"]
        if not retest:
            continue

        entry = last["c"]
        if side == "BUY":
            sl = ob_lo - SL_BUFFER_ATR * a
            risk = entry - sl
        else:
            sl = ob_hi + SL_BUFFER_ATR * a
            risk = sl - entry
        if risk <= 0 or risk > MAX_RISK_ATR * a:
            continue

        d = 1 if side == "BUY" else -1
        return {
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": entry + d * risk * RR[0],
            "tp2": entry + d * risk * RR[1],
            "tp3": entry + d * risk * RR[2],
            "risk": risk,
            "bos_level": level,
            "ob_lo": ob_lo,
            "ob_hi": ob_hi,
            "key": f"{side}-{candles[ob]['t'].isoformat()}",
        }
    return None


# ---------- 4. LOGIKA SINYAL HIBRID ----------
def analyze(candles):
    """
    Hibrid: EMA menentukan ARAH, SMC menentukan TITIK ENTRY.
      - Tren naik  (EMA20 > EMA50) -> hanya cari setup BUY.
      - Tren turun (EMA20 < EMA50) -> hanya cari setup SELL.
    """
    closes = [c["c"] for c in candles]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    a = atr(candles)

    if e20 > e50:
        side = "BUY"
    elif e20 < e50:
        side = "SELL"
    else:
        return None

    sig = find_setup(candles, side, a)
    if not sig:
        return None
    sig.update({"ema20": e20, "ema50": e50, "rsi": rsi(closes), "atr": a})
    return sig


# ---------- 5. ALASAN ----------
def alasan_aturan(sig):
    """Alasan dari aturan itu sendiri (selalu akurat, tidak bergantung AI)."""
    arah = "naik" if sig["side"] == "BUY" else "turun"
    return (
        f"Tren {arah} (EMA20 vs EMA50). Harga menembus struktur di {sig['bos_level']:.2f} (BOS), "
        f"lalu retest order block {sig['ob_lo']:.2f}-{sig['ob_hi']:.2f} dengan konfirmasi FVG dan rejection."
    )


def ai_reason(sig):
    """Opsional: LLM gratis (Groq) menulis catatan risiko singkat. Arah sinyal tetap dari aturan."""
    if not GROQ_KEY:
        return ""
    try:
        prompt = (
            f"Setup {sig['side']} XAUUSD 5 menit: BOS di {sig['bos_level']:.2f}, retest order block "
            f"{sig['ob_lo']:.2f}-{sig['ob_hi']:.2f}, RSI={sig['rsi']:.1f}, ATR={sig['atr']:.2f}. "
            "Tulis 1 kalimat catatan risiko dalam bahasa Indonesia. Jangan menjanjikan profit."
        )
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100,
            },
            timeout=20,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


# ---------- 6. ANTI SINYAL DOBEL ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(key):
    with open(STATE_FILE, "w") as f:
        json.dump({"key": key, "ts": time.time()}, f)


def boleh_kirim(key):
    """Tolak jika order block yang sama sudah pernah dikirim, atau masih dalam cooldown."""
    s = load_state()
    if s.get("key") == key:
        return False
    if time.time() - s.get("ts", 0) < COOLDOWN_MENIT * 60:
        return False
    return True


# ---------- 7. KIRIM TELEGRAM ----------
def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.text}")


def format_pesan(sig, alasan, catatan_ai):
    ikon = "🟢" if sig["side"] == "BUY" else "🔴"
    teks = (
        f"{ikon} <b>[SMC HYBRID] XAUUSD {sig['side']}</b>\n\n"
        f"Entry : <code>{sig['entry']:.2f}</code>\n"
        f"SL    : <code>{sig['sl']:.2f}</code>\n"
        f"TP1   : <code>{sig['tp1']:.2f}</code> ({RR[0]:g}R)\n"
        f"TP2   : <code>{sig['tp2']:.2f}</code> ({RR[1]:g}R)\n"
        f"TP3   : <code>{sig['tp3']:.2f}</code> ({RR[2]:g}R)\n\n"
        f"OB    : {sig['ob_lo']:.2f} - {sig['ob_hi']:.2f}\n"
        f"RSI {sig['rsi']:.1f} | ATR {sig['atr']:.2f}\n\n"
        f"📌 {alasan}\n"
    )
    if catatan_ai:
        teks += f"\n💡 {catatan_ai}\n"
    teks += "\n⚠️ Bukan saran finansial. Uji di akun demo dulu."
    return teks


# ---------- MAIN ----------
def dalam_sesi(now):
    if now.weekday() >= 5:
        return False
    return SESSION_START_UTC <= now.hour < SESSION_END_UTC


def main():
    now = datetime.now(timezone.utc)
    if SESSION_FILTER and not dalam_sesi(now):
        print("Di luar sesi London/New York, lewati.")
        return

    candles = get_candles()

    umur = (now - candles[-1]["t"]).total_seconds() / 60
    if umur > MAX_UMUR_DATA_MENIT:
        print(f"Data sudah {umur:.0f} menit, pasar kemungkinan tutup. Lewati.")
        return

    sig = analyze(candles)
    if not sig:
        print("Tidak ada setup.")
        return
    if not boleh_kirim(sig["key"]):
        print("Setup sudah dikirim / masih cooldown, lewati.")
        return

    send_telegram(format_pesan(sig, alasan_aturan(sig), ai_reason(sig)))
    save_state(sig["key"])
    print("Sinyal terkirim:", sig["side"], round(sig["entry"], 2))


if __name__ == "__main__":
    main()
