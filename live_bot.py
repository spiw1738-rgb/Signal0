#!/usr/bin/env python3
"""
Live Price Alert Bot (Telegram)
--------------------------------
- پوزیشن‌هات رو مستقیم تو تلگرام برای ربات می‌نویسی:
      XAUUSD BUY 4000 3990 4020
- ربات هر ~۱۵ ثانیه قیمت رو چک می‌کنه و وقتی نزدیک ورود شد / به ورود رسید /
  به تی‌پی یا استاپ خورد بهت پیام می‌ده.
- دستورها: /list  /del 2  /clear  /price XAUUSD  /help

⚠️ فقط هشدار می‌ده؛ هیچ معامله‌ای باز/بسته نمی‌کنه. توصیه‌ی مالی نیست.
"""

import csv
import html
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

DATA_FILE = "data.json"
PRICE_INTERVAL = float(os.environ.get("PRICE_INTERVAL", "15"))
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

SIDE_MAP = {"BUY": "BUY", "LONG": "BUY", "خرید": "BUY",
            "SELL": "SELL", "SHORT": "SELL", "فروش": "SELL"}
ALIASES = {"طلا": "XAUUSD", "گلد": "XAUUSD", "GOLD": "XAUUSD", "XAU": "XAUUSD",
           "پوند": "GBPUSD", "GBP": "GBPUSD", "اتریوم": "ETHUSD", "ETH": "ETHUSD",
           "بیتکوین": "BTCUSD", "BTC": "BTCUSD"}
CRYPTO_BASES = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LTC", "AVAX"]
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫،", "01234567890123456789. ")

STAGE_LABEL = {"waiting": "⏳ منتظر ورود", "near": "🔔 نزدیک ورود",
               "entered": "🎯 وارد شده", "done": "🏁 تمام شد"}

HELP = (
    "👋 <b>ربات هشدار قیمت</b>\n\n"
    "برای ثبت پوزیشن همین فرمت رو بفرست:\n"
    "<code>XAUUSD BUY 4000 3990 4020</code>\n"
    "(نماد، جهت، ورود، استاپ، تی‌پی)\n"
    "می‌تونی بنویسی «طلا خرید ...» یا «پوند فروش ...» هم.\n\n"
    "دستورها:\n"
    "/list — پوزیشن‌های ثبت‌شده\n"
    "/del 2 — حذف پوزیشن شماره‌ی ۲\n"
    "/clear — پاک‌کردن پوزیشن‌های تمام‌شده\n"
    "/price XAUUSD — قیمت الان"
)


# ---------------------------------------------------------------------------
# قیمت (چند منبع پشتیبان)
# ---------------------------------------------------------------------------
def _ok(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def price_cryptocompare(sym):
    base = next((b for b in CRYPTO_BASES if sym.startswith(b)), None)
    if not base:
        return None
    headers = dict(HEADERS)
    key = os.environ.get("CRYPTOCOMPARE_API_KEY")
    if key:
        headers["authorization"] = f"Apikey {key}"
    r = requests.get("https://min-api.cryptocompare.com/data/price",
                     params={"fsym": base, "tsyms": "USD"}, headers=headers, timeout=15)
    r.raise_for_status()
    return _ok(r.json().get("USD"))


def price_goldapi(sym):
    if sym[:3] not in ("XAU", "XAG"):
        return None
    r = requests.get(f"https://api.gold-api.com/price/{sym[:3]}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return _ok(r.json().get("price"))


def price_stooq(sym):
    r = requests.get("https://stooq.com/q/l/", params={"s": sym.lower(), "f": "sd2t2c", "h": "", "e": "csv"},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.text)))
    if len(rows) < 2 or len(rows[1]) < 4:
        return None
    return _ok(rows[1][3])


def price_yahoo(sym):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}=X",
                     params={"interval": "1m", "range": "1d"}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return _ok(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


def get_price(symbol):
    """(قیمت, اسم منبع) یا (None, None)"""
    if any(symbol.startswith(b) for b in CRYPTO_BASES):
        sources = [("CryptoCompare", price_cryptocompare)]
    elif symbol[:3] in ("XAU", "XAG"):
        sources = [("GoldAPI", price_goldapi), ("Stooq", price_stooq), ("Yahoo", price_yahoo)]
    else:
        sources = [("Stooq", price_stooq), ("Yahoo", price_yahoo)]
    for name, fn in sources:
        try:
            p = fn(symbol)
            if p:
                return p, name
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  {name} برای {symbol}: {e}", file=sys.stderr)
    return None, None


# ---------------------------------------------------------------------------
# تلگرام
# ---------------------------------------------------------------------------
def send(token, chat_ids, text):
    for cid in chat_ids:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": cid, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
            if not r.ok:
                print(f"⚠️  ارسال به {cid} ناموفق: {r.status_code} {r.text}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  ارسال به {cid} ناموفق: {e}", file=sys.stderr)


def get_updates(token, offset, timeout):
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                     params={"offset": offset, "timeout": int(timeout), "allowed_updates": '["message"]'},
                     timeout=timeout + 15)
    r.raise_for_status()
    return r.json().get("result", [])


# ---------------------------------------------------------------------------
# فرمت پیام‌ها
# ---------------------------------------------------------------------------
def fmt(x, ref=None):
    ref = x if ref is None else ref
    return f"{x:,.2f}" if abs(ref) >= 100 else f"{x:.5f}"


def rr_of(pos):
    return abs(pos["tp"] - pos["entry"]) / abs(pos["entry"] - pos["sl"])


def side_txt(pos):
    return "خرید 🟢" if pos["side"] == "BUY" else "فروش 🔴"


def alert_message(kind, pos, price, source):
    head = {
        "near": "🔔 <b>نزدیک قیمت ورودی!</b>",
        "entered": "🎯 <b>به قیمت ورود رسید!</b>",
        "tp": "✅ <b>به تی‌پی خورد!</b>",
        "sl": "❌ <b>به استاپ خورد</b>",
        "invalid": "⚠️ <b>ستاپ باطل شد</b> (قبل از ورود، استاپ لمس شد)",
    }[kind]
    dist = abs(price - pos["entry"])
    return "\n".join([
        head, "",
        f"<b>{pos['symbol']}</b> — {side_txt(pos)}",
        f"قیمت الان: <code>{fmt(price)}</code>",
        f"ورود: <code>{fmt(pos['entry'])}</code>  (فاصله: {fmt(dist, pos['entry'])})",
        f"استاپ: <code>{fmt(pos['sl'])}</code>",
        f"تی‌پی: <code>{fmt(pos['tp'])}</code>",
        f"ریسک به ریوارد: 1:{rr_of(pos):.1f}", "",
        f"منبع قیمت: {source} (ممکنه با بروکرت کمی فرق داشته باشه)",
    ])


# ---------------------------------------------------------------------------
# منطق هشدار
# ---------------------------------------------------------------------------
def evaluate(pos, st, price):
    stage = st.get("stage", "waiting")
    prev_side = st.get("side")
    cur_side = 1 if price >= pos["entry"] else -1
    buy = pos["side"] == "BUY"
    sl_hit = price <= pos["sl"] if buy else price >= pos["sl"]
    tp_hit = price >= pos["tp"] if buy else price <= pos["tp"]
    kinds = []

    if stage in ("waiting", "near"):
        if sl_hit:
            kinds.append("invalid"); stage = "done"
        elif (prev_side is not None and prev_side != cur_side) or price == pos["entry"]:
            kinds.append("entered"); stage = "entered"
        elif abs(price - pos["entry"]) <= pos["near"]:
            if stage == "waiting":
                kinds.append("near"); stage = "near"
        elif stage == "near" and abs(price - pos["entry"]) > 2 * pos["near"]:
            stage = "waiting"
    elif stage == "entered":
        if tp_hit:
            kinds.append("tp"); stage = "done"
        elif sl_hit:
            kinds.append("sl"); stage = "done"
    return {"stage": stage, "side": cur_side}, kinds


# ---------------------------------------------------------------------------
# خوندن پیام کاربر
# ---------------------------------------------------------------------------
def parse_position(text):
    """(پوزیشن, None) یا (None, پیام خطا)"""
    parts = text.translate(DIGITS).replace(",", " ").split()
    if len(parts) < 5:
        return None, "فرمت درست: <code>XAUUSD BUY 4000 3990 4020</code>\n(نماد، جهت، ورود، استاپ، تی‌پی)"
    raw_sym = parts[0]
    symbol = ALIASES.get(raw_sym) or ALIASES.get(raw_sym.upper()) or raw_sym.upper()
    if not (symbol.isascii() and symbol.isalpha() and 3 <= len(symbol) <= 10):
        return None, f"نماد «{html.escape(raw_sym)}» رو نشناختم. مثال: XAUUSD ، GBPUSD ، ETHUSD"
    side = SIDE_MAP.get(parts[1].upper()) or SIDE_MAP.get(parts[1])
    if not side:
        return None, "جهت باید BUY یا SELL باشه (یا خرید / فروش)."
    try:
        entry, sl, tp = float(parts[2]), float(parts[3]), float(parts[4])
        near = float(parts[5]) if len(parts) > 5 else abs(entry - sl)
    except ValueError:
        return None, "ورود، استاپ و تی‌پی باید عدد باشن."
    ok = (sl < entry < tp) if side == "BUY" else (tp < entry < sl)
    if not ok:
        need = "استاپ &lt; ورود &lt; تی‌پی" if side == "BUY" else "تی‌پی &lt; ورود &lt; استاپ"
        return None, f"ترتیب اعداد با جهت نمی‌خونه. برای {side} باید باشه: {need}"
    if near <= 0:
        near = abs(entry - sl)
    return {"symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp, "near": near}, None


def handle_message(data, msg, token, allowed):
    """True برمی‌گردونه اگه داده تغییر کرد."""
    chat_id = str(msg.get("chat", {}).get("id"))
    if chat_id not in allowed:
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    reply = lambda t: send(token, [chat_id], t)  # noqa: E731
    low = text.lower()

    if low.startswith("/start") or low.startswith("/help"):
        reply(HELP)
        return False

    if low.startswith("/list"):
        if not data["positions"]:
            reply("هنوز پوزیشنی ثبت نکردی.\n\n" + HELP)
            return False
        lines = ["📋 <b>پوزیشن‌های ثبت‌شده</b>", ""]
        for p in data["positions"]:
            stage = data["states"].get(str(p["id"]), {}).get("stage", "waiting")
            lines.append(f"<b>#{p['id']}</b> {p['symbol']} {p['side']} "
                         f"<code>{fmt(p['entry'])}</code> / <code>{fmt(p['sl'])}</code> / "
                         f"<code>{fmt(p['tp'])}</code> — {STAGE_LABEL.get(stage, stage)}")
        reply("\n".join(lines))
        return False

    if low.startswith("/del"):
        try:
            pid = int(text.translate(DIGITS).split()[1])
        except (IndexError, ValueError):
            reply("مثال: <code>/del 2</code> (شماره رو از /list ببین)")
            return False
        before = len(data["positions"])
        data["positions"] = [p for p in data["positions"] if p["id"] != pid]
        data["states"].pop(str(pid), None)
        if len(data["positions"]) < before:
            reply(f"🗑 پوزیشن #{pid} حذف شد.")
            return True
        reply(f"پوزیشن #{pid} پیدا نشد.")
        return False

    if low.startswith("/clear"):
        done_ids = {p["id"] for p in data["positions"]
                    if data["states"].get(str(p["id"]), {}).get("stage") == "done"}
        data["positions"] = [p for p in data["positions"] if p["id"] not in done_ids]
        for i in done_ids:
            data["states"].pop(str(i), None)
        reply(f"🧹 {len(done_ids)} پوزیشن تمام‌شده پاک شد.")
        return bool(done_ids)

    if low.startswith("/price"):
        parts = text.split()
        raw = parts[1] if len(parts) > 1 else "XAUUSD"
        sym = ALIASES.get(raw) or ALIASES.get(raw.upper()) or raw.upper()
        price, src = get_price(sym)
        reply(f"{sym}: <code>{fmt(price)}</code> ({src})" if price else f"قیمت {html.escape(sym)} رو نتونستم بگیرم.")
        return False

    pos, err = parse_position(text)
    if err:
        reply("⚠️ " + err)
        return False
    for p in data["positions"]:
        if all(p[k] == pos[k] for k in ("symbol", "side", "entry", "sl", "tp")):
            reply(f"این پوزیشن از قبل ثبت شده (#{p['id']}).")
            return False
    pos["id"] = data["next_id"]
    data["next_id"] += 1
    data["positions"].append(pos)
    data["states"][str(pos["id"])] = {"stage": "waiting", "side": None}
    price, src = get_price(pos["symbol"])
    now_line = f"\nقیمت الان: <code>{fmt(price)}</code> ({src})" if price else ""
    reply(f"✅ <b>ثبت شد (#{pos['id']})</b>\n{pos['symbol']} — {side_txt(pos)}\n"
          f"ورود: <code>{fmt(pos['entry'])}</code>\nاستاپ: <code>{fmt(pos['sl'])}</code>\n"
          f"تی‌پی: <code>{fmt(pos['tp'])}</code>\nریسک به ریوارد: 1:{rr_of(pos):.1f}{now_line}\n\n"
          f"وقتی نزدیک ورود شد خبرت می‌کنم.")
    return True


# ---------------------------------------------------------------------------
# چک قیمت‌ها
# ---------------------------------------------------------------------------
def check_prices(data, token, chat_ids):
    changed = False
    active = [p for p in data["positions"]
              if data["states"].get(str(p["id"]), {}).get("stage") != "done"]
    prices = {}
    for sym in {p["symbol"] for p in active}:
        prices[sym] = get_price(sym)
    for pos in active:
        price, src = prices.get(pos["symbol"], (None, None))
        if price is None:
            continue
        old = data["states"].get(str(pos["id"]), {})
        new, kinds = evaluate(pos, old, price)
        for kind in kinds:
            send(token, chat_ids, alert_message(kind, pos, price, src))
            print(f"📨 {kind}: #{pos['id']} {pos['symbol']} @ {price}")
        if new != {"stage": old.get("stage", "waiting"), "side": old.get("side")}:
            data["states"][str(pos["id"])] = new
            changed = True
    return changed


# ---------------------------------------------------------------------------
# ذخیره‌ی داده (و ثبت تو گیت‌هاب تا با ری‌استارت چیزی گم نشه)
# ---------------------------------------------------------------------------
def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("offset", 0)
    d.setdefault("positions", [])
    d.setdefault("states", {})
    d.setdefault("next_id", 1 + max([p["id"] for p in d["positions"]] or [0]))
    return d


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def git_sync():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    run = lambda *a: subprocess.run(a, capture_output=True, text=True)  # noqa: E731
    run("git", "config", "user.name", "live-bot")
    run("git", "config", "user.email", "actions@github.com")
    run("git", "add", DATA_FILE)
    if run("git", "diff", "--staged", "--quiet").returncode == 0:
        return
    run("git", "commit", "-m", "Update bot data")
    for _ in range(3):
        run("git", "pull", "--rebase")
        if run("git", "push").returncode == 0:
            return
        time.sleep(2)
    print("⚠️  push به گیت‌هاب ناموفق بود.", file=sys.stderr)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_raw:
        print("❌ TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID باید ست بشن.", file=sys.stderr)
        sys.exit(1)
    chat_ids = [c.strip() for c in chat_raw.split(",") if c.strip()]
    allowed = set(chat_ids)

    if os.environ.get("SEND_TEST", "").lower() == "true":
        send(token, chat_ids, "✅ <b>ربات روشنه</b>\nهمین‌جا برام پوزیشن بنویس. راهنما: /help")

    data = load_data()
    deadline = time.time() + float(os.environ.get("MAX_MINUTES", "345")) * 60
    last_check = 0.0
    dirty = False
    print("🟢 ربات شروع شد.")

    while time.time() < deadline:
        wait = max(1, min(15, deadline - time.time()))
        try:
            updates = get_updates(token, data["offset"], wait)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  getUpdates: {e}", file=sys.stderr)
            time.sleep(5)
            updates = []
        for u in updates:
            data["offset"] = u["update_id"] + 1
            dirty = True
            if "message" in u:
                try:
                    handle_message(data, u["message"], token, allowed)
                except Exception as e:  # noqa: BLE001
                    print(f"⚠️  خطا تو پردازش پیام: {e}", file=sys.stderr)
        if time.time() - last_check >= PRICE_INTERVAL:
            last_check = time.time()
            if check_prices(data, token, chat_ids):
                dirty = True
        if dirty:
            save_data(data)
            git_sync()
            dirty = False

    save_data(data)
    git_sync()
    print("⏹ زمان این دور تموم شد؛ دور بعدی خودکار شروع می‌شه.")


if __name__ == "__main__":
    main()
