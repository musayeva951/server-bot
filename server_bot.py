"""
Server bot — asosiy bot (MVP, birinchi versiya).

G'oya (Umid, 2026-10-22): bitta bot "server" bo'lib ishlab turadi.
Odamlar anime bot ochish uchun alohida server qidirib yurmaydi:
  1-usul (token + bot turi): foydalanuvchi bot turini tanlaydi
      (anime yoki kino), keyin o'z boti tokenini beradi ->
      server bot tokenni getMe bilan tekshiradi, shifrlab saqlaydi va
      tanlangan shablondan (child_anime_bot.py yoki child_movie_bot.py)
      nusxa ishga tushiradi, har bir bot uchun ALOHIDA SQLite baza bilan,
      va shablon faylini foydalanuvchiga ham yuboradi.
      Bot yasash pullik: 1 ta bot = 69 000 so'm / 200 ⭐ (Stars, avto).
      To'lovdan keyin 1 premium krediti ochiladi, bot yaratilganda
      ishlatiladi. Super admin admin paneldan premium berishi/olishi mumkin.
  2-usul (kod): foydalanuvchi o'z botining kodini (.py bitta fayl yoki
      .zip ichida bot.py/main.py) tashlaydi, keyin shu kod uchun bot
      tokenini alohida yuboradi -> server bot tokenni getMe bilan
      tekshiradi, shifrlab saqlaydi va ishga tushirishda BOT_TOKEN env
      orqali kodga beradi. Kod ichida token bo'lishi shart emas; token
      kodga env orqali beriladi, kod egasi o'zi javobgar.

Ishga tushirish:
    SERVER_BOT_TOKEN=... SERVER_BOT_SECRET=... python server_bot.py

Muhim xavfsizlik eslatmalari:
  * Tokenlar logga HECH QACHON yozilmaydi.
  * Tokenlar bazada shifrlangan holda saqlanadi (pastdagi encrypt_token).
  * Har bir bola-jarayonga (subprocess) TOZA muhit beriladi: asosiy
    bot tokeni va SERVER_BOT_SECRET bola jarayon env iga TUSHMAYDI.
  * Kod usuli — foydalanuvchi kodini ochiq ishga tushirish demakdir.
    Tekin serverda to'liq izolyatsiya (konteyner/sandbox) YO'Q: boshqa
    botlar tokenlariga kira olmaslik uchun env tozalash + alohida papka
    qo'llangan, lekin kod egasi o'z kodi uchun o'zi javobgar va server
    egasi kod usulini faqat ishonchli odamlarga ochishi kerak.
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- sozlamalar

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.environ.get("SERVER_DB_PATH", str(BASE_DIR / "server.db")))
SERVER_BOT_TOKEN = os.environ.get("SERVER_BOT_TOKEN", "")
SERVER_BOT_SECRET = os.environ.get("SERVER_BOT_SECRET", "")
# Super admin (Umid) — Telegram ID si. FadeHost panelda SUPER_ADMIN_ID
# sifatida o'rnatiladi. O'rnatilmagan bo'lsa admin panel yopiq turadi,
# lekin Stars orqali avto to'lov va premium baribir ishlaydi.
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "0") or 0)
# Narx: 1 ta bot yasash + serverga ulash.
# So'mdagi narx va Starsdagi ekvivalenti — to'lov Stars orqali olinadi,
# yulduzlar to'g'ridan-to'g'ri shu server botining hisobiga tushadi
# (ya'ni bot egasiga — super adminga).
PRICE_SOM = int(os.environ.get("PRICE_SOM", "69000") or 69000)
PRICE_STARS = int(os.environ.get("PRICE_STARS", "200") or 200)
# Oylik server to'lovi (2026-10-26 Umid qarori): har bir bot uchun har oy.
# Yaratish narxidan alohida, lekin yangi botning birinchi oyi yaratish
# narxiga kiradi (paid_until = yaratilgan kun + 30 kun).
# Stars avtomatik (invoice), so'm/karta qo'lda tasdiqlash bilan
# (Telegram'da Uzcard/Humo uchun avtomatik provayder yo'q, merchant
# API bo'lmasa shaxsiy karta o'tkazmasi avtomatik tekshirilmaydi).
MONTHLY_SOM = int(os.environ.get("MONTHLY_SOM", "15000") or 15000)
MONTHLY_STARS = int(os.environ.get("MONTHLY_STARS", "50") or 50)
MONTHLY_DAYS = 30
# Ichki hamyon kursi: 1 ⭐ = 300 so'm (50 ⭐ = 15 000 so'm oylik narxiga mos).
# Balans faqat so'mda yuritiladi: Stars bilan to'ldirsa shu kursda so'mga
# aylanadi, karta bilan to'ldirsa to'g'ridan-to'g'ri so'm tushadi.
# Real pul to'ldirish vaqtida super adminga tushadi, balans — faqat hisob.
WALLET_STAR_RATE = 300
MAX_BOTS = int(os.environ.get("MAX_BOTS", "5") or 5)
MAX_BOTS_PER_USER = int(os.environ.get("MAX_BOTS_PER_USER", "3") or 3)
MAX_CODE_SIZE = int(os.environ.get("MAX_CODE_SIZE", str(2 * 1024 * 1024)) or 2 * 1024 * 1024)
MAX_DB_SIZE = int(os.environ.get("MAX_DB_SIZE", str(20 * 1024 * 1024)) or 20 * 1024 * 1024)
MAX_ZIP_EXTRACTED = 10 * 1024 * 1024  # zip ochilgandagi umumiy limit

CHILD_SCRIPT = BASE_DIR / "child_anime_bot.py"
CHILD_MOVIE_SCRIPT = BASE_DIR / "child_movie_bot.py"
CHILD_GENERAL_SCRIPT = BASE_DIR / "child_general_bot.py"
CHILD_USERBOT_SCRIPT = BASE_DIR / "child_userbot.py"
# Userbotlar uchun alohida bo'lim limiti (Telethon og'ir, RAM cheklangan):
MAX_USERBOTS_PER_USER = 1
MAX_USERBOTS_TOTAL = 3
# Bot turi -> ishga tushadigan shablon fayli va foydalanuvchiga
# yuboriladigan fayl (bot-maker "fayllarni tashlab beradi" sharti).
TEMPLATE_FILES = {
    "anime": CHILD_SCRIPT,
    "movie": CHILD_MOVIE_SCRIPT,
    "general": CHILD_GENERAL_SCRIPT,
}
TEMPLATE_NAMES = {"anime": "Anime bot", "movie": "Kino bot", "general": "Umumiy bot"}
# Bot yaratish so'rovnomasi: foydalanuvchi qaysi funksiyalar qo'shilishini
# tanlaydi, keyin panel uslubini tanlaydi. Tanlov bots jadvalida saqlanadi
# va bola botga env orqali beriladi (CHILD_FEATURES, PANEL_STYLE).
MAKE_FEATURES = [
    ("shorts", "🎬 Shortlar / Treylerlar"),
    ("premium", "⭐ Premium"),
    ("sub", "📺 Majburiy obuna"),
    ("post", "📤 Post kanallari"),
    ("storage", "📦 Saqlash kanali"),
]
MAKE_FEATURE_KEYS = [k for k, _ in MAKE_FEATURES]
PANEL_STYLES = {"colorful": "🌈 Rangli", "simple": "✨ Oddiy", "minimal": "🌑 Minimal"}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("server_bot")
# Tokenlar tasodifan logga tushmasligi uchun httpx/telegram loglarini jim qilamiz:
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,}$")

# ---------------------------------------------------------------- shifrlash
# Tokenlarni bazada ochiq saqlamaslik uchun oddiy qatlam:
# SERVER_BOT_SECRET dan SHA-256 orqali Fernet kaliti yasaladi.
# cryptography bo'lmasa — OGOHLANTIRISH bilan zaif fallback (base64 "yashirish",
# bu shifrlash EMAS). requirements.txt da cryptography bor, shuning uchun
# normal o'rnatishda doim haqiqiy Fernet ishlaydi.

try:
    import base64 as _b64

    from cryptography.fernet import Fernet, InvalidToken as _FernetInvalid

    def _fernet():
        if not SERVER_BOT_SECRET:
            return None
        key = _b64.urlsafe_b64encode(hashlib.sha256(SERVER_BOT_SECRET.encode()).digest())
        return Fernet(key)

    _FERNET_OK = True
except Exception:  # pragma: no cover - cryptography o'rnatilmagan holat
    _FERNET_OK = False
    _FernetInvalid = Exception  # type: ignore

    def _fernet():
        return None


def encrypt_token(token: str) -> str:
    """Tokenni bazaga yozishdan oldin shifrlaydi (yoki fallback)."""
    f = _fernet()
    if f is not None:
        return "enc:" + f.encrypt(token.encode()).decode()
    # FALLBACK (zaif!): faqat SERVER_BOT_SECRET/cryptography yo'qligida.
    # Bu shifrlash emas — production da SERVER_BOT_SECRET shart.
    log.warning("Token zaif fallback bilan saqlandi (SERVER_BOT_SECRET yoki cryptography yo'q!)")
    import base64 as _b64

    return "raw:" + _b64.b64encode(token[::-1].encode()).decode()


def decrypt_token(stored: str) -> str:
    """Saqlangan tokenni qayta o'qiydi (faqat jarayonni ishga tushirish uchun)."""
    if not stored:
        return ""
    if stored.startswith("enc:"):
        f = _fernet()
        if f is None:
            raise RuntimeError("Shifrlangan tokenni ochish uchun SERVER_BOT_SECRET kerak")
        return f.decrypt(stored[4:].encode()).decode()
    if stored.startswith("raw:"):
        import base64 as _b64

        return _b64.b64decode(stored[4:].encode()).decode()[::-1]
    return stored  # eski format (ochiq) — migratsiya uchun qoldirilgan


def token_fingerprint(token: str) -> str:
    """Takroriy tokenni topish uchun hash (tokenning o'zi saqlanmaydi)."""
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_token(text: str) -> bool:
    """Token formatini taxminiy tekshiradi (haqiqiylik getMe bilan bilinadi)."""
    return bool(TOKEN_RE.match((text or "").strip()))


# ---------------------------------------------------------------- baza (SQLite)

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users(
                   user_id INTEGER PRIMARY KEY,
                   username TEXT,
                   first_seen TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS bots(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   owner_id INTEGER NOT NULL,
                   kind TEXT NOT NULL CHECK(kind IN ('token','code')),
                   token_enc TEXT DEFAULT '',
                   token_hash TEXT DEFAULT '',
                   code_dir TEXT DEFAULT '',
                   entry_file TEXT DEFAULT '',
                   bot_username TEXT DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'stopped',
                   created_at TEXT NOT NULL)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_id)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_tokenhash ON bots(token_hash) WHERE token_hash != ''")
        # Eski bazalarda bot_type ustuni bo'lmasligi mumkin — migratsiya.
        cols = [r[1] for r in c.execute("PRAGMA table_info(bots)").fetchall()]
        if "bot_type" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN bot_type TEXT DEFAULT 'anime'")
        if "bot_features" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN bot_features TEXT DEFAULT 'shorts,premium,sub,post,storage'")
        if "panel_style" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN panel_style TEXT DEFAULT 'simple'")
        if "paid_until" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN paid_until TEXT DEFAULT ''")
            # Eski botlarga jonli yangilanishdan boshlab 30 kun imtiyoz:
            # birdaniga to'xtab qolmasin, egasi oylik haqida xabar olsin.
            # Super admin botlari bundan mustasno (ular uchun to'lov yo'q).
            c.execute(
                "UPDATE bots SET paid_until = datetime('now', '+30 days') "
                "WHERE (paid_until IS NULL OR paid_until = '') AND owner_id != ?",
                (SUPER_ADMIN_ID,),
            )
        # Server sozlamalari: karta to'lov ma'lumoti (faqat super admin
        # bot ichida o'rnatadi, kodda karta raqami yozilmaydi).
        c.execute(
            """CREATE TABLE IF NOT EXISTS server_config(
                   key TEXT PRIMARY KEY,
                   value TEXT DEFAULT '')"""
        )
        # Karta to'lov so'rovlari: foydalanuvchi chek yuboradi, super
        # admin tasdiqlasa oylik uzayadi, rad etsa uzaymaydi.
        c.execute(
            """CREATE TABLE IF NOT EXISTS card_payments(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   bot_id INTEGER NOT NULL,
                   amount_som INTEGER NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending',
                   created_at TEXT NOT NULL,
                   decided_at TEXT DEFAULT '')"""
        )
        # Ichki hamyon: bot_id=0 bo'lgan karta so'rovi hamyon to'ldirish
        # degani (oylik emas). Balans so'mda, Stars 300 so'm/⭐ kursda tushadi.
        c.execute(
            """CREATE TABLE IF NOT EXISTS wallet_users(
                   user_id INTEGER PRIMARY KEY,
                   balance_som INTEGER NOT NULL DEFAULT 0,
                   updated_at TEXT DEFAULT '')"""
        )
        # Premium: har bir kredit = 1 ta bot yasash + serverga ulash huquqi.
        # To'lov (Stars) yoki super admin berganda kredit qo'shiladi,
        # bot muvaffaqiyatli yaratilganda 1 kredit ishlatiladi.
        c.execute(
            """CREATE TABLE IF NOT EXISTS premium_users(
                   user_id INTEGER PRIMARY KEY,
                   credits INTEGER NOT NULL DEFAULT 0,
                   granted_at TEXT,
                   granted_by TEXT DEFAULT '')"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS payments(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   username TEXT DEFAULT '',
                   amount_stars INTEGER NOT NULL,
                   payload TEXT DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'paid',
                   created_at TEXT NOT NULL)"""
        )
        # ALOHIDA BO'LIM — userbotlar (Bot API botlaridan alohida jadval).
        # Sessiya stringi hisob kalitiga teng, shuning uchun shifrlab saqlanadi.
        c.execute(
            """CREATE TABLE IF NOT EXISTS userbots(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   owner_id INTEGER NOT NULL,
                   api_id_enc TEXT DEFAULT '',
                   api_hash_enc TEXT DEFAULT '',
                   session_enc TEXT DEFAULT '',
                   session_hash TEXT DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'stopped',
                   created_at TEXT NOT NULL)"""
        )


def add_user(user_id: int, username: str = ""):
    with db_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,?)",
            (user_id, username, datetime.now().isoformat(timespec="seconds")),
        )


def count_bots_total() -> int:
    with db_conn() as c:
        return c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]


def count_bots_user(owner_id: int) -> int:
    with db_conn() as c:
        return c.execute("SELECT COUNT(*) FROM bots WHERE owner_id=?", (owner_id,)).fetchone()[0]


def token_hash_exists(fp: str) -> bool:
    with db_conn() as c:
        row = c.execute("SELECT id FROM bots WHERE token_hash=?", (fp,)).fetchone()
    return row is not None


def smallest_free_bot_id() -> int:
    """O'chirilgan bot raqamini qayta ishlatish uchun eng kichik bo'sh raqam.

    SQLite AUTOINCREMENT o'chirilgan id ni hech qachon qaytarmaydi, shuning
    uchun yangi bot doim #3, #4... bo'lib ketardi. Endi bo'sh raqam qayta
    beriladi: masalan, #1 va #2 o'chirilgan bo'lsa, keyingi bot yana #1 bo'ladi.
    """
    with db_conn() as c:
        used = {r[0] for r in c.execute("SELECT id FROM bots").fetchall()}
    n = 1
    while n in used:
        n += 1
    return n


def create_bot(owner_id, kind, token_enc="", token_hash="", code_dir="", entry_file="", bot_username="", bot_type="anime", bot_features="shorts,premium,sub,post,storage", panel_style="simple"):
    bot_id = smallest_free_bot_id()
    now = datetime.now()
    # Yangi botning birinchi oyi yaratish narxiga kiradi.
    paid_until = "" if is_super_admin(owner_id) else (now + timedelta(days=MONTHLY_DAYS)).isoformat(timespec="seconds")
    with db_conn() as c:
        c.execute(
            """INSERT INTO bots(id, owner_id, kind, token_enc, token_hash, code_dir,
                                entry_file, bot_username, status, created_at, bot_type, bot_features, panel_style, paid_until)
               VALUES(?,?,?,?,?,?,?,?,'stopped',?,?,?,?,?)""",
            (bot_id, owner_id, kind, token_enc, token_hash, code_dir, entry_file,
             bot_username, now.isoformat(timespec="seconds"), bot_type, bot_features, panel_style, paid_until),
        )
        return bot_id


def update_bot(bot_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db_conn() as c:
        c.execute(f"UPDATE bots SET {cols} WHERE id=?", (*fields.values(), bot_id))


def get_bot(bot_id: int):
    with db_conn() as c:
        return c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()


def get_user_bots(owner_id: int):
    with db_conn() as c:
        return c.execute("SELECT * FROM bots WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()


def get_all_bots():
    with db_conn() as c:
        return c.execute("SELECT * FROM bots ORDER BY id").fetchall()


def delete_bot_row(bot_id: int):
    with db_conn() as c:
        c.execute("DELETE FROM bots WHERE id=?", (bot_id,))


# ---------------------------------------------------------------- userbotlar (alohida bo'lim)

def count_userbots_total() -> int:
    with db_conn() as c:
        return c.execute("SELECT COUNT(*) FROM userbots").fetchone()[0]


def count_userbots_user(owner_id: int) -> int:
    with db_conn() as c:
        return c.execute("SELECT COUNT(*) FROM userbots WHERE owner_id=?", (owner_id,)).fetchone()[0]


def create_userbot(owner_id: int, api_id_enc: str, api_hash_enc: str, session_enc: str, session_hash: str) -> int:
    with db_conn() as c:
        cur = c.execute(
            """INSERT INTO userbots(owner_id, api_id_enc, api_hash_enc, session_enc, session_hash, status, created_at)
               VALUES(?,?,?,?,?,'stopped',?)""",
            (owner_id, api_id_enc, api_hash_enc, session_enc, session_hash,
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def get_userbot(ub_id: int):
    with db_conn() as c:
        return c.execute("SELECT * FROM userbots WHERE id=?", (ub_id,)).fetchone()


def get_user_userbots(owner_id: int):
    with db_conn() as c:
        return c.execute("SELECT * FROM userbots WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()


def update_userbot(ub_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db_conn() as c:
        c.execute(f"UPDATE userbots SET {cols} WHERE id=?", (*fields.values(), ub_id))


def delete_userbot_row(ub_id: int):
    with db_conn() as c:
        c.execute("DELETE FROM userbots WHERE id=?", (ub_id,))


# ---------------------------------------------------------------- oylik to'lov

def get_config(key: str) -> str:
    with db_conn() as c:
        row = c.execute("SELECT value FROM server_config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""


def set_config(key: str, value: str) -> None:
    with db_conn() as c:
        c.execute(
            "INSERT INTO server_config(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _parse_paid_until(row) -> object | None:
    try:
        raw = row["paid_until"] if "paid_until" in row.keys() else ""
    except Exception:
        raw = ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def monthly_status(row) -> tuple[bool, str]:
    """(to'langanmi, ko'rsatish matni). Super admin botlari har doim to'langan."""
    if is_super_admin(row["owner_id"]):
        return True, "super admin — oylik yo'q"
    until = _parse_paid_until(row)
    if until is None:
        return True, "imtiyoz davri (30 kun)"
    if until >= datetime.now():
        return True, until.strftime("%Y-%m-%d") + " gacha"
    return False, until.strftime("%Y-%m-%d") + " da tugagan"


def extend_monthly(bot_id: int) -> str:
    """Oylikni 30 kunga uzaytiradi: muddat tugamagan bo'lsa ustiga qo'shiladi."""
    row = get_bot(bot_id)
    if row is None:
        return ""
    until = _parse_paid_until(row)
    base = until if until and until > datetime.now() else datetime.now()
    new_until = base + timedelta(days=MONTHLY_DAYS)
    new_str = new_until.isoformat(timespec="seconds")
    update_bot(bot_id, paid_until=new_str)
    return new_str


def create_card_payment(user_id: int, bot_id: int, amount_som: int = 0) -> int:
    if not amount_som:
        amount_som = MONTHLY_SOM
    with db_conn() as c:
        cur = c.execute(
            "INSERT INTO card_payments(user_id, bot_id, amount_som, created_at) "
            "VALUES(?,?,?,?)",
            (user_id, bot_id, amount_som, datetime.now().isoformat(timespec="seconds")),
        )
        return int(cur.lastrowid)


def get_card_payment(req_id: int):
    with db_conn() as c:
        return c.execute("SELECT * FROM card_payments WHERE id=?", (req_id,)).fetchone()


def decide_card_payment(req_id: int, status: str) -> None:
    with db_conn() as c:
        c.execute(
            "UPDATE card_payments SET status=?, decided_at=? WHERE id=?",
            (status, datetime.now().isoformat(timespec="seconds"), req_id),
        )


def list_pending_card_payments():
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM card_payments WHERE status='pending' ORDER BY id"
        ).fetchall()


def get_all_users():
    with db_conn() as c:
        return c.execute("SELECT * FROM users ORDER BY user_id").fetchall()


def get_balance(user_id: int) -> int:
    with db_conn() as c:
        row = c.execute("SELECT balance_som FROM wallet_users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance_som"]) if row else 0


def add_balance(user_id: int, amount_som: int) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with db_conn() as c:
        c.execute(
            "INSERT INTO wallet_users(user_id, balance_som, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "balance_som = wallet_users.balance_som + excluded.balance_som, "
            "updated_at = excluded.updated_at",
            (user_id, amount_som, now),
        )
        row = c.execute("SELECT balance_som FROM wallet_users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance_som"])


def deduct_balance(user_id: int, amount_som: int) -> bool:
    """Balans yetarli bo'lsa yechadi, yetmasa hech narsa o'zgarmaydi."""
    with db_conn() as c:
        row = c.execute("SELECT balance_som FROM wallet_users WHERE user_id=?", (user_id,)).fetchone()
        if not row or int(row["balance_som"]) < amount_som:
            return False
        c.execute(
            "UPDATE wallet_users SET balance_som = balance_som - ?, updated_at = ? WHERE user_id=?",
            (amount_som, datetime.now().isoformat(timespec="seconds"), user_id),
        )
        return True


# ---------------------------------------------------------------- premium / to'lov

def is_super_admin(user_id: int) -> bool:
    return bool(SUPER_ADMIN_ID) and int(user_id) == SUPER_ADMIN_ID


def get_credits(user_id: int) -> int:
    if is_super_admin(user_id):
        return 999  # super admin uchun cheksiz deb ko'rsatiladi
    with db_conn() as c:
        row = c.execute("SELECT credits FROM premium_users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["credits"]) if row else 0


def has_premium(user_id: int) -> bool:
    return is_super_admin(user_id) or get_credits(user_id) > 0


def grant_credit(user_id: int, by: str = "admin", amount: int = 1) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with db_conn() as c:
        c.execute(
            """INSERT INTO premium_users(user_id, credits, granted_at, granted_by)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 credits = premium_users.credits + excluded.credits,
                 granted_at = excluded.granted_at, granted_by = excluded.granted_by""",
            (user_id, amount, now, by),
        )
        row = c.execute("SELECT credits FROM premium_users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["credits"])


def revoke_premium(user_id: int) -> None:
    with db_conn() as c:
        c.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))


def use_credit(user_id: int) -> bool:
    """Bot yaratilganda 1 kredit ishlatadi. Super admin uchun kerak emas."""
    if is_super_admin(user_id):
        return True
    with db_conn() as c:
        row = c.execute("SELECT credits FROM premium_users WHERE user_id=?", (user_id,)).fetchone()
        if not row or int(row["credits"]) <= 0:
            return False
        c.execute("UPDATE premium_users SET credits = credits - 1 WHERE user_id=?", (user_id,))
        return True


def record_payment(user_id: int, username: str, amount_stars: int, payload: str = "") -> None:
    with db_conn() as c:
        c.execute(
            "INSERT INTO payments(user_id, username, amount_stars, payload, created_at)"
            " VALUES(?,?,?,?,?)",
            (user_id, username or "", amount_stars, payload,
             datetime.now().isoformat(timespec="seconds")),
        )


def list_premium_users():
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM premium_users WHERE credits > 0 ORDER BY user_id"
        ).fetchall()


def count_premium_users() -> int:
    with db_conn() as c:
        return c.execute("SELECT COUNT(*) FROM premium_users WHERE credits > 0").fetchone()[0]


# ---------------------------------------------------------------- jarayon boshqaruvi

_processes: dict[int, subprocess.Popen] = {}
_userbot_processes: dict[int, subprocess.Popen] = {}
# Telefon orqali userbot qo'shish paytida ochiq turgan Telethon mijozlari.
# Server qayta yonganda bu oqim yarim qoladi — foydalanuvchi qayta boshlaydi.
_ub_phone_clients: dict[int, dict] = {}
# QR kutish vazifalari (foydalanuvchi skanerlashini kutadi).
_ub_qr_tasks: dict[int, object] = {}


def userbot_work_dir(ub_id: int) -> Path:
    return DATA_DIR / "userbots" / str(ub_id)


def userbot_log_path(ub_id: int) -> Path:
    return userbot_work_dir(ub_id) / "userbot.log"


def is_userbot_running(ub_id: int) -> bool:
    p = _userbot_processes.get(ub_id)
    return p is not None and p.poll() is None


def sync_userbot_status(ub_id: int):
    row = get_userbot(ub_id)
    if row and row["status"] == "running" and not is_userbot_running(ub_id):
        update_userbot(ub_id, status="stopped")


def start_userbot(ub_id: int) -> tuple[bool, str]:
    row = get_userbot(ub_id)
    if row is None:
        return False, "Userbot topilmadi"
    if is_userbot_running(ub_id):
        return True, "Allaqachon ishlayapti"
    if not CHILD_USERBOT_SCRIPT.exists():
        return False, "child_userbot.py topilmadi — deployda shu fayl ham bo'lishi kerak"
    try:
        api_id = decrypt_token(row["api_id_enc"])
        api_hash = decrypt_token(row["api_hash_enc"])
        session = decrypt_token(row["session_enc"])
    except Exception as e:
        return False, f"Ma'lumotlarni ochib bo'lmadi: {e}"
    if not api_id or not api_hash or not session:
        return False, "API_ID / API_HASH / sessiya to'liq emas"
    work = userbot_work_dir(ub_id)
    work.mkdir(parents=True, exist_ok=True)
    env = _clean_env({
        "USERBOT_API_ID": api_id,
        "USERBOT_API_HASH": api_hash,
        "USERBOT_SESSION": session,
        "OWNER_ID": str(row["owner_id"]),
        "USERBOT_DB_PATH": str(work / "userbot.db"),
        "USERBOT_PRODUCTS_FILE": str(work / "products.json"),
    })
    # products.json bo'lmasa bo'sh ro'yxat bilan yaratiladi (savdo keyin to'ldiriladi)
    prod = work / "products.json"
    if not prod.exists():
        prod.write_text("[]", encoding="utf-8")
    log_file = open(userbot_log_path(ub_id), "ab")
    try:
        p = subprocess.Popen(
            [sys.executable, str(CHILD_USERBOT_SCRIPT)],
            cwd=str(work), env=env,
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_preexec_limits if os.name == "posix" else None,
        )
    except Exception as e:
        log_file.close()
        return False, f"Ishga tushmadi: {e}"
    _userbot_processes[ub_id] = p
    update_userbot(ub_id, status="running")
    log.info("Userbot #%s ishga tushdi (pid=%s)", ub_id, p.pid)
    return True, "Ishga tushdi ✅"


def stop_userbot(ub_id: int) -> tuple[bool, str]:
    p = _userbot_processes.get(ub_id)
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _userbot_processes.pop(ub_id, None)
    update_userbot(ub_id, status="stopped")
    return True, "To'xtatildi"


def restore_running_userbots():
    with db_conn() as c:
        rows = c.execute("SELECT id FROM userbots WHERE status='running'").fetchall()
    for r in rows:
        ok, msg = start_userbot(int(r[0]))
        if not ok:
            update_userbot(int(r[0]), status="stopped")
            log.warning("Userbot #%s qayta yoqilmadi: %s", r[0], msg)


def bot_work_dir(bot_id: int) -> Path:
    return DATA_DIR / "bots" / str(bot_id)


def bot_log_path(bot_id: int) -> Path:
    return bot_work_dir(bot_id) / "bot.log"


def _preexec_limits():
    """Bola jarayonga yumshoq resurs limiti (faqat Unix).

    RLIMIT_AS — xotira (512 MB), RLIMIT_NOFILE — ochiq fayllar (256).
    Bu to'liq izolyatsiya EMAS, shunchaki bitta bot butun serverni
    yiqitib yubormasligi uchun himoya. CPU limiti atay qo'yilmadi:
    polling botlari uzoq yashaydi, yig'ilgan CPU vaqti ularni o'ldirardi.
    """
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    except Exception:
        pass


def _clean_env(extra: dict) -> dict:
    """Bola jarayon uchun TOZA muhit: asosiy bot sirlari TUSHMAYDI."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    env.update(extra)
    return env


def is_running(bot_id: int) -> bool:
    p = _processes.get(bot_id)
    return p is not None and p.poll() is None


def sync_status(bot_id: int):
    """Jarayon o'lib qolgan bo'lsa, bazadagi holatni ham yangilaydi."""
    row = get_bot(bot_id)
    if row and row["status"] == "running" and not is_running(bot_id):
        update_bot(bot_id, status="stopped")


def start_bot(bot_id: int) -> tuple[bool, str]:
    row = get_bot(bot_id)
    if row is None:
        return False, "Bot topilmadi"
    paid, _label = monthly_status(row)
    if not paid:
        return False, (
            f"Oylik to'lov muddati tugagan. «🤖 Mening botlarim» → Bot #{bot_id} → "
            f"«💳 Oylik to'lash» dan {MONTHLY_SOM:,} so'm / {MONTHLY_STARS} ⭐ to'lang, "
            "keyin qayta ishga tushiring."
        )
    if is_running(bot_id):
        return True, "Allaqachon ishlayapti"
    work = bot_work_dir(bot_id)
    work.mkdir(parents=True, exist_ok=True)
    if row["kind"] == "token":
        try:
            token = decrypt_token(row["token_enc"])
        except Exception as e:
            return False, f"Tokenni ochib bo'lmadi: {e}"
        if not token:
            return False, "Token saqlanmagan"
        # Bot turi bo'yicha shablon: anime -> child_anime_bot.py,
        # kino -> child_movie_bot.py. Eski qatorlarda bot_type bo'sh
        # bo'lsa, anime deb qabul qilinadi.
        bot_type = (row["bot_type"] if "bot_type" in row.keys() else None) or "anime"
        script = TEMPLATE_FILES.get(bot_type, CHILD_SCRIPT)
        env = _clean_env({
            "CHILD_BOT_TOKEN": token,
            "CHILD_DB_PATH": str(work / "child.db"),
            "OWNER_ID": str(row["owner_id"]),
            "CHILD_FEATURES": (row["bot_features"] if "bot_features" in row.keys() and row["bot_features"] else "shorts,premium,sub,post,storage"),
            "PANEL_STYLE": (row["panel_style"] if "panel_style" in row.keys() and row["panel_style"] else "simple"),
        })
        cwd = work
    else:  # kind == 'code'
        entry = Path(row["code_dir"]) / row["entry_file"] if row["code_dir"] else None
        if not entry or not entry.exists():
            return False, "Kod fayli topilmadi"
        script = entry
        # Kod boti tokeni: bazada shifrlangan holda saqlanadi, ishga
        # tushirishda BOT_TOKEN env orqali beriladi (masalan, Uzbek bot
        # kodi BOT_TOKEN ni env dan o'qiydi). Token bo'lmasa — boshlanmaydi,
        # foydalanuvchi avval «🔑 Token kiritish» orqali token berishi kerak.
        # Eslatma: asosiy bot tokeni va SERVER_BOT_SECRET bolaga berilmaydi.
        try:
            code_token = decrypt_token(row["token_enc"]) if row["token_enc"] else ""
        except Exception as e:
            return False, f"Tokenni ochib bo'lmadi: {e}"
        if not code_token:
            return False, "Bu kod botiga token kiritilmagan — «🔑 Token kiritish» ni bosing."
        env = _clean_env({
            "OWNER_ID": str(row["owner_id"]),
            "CHILD_DB_PATH": str(work / "child.db"),
            "BOT_TOKEN": code_token,
            "CHILD_BOT_TOKEN": code_token,
            # Kod boti egasi — shu botning admini (Uzbek bot kodi
            # SUPER_ADMIN_ID ni env dan o'qiydi, OWNER_ID ni emas).
            "SUPER_ADMIN_ID": str(row["owner_id"]),
        })
        cwd = Path(row["code_dir"])
    log_file = open(bot_log_path(bot_id), "ab")  # noqa: SIM115 - jarayon yashaguncha ochiq turadi
    try:
        p = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(cwd), env=env,
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_preexec_limits if os.name == "posix" else None,
        )
    except Exception as e:
        log_file.close()
        return False, f"Ishga tushmadi: {e}"
    _processes[bot_id] = p
    update_bot(bot_id, status="running")
    log.info("Bot #%s ishga tushdi (pid=%s, turi=%s)", bot_id, p.pid, row["kind"])
    return True, "Ishga tushdi ✅"


def stop_bot(bot_id: int) -> tuple[bool, str]:
    p = _processes.get(bot_id)
    if p is not None and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), 15)  # SIGTERM butun guruhga
        except Exception:
            p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), 9)
            except Exception:
                p.kill()
    _processes.pop(bot_id, None)
    update_bot(bot_id, status="stopped")
    return True, "To'xtatildi ⏹"


def read_last_log(bot_id: int, lines: int = 25) -> str:
    path = bot_log_path(bot_id)
    if not path.exists():
        return "Log hali yo'q."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Logni o'qib bo'lmadi: {e}"
    tail = "\n".join(text.splitlines()[-lines:])
    return tail[-3000:] if tail else "Log bo'sh."


def restore_running_bots():
    """Server bot qayta ishga tushganda faol botlarni avtomatik qayta yoqadi."""
    for row in get_all_bots():
        if row["status"] == "running":
            ok, msg = start_bot(row["id"])
            if not ok:
                log.warning("Bot #%s qayta yoqilmadi: %s", row["id"], msg)
                update_bot(row["id"], status="stopped")


# ---------------------------------------------------------------- kod usuli: fayl tekshiruvi

ALLOWED_CODE_EXT = {".py", ".zip"}


def find_entry_in_dir(folder: Path) -> str | None:
    """Papkada ishga tushiriladigan faylni topadi: bot.py > main.py > yagona .py."""
    for name in ("bot.py", "main.py"):
        if (folder / name).exists():
            return name
    pys = sorted(p.name for p in folder.glob("*.py"))
    if len(pys) == 1:
        return pys[0]
    return None


def extract_code_zip(zip_bytes: bytes, dest: Path) -> tuple[bool, str, str]:
    """Zipni xavfsiz ochadi. Qaytaradi: (ok, xabar, entry_file nomi).

    Xavfsizlik: ../ va mutlaq yo'llar rad etiladi (zip-slip),
    ochilgan umumiy hajm MAX_ZIP_EXTRACTED dan oshsa rad etiladi.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return False, "Zip fayl buzilgan yoki zip emas", ""
    total = 0
    for info in zf.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        pure = Path(name)
        if pure.is_absolute() or ".." in pure.parts:
            return False, f"Zip ichida xavfli yo'l topildi: {name}", ""
        total += info.file_size
        if total > MAX_ZIP_EXTRACTED:
            return False, "Zip ochilganda hajmi juda katta (limit 10 MB)", ""
    dest.mkdir(parents=True, exist_ok=True)
    zf.extractall(dest)
    # Bir papka ichiga o'ralgan bo'lsa (folder/bot.py) — o'sha papkaga ko'taramiz
    entry = find_entry_in_dir(dest)
    if entry is None:
        subdirs = [d for d in dest.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            inner = find_entry_in_dir(subdirs[0])
            if inner:
                return True, "", str(Path(subdirs[0].name) / inner)
        return False, "Zip ichida bot.py yoki main.py topilmadi", ""
    return True, "", entry


def check_python_syntax(file_path: Path) -> tuple[bool, str]:
    import py_compile

    try:
        py_compile.compile(str(file_path), doraise=True)
        return True, ""
    except Exception as e:
        return False, str(e)[:500]


def validate_anime_db(file_path: Path) -> tuple[bool, str]:
    """Kod boti bazasini tekshirish: SQLite bo'lishi va animelar +
    serial_qismlar jadvallari bo'lishi kerak. Muvaffaqiyatda anime soni qaytadi."""
    try:
        con = sqlite3.connect(str(file_path), timeout=10)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "animelar" not in tables or "serial_qismlar" not in tables:
                return False, "animelar yoki serial_qismlar jadvali topilmadi"
            n = con.execute("SELECT COUNT(*) FROM animelar").fetchone()[0]
            return True, f"{n} ta anime"
        finally:
            con.close()
    except Exception as e:
        return False, str(e)[:300]


# ---------------------------------------------------------------- telegram qismi

# ---------------------------------------------------------------- sayt API (Uzbek bot sayti uchun)
# Maqsad: server botda ishlayotgan kod botining (masalan, Uzbek bot, Bot #1)
# katalogini GitHub Pages saytiga berish. FadeHost da ilova veb manzili
# allaqachon yoqilgan (lekin-server-bot.fadehost.app, PORT=8080), shuning
# uchun shu jarayonda alohida oqimda kichik HTTP server ishlaydi.
# Xavfsizlik: tokenlar logga yozilmaydi, faqat BOT_TOKEN orqali Telegram
# getFile/file proxy qilinadi; admin/auth endpointlar bu v1 da yo'q.
# Katta videolar (>20 MB) Bot API orqali yuklab bo'lmaydi — bu holda
# proxy 404 qaytaradi (MTProto fazasi alohida, API_ID/HASH kerak bo'ladi).

SITE_API_ORIGIN = "https://musayeva951.github.io"
SITE_CHAT_SECRET = os.environ.get("SITE_CHAT_SECRET", "")
_SITE_POSTER_CACHE: dict = {}


def _site_api_bot_row():
    """Sayt uchun bot qatori: SITE_BOT_ID bo'lsa shu, bo'lmasa birinchi kod boti."""
    wanted = os.environ.get("SITE_BOT_ID", "").strip()
    if wanted:
        try:
            row = get_bot(int(wanted))
            if row is not None and row["kind"] == "code":
                return row
        except Exception:
            pass
    # get_all_bots dan birinchi kod botini olamiz (server botda odatda bitta bot bo'ladi)
    for row in get_all_bots():
        if row["kind"] == "code":
            return row
    return None


def _site_api_db_path(row):
    if row is None or not row["code_dir"]:
        return None
    code_dir = Path(row["code_dir"])
    preferred = code_dir / "uz.dubanime_v1.db"
    if preferred.exists():
        return preferred
    # Kod papkasidagi birinchi .db (animelar jadvali borligini keyin tekshiramiz)
    for p in sorted(code_dir.glob("*.db")):
        return p
    return None


def _site_api_token(row):
    if row is None or not row["token_enc"]:
        return ""
    try:
        return decrypt_token(row["token_enc"])
    except Exception:
        return ""


def _site_api_db():
    row = _site_api_bot_row()
    path = _site_api_db_path(row)
    if path is None:
        return None, row, None
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    return con, row, path


def _site_anime_list():
    con, _, _ = _site_api_db()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT kod, title, caption, is_premium, holat FROM animelar ORDER BY kod DESC"
        ).fetchall()
        out = []
        for r in rows:
            try:
                cnt = con.execute(
                    "SELECT COUNT(*) c FROM serial_qismlar WHERE anime_kod=?", (r["kod"],)
                ).fetchone()["c"]
            except Exception:
                cnt = 0
            out.append({
                "kod": r["kod"],
                "title": r["title"] or "",
                "caption": r["caption"] or "",
                "holat": r["holat"] or "tugatigan",
                "is_premium": int(r["is_premium"] or 0),
                "qism_soni": cnt,
                "poster": "/api/poster/%d" % r["kod"],
            })
        return out
    finally:
        con.close()


def _site_anime_detail(kod):
    con, _, _ = _site_api_db()
    if con is None:
        return None
    try:
        r = con.execute(
            "SELECT kod, title, caption, is_premium, holat FROM animelar WHERE kod=?",
            (kod,),
        ).fetchone()
        if not r:
            return None
        eps = con.execute(
            "SELECT qism_raqam FROM serial_qismlar WHERE anime_kod=? ORDER BY qism_raqam",
            (kod,),
        ).fetchall()
        return {
            "kod": r["kod"],
            "title": r["title"] or "",
            "caption": r["caption"] or "",
            "holat": r["holat"] or "tugatigan",
            "is_premium": int(r["is_premium"] or 0),
            "qism_soni": len(eps),
            "poster": "/api/poster/%d" % r["kod"],
            "qismlar": [e["qism_raqam"] for e in eps],
        }
    finally:
        con.close()


def _site_episode_file(kod, qism):
    con, _, _ = _site_api_db()
    if con is None:
        return None, 0
    try:
        row = con.execute(
            "SELECT file_id, file_size FROM serial_qismlar WHERE anime_kod=? AND qism_raqam=?",
            (kod, qism),
        ).fetchone()
        if not row:
            return None, 0
        return row["file_id"], row["file_size"] or 0
    finally:
        con.close()


def _site_shorts_list():
    con, _, _ = _site_api_db()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT s.id, s.anime_kod, s.sarlavha, s.yaratilgan, a.title "
            "FROM anime_shorts s LEFT JOIN animelar a ON a.kod = s.anime_kod "
            "ORDER BY s.id DESC"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "anime_kod": r["anime_kod"],
                "anime_title": r["title"] or "",
                "sarlavha": r["sarlavha"] or "",
                "yaratilgan": r["yaratilgan"] or "",
                "video": "/api/shorts/video/%d" % r["id"],
                "poster": "/api/poster/%d" % r["anime_kod"],
            })
        return out
    except Exception:
        return []
    finally:
        con.close()


def _site_short_file(short_id):
    con, _, _ = _site_api_db()
    if con is None:
        return None, 0
    try:
        row = con.execute(
            "SELECT file_id, file_size FROM anime_shorts WHERE id = ?", (short_id,)
        ).fetchone()
        if not row:
            return None, 0
        return row["file_id"], row["file_size"] or 0
    finally:
        con.close()


def _site_telegram_file_bytes(token, file_id, cache_key=None):
    if cache_key and cache_key in _SITE_POSTER_CACHE:
        return _SITE_POSTER_CACHE[cache_key]
    try:
        with urllib.request.urlopen(
            "https://api.telegram.org/bot%s/getFile?file_id=%s" % (token, urllib.parse.quote(file_id, safe="")),
            timeout=15,
        ) as resp:
            info = json.loads(resp.read().decode())
        fp = info.get("result", {}).get("file_path")
        if not fp:
            return None
        with urllib.request.urlopen(
            "https://api.telegram.org/file/bot%s/%s" % (token, fp), timeout=30
        ) as resp:
            data = resp.read()
        if cache_key and len(data) < 15000000:
            _SITE_POSTER_CACHE[cache_key] = data
        return data
    except Exception:
        return None


def _site_poster_bytes(kod):
    if kod in _SITE_POSTER_CACHE:
        return _SITE_POSTER_CACHE[kod]
    row = _site_api_bot_row()
    token = _site_api_token(row)
    if not token:
        return None
    con, _, _ = _site_api_db()
    if con is None:
        return None
    try:
        r = con.execute("SELECT file_id FROM animelar WHERE kod=?", (kod,)).fetchone()
    finally:
        con.close()
    if not r or not r["file_id"]:
        return None
    data = _site_telegram_file_bytes(token, r["file_id"])
    if data and len(data) < 5000000:
        _SITE_POSTER_CACHE[kod] = data
    return data


def _site_config_value(key):
    con, _, _ = _site_api_db()
    if con is None:
        return None, ""
    try:
        r = con.execute("SELECT file_id, extra FROM site_config WHERE key=?", (key,)).fetchone()
        return (r["file_id"], r["extra"]) if r else (None, "")
    except Exception:
        return None, ""
    finally:
        con.close()


class _SiteApiHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", SITE_API_ORIGIN)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _proxy_telegram_file(self, token, file_id, file_size=0):
        # Bot API orqali uzatish, Range ni Telegram'ga uzatamiz (seek ishlashi uchun)
        try:
            with urllib.request.urlopen(
                "https://api.telegram.org/bot%s/getFile?file_id=%s" % (token, urllib.parse.quote(file_id, safe="")),
                timeout=15,
            ) as resp:
                info = json.loads(resp.read().decode())
            if not info.get("ok"):
                return self._send(404, json.dumps({"ok": False}))
            fp = info.get("result", {}).get("file_path")
            if not fp:
                return self._send(404, json.dumps({"ok": False}))
        except Exception:
            return self._send(404, json.dumps({"ok": False}))
        file_url = "https://api.telegram.org/file/bot%s/%s" % (token, fp)
        req = urllib.request.Request(file_url)
        range_hdr = self.headers.get("Range")
        if range_hdr:
            req.add_header("Range", range_hdr)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except Exception:
            return self._send(502, json.dumps({"ok": False}))
        code = resp.getcode()
        self.send_response(code)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "video/mp4"))
        if resp.headers.get("Content-Length"):
            self.send_header("Content-Length", resp.headers.get("Content-Length"))
        if resp.headers.get("Content-Range"):
            self.send_header("Content-Range", resp.headers.get("Content-Range"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", SITE_API_ORIGIN)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    def do_GET(self):
        path = self.path.split("?")[0]
        row = _site_api_bot_row()
        token = _site_api_token(row)
        if path in ("/", "/health"):
            return self._send(200, json.dumps({"ok": True, "service": "server-bot-site-api"}))
        if path == "/api/animes":
            try:
                return self._send(200, json.dumps({"ok": True, "animes": _site_anime_list()}, ensure_ascii=False))
            except Exception:
                return self._send(500, json.dumps({"ok": False, "error": "db"}))
        if path.startswith("/api/anime/"):
            try:
                kod = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send(404, json.dumps({"ok": False}))
            detail = _site_anime_detail(kod)
            if not detail:
                return self._send(404, json.dumps({"ok": False}))
            return self._send(200, json.dumps({"ok": True, "anime": detail}, ensure_ascii=False))
        if path.startswith("/api/video/"):
            parts = path.split("/")
            try:
                kod = int(parts[3]); qism = int(parts[4])
            except (IndexError, ValueError):
                return self._send(404, json.dumps({"ok": False}))
            if not token:
                return self._send(404, json.dumps({"ok": False}))
            file_id, file_size = _site_episode_file(kod, qism)
            if not file_id:
                return self._send(404, json.dumps({"ok": False}))
            return self._proxy_telegram_file(token, file_id, file_size)
        if path == "/api/shorts":
            try:
                return self._send(200, json.dumps({"ok": True, "shorts": _site_shorts_list()}, ensure_ascii=False))
            except Exception:
                return self._send(500, json.dumps({"ok": False}))
        if path.startswith("/api/shorts/video/"):
            try:
                sid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send(404, json.dumps({"ok": False}))
            if not token:
                return self._send(404, json.dumps({"ok": False}))
            file_id, file_size = _site_short_file(sid)
            if not file_id:
                return self._send(404, json.dumps({"ok": False}))
            return self._proxy_telegram_file(token, file_id, file_size)
        if path.startswith("/api/poster/"):
            try:
                kod = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send(404, json.dumps({"ok": False}))
            data = _site_poster_bytes(kod)
            if not data:
                return self._send(404, json.dumps({"ok": False}))
            ctype = "image/jpeg"
            if data[:8].startswith(b"\x89PNG"):
                ctype = "image/png"
            elif data[:6] in (b"GIF87a", b"GIF89a"):
                ctype = "image/gif"
            elif data[:4] == b"RIFF":
                ctype = "image/webp"
            return self._send(200, data, ctype)
        if path == "/api/site-config":
            bg_id, bg_type = _site_config_value("bg_image")
            mus_id, mus_name = _site_config_value("music")
            return self._send(200, json.dumps({
                "ok": True,
                "bg": "/api/site-bg" if bg_id else None,
                "bg_type": bg_type or "image",
                "music": "/api/site-music" if mus_id else None,
                "music_name": mus_name or "",
            }, ensure_ascii=False))
        if path == "/api/site-bg":
            bg_id, bg_type = _site_config_value("bg_image")
            if not bg_id or not token:
                return self._send(404, json.dumps({"ok": False}))
            if bg_type == "video":
                return self._proxy_telegram_file(token, bg_id)
            data = _site_telegram_file_bytes(token, bg_id, "site_bg")
            if not data:
                return self._send(404, json.dumps({"ok": False}))
            return self._send(200, data, "image/jpeg")
        if path == "/api/site-music":
            mus_id, _ = _site_config_value("music")
            if not mus_id or not token:
                return self._send(404, json.dumps({"ok": False}))
            return self._proxy_telegram_file(token, mus_id)
        if path == "/api/chat":
            con, _, _ = _site_api_db()
            if con is None:
                return self._send(200, json.dumps({"ok": True, "messages": []}, ensure_ascii=False))
            try:
                rows = con.execute(
                    "SELECT username, text, created_at FROM chat_messages ORDER BY id DESC LIMIT 50"
                ).fetchall()
                msgs = [{"user": r["username"] or "Anonim", "text": r["text"],
                         "time": r["created_at"][11:16] if r["created_at"] else ""}
                        for r in reversed(rows)]
                return self._send(200, json.dumps({"ok": True, "messages": msgs}, ensure_ascii=False))
            except Exception:
                return self._send(200, json.dumps({"ok": True, "messages": []}, ensure_ascii=False))
            finally:
                con.close()
        return self._send(404, json.dumps({"ok": False}))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/chat/send":
            return self._send(404, json.dumps({"ok": False}))
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length).decode() or "{}")
            text = re.sub(r"<[^>]*>", "", (body.get("text") or "").strip()[:500])
            username = re.sub(r"<[^>]*>", "", (body.get("username") or "Anonim").strip()[:30]) or "Anonim"
            if not text:
                return self._send(400, json.dumps({"ok": False}))
            name = username.strip().lower().lstrip("@")
            secret = body.get("secret") or ""
            if ("kitagawa" in name or "umid_ss7" in name or name in ("admin", "animed", "animed.uz")):
                if not SITE_CHAT_SECRET or secret != SITE_CHAT_SECRET:
                    return self._send(403, json.dumps({"ok": False, "err": "reserved_name"}))
            ip = (self.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip() or self.client_address[0]
            con, _, _ = _site_api_db()
            if con is None:
                return self._send(500, json.dumps({"ok": False}))
            try:
                con.execute("CREATE TABLE IF NOT EXISTS chat_rate (ip TEXT, username TEXT, ts REAL)")
                import time as _time
                now = _time.time()
                con.execute("DELETE FROM chat_rate WHERE ts < ?", (now - 3600,))
                global_n = con.execute("SELECT COUNT(*) FROM chat_rate WHERE ts > ?", (now - 10,)).fetchone()[0]
                if global_n >= 20:
                    return self._send(429, json.dumps({"ok": False, "err": "too_many"}))
                ip_rows = con.execute("SELECT ts FROM chat_rate WHERE ip = ? ORDER BY ts DESC LIMIT 1", (ip,)).fetchall()
                if ip_rows and now - ip_rows[0][0] < 5:
                    return self._send(429, json.dumps({"ok": False, "err": "too_fast"}))
                con.execute("INSERT INTO chat_rate (ip, username, ts) VALUES (?, ?, ?)", (ip, name, now))
                con.execute("INSERT INTO chat_messages (user_id, username, text, created_at) VALUES (0, ?, ?, ?)",
                            (username, text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                con.execute("DELETE FROM chat_messages WHERE id NOT IN (SELECT id FROM chat_messages ORDER BY id DESC LIMIT 200)")
                con.commit()
            finally:
                con.close()
            return self._send(200, json.dumps({"ok": True}))
        except Exception:
            return self._send(500, json.dumps({"ok": False}))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", SITE_API_ORIGIN)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *a):
        pass


def start_site_api_server():
    port = int(os.environ.get("PORT", "8080") or 8080)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _SiteApiHandler)
    except OSError as e:
        log.warning("Site API server %d-portda ochilmadi: %s", port, e)
        return
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log.info("Site API server ishga tushdi: 0.0.0.0:%d", port)


def build_app():
    """python-telegram-bot ilovasini yig'adi (import shu yerda — sinov importi yengil bo'lsin)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    BTN_TOKEN = "➕ Bot yasash (token)"
    BTN_CODE = "📁 Bot qo'shish (kod)"
    BTN_MY = "🤖 Mening botlarim"
    BTN_USERBOTS = "👤 Userbotlar"
    BTN_HELP = "ℹ️ Yordam"
    BTN_PREMIUM = "💎 Premium olish"
    BTN_WALLET = "💰 Hamyonim"
    BTN_ADMIN = "🛡 Admin panel"

    # Rangli panel (Bot API style, PTB 22.7+): primary=ko'k, success=yashil, danger=qizil.
    _REPLY_COLORS = {
        "💎 Premium olish": "success",
        "➕ Premium berish": "success",
        "➖ Premium olish": "danger",
    }

    def _cbtn(text: str) -> KeyboardButton:
        return KeyboardButton(text, style=_REPLY_COLORS.get(text, "primary"))

    def _colored(rows):
        return [[_cbtn(cell) for cell in row] for row in rows]

    def _ibtn(text: str, callback_data=None, url=None):
        if "Ishga tushirish" in text or "Davom" in text or "Stars" in text:
            style = "success"
        elif "To'xtatish" in text or "O'chirish" in text or "o'chir" in text:
            style = "danger"
        else:
            style = "primary"
        if url is not None:
            return InlineKeyboardButton(text, url=url, style=style)
        return InlineKeyboardButton(text, callback_data=callback_data, style=style)

    def main_menu(user_id: int = 0):
        rows = [[BTN_TOKEN, BTN_CODE], [BTN_MY, BTN_USERBOTS], [BTN_PREMIUM, BTN_WALLET], [BTN_HELP]]
        if user_id and is_super_admin(user_id):
            rows.append([BTN_ADMIN])
        return ReplyKeyboardMarkup(_colored(rows), resize_keyboard=True)

    HELP_TEXT = (
        "ℹ️ *Yordam*\n\n"
        "Bu bot — bot-maker + server bot: botingizni shu yerda yasab, "
        "shu serverda ishga tushirib beraman, alohida server qidirib yurmaysiz.\n\n"
        "*Bot yasash — pullik:* 1 ta bot yasash + serverga ulash — "
        f"{PRICE_SOM:,} so'm / {PRICE_STARS} ⭐ (Telegram Stars). "
        "To'lov avtomatik: Stars bilan to'lasangiz, shu zahoti premium kreditingiz "
        "ochiladi va bot yasay olasiz. Yulduzlar to'g'ridan-to'g'ri shu botning "
        "hisobiga, ya'ni super adminga tushadi.\n\n"
        "*1-usul — token + bot turi:* «➕ Bot yasash (token)» ni bosing, "
        "«🎬 Anime bot» yoki «🎞 Kino bot» ni tanlang, keyin BotFather bergan "
        "tokenni yuboring. Shu token bilan tayyor shablon nusxasi ishga tushadi, "
        "o'z katalogingiz, alohida baza — va shablon fayli ham sizga yuboriladi.\n\n"
        "*Anime bot:* anime ro'yxati, qidiruv, qismlar, video tomosha.\n"
        "*Kino bot:* kinolar ro'yxati, qidiruv, bitta film — bitta video.\n"
        "*Umumiy bot:* egasi bo'limlar va kontentni (matn, rasm, video) o'zi "
        "to'ldiradi, majburiy obuna + premium + admin panel tayyor.\n\n"
        "*2-usul — kod:* «📁 Bot qo'shish (kod)» ni bosib, botingiz kodini (.py fayl yoki ichida "
        "bot.py/main.py bor .zip) tashlang, keyin shu kod uchun bot tokenini yuboring. Token getMe bilan "
        "tekshirilib, shifrlab saqlanadi va ishga tushirishda `BOT_TOKEN` env orqali kodga beriladi. "
        "Kod o'z papkasida alohida jarayon bo'lib ishlaydi.\n\n"
        f"*💰 Hamyon:* «💰 Hamyonim» dan balans to'ldiring (Stars — 1 ⭐ = 300 so'm, karta — chek tasdiqlangach). "
        "Oylik muddati tugaganda, balansda pul bo'lsa, oylik avtomatik yechilib uzayadi.\n\n"
        f"*Oylik server to'lovi:* har bir bot uchun har oy {MONTHLY_SOM:,} so'm / {MONTHLY_STARS} ⭐ (30 kun). "
        "Yangi botning birinchi oyi yaratish narxiga kiradi. Muddat tugasa bot ishga tushmaydi — "
        "«🤖 Mening botlarim» → «💳 Oylik to'lash» dan to'lang: Stars avtomatik uzaytiradi, "
        "karta to'lovida chek yuborasiz, super admin tasdiqlasa uzayadi.\n\n"
        "*👤 Userbotlar — ALOHIDA BO'LIM:* «👤 Userbotlar» ni bosing, keyin API_ID, "
        "API_HASH ni yuboring, so'ng usulni tanlang: telefon raqami + kod, QR kodni "
        "skanerlash (kod yozmaysiz) yoki tayyor sessiya stringi. Userbot sizning hisobingizdan yozadi, "
        "buyruqlar nuqta (.) bilan: .yordam, .afk, .sales, .hisobot va boshqalar. "
        "⚠️ Sessiya stringi hisob kalitiga teng: uni server botga yuborganingizda "
        "server operatori (super admin) ham ko'ra oladi, shuning uchun faqat o'z "
        "hisobingiz uchun ishlating, boshqalarga bermang. Telegram userbotni ban qilishi mumkin.\n\n"
        f"Cheklovlar: hozircha har foydalanuvchiga {MAX_BOTS_PER_USER} ta bot, jami {MAX_BOTS} ta bot "
        "(tekin server kuchsiz: 256 MB xotira, kuniga 1 GB trafik — shuning uchun limit bor). "
        "Fayl limiti: 2 MB.\n\n"
        "⚠️ Kod usuli ochiq ishga tushirishdir: to'liq izolyatsiya yo'q, o'z kodingiz uchun o'zingiz javobgarsiz."
    )

    async def cmd_start(update, context):
        user = update.effective_user
        add_user(user.id, user.username or "")
        credits = get_credits(user.id)
        credit_line = ""
        if is_super_admin(user.id):
            credit_line = "\n🛡 Siz super admins — premium cheksiz."
        elif credits > 0:
            credit_line = f"\n💎 Premium kreditingiz: {credits} ta bot."
        await update.message.reply_text(
            "Assalomu alaykum! 👋 Bu — bot-maker + server bot.\n"
            "Token bering, bot turini ayting — anime, kino yoki umumiy botmi — "
            "shunaqa bot yasab, shu serverga ulab beraman, faylini ham tashlab beraman.\n"
            f"💰 1 ta bot: {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐."
            + credit_line,
            reply_markup=main_menu(user.id),
        )

    async def show_help(update, context):
        # Markdown xatosi bo'lsa ham Yordam matni yetib borsin: avval
        # Markdown bilan, xato bo'lsa oddiy matn bilan qayta yuboriladi.
        # Yordam bosilganda kutish holati (token/karta/elon kutilayotgani)
        # tozalanadi — aks holda keyingi xabar noto'g'ri joyga tushadi.
        context.user_data.pop("await", None)
        try:
            await update.message.reply_text(
                HELP_TEXT, parse_mode="Markdown", reply_markup=main_menu(update.effective_user.id)
            )
        except Exception:
            await update.message.reply_text(
                HELP_TEXT, reply_markup=main_menu(update.effective_user.id)
            )

    # ---------- mening botlarim ----------

    def bot_buttons(bot_id: int, kind: str = "", has_token: bool = True):
        rows = [
            [_ibtn("▶️ Ishga tushirish", callback_data=f"act:start:{bot_id}"),
             _ibtn("⏹ To'xtatish", callback_data=f"act:stop:{bot_id}")],
            [_ibtn("📄 Log", callback_data=f"act:log:{bot_id}"),
             _ibtn("🗑 O'chirish", callback_data=f"act:delask:{bot_id}")],
            [_ibtn("💳 Oylik to'lash", callback_data=f"monthly:show:{bot_id}")],
        ]
        # Kod botiga token tugmasi har doim ko'rinadi: token yo'q bo'lsa kiritish,
        # bor bo'lsa almashtirish (masalan, token oshkor bo'lganda yangilash uchun).
        if kind == "code":
            label = "🔑 Token kiritish" if not has_token else "🔑 Token almashtirish"
            rows.append([_ibtn(label, callback_data=f"act:codetoken:{bot_id}")])
        # Kod boti: yangi kodni baza o'chmasdan almashtirish.
        if kind == "code":
            rows.append([_ibtn("🔄 Kod yangilash", callback_data=f"act:codeupdate:{bot_id}")])
            rows.append([_ibtn("📦 Baza yuklash", callback_data=f"act:dbupdate:{bot_id}")])
        return InlineKeyboardMarkup(rows)

    async def my_bots(update, context):
        user_id = update.effective_user.id
        rows = get_user_bots(user_id)
        if not rows:
            await update.message.reply_text("Sizda hali bot yo'q. «➕ Bot qo'shish (token)» yoki «📁 Bot qo'shish (kod)» ni bosing.")
            return
        for row in rows:
            sync_status(row["id"])
            row = get_bot(row["id"])
            status = "▶️ ishlayapti" if row["status"] == "running" and is_running(row["id"]) else "⏹ to'xtagan"
            kind = "token" if row["kind"] == "token" else "kod"
            name = f"@{row['bot_username']}" if row["bot_username"] else "(kod usuli)"
            btype = ""
            if row["kind"] == "token":
                bt = (row["bot_type"] if "bot_type" in row.keys() else None) or "anime"
                btype = f"\nTuri: {TEMPLATE_NAMES.get(bt, bt)}"
                if "panel_style" in row.keys() and row["panel_style"]:
                    btype += f"\nPanel: {PANEL_STYLES.get(row['panel_style'], row['panel_style'])}"
            _paid, _until_label = monthly_status(row)
            _monthly_line = "" if is_super_admin(user_id) else f"\n💳 Oylik: {_until_label}"
            await update.message.reply_text(
                f"🤖 Bot #{row['id']} — {name}\nUsul: {kind}{btype}\nHolat: {status}\nYaratilgan: {row['created_at']}{_monthly_line}",
                reply_markup=bot_buttons(row["id"], kind=row["kind"], has_token=bool(row["token_enc"])),
            )

    # ---------- userbotlar (alohida bo'lim) ----------

    def userbot_buttons(ub_id: int):
        return InlineKeyboardMarkup([
            [_ibtn("▶️ Ishga tushirish", callback_data=f"ubact:start:{ub_id}"),
             _ibtn("⏹ To'xtatish", callback_data=f"ubact:stop:{ub_id}")],
            [_ibtn("📄 Log", callback_data=f"ubact:log:{ub_id}"),
             _ibtn("🗑 O'chirish", callback_data=f"ubact:delask:{ub_id}")],
        ])

    async def show_userbots(update, context):
        user_id = update.effective_user.id
        rows = get_user_userbots(user_id)
        if not rows:
            await update.message.reply_text(
                "👤 Userbotlar bo'limi — bu yerda faqat userbotlar, Bot API botlari emas.\n\n"
                "Yangi userbot qo'shish uchun «📷 QR orqali» ni bosing (kod yozmaysiz) "
                "yoki «📱 Telefon orqali» ni bosing.\n"
                "Kerak bo'ladi (ketma-ket so'raladi):\n"
                "1) API_ID — my.telegram.org dan\n"
                "2) API_HASH — my.telegram.org dan\n"
                "3) QR ni Telegram ilovangda skanerlash YOKI telefon raqami + Telegram kodi "
                "YOKI tayyor sessiya stringi — Telethon StringSession\n\n"
                "⚠️ Telefon kodi va sessiya stringi hisob kalitiga teng. Ularni yuborganingizda serverda shifrlab saqlanadi, "
                "lekin server operatori ham texnik jihatdan ko'ra oladi. Faqat o'z hisobingiz uchun ishlating, "
                "kodni boshqa hech kimga/botga bermang.\n"
                f"Limit: har biriga {MAX_USERBOTS_PER_USER} ta, jami {MAX_USERBOTS_TOTAL} ta userbot.",
                reply_markup=InlineKeyboardMarkup([[
                    _ibtn("📷 QR orqali", callback_data="ubact:qradd:0"),
                    _ibtn("📱 Telefon orqali", callback_data="ubact:phoneadd:0"),
                ], [
                    _ibtn("➕ Userbot qo'shish", callback_data="ubact:add:0"),
                ]]),
            )
            return
        for row in rows:
            sync_userbot_status(row["id"])
            row = get_userbot(row["id"])
            status = "▶️ ishlayapti" if row["status"] == "running" and is_userbot_running(row["id"]) else "⏹ to'xtagan"
            await update.message.reply_text(
                f"👤 Userbot #{row['id']}\nHolat: {status}\nYaratilgan: {row['created_at']}",
                reply_markup=userbot_buttons(row["id"]),
            )
        await update.message.reply_text(
            "Yangi userbot qo'shish uchun:",
            reply_markup=InlineKeyboardMarkup([[
                _ibtn("📷 QR orqali", callback_data="ubact:qradd:0"),
                _ibtn("📱 Telefon orqali", callback_data="ubact:phoneadd:0"),
            ], [
                _ibtn("➕ Userbot qo'shish", callback_data="ubact:add:0"),
            ]]),
        )

    async def ask_userbot(update, context):
        user_id = update.effective_user.id
        if count_userbots_user(user_id) >= MAX_USERBOTS_PER_USER and not is_super_admin(user_id):
            await update.message.reply_text(f"Limit: har biriga {MAX_USERBOTS_PER_USER} ta userbot.")
            return
        if count_userbots_total() >= MAX_USERBOTS_TOTAL:
            await update.message.reply_text("Userbotlar joyi to'lgan (jami limit). Keyinroq urinib ko'ring.")
            return
        if not has_premium(user_id):
            await update.message.reply_text(
                f"💎 Userbot qo'shish ham pullik: 1 ta — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐.\n"
                "Avval «💎 Premium olish» dan to'lang."
            )
            return
        context.user_data["await"] = "ub_api_id"
        context.user_data.pop("ub_api_id", None)
        context.user_data.pop("ub_api_hash", None)
        context.user_data.pop("ub_phone_first", None)
        context.user_data.pop("ub_qr_first", None)
        await update.message.reply_text("👤 Userbot qo'shamiz.\n1/3: API_ID ni yuboring (faqat raqam, my.telegram.org dan):")

    async def handle_ub_api_id(update, context, text):
        if not text.strip().isdigit():
            await update.message.reply_text("API_ID faqat raqam bo'lishi kerak. Qayta yuboring:")
            return
        context.user_data["ub_api_id"] = text.strip()
        context.user_data["await"] = "ub_api_hash"
        await update.message.reply_text("2/3: API_HASH ni yuboring (my.telegram.org dan):")

    async def handle_ub_api_hash(update, context, text):
        if len(text.strip()) < 10:
            await update.message.reply_text("API_HASH juda qisqa. Qayta yuboring:")
            return
        context.user_data["ub_api_hash"] = text.strip()
        if context.user_data.get("ub_phone_first"):
            context.user_data["await"] = "ub_phone"
            await update.message.reply_text(
                "3) Telefon raqamingizni yuboring (+998 bilan, masalan +998901234567):"
            )
            return
        if context.user_data.get("ub_qr_first"):
            context.user_data["await"] = "ub_qr_wait"
            await handle_ub_qr_start(update, context)
            return
        context.user_data["await"] = "ub_method"
        await update.message.reply_text(
            "Qaysi usulda davom etamiz?",
            reply_markup=InlineKeyboardMarkup([[
                _ibtn("📷 QR orqali", callback_data="ubact:qr:0"),
                _ibtn("📱 Telefon orqali", callback_data="ubact:phone:0"),
            ], [
                _ibtn("🔑 Sessiya stringi", callback_data="ubact:session:0"),
            ]]),
        )

    async def _clear_phone_client(user_id: int):
        data = _ub_phone_clients.pop(user_id, None)
        if data and data.get("client") is not None:
            try:
                await data["client"].disconnect()
            except Exception:
                pass

    async def _cancel_qr_task(user_id: int):
        task = _ub_qr_tasks.pop(user_id, None)
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass

    async def _wait_qr_login(bot, app, user_id: int, chat_id: int):
        """QR skanerlanishini kutadi. Muvaffaqiyatda userbot yaratadi."""
        data = _ub_phone_clients.get(user_id)
        if not data or data.get("client") is None or data.get("qr_login") is None:
            return
        client = data["client"]
        qr_login = data["qr_login"]
        try:
            await qr_login.wait()
        except Exception as e:
            name = type(e).__name__
            if "SessionPasswordNeeded" in name:
                # 2FA paroli kerak — mijozni saqlab, parol so'rashga o'tamiz.
                try:
                    ud = app.user_data.get(user_id, {})
                    ud["await"] = "ub_password"
                except Exception:
                    pass
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="QR skanerlandi, lekin ikki bosqichli tasdiqlash yoqilgan. "
                             "Telegram parolingizni yuboring (kod EMAS):",
                    )
                except Exception:
                    pass
                return
            log.warning("Userbot QR xatosi: %s: %s", name, e)
            await _clear_phone_client(user_id)
            try:
                ud = app.user_data.get(user_id, {})
                ud.pop("await", None)
                ud.pop("ub_api_id", None)
                ud.pop("ub_api_hash", None)
                ud.pop("ub_qr_first", None)
            except Exception:
                pass
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="QR eskirgan yoki skanerlanmadi. «👤 Userbotlar» dan qayta boshlang.",
                )
            except Exception:
                pass
            return
        # Muvaffaqiyat: sessiyani saqlab, userbot yaratamiz.
        try:
            session = client.session.save()
        except Exception as e:
            log.warning("Userbot QR sessiya xatosi: %s", e)
            await _clear_phone_client(user_id)
            return
        await _clear_phone_client(user_id)
        api_id = ""
        api_hash = ""
        try:
            ud = app.user_data.get(user_id, {})
            api_id = ud.get("ub_api_id", "")
            api_hash = ud.get("ub_api_hash", "")
            ud.pop("await", None)
            ud.pop("ub_api_id", None)
            ud.pop("ub_api_hash", None)
            ud.pop("ub_qr_first", None)
        except Exception:
            pass
        if not use_credit(user_id):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="Premium kredit topilmadi. Avval «💎 Premium olish» dan to'lang.",
                )
            except Exception:
                pass
            return
        ub_id = create_userbot(user_id, encrypt_token(api_id), encrypt_token(api_hash),
                               encrypt_token(session), token_fingerprint(session))
        ok, msg = start_userbot(ub_id)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"👤 Userbot #{ub_id} yaratildi (QR orqali).\nHolat: {msg}\n"
                     "Logni Userbotlar bo'limidan «📄 Log» dan ko'ring.",
            )
        except Exception:
            pass

    async def handle_ub_qr_start(update, context):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        await _cancel_qr_task(user_id)
        await _clear_phone_client(user_id)
        api_id = context.user_data.get("ub_api_id", "")
        api_hash = context.user_data.get("ub_api_hash", "")
        try:
            api_id_int = int(api_id)
        except Exception:
            context.user_data["await"] = "ub_api_id"
            await context.bot.send_message(
                chat_id=chat_id,
                text="API_ID noto'g'ri. «👤 Userbotlar» dan qayta boshlang.",
            )
            return
        try:
            import io as _io
            import qrcode as _qrcode
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            client = TelegramClient(StringSession(), api_id_int, api_hash)
            await client.connect()
            qr_login = await client.qr_login()
            url = qr_login.url
            buf = _io.BytesIO()
            img = _qrcode.make(url)
            img.save(buf, format="PNG")
            buf.name = "qr.png"
            buf.seek(0)
            _ub_phone_clients[user_id] = {
                "client": client, "qr_login": qr_login,
                "api_id": api_id, "api_hash": api_hash,
            }
        except Exception as e:
            await _clear_phone_client(user_id)
            log.warning("Userbot QR yaratish xatosi: %s: %s", type(e).__name__, e)
            await context.bot.send_message(
                chat_id=chat_id,
                text="QR yaratib bo'lmadi. API ma'lumotlarini tekshirib, «👤 Userbotlar» dan qayta boshlang.",
            )
            context.user_data.pop("await", None)
            return
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=buf,
                caption="📷 Shu QR ni Telegram ilovangda skanerla:\n"
                        "Telegram → Sozlamalar → Qurilmalar → Qurilmani ulash.\n"
                        "Kod yozmaysiz. QR eskirsa, qayta boshlash kerak bo'ladi.",
            )
        except Exception as e:
            await _clear_phone_client(user_id)
            log.warning("Userbot QR yuborish xatosi: %s: %s", type(e).__name__, e)
            await context.bot.send_message(
                chat_id=chat_id,
                text="QR yuborib bo'lmadi. «👤 Userbotlar» dan qayta boshlang.",
            )
            context.user_data.pop("await", None)
            return
        context.user_data["await"] = "ub_qr_wait"
        try:
            import asyncio as _asyncio
            task = _asyncio.create_task(
                _wait_qr_login(context.bot, context.application, user_id, chat_id)
            )
            _ub_qr_tasks[user_id] = task
        except Exception as e:
            log.warning("Userbot QR kutish xatosi: %s: %s", type(e).__name__, e)

    async def _finish_phone_userbot(update, context, session: str):
        api_id = context.user_data.get("ub_api_id", "")
        api_hash = context.user_data.get("ub_api_hash", "")
        context.user_data.pop("await", None)
        context.user_data.pop("ub_api_id", None)
        context.user_data.pop("ub_api_hash", None)
        context.user_data.pop("ub_phone_first", None)
        context.user_data.pop("ub_qr_first", None)
        user_id = update.effective_user.id
        if not use_credit(user_id):
            await update.message.reply_text("Premium kredit topilmadi. Avval «💎 Premium olish» dan to'lang.")
            return
        ub_id = create_userbot(user_id, encrypt_token(api_id), encrypt_token(api_hash),
                               encrypt_token(session), token_fingerprint(session))
        ok, msg = start_userbot(ub_id)
        await update.message.reply_text(
            f"👤 Userbot #{ub_id} yaratildi (telefon orqali).\nHolat: {msg}\n"
            "Logni Userbotlar bo'limidan «📄 Log» dan ko'ring. "
            "Agar ishga tushmasa, o'chirib qayta qo'shing."
        )

    async def handle_ub_phone(update, context, text):
        phone = "".join(ch for ch in text.strip() if ch.isdigit() or ch == "+")
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 9 or len(digits) > 15:
            await update.message.reply_text(
                "Telefon raqami noto'g'ri. +998 bilan to'liq yuboring, masalan +998901234567:"
            )
            return
        if not phone.startswith("+"):
            phone = "+" + digits
        user_id = update.effective_user.id
        await _clear_phone_client(user_id)
        api_id = context.user_data.get("ub_api_id", "")
        api_hash = context.user_data.get("ub_api_hash", "")
        try:
            api_id_int = int(api_id)
        except Exception:
            context.user_data["await"] = "ub_api_id"
            await update.message.reply_text("API_ID noto'g'ri. «👤 Userbotlar» dan qayta boshlang.")
            return
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            client = TelegramClient(StringSession(), api_id_int, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                session = client.session.save()
                await client.disconnect()
                return await _finish_phone_userbot(update, context, session)
            result = await client.send_code_request(phone)
            _ub_phone_clients[user_id] = {
                "client": client, "phone": phone,
                "phone_code_hash": result.phone_code_hash,
            }
        except Exception as e:
            await _clear_phone_client(user_id)
            name = type(e).__name__
            if "FloodWait" in name:
                secs = getattr(e, "seconds", 0)
                await update.message.reply_text(
                    f"Telegram biroz kutishni so'radi ({secs} soniya). Keyinroq qayta urinib ko'ring."
                )
                return
            if "PhoneNumberInvalid" in name:
                await update.message.reply_text("Telefon raqami noto'g'ri. Qayta yuboring:")
                return
            if "ApiIdInvalid" in name or "ApiHash" in name:
                await update.message.reply_text(
                    "API_ID / API_HASH noto'g'ri. my.telegram.org dan tekshirib, «👤 Userbotlar» dan qayta boshlang."
                )
                context.user_data.pop("await", None)
                return
            log.warning("Telefon orqali userbot xatosi: %s: %s", name, e)
            await update.message.reply_text(
                "Kod yuborib bo'lmadi. Raqamni va API ma'lumotlarini tekshirib qayta urinib ko'ring."
            )
            return
        context.user_data["await"] = "ub_code"
        await update.message.reply_text(
            "Telegram ilovangizga kelgan kodni yuboring.\n"
            "⚠️ Kodni faqat shu botga yuboring, boshqa hech kimga bermang. "
            "Yuborganingizdan keyin xabaringizni o'chirishga harakat qilaman."
        )

    async def handle_ub_code(update, context, text):
        code = "".join(ch for ch in text.strip() if ch.isdigit())
        try:
            await update.message.delete()
        except Exception:
            pass
        if len(code) not in (5, 6):
            # Butun xabarni forward qilishda raqamlar aralashib ketishi mumkin:
            # mijozni bekor qilmasdan, faqat raqamlarni so'rash kerak.
            await update.message.reply_text(
                "Kod faqat 5-6 ta raqam bo'lishi kerak. Butun xabarni forward qilma, "
                "faqat kod raqamlarini yozib yubor:"
            )
            return
        user_id = update.effective_user.id
        data = _ub_phone_clients.get(user_id)
        if not data or data.get("client") is None:
            context.user_data.pop("await", None)
            await update.message.reply_text("Oqim uzildi. «👤 Userbotlar» dan qayta boshlang.")
            return
        client = data["client"]
        try:
            from telethon.errors import SessionPasswordNeededError
            await client.sign_in(data["phone"], code, phone_code_hash=data.get("phone_code_hash"))
        except Exception as e:
            name = type(e).__name__
            if "SessionPasswordNeeded" in name:
                context.user_data["await"] = "ub_password"
                await update.message.reply_text(
                    "Ikki bosqichli tasdiqlash yoqilgan. Telegram parolingizni yuboring (kod EMAS):"
                )
                return
            if "PhoneCodeInvalid" in name:
                log.warning("Userbot kod xatosi: %s (kod uzunligi %s)", name, len(code))
                await update.message.reply_text("Kod noto'g'ri. Qayta yuboring:")
                return
            if "PhoneCodeExpired" in name:
                log.warning("Userbot kod xatosi: %s (kod uzunligi %s)", name, len(code))
                await _clear_phone_client(user_id)
                context.user_data["await"] = "ub_phone"
                await update.message.reply_text("Kod eskirgan. Telefon raqamingizni qayta yuboring:")
                return
            if "FloodWait" in name:
                await update.message.reply_text("Telegram biroz kutishni so'radi. Birozdan keyin kodni qayta yuboring.")
                return
            log.warning("Userbot kod xatosi: %s: %s", name, e)
            await update.message.reply_text("Kod qabul qilinmadi. Qayta yuboring:")
            return
        session = client.session.save()
        await _clear_phone_client(user_id)
        await _finish_phone_userbot(update, context, session)

    async def handle_ub_password(update, context, text):
        password = text.strip()
        try:
            await update.message.delete()
        except Exception:
            pass
        if not password:
            await update.message.reply_text("Parolni yuboring:")
            return
        user_id = update.effective_user.id
        data = _ub_phone_clients.get(user_id)
        if not data or data.get("client") is None:
            context.user_data.pop("await", None)
            await update.message.reply_text("Oqim uzildi. «👤 Userbotlar» dan qayta boshlang.")
            return
        client = data["client"]
        try:
            await client.sign_in(password=password)
        except Exception as e:
            name = type(e).__name__
            if "PasswordHashInvalid" in name:
                await update.message.reply_text("Parol noto'g'ri. Qayta yuboring:")
                return
            log.warning("Userbot parol xatosi: %s: %s", name, e)
            await update.message.reply_text("Parol qabul qilinmadi. Qayta yuboring:")
            return
        session = client.session.save()
        await _clear_phone_client(user_id)
        await _finish_phone_userbot(update, context, session)

    async def handle_ub_session(update, context, text):
        session = text.strip()
        try:
            await update.message.delete()
        except Exception:
            pass
        api_id = context.user_data.get("ub_api_id", "")
        api_hash = context.user_data.get("ub_api_hash", "")
        context.user_data.pop("await", None)
        context.user_data.pop("ub_api_id", None)
        context.user_data.pop("ub_api_hash", None)
        if len(session) < 20:
            await update.message.reply_text("Sessiya stringi juda qisqa. «👤 Userbotlar» dan qayta boshlang.")
            return
        user_id = update.effective_user.id
        if not use_credit(user_id):
            await update.message.reply_text("Premium kredit topilmadi. Avval «💎 Premium olish» dan to'lang.")
            return
        ub_id = create_userbot(user_id, encrypt_token(api_id), encrypt_token(api_hash),
                               encrypt_token(session), token_fingerprint(session))
        ok, msg = start_userbot(ub_id)
        await update.message.reply_text(
            f"👤 Userbot #{ub_id} yaratildi.\nHolat: {msg}\n"
            "Logni Userbotlar bo'limidan «📄 Log» dan ko'ring. "
            "Agar ishga tushmasa, sessiya noto'g'ri yoki eskirgan bo'lishi mumkin — o'chirib qayta qo'shing."
        )

    async def userbot_action(update, context):
        q = update.callback_query
        await q.answer()
        _, action, raw_id = q.data.split(":", 2)
        ub_id = int(raw_id)
        if action == "add":
            # Callback dan so'ralganda matnli so'rov uchun oddiy xabar yuborish
            await q.message.reply_text("👤 Userbot qo'shamiz — avval API_ID ni yuboring:")
            context.user_data["await"] = "ub_api_id"
            context.user_data.pop("ub_phone_first", None)
            context.user_data.pop("ub_qr_first", None)
            return
        if action == "qradd":
            if count_userbots_user(q.from_user.id) >= MAX_USERBOTS_PER_USER and not is_super_admin(q.from_user.id):
                await q.message.reply_text(f"Limit: har biriga {MAX_USERBOTS_PER_USER} ta userbot.")
                return
            if count_userbots_total() >= MAX_USERBOTS_TOTAL:
                await q.message.reply_text("Userbotlar joyi to'lgan (jami limit). Keyinroq urinib ko'ring.")
                return
            if not has_premium(q.from_user.id):
                await q.message.reply_text(
                    f"💎 Userbot qo'shish ham pullik: 1 ta — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐.\n"
                    "Avval «💎 Premium olish» dan to'lang."
                )
                return
            await q.message.reply_text("📷 QR orqali userbot qo'shamiz — avval API_ID ni yuboring:")
            context.user_data["await"] = "ub_api_id"
            context.user_data["ub_qr_first"] = True
            context.user_data.pop("ub_phone_first", None)
            return
        if action == "qr":
            context.user_data["await"] = "ub_qr_wait"
            await handle_ub_qr_start(update, context)
            return
        if action == "phoneadd":
            if count_userbots_user(q.from_user.id) >= MAX_USERBOTS_PER_USER and not is_super_admin(q.from_user.id):
                await q.message.reply_text(f"Limit: har biriga {MAX_USERBOTS_PER_USER} ta userbot.")
                return
            if count_userbots_total() >= MAX_USERBOTS_TOTAL:
                await q.message.reply_text("Userbotlar joyi to'lgan (jami limit). Keyinroq urinib ko'ring.")
                return
            if not has_premium(q.from_user.id):
                await q.message.reply_text(
                    f"💎 Userbot qo'shish ham pullik: 1 ta — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐.\n"
                    "Avval «💎 Premium olish» dan to'lang."
                )
                return
            await q.message.reply_text("📱 Telefon orqali userbot qo'shamiz — avval API_ID ni yuboring:")
            context.user_data["await"] = "ub_api_id"
            context.user_data["ub_phone_first"] = True
            context.user_data.pop("ub_qr_first", None)
            return
        if action == "phone":
            await q.message.reply_text(
                "Telefon raqamingizni yuboring (+998 bilan, masalan +998901234567):"
            )
            context.user_data["await"] = "ub_phone"
            return
        if action == "session":
            await q.message.reply_text(
                "Sessiya stringini yuboring.\n"
                "Bu telefon kodi EMAS. Telethon StringSession stringi kerak.\n"
                "Yuborganingizdan keyin xabaringizni o'chirishga harakat qilaman, "
                "sessiya bazada shifrlab saqlanadi."
            )
            context.user_data["await"] = "ub_session"
            return
        row = get_userbot(ub_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Userbot topilmadi yoki sizniki emas.")
            return
        if action == "start":
            ok, msg = start_userbot(ub_id)
            await q.edit_message_text(f"👤 Userbot #{ub_id}: {msg}")
        elif action == "stop":
            ok, msg = stop_userbot(ub_id)
            await q.edit_message_text(f"👤 Userbot #{ub_id}: {msg}")
        elif action == "log":
            try:
                data = userbot_log_path(ub_id).read_text(encoding="utf-8", errors="ignore")[-2000:] if userbot_log_path(ub_id).exists() else "(log yo'q)"
            except Exception as e:
                data = f"Logni o'qib bo'lmadi: {e}"
            await q.edit_message_text(f"👤 Userbot #{ub_id} log (oxiri):\n{data}")
        elif action == "delask":
            await q.edit_message_text(
                f"👤 Userbot #{ub_id} ni o'chirasizmi? Sessiya ham o'chadi.",
                reply_markup=InlineKeyboardMarkup([[
                    _ibtn("✅ Ha, o'chir", callback_data=f"ubact:del:{ub_id}"),
                    _ibtn("❌ Bekor", callback_data=f"ubact:cancel:{ub_id}"),
                ]]),
            )
        elif action == "del":
            stop_userbot(ub_id)
            delete_userbot_row(ub_id)
            await q.edit_message_text(f"👤 Userbot #{ub_id} o'chirildi.")
        elif action == "cancel":
            await q.edit_message_text(f"👤 Userbot #{ub_id} o'chirish bekor qilindi.")

    async def bot_action(update, context):
        q = update.callback_query
        await q.answer()
        _, action, raw_id = q.data.split(":", 2)
        bot_id = int(raw_id)
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
            return
        if action == "codeupdate":
            if row["kind"] != "code":
                await q.edit_message_text(f"Bot #{bot_id}: kod yangilash faqat kod usulidagi botlar uchun.")
                return
            context.user_data["await"] = "code_update"
            context.user_data["code_bot_id"] = bot_id
            await q.message.reply_text(
                f"Bot #{bot_id} uchun yangi .py faylni yuboring.\n"
                "Baza o'chmaydi — faqat kod fayli almashtiriladi, keyin bot qayta ishga tushadi. "
                "Avval sintaksis tekshiriladi: xato bo'lsa eski kod ishlashda davom etadi."
            )
            return
        if action == "dbupdate":
            if row["kind"] != "code":
                await q.edit_message_text(f"Bot #{bot_id}: baza yuklash faqat kod usulidagi botlar uchun.")
                return
            context.user_data["await"] = "db_update"
            context.user_data["code_bot_id"] = bot_id
            await q.message.reply_text(
                f"Bot #{bot_id} uchun baza .db faylini yuboring (masalan, animed_backup_....db).\n"
                "Bot avval to'xtatiladi, eski baza zaxiraga olinadi, yangi baza tekshiriladi: "
                "SQLite bo'lishi va animelar + serial_qismlar jadvallari bo'lishi kerak. "
                "Tekshiruvdan o'tmasa eski baza qaytariladi. O'tsa bot yangi baza bilan qayta ishga tushadi."
            )
            return
        if action == "codetoken":
            if row["kind"] != "code":
                await q.edit_message_text(f"Bot #{bot_id}: token faqat kod usulidagi botlar uchun kiritiladi.")
                return
            context.user_data["await"] = "code_token"
            context.user_data["code_bot_id"] = bot_id
            replacing = bool(row["token_enc"])
            await q.message.reply_text(
                f"Bot #{bot_id} uchun tokenni yuboring.\n"
                "Token getMe bilan tekshirilib, shifrlab saqlanadi va ishga tushirishda "
                "BOT_TOKEN env orqali kodga beriladi. Token chatdan o'chirishga harakat qilinadi."
                + (" Yangi token eski token o'rniga qo'yiladi, bot to'xtab yangi token bilan qayta ishga tushadi." if replacing else "")
            )
            return
        if action == "start":
            ok, msg = start_bot(bot_id)
            await q.edit_message_text(f"Bot #{bot_id}: {msg}")
        elif action == "stop":
            ok, msg = stop_bot(bot_id)
            await q.edit_message_text(f"Bot #{bot_id}: {msg}")
        elif action == "log":
            tail = read_last_log(bot_id)
            await q.message.reply_text(f"📄 Bot #{bot_id} logi (oxirgi qatorlar):\n\n{tail}")
        elif action == "delask":
            kb = InlineKeyboardMarkup([[
                _ibtn("Ha, o'chir ✅", callback_data=f"act:delyes:{bot_id}"),
                _ibtn("Yo'q ❌", callback_data=f"act:delno:{bot_id}"),
            ]])
            await q.edit_message_text(f"Bot #{bot_id} o'chirilsinmi? Papkasi va bazasi ham o'chadi.", reply_markup=kb)
        elif action == "delyes":
            stop_bot(bot_id)
            work = bot_work_dir(bot_id)
            code_dir = row["code_dir"]
            delete_bot_row(bot_id)
            shutil.rmtree(work, ignore_errors=True)
            if code_dir and Path(code_dir).exists() and Path(code_dir) != work:
                shutil.rmtree(code_dir, ignore_errors=True)
            await q.edit_message_text(f"Bot #{bot_id} o'chirildi 🗑")
        elif action == "delno":
            await q.edit_message_text(f"Bot #{bot_id} qoldirildi.")

    # ---------- oylik to'lov (Stars avtomatik + karta qo'lda) ----------

    async def show_monthly(update, context):
        q = update.callback_query
        await q.answer()
        parts = q.data.split(":")
        bot_id = int(parts[2]) if len(parts) > 2 else 0
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
            return
        _paid, _label = monthly_status(row)
        kb = InlineKeyboardMarkup([
            [_ibtn(f"⭐ {MONTHLY_STARS} Stars bilan to'lash", callback_data=f"monthly:stars:{bot_id}")],
            [_ibtn("💳 Karta orqali to'lash (so'm)", callback_data=f"monthly:card:{bot_id}")],
            [_ibtn(f"💰 Hamyondan to'lash ({get_balance(q.from_user.id):,} so'm)", callback_data=f"monthly:wallet:{bot_id}")],
        ])
        await q.edit_message_text(
            f"💳 Bot #{bot_id} uchun oylik server to'lovi\n"
            f"Narxi: {MONTHLY_SOM:,} so'm / {MONTHLY_STARS} ⭐ (30 kun)\n"
            f"Hozirgi holat: {_label}\n\n"
            "⭐ Stars — avtomatik: to'lagan zahoti oylik uzayadi.\n"
            "💳 Karta — qo'lda: to'lab chek yuborasiz, super admin tasdiqlasa oylik uzayadi.\n"
            "💰 Hamyon — balans yetarli bo'lsa, shu zahoti yechiladi; muddat tugaganda "
            "ham balansdan avtomatik yechiladi.",
            reply_markup=kb,
        )

    async def monthly_stars(update, context):
        from telegram import LabeledPrice

        q = update.callback_query
        await q.answer()
        bot_id = int(q.data.split(":")[2])
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
            return
        if is_super_admin(q.from_user.id):
            await q.edit_message_text("Siz super admins — oylik to'lov kerak emas.")
            return
        try:
            await context.bot.send_invoice(
                chat_id=q.message.chat_id,
                title=f"Oylik server to'lovi — Bot #{bot_id}",
                description=f"Bot #{bot_id} ni serverda yana 30 kun ushlab turish. Narxi: {MONTHLY_SOM:,} so'm / {MONTHLY_STARS} ⭐.",
                payload=f"monthly:{bot_id}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(f"Oylik — Bot #{bot_id}", MONTHLY_STARS)],
            )
        except Exception as e:
            await q.message.reply_text(f"To'lov invoysini yuborib bo'lmadi: {e}")

    async def monthly_card(update, context):
        q = update.callback_query
        await q.answer()
        bot_id = int(q.data.split(":")[2])
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
            return
        card_info = get_config("card_info")
        if not card_info:
            await q.edit_message_text(
                "💳 Karta to'lovi hozircha sozlanmagan.\n"
                "Super admin hali karta ma'lumotini kiritmagan — "
                "«⭐ Stars» bilan to'lang yoki super adminga yozing."
            )
            return
        kb = InlineKeyboardMarkup([[
            _ibtn("✅ To'ladim, chek yuboraman", callback_data=f"monthly:carddone:{bot_id}"),
        ]])
        await q.edit_message_text(
            f"💳 Bot #{bot_id} uchun oylik: {MONTHLY_SOM:,} so'm\n\n"
            f"{card_info}\n\n"
            "To'lovni shu rekvizitga o'tkazing, keyin «✅ To'ladim» ni bosib "
            "chek rasmini yuboring. Super admin tasdiqlasa, oylik 30 kunga uzayadi.",
            reply_markup=kb,
        )

    async def monthly_card_done(update, context):
        q = update.callback_query
        await q.answer()
        bot_id = int(q.data.split(":")[2])
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
            return
        context.user_data["await"] = "card_check"
        context.user_data["card_kind"] = "monthly"
        context.user_data["card_bot_id"] = bot_id
        await q.message.reply_text(
            f"Bot #{bot_id} uchun {MONTHLY_SOM:,} so'm to'lov cheki rasmini yuboring "
            "(foto yoki fayl). Chek super adminga boradi, tasdiqlasa oylik uzayadi."
        )

    async def monthly_router(update, context):
        action = update.callback_query.data.split(":")[1]
        if action == "show":
            return await show_monthly(update, context)
        if action == "stars":
            return await monthly_stars(update, context)
        if action == "card":
            return await monthly_card(update, context)
        if action == "carddone":
            return await monthly_card_done(update, context)
        if action == "wallet":
            return await monthly_wallet_pay(update, context)

    async def cardpay_decide(update, context):
        """Super adminning karta to'lovini tasdiqlash / rad etishi."""
        q = update.callback_query
        await q.answer()
        if not is_super_admin(q.from_user.id):
            await q.edit_message_text("Faqat super admin tasdiqlay oladi.")
            return
        _prefix, action, raw_id = q.data.split(":", 2)
        req_id = int(raw_id)
        req = get_card_payment(req_id)
        if req is None or req["status"] != "pending":
            await q.edit_message_text("Bu to'lov so'rovi allaqachon ko'rib chiqilgan.")
            return
        if action == "approve":
            decide_card_payment(req_id, "approved")
            if int(req["bot_id"]) == 0:
                new_balance = add_balance(req["user_id"], int(req["amount_som"]))
                await q.edit_message_text(
                    f"✅ Karta to'lovi tasdiqlandi: so'rov #{req_id}, "
                    f"hamyonga {int(req['amount_som']):,} so'm tushdi. "
                    f"Balans endi: {new_balance:,} so'm."
                )
                try:
                    await context.bot.send_message(
                        chat_id=req["user_id"],
                        text=f"✅ Karta to'lovingiz tasdiqlandi! Hamyoningizga "
                             f"{int(req['amount_som']):,} so'm tushdi. "
                             f"Balans: {new_balance:,} so'm.",
                    )
                except Exception:
                    pass
                return
            new_until = extend_monthly(req["bot_id"])
            await q.edit_message_text(
                f"✅ Karta to'lovi tasdiqlandi: so'rov #{req_id}, Bot #{req['bot_id']} "
                f"oyligi {new_until[:10]} gacha uzaytirildi."
            )
            try:
                await context.bot.send_message(
                    chat_id=req["user_id"],
                    text=f"✅ Karta to'lovingiz tasdiqlandi! Bot #{req['bot_id']} oyligi "
                         f"{new_until[:10]} gacha uzaytirildi.",
                )
            except Exception:
                pass
        else:
            decide_card_payment(req_id, "rejected")
            await q.edit_message_text(f"❌ Karta to'lovi rad etildi: so'rov #{req_id}.")
            try:
                await context.bot.send_message(
                    chat_id=req["user_id"],
                    text="❌ Karta to'lovingiz rad etildi. "
                         "Chekni tekshirib, qayta yuboring yoki Stars bilan to'lang.",
                )
            except Exception:
                pass

    async def handle_card_check_photo(update, context):
        """Foydalanuvchi chek rasmini yuborganda: super adminga so'rov boradi."""
        kind = context.user_data.get("card_kind", "monthly")
        if kind == "wallet":
            amount = int(context.user_data.get("card_amount") or 0)
            if not amount:
                await update.message.reply_text("Avval «💰 Hamyonim» → «💳 Karta» dan summani tanlang.")
                return
            context.user_data.pop("await", None)
            context.user_data.pop("card_kind", None)
            context.user_data.pop("card_amount", None)
            # bot_id=0 — hamyon to'ldirish belgisi (oylik emas).
            req_id = create_card_payment(update.effective_user.id, 0, amount)
            await update.message.reply_text(
                f"✅ Chek qabul qilindi (so'rov #{req_id}). Super admin tasdiqlasa, "
                f"{amount:,} so'm hamyoningizga tushadi. Kuting."
            )
            bot_id = 0
        else:
            bot_id = context.user_data.get("card_bot_id")
            if not bot_id:
                await update.message.reply_text("Avval «💳 Oylik to'lash» → «💳 Karta» ni bosing.")
                return
            context.user_data.pop("await", None)
            context.user_data.pop("card_kind", None)
            context.user_data.pop("card_bot_id", None)
            req_id = create_card_payment(update.effective_user.id, bot_id)
            await update.message.reply_text(
                f"✅ Chek qabul qilindi (so'rov #{req_id}). Super admin tasdiqlasa, "
                f"Bot #{bot_id} oyligi 30 kunga uzayadi. Kuting."
            )
        if SUPER_ADMIN_ID:
            kb = InlineKeyboardMarkup([[
                _ibtn("✅ Tasdiqlash", callback_data=f"cardpay:approve:{req_id}"),
                _ibtn("❌ Rad etish", callback_data=f"cardpay:reject:{req_id}"),
            ]])
            try:
                # Avval chekni admin chatiga forward qilamiz, keyin so'rov xabari.
                await context.bot.forward_message(
                    chat_id=SUPER_ADMIN_ID,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id,
                )
                _req = get_card_payment(req_id)
                _purpose = "hamyon to'ldirish" if bot_id == 0 else f"Bot #{bot_id} oyligi"
                _amount = int(_req["amount_som"]) if _req else MONTHLY_SOM
                await context.bot.send_message(
                    chat_id=SUPER_ADMIN_ID,
                    text=f"💳 Yangi karta to'lov so'rovi #{req_id}\n"
                         f"Foydalanuvchi: {update.effective_user.id} (@{update.effective_user.username or ''})\n"
                         f"Maqsad: {_purpose}, summa: {_amount:,} so'm\n"
                         "Yuqoridagi chekni tekshirib, tasdiqlang yoki rad eting.",
                    reply_markup=kb,
                )
            except Exception:
                pass

    # ---------- ichki hamyon ----------

    WALLET_TOPUP_STARS = [50, 150, 300]  # Stars: 1 oy, 3 oy, 6 oy ekvivalenti
    WALLET_TOPUP_SOM = [15000, 45000, 90000]

    async def show_wallet(update, context):
        user_id = update.effective_user.id
        balance = get_balance(user_id)
        months = balance // MONTHLY_SOM
        kb = InlineKeyboardMarkup([
            [_ibtn("⭐ Stars bilan to'ldirish", callback_data="wallet:starsmenu")],
            [_ibtn("💳 Karta bilan to'ldirish", callback_data="wallet:cardmenu")],
        ])
        text = (
            f"💰 Hamyoningiz\n"
            f"Balans: {balance:,} so'm\n"
            f"Bu — taxminan {months} ta oylik to'lov ({MONTHLY_SOM:,} so'm / oy).\n\n"
            "Stars bilan to'ldirsa, 1 ⭐ = 300 so'm kursda balansga tushadi "
            "(50 ⭐ = 15 000 so'm, 1 oy). Karta bilan to'ldirsa, chekni super "
            "admin tasdiqlagach balansga tushadi.\n"
            "Oylik muddati tugaganda, balansda pul bo'lsa, oylik avtomatik "
            "yechilib uzayadi — har oy qo'lda to'lash shart emas."
        )
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            await update.message.reply_text(text, reply_markup=kb)

    async def wallet_stars_menu(update, context):
        q = update.callback_query
        await q.answer()
        rows = [[_ibtn(f"⭐ {s} Stars = {s * WALLET_STAR_RATE:,} so'm", callback_data=f"wallet:stars:{s}")]
                for s in WALLET_TOPUP_STARS]
        await q.edit_message_text(
            "⭐ Qancha to'ldirasiz? To'lov avtomatik, balans shu zahoti ochiladi.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def wallet_card_menu(update, context):
        q = update.callback_query
        await q.answer()
        card_info = get_config("card_info")
        if not card_info:
            await q.edit_message_text(
                "💳 Karta to'lovi hozircha sozlanmagan.\n"
                "Super admin hali karta ma'lumotini kiritmagan — "
                "«⭐ Stars» bilan to'ldiring yoki super adminga yozing."
            )
            return
        rows = [[_ibtn(f"💳 {s:,} so'm", callback_data=f"wallet:card:{s}")]
                for s in WALLET_TOPUP_SOM]
        await q.edit_message_text(
            f"💳 Qancha to'ldirasiz?\n\n{card_info}\n\n"
            "Summani tanlang, keyin to'lab chek rasmini yuboring — "
            "super admin tasdiqlasa balansga tushadi.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def wallet_stars_invoice(update, context):
        from telegram import LabeledPrice

        q = update.callback_query
        await q.answer()
        stars = int(q.data.split(":")[2])
        if stars not in WALLET_TOPUP_STARS:
            await q.edit_message_text("Noma'lum summa.")
            return
        try:
            await context.bot.send_invoice(
                chat_id=q.message.chat_id,
                title="Hamyon to'ldirish",
                description=f"Server bot hamyoniga {stars} ⭐ = {stars * WALLET_STAR_RATE:,} so'm.",
                payload=f"wallet:{stars}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice("Hamyon to'ldirish", stars)],
            )
        except Exception as e:
            await q.message.reply_text(f"To'lov invoysini yuborib bo'lmadi: {e}")

    async def wallet_card_done(update, context):
        q = update.callback_query
        await q.answer()
        amount = int(q.data.split(":")[2])
        if amount not in WALLET_TOPUP_SOM:
            await q.edit_message_text("Noma'lum summa.")
            return
        context.user_data["await"] = "card_check"
        context.user_data["card_kind"] = "wallet"
        context.user_data["card_amount"] = amount
        context.user_data.pop("card_bot_id", None)
        await q.message.reply_text(
            f"Hamyon uchun {amount:,} so'm to'lov cheki rasmini yuboring "
            "(foto yoki fayl). Super admin tasdiqlasa, balansga tushadi."
        )

    async def wallet_router(update, context):
        action = update.callback_query.data.split(":")[1]
        if action == "show":
            return await show_wallet(update, context)
        if action == "starsmenu":
            return await wallet_stars_menu(update, context)
        if action == "cardmenu":
            return await wallet_card_menu(update, context)
        if action == "stars":
            return await wallet_stars_invoice(update, context)
        if action == "card":
            return await wallet_card_done(update, context)

    async def monthly_wallet_pay(update, context):
        """Oylikni hamyondan to'lash (bitta bot uchun, qo'lda)."""
        q = update.callback_query
        await q.answer()
        bot_id = int(q.data.split(":")[2])
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
            return
        if is_super_admin(q.from_user.id):
            await q.edit_message_text("Siz super admins — oylik to'lov kerak emas.")
            return
        if not deduct_balance(q.from_user.id, MONTHLY_SOM):
            await q.edit_message_text(
                f"💰 Hamyoningizda yetarli pul yo'q. Kerak: {MONTHLY_SOM:,} so'm, "
                f"balans: {get_balance(q.from_user.id):,} so'm.\n"
                "Avval «💰 Hamyonim» dan to'ldiring."
            )
            return
        new_until = extend_monthly(bot_id)
        await q.edit_message_text(
            f"✅ Hamyondan to'landi: {MONTHLY_SOM:,} so'm\n"
            f"💳 Bot #{bot_id} oyligi {new_until[:10]} gacha uzaytirildi.\n"
            f"Qolgan balans: {get_balance(q.from_user.id):,} so'm."
        )

    async def check_monthly_wallets(bot):
        """Avtomatik oylik yechish — bir marta tekshiruv (loop ham, test ham shu)."""
        for row in get_all_bots():
            if is_super_admin(row["owner_id"]):
                continue
            _paid, _label = monthly_status(row)
            if _paid:
                continue
            owner_id = row["owner_id"]
            if deduct_balance(owner_id, MONTHLY_SOM):
                new_until = extend_monthly(row["id"])
                try:
                    await bot.send_message(
                        chat_id=owner_id,
                        text=f"💰 Bot #{row['id']} oyligi hamyoningizdan avtomatik uzaytirildi: "
                             f"{MONTHLY_SOM:,} so'm, {new_until[:10]} gacha. "
                             f"Qolgan balans: {get_balance(owner_id):,} so'm.",
                    )
                except Exception:
                    pass
            else:
                # Balans yetmasa, ishlayotgan bot to'xtatiladi (bir marta:
                # status stopped bo'lgach keyingi tekshiruvda xabar takrorlanmaydi).
                was_running = row["status"] == "running" and is_running(row["id"])
                if was_running:
                    stop_bot(row["id"])
                if was_running or row["status"] == "running":
                    try:
                        await bot.send_message(
                            chat_id=owner_id,
                            text=f"⚠️ Bot #{row['id']} oylik muddati tugadi va hamyoningizda "
                                 f"yetarli pul yo'q (kerak: {MONTHLY_SOM:,} so'm, "
                                 f"balans: {get_balance(owner_id):,} so'm). Bot to'xtatildi. "
                                 "«💰 Hamyonim» dan to'ldirib, «🤖 Mening botlarim» dan qayta ishga tushiring.",
                        )
                    except Exception:
                        pass

    async def monthly_wallet_loop(app):
        import asyncio as _asyncio

        while True:
            try:
                await _asyncio.sleep(3600)
                await check_monthly_wallets(app.bot)
            except Exception as e:
                log.warning("Oylik hamyon tekshiruvi xato: %s", e)

    # ---------- 1-usul: token ----------

    async def ask_token(update, context):
        """Bot yasash: avval turini tanlash (anime / kino)."""
        # Super admin uchun kishi limiti yo'q (jami 5 ta limit baribir turadi).
        if not is_super_admin(update.effective_user.id) and count_bots_user(update.effective_user.id) >= MAX_BOTS_PER_USER:
            await update.message.reply_text(f"Limit: hozircha har foydalanuvchiga {MAX_BOTS_PER_USER} ta bot. Avval eskisini o'chiring.")
            return
        if count_bots_total() >= MAX_BOTS:
            await update.message.reply_text("Server to'lgan (tekin server kuchsiz, umumiy limit bor). Keyinroq urinib ko'ring.")
            return
        kb = InlineKeyboardMarkup([
            [_ibtn("🎬 Anime bot", callback_data="make:anime"),
             _ibtn("🎞 Kino bot", callback_data="make:movie")],
            [_ibtn("🧩 Umumiy bot", callback_data="make:general")],
        ])
        await update.message.reply_text(
            "Qanaqa bot yasash kerak?\n"
            "🎬 Anime bot — Uzbek_animelar_olami ga o'xshagan: anime ro'yxati, qismlar.\n"
            "🎞 Kino bot — kinolar ro'yxati, har bir film bitta video.\n"
            "🧩 Umumiy bot — har qanaqa bot: egasi bo'limlar va kontentni "
            "(matn, rasm, video) o'zi to'ldiradi, majburiy obuna + premium tayyor.",
            reply_markup=kb,
        )

    async def make_choice(update, context):
        """Inline tur tanlash: premium tekshiruv, keyin token so'rash."""
        q = update.callback_query
        await q.answer()
        bot_type = q.data.split(":", 1)[1]
        if bot_type not in TEMPLATE_NAMES:
            await q.edit_message_text("Noma'lum bot turi.")
            return
        user_id = q.from_user.id
        if not has_premium(user_id):
            await q.edit_message_text(
                f"💎 Bot yasash pullik: 1 ta bot — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐.\n"
                "Avval «💎 Premium olish» dan Stars bilan to'lang — to'lov avtomatik, "
                "shu zahoti kredit ochiladi.",
                reply_markup=InlineKeyboardMarkup([[
                    _ibtn(f"⭐ {PRICE_STARS} Stars bilan to'lash", callback_data="pay:stars"),
                ]]),
            )
            return
        context.user_data["make_type"] = bot_type
        context.user_data["make_features"] = list(MAKE_FEATURE_KEYS)
        await q.edit_message_text(
            f"{TEMPLATE_NAMES[bot_type]} yasaymiz.\n"
            "Botga nimalar qo'shilsin? Keraklisini belgilang, keraksizini o'chiring, "
            "keyin «➡️ Davom» ni bosing:",
            reply_markup=features_keyboard(context.user_data["make_features"]),
        )

    def features_keyboard(selected):
        rows = []
        for key, label in MAKE_FEATURES:
            mark = "✅" if key in selected else "⬜"
            rows.append([_ibtn(f"{mark} {label}", callback_data=f"makefeat:{key}")])
        rows.append([_ibtn("➡️ Davom", callback_data="makefeat:done")])
        return InlineKeyboardMarkup(rows)

    async def make_feature_toggle(update, context):
        """Funksiya tanlash: belgilash/o'chirish, keyin panel uslubi."""
        q = update.callback_query
        await q.answer()
        action = q.data.split(":", 1)[1]
        selected = context.user_data.get("make_features", list(MAKE_FEATURE_KEYS))
        if action == "done":
            if not selected:
                selected = list(MAKE_FEATURE_KEYS)
                context.user_data["make_features"] = selected
            kb = InlineKeyboardMarkup([[
                _ibtn(label, callback_data=f"makestyle:{key}")
                for key, label in PANEL_STYLES.items()
            ]])
            await q.edit_message_text(
                "Panel qanaqa bo'lsin?\n"
                "🌈 Rangli — emoji ko'p, chiroyli sarlavhalar.\n"
                "✨ Oddiy — hozirgi ko'rinish.\n"
                "🌑 Minimal — emoji kam, sodda tugmalar.",
                reply_markup=kb,
            )
            return
        if action in MAKE_FEATURE_KEYS:
            if action in selected:
                selected.remove(action)
            else:
                selected.append(action)
            context.user_data["make_features"] = selected
            await q.edit_message_text(
                "Botga nimalar qo'shilsin? Keraklisini belgilang, keraksizini o'chiring, "
                "keyin «➡️ Davom» ni bosing:",
                reply_markup=features_keyboard(selected),
            )

    async def make_style_choice(update, context):
        """Panel uslubi tanlash: keyin token so'rash."""
        q = update.callback_query
        await q.answer()
        style = q.data.split(":", 1)[1]
        if style not in PANEL_STYLES:
            style = "colorful"
        context.user_data["make_style"] = style
        context.user_data["await"] = "token"
        bot_type = context.user_data.get("make_type", "anime")
        feats = ", ".join(label for key, label in MAKE_FEATURES if key in context.user_data.get("make_features", [])) or "hammasi"
        await q.message.reply_text(
            f"{TEMPLATE_NAMES[bot_type]} yasaymiz.\n"
            f"Tanlangan funksiyalar: {feats}\n"
            f"Panel uslubi: {PANEL_STYLES[style]}\n"
            "BotFather bergan tokenni yuboring.\n"
            "Token tekshiriladi (getMe), shifrlab saqlanadi, shu zahoti bot ishga "
            "tushadi va shablon fayli ham sizga yuboriladi.\n"
            "Token logga yozilmaydi."
        )

    async def show_premium(update, context):
        user_id = update.effective_user.id
        credits = get_credits(user_id)
        kb = InlineKeyboardMarkup([[
            _ibtn(f"⭐ {PRICE_STARS} Stars bilan to'lash", callback_data="pay:stars"),
        ]])
        await update.message.reply_text(
            f"💎 Premium — 1 ta bot yasash + serverga ulash\n"
            f"Narxi: {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐ (Telegram Stars)\n"
            f"Sizning kreditingiz: {'cheksiz (super admin)' if is_super_admin(user_id) else str(credits) + ' ta bot'}\n\n"
            "To'lov avtomatik: Stars bilan to'lasangiz, shu zahoti kredit ochiladi, "
            "keyin «➕ Bot yasash (token)» dan bot yasay olasiz.\n"
            "Yulduzlar to'g'ridan-to'g'ri shu botning hisobiga tushadi.",
            reply_markup=kb,
        )

    async def pay_stars(update, context):
        """Stars invoysini yuborish (avto to'lov)."""
        from telegram import LabeledPrice

        q = update.callback_query
        await q.answer()
        try:
            await context.bot.send_invoice(
                chat_id=q.message.chat_id,
                title="Premium — 1 ta bot",
                description=(
                    f"Server botda 1 ta bot yasash + serverga ulash. "
                    f"Narxi: {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐."
                ),
                payload="premium-1bot",
                provider_token="",  # Telegram Stars uchun bo'sh bo'ladi
                currency="XTR",
                prices=[LabeledPrice("Premium — 1 ta bot", PRICE_STARS)],
            )
        except Exception as e:
            await q.message.reply_text(f"To'lov invoysini yuborib bo'lmadi: {e}")

    async def precheckout(update, context):
        q = update.pre_checkout_query
        payload = q.invoice_payload or ""
        if payload == "premium-1bot":
            expected = PRICE_STARS
        elif payload.startswith("monthly:"):
            expected = MONTHLY_STARS
        elif payload.startswith("wallet:"):
            try:
                expected = int(payload.split(":", 1)[1])
            except ValueError:
                await q.answer(ok=False, error_message="Noma'lum to'lov.")
                return
        else:
            await q.answer(ok=False, error_message="Noma'lum to'lov.")
            return
        if q.currency != "XTR" or q.total_amount != expected:
            await q.answer(ok=False, error_message="To'lov summasi noto'g'ri.")
            return
        await q.answer(ok=True)

    async def successful_payment(update, context):
        pay = update.message.successful_payment
        user = update.effective_user
        if pay is None:
            return
        payload = pay.invoice_payload or ""
        if payload.startswith("wallet:"):
            try:
                stars = int(payload.split(":", 1)[1])
            except ValueError:
                return
            som = stars * WALLET_STAR_RATE
            record_payment(user.id, user.username or "", pay.total_amount, payload)
            new_balance = add_balance(user.id, som)
            await update.message.reply_text(
                f"✅ Hamyon to'ldirildi: {stars} ⭐ = {som:,} so'm\n"
                f"💰 Balans endi: {new_balance:,} so'm.",
                reply_markup=main_menu(user.id),
            )
            if SUPER_ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=SUPER_ADMIN_ID,
                        text=f"💰 Yangi hamyon to'lovi: @{user.username or user.id} ({user.id}) — "
                             f"{stars} ⭐ = {som:,} so'm, balans: {new_balance:,} so'm.",
                    )
                except Exception:
                    pass
            return
        if payload.startswith("monthly:"):
            try:
                bot_id = int(payload.split(":", 1)[1])
            except ValueError:
                return
            row = get_bot(bot_id)
            if row is None or row["owner_id"] != user.id:
                return
            record_payment(user.id, user.username or "", pay.total_amount, payload)
            new_until = extend_monthly(bot_id)
            await update.message.reply_text(
                f"✅ Oylik to'lov qabul qilindi: {pay.total_amount} ⭐\n"
                f"💳 Bot #{bot_id} oyligi {new_until[:10]} gacha uzaytirildi.\n"
                "Botni «🤖 Mening botlarim» dan ishga tushirishingiz mumkin.",
                reply_markup=main_menu(user.id),
            )
            if SUPER_ADMIN_ID:
                try:
                    await context.bot.send_message(
                        chat_id=SUPER_ADMIN_ID,
                        text=f"💰 Yangi oylik Stars to'lovi: @{user.username or user.id} ({user.id}) — "
                             f"Bot #{bot_id}, {pay.total_amount} ⭐, {new_until[:10]} gacha.",
                    )
                except Exception:
                    pass
            return
        if payload != "premium-1bot":
            return
        record_payment(user.id, user.username or "", pay.total_amount, pay.invoice_payload)
        credits = grant_credit(user.id, by="stars")
        await update.message.reply_text(
            f"✅ To'lov qabul qilindi: {pay.total_amount} ⭐\n"
            f"💎 Premium kreditingiz endi: {credits} ta bot.\n"
            "Endi «➕ Bot yasash (token)» dan bot turini tanlab, token yuboring.",
            reply_markup=main_menu(user.id),
        )
        if SUPER_ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=SUPER_ADMIN_ID,
                    text=f"💰 Yangi Stars to'lovi: @{user.username or user.id} ({user.id}) — "
                         f"{pay.total_amount} ⭐, kredit: {credits} ta bot.",
                )
            except Exception:
                pass

    # ---------- admin panel (faqat super admin) ----------

    async def show_admin(update, context):
        user_id = update.effective_user.id
        if not is_super_admin(user_id):
            if not SUPER_ADMIN_ID:
                await update.message.reply_text(
                    "🛡 Admin panel faqat super adminga ochiq.\n"
                    "Hozir SUPER_ADMIN_ID FadeHost panelda o'rnatilmagan — "
                    "Umid o'z Telegram ID sini (@userinfobot dan olib) "
                    "FadeHost lekin-server-bot panelida SUPER_ADMIN_ID ga yozishi kerak."
                )
            else:
                await update.message.reply_text("🛡 Admin panel faqat super adminga ochiq.")
            return
        rows = list_premium_users()
        lines = "\n".join(f"• {r['user_id']} — {r['credits']} ta kredit" for r in rows) or "Hozircha premium foydalanuvchi yo'q."
        _card_info = get_config("card_info") or "(hali kiritilmagan)"
        _pending = len(list_pending_card_payments())
        await update.message.reply_text(
            "🛡 Admin panel (super admin)\n\n"
            f"👥 Premium foydalanuvchilar ({len(rows)}):\n{lines}\n\n"
            f"📊 Jami botlar: {count_bots_total()} / {MAX_BOTS}\n"
            f"💳 Oylik: {MONTHLY_SOM:,} so'm / {MONTHLY_STARS} ⭐ (30 kun)\n"
            f"💳 Karta ma'lumoti: {_card_info}\n"
            f"📋 Kutilayotgan karta to'lovlari: {_pending} ta\n\n"
            "Premium berish uchun foydalanuvchi Telegram ID sini yuboring — "
            "avval «➕ Premium berish», olish uchun «➖ Premium olish» ni bosing.\n"
            "📣 Elon — server botning barcha foydalanuvchilariga boradi.",
            reply_markup=ReplyKeyboardMarkup(
                _colored([
                    ["➕ Premium berish", "➖ Premium olish"],
                    ["📣 Elon yuborish", "💳 Karta ma'lumoti"],
                    ["📋 Karta to'lovlari", "👥 Premiumlar"],
                    ["⬅️ Orqaga"],
                ]),
                resize_keyboard=True,
            ),
        )

    async def admin_list(update, context):
        rows = list_premium_users()
        lines = "\n".join(f"• {r['user_id']} — {r['credits']} ta kredit" for r in rows) or "Hozircha premium foydalanuvchi yo'q."
        await update.message.reply_text(f"👥 Premium foydalanuvchilar:\n{lines}")

    async def handle_admin_id(update, context, text: str):
        """Admin grant/revoke: matn sifatida Telegram ID kutiladi."""
        mode = context.user_data.get("await")
        try:
            target = int(text.strip())
        except ValueError:
            await update.message.reply_text("Telegram ID raqam bo'lishi kerak, masalan: 123456789")
            return
        context.user_data.pop("await", None)
        if mode == "admin_grant":
            credits = grant_credit(target, by="admin")
            await update.message.reply_text(f"✅ {target} ga premium berildi. Krediti endi: {credits} ta bot.")
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text="💎 Sizga server botda premium berildi — endi 1 ta bot yasay olasiz.",
                )
            except Exception:
                pass
        elif mode == "admin_revoke":
            revoke_premium(target)
            await update.message.reply_text(f"✅ {target} dan premium olindi (krediti 0 ga tushirildi).")
            try:
                await context.bot.send_message(
                    chat_id=target, text="💎 Server botdagi premiumingiz olindi.",
                )
            except Exception:
                pass

    async def handle_token(update, context, token_text: str):
        from telegram import Bot
        from telegram.error import InvalidToken, NetworkError, TelegramError, TimedOut

        token = token_text.strip()
        # Xabarni darhol o'chirishga harakat qilamiz (token chatda qolmasin)
        try:
            await update.message.delete()
        except Exception:
            pass
        if not looks_like_token(token):
            await update.effective_chat.send_message("Token formati noto'g'ri ko'rinyapti. BotFather tokeni odatda `123456:ABC...` ko'rinishida bo'ladi.")
            return
        fp = token_fingerprint(token)
        if token_hash_exists(fp):
            # Token hali bazada bor degani — bot o'chirilmagan, faqat to'xtatilgan
            # bo'lishi mumkin. Qaysi bot ekanini aytamiz, shunda foydalanuvchi
            # avval o'sha botni o'chirib, keyin tokenni qayta ishlata oladi.
            # O'chirilgan botning tokeni bazadan ham o'chadi, shuning uchun
            # o'chirishdan keyin qayta ishlatish to'silmaydi.
            with db_conn() as _c:
                _row = _c.execute("SELECT id, owner_id FROM bots WHERE token_hash=?", (fp,)).fetchone()
            _num = _row["id"] if _row is not None else "?"
            await update.effective_chat.send_message(
                f"Bu token allaqachon Bot #{_num} da ishlatilgan. "
                "Agar o'sha botni o'chirgan bo'lsangiz, «🤖 Botlarim» dan u rostdan "
                "o'chganini tekshiring — faqat to'xtatilgan bot tokenni band qilib turadi. "
                "O'chirganingizdan keyin shu tokenni qayta ishlata olasiz."
            )
            return
        try:
            me = await Bot(token=token).get_me()
        except InvalidToken:
            await update.effective_chat.send_message("Telegram bu tokenni qabul qilmadi — token noto'g'ri yoki bekor qilingan.")
            return
        except (NetworkError, TimedOut):
            await update.effective_chat.send_message("Telegramga ulanib bo'lmadi (tarmoq xatosi). Token saqlanmadi — keyinroq qayta yuboring.")
            return
        except TelegramError as e:
            await update.effective_chat.send_message(f"Telegram xatosi: {e}. Token saqlanmadi.")
            return
        bot_type = context.user_data.pop("make_type", "anime")
        if bot_type not in TEMPLATE_NAMES:
            bot_type = "anime"
        # Premium krediti shu yerda ishlatiladi — faqat muvaffaqiyatli
        # token tekshiruvidan keyin, bot yaratilishidan oldin.
        if not use_credit(update.effective_user.id):
            await update.effective_chat.send_message(
                f"💎 Premium kreditingiz tugagan. 1 ta bot — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐. "
                "«💎 Premium olish» dan to'lang, keyin qayta urinib ko'ring."
            )
            return
        feats = context.user_data.pop("make_features", list(MAKE_FEATURE_KEYS))
        style = context.user_data.pop("make_style", "colorful")
        bot_id = create_bot(
            owner_id=update.effective_user.id, kind="token",
            token_enc=encrypt_token(token), token_hash=fp,
            bot_username=me.username or "", bot_type=bot_type,
            bot_features=",".join(feats) if feats else "shorts,premium,sub,post,storage",
            panel_style=style if style in PANEL_STYLES else "colorful",
        )
        bot_work_dir(bot_id).mkdir(parents=True, exist_ok=True)
        ok, msg = start_bot(bot_id)
        add_text = {"anime": "anime va qism", "movie": "kino", "general": "kontent"}.get(bot_type, "kontent")
        await update.effective_chat.send_message(
            f"✅ {TEMPLATE_NAMES[bot_type]} qo'shildi: @{me.username}\nHolat: {msg}\n"
            f"Endi o'sha botga kiring: egasi sifatida siz {add_text} qo'sha olasiz.\n"
            "Bot fayli ham quyida — o'zingizda saqlang, serverda ham shu fayl ishlayapti."
        )
        # Bot-maker sharti: shablon faylini foydalanuvchiga tashlab berish.
        template_path = TEMPLATE_FILES.get(bot_type)
        if template_path and template_path.exists():
            try:
                with open(template_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        filename=template_path.name,
                        caption=f"📁 {TEMPLATE_NAMES[bot_type]} shablon fayli — Bot #{bot_id}",
                    )
            except Exception:
                pass

    # ---------- 2-usul: kod ----------

    async def ask_code(update, context):
        # Super admin uchun kishi limiti yo'q (jami 5 ta limit baribir turadi).
        if not is_super_admin(update.effective_user.id) and count_bots_user(update.effective_user.id) >= MAX_BOTS_PER_USER:
            await update.message.reply_text(f"Limit: hozircha har foydalanuvchiga {MAX_BOTS_PER_USER} ta bot. Avval eskisini o'chiring.")
            return
        if count_bots_total() >= MAX_BOTS:
            await update.message.reply_text("Server to'lgan (tekin server kuchsiz, umumiy limit bor). Keyinroq urinib ko'ring.")
            return
        if not has_premium(update.effective_user.id):
            await update.message.reply_text(
                f"💎 Bot qo'shish pullik: 1 ta bot — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐.\n"
                "Avval «💎 Premium olish» dan Stars bilan to'lang — to'lov avtomatik.",
            )
            return
        context.user_data["await"] = "code"
        await update.message.reply_text(
            "Bot kodingizni tashlang: bitta .py fayl yoki ichida bot.py/main.py bor .zip (2 MB gacha).\n"
            "Kod qabul qilingach, shu kod uchun bot tokenini so'rayman — token shifrlab saqlanadi va "
            "ishlashda BOT_TOKEN env orqali kodga beriladi.\n"
            "⚠️ Kod alohida jarayon bo'lib ochiq ishga tushadi: to'liq izolyatsiya yo'q, kodingiz uchun o'zingiz javobgarsiz."
        )

    async def handle_code_doc(update, context):
        doc = update.message.document
        if doc is None:
            return
        name = doc.file_name or ""
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_CODE_EXT:
            await update.message.reply_text("Faqat .py yoki .zip fayl qabul qilinadi.")
            return
        if doc.file_size and doc.file_size > MAX_CODE_SIZE:
            await update.message.reply_text("Fayl juda katta — limit 2 MB.")
            return
        bot_id = create_bot(owner_id=update.effective_user.id, kind="code")
        work = bot_work_dir(bot_id)
        code_dir = work / "code"
        code_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = work / ("upload" + ext)
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(str(tmp_path))
        except Exception as e:
            delete_bot_row(bot_id)
            shutil.rmtree(work, ignore_errors=True)
            await update.message.reply_text(f"Faylni yuklab bo'lmadi: {e}")
            return
        if tmp_path.stat().st_size > MAX_CODE_SIZE:
            delete_bot_row(bot_id)
            shutil.rmtree(work, ignore_errors=True)
            await update.message.reply_text("Fayl juda katta — limit 2 MB.")
            return
        if ext == ".py":
            entry = "bot.py"
            shutil.move(str(tmp_path), str(code_dir / entry))
        else:
            ok, msg, entry = extract_code_zip(tmp_path.read_bytes(), code_dir)
            tmp_path.unlink(missing_ok=True)
            if not ok:
                delete_bot_row(bot_id)
                shutil.rmtree(work, ignore_errors=True)
                await update.message.reply_text(f"Zip qabul qilinmadi: {msg}")
                return
        ok, msg = check_python_syntax(code_dir / entry)
        if not ok:
            delete_bot_row(bot_id)
            shutil.rmtree(work, ignore_errors=True)
            await update.message.reply_text(f"Kodda sintaksis xato bor, qabul qilinmadi:\n{msg}")
            return
        update_bot(bot_id, code_dir=str(code_dir), entry_file=entry)
        if not use_credit(update.effective_user.id):
            delete_bot_row(bot_id)
            shutil.rmtree(work, ignore_errors=True)
            await update.message.reply_text(
                f"💎 Premium kreditingiz tugagan. 1 ta bot — {PRICE_SOM:,} so'm / {PRICE_STARS} ⭐. "
                "«💎 Premium olish» dan to'lang, keyin kodni qayta tashlang."
            )
            return
        context.user_data["await"] = "code_token"
        context.user_data["code_bot_id"] = bot_id
        await update.message.reply_text(
            f"✅ Kod qabul qilindi, bot #{bot_id} yaratildi.\n"
            "Endi shu kod uchun bot tokenini yuboring — token tekshirilib, shifrlab saqlanadi, "
            "so'ng bot BOT_TOKEN env bilan ishga tushadi."
        )

    async def handle_code_update_doc(update, context):
        """Kod boti yangilanishi: faqat .py, baza o'chmaydi, sintaksis avval tekshiriladi."""
        doc = update.message.document
        if doc is None:
            return
        bot_id = context.user_data.get("code_bot_id")
        row = get_bot(bot_id) if bot_id else None
        if row is None or row["owner_id"] != update.effective_user.id or row["kind"] != "code":
            await update.message.reply_text("Bot topilmadi yoki sizniki emas.")
            return
        name = doc.file_name or ""
        if Path(name).suffix.lower() != ".py":
            await update.message.reply_text("Yangilash uchun faqat .py fayl yuboring (zip emas).")
            return
        if doc.file_size and doc.file_size > MAX_CODE_SIZE:
            await update.message.reply_text("Fayl juda katta — limit 2 MB.")
            return
        code_dir = Path(row["code_dir"])
        entry = code_dir / row["entry_file"]
        if not entry.exists():
            await update.message.reply_text("Eski kod fayli topilmadi — yangilab bo'lmadi.")
            return
        work = bot_work_dir(bot_id)
        tmp_path = work / "update_tmp.py"
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(str(tmp_path))
        except Exception as e:
            await update.message.reply_text(f"Faylni yuklab bo'lmadi: {e}")
            return
        ok, msg = check_python_syntax(tmp_path)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            await update.message.reply_text(
                f"Kodda sintaksis xato bor, almashtirilmadi — eski bot ishlashda davom etadi:\n{msg}"
            )
            return
        stop_bot(bot_id)
        backup = work / "entry_backup.py"
        shutil.copyfile(entry, backup)
        shutil.move(str(tmp_path), str(entry))
        context.user_data.pop("await", None)
        ok, msg = start_bot(bot_id)
        if ok:
            await update.message.reply_text(f"✅ Bot #{bot_id} kodi yangilandi, bot qayta ishga tushdi. Baza o'zgarmadi.")
        else:
            shutil.copyfile(backup, entry)
            start_bot(bot_id)
            await update.message.reply_text(f"Yangi kod ishga tushmadi, eski kod qaytarildi: {msg}")

    async def handle_db_update_doc(update, context):
        """Kod boti bazasini yuklash: faqat .db, tekshiruvdan o'tsa almashtirish."""
        doc = update.message.document
        if doc is None:
            return
        bot_id = context.user_data.get("code_bot_id")
        row = get_bot(bot_id) if bot_id else None
        if row is None or row["owner_id"] != update.effective_user.id or row["kind"] != "code":
            await update.message.reply_text("Bot topilmadi yoki sizniki emas.")
            return
        name = doc.file_name or ""
        if Path(name).suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
            await update.message.reply_text("Baza uchun faqat .db fayl yuboring.")
            return
        if doc.file_size and doc.file_size > MAX_DB_SIZE:
            await update.message.reply_text("Baza fayli juda katta — limit 20 MB.")
            return
        code_dir = Path(row["code_dir"])
        # Kod boti bazasi code_dir dagi uz.dubanime_v1.db — shu faylni ham
        # botning o'zi, ham server bot sayt API si o'qiydi.
        target = code_dir / "uz.dubanime_v1.db"
        work = bot_work_dir(bot_id)
        work.mkdir(parents=True, exist_ok=True)
        tmp_path = work / "db_update_tmp.db"
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(str(tmp_path))
        except Exception as e:
            await update.message.reply_text(f"Faylni yuklab bo'lmadi: {e}")
            return
        ok, info = validate_anime_db(tmp_path)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            await update.message.reply_text(
                f"Baza tekshiruvdan o'tmadi, almashtirilmadi — eski baza ishlashda davom etadi:\n{info}"
            )
            return
        stop_bot(bot_id)
        backup = work / "db_backup.db"
        if target.exists():
            shutil.copyfile(target, backup)
        try:
            shutil.move(str(tmp_path), str(target))
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            await update.message.reply_text(f"Bazani joylab bo'lmadi: {e}")
            return
        context.user_data.pop("await", None)
        ok, msg = start_bot(bot_id)
        if ok:
            await update.message.reply_text(
                f"✅ Bot #{bot_id} bazasi yuklandi ({info}), bot qayta ishga tushdi.")
        else:
            if backup.exists():
                shutil.copyfile(backup, target)
            start_bot(bot_id)
            await update.message.reply_text(
                f"Bot yangi baza bilan ishga tushmadi, eski baza qaytarildi: {msg}")

    async def handle_code_token(update, context, token_text: str):
        from telegram import Bot
        from telegram.error import InvalidToken, NetworkError, TelegramError, TimedOut

        bot_id = context.user_data.get("code_bot_id")
        token = token_text.strip()
        try:
            await update.message.delete()
        except Exception:
            pass
        if not bot_id:
            await update.effective_chat.send_message("Avval kod botini tanlang — «🤖 Mening botlarim» dan «🔑 Token kiritish / almashtirish» ni bosing.")
            return
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != update.effective_user.id or row["kind"] != "code":
            await update.effective_chat.send_message("Bot topilmadi yoki sizniki emas.")
            return
        if not looks_like_token(token):
            await update.effective_chat.send_message("Token formati noto'g'ri ko'rinyapti. BotFather tokeni odatda `123456:ABC...` ko'rinishida bo'ladi.")
            return
        fp = token_fingerprint(token)
        # Shu token allaqachon boshqa botda ishlatilgan bo'lsa — ikki polling to'qnashadi.
        existing = token_hash_exists(fp)
        if existing and row["token_hash"] != fp:
            with db_conn() as _c:
                _row = _c.execute("SELECT id FROM bots WHERE token_hash=?", (fp,)).fetchone()
            _num = _row["id"] if _row is not None else "?"
            await update.effective_chat.send_message(
                f"Bu token allaqachon Bot #{_num} da ishlatilgan. "
                "Avval o'sha botni o'chiring, keyin tokenni qayta ishlata olasiz — "
                "o'chirilgan botning tokeni bazadan ham o'chadi."
            )
            return
        try:
            me = await Bot(token=token).get_me()
        except InvalidToken:
            await update.effective_chat.send_message("Telegram bu tokenni qabul qilmadi — token noto'g'ri yoki bekor qilingan.")
            return
        except (NetworkError, TimedOut):
            await update.effective_chat.send_message("Telegramga ulanib bo'lmadi (tarmoq xatosi). Token saqlanmadi — keyinroq qayta yuboring.")
            return
        except TelegramError as e:
            await update.effective_chat.send_message(f"Telegram xatosi: {e}. Token saqlanmadi.")
            return
        was_running = is_running(bot_id)
        if was_running:
            stop_bot(bot_id)
        update_bot(bot_id, token_enc=encrypt_token(token), token_hash=fp,
                   bot_username=me.username or "")
        context.user_data.pop("await", None)
        context.user_data.pop("code_bot_id", None)
        ok, msg = start_bot(bot_id)
        await update.effective_chat.send_message(
            f"✅ Bot #{bot_id} tokeni saqlandi: @{me.username}\nHolat: {msg}\n"
            "Logni «🤖 Mening botlarim» → «📄 Log» dan ko'ring."
        )

    # ---------- umumiy matn yo'naltirish ----------

    async def handle_text(update, context):
        text = (update.message.text or "").strip()
        if text == BTN_TOKEN:
            return await ask_token(update, context)
        if text == BTN_CODE:
            return await ask_code(update, context)
        if text == BTN_MY:
            return await my_bots(update, context)
        if text == BTN_USERBOTS:
            return await show_userbots(update, context)
        if text == BTN_HELP:
            return await show_help(update, context)
        if text == BTN_PREMIUM:
            return await show_premium(update, context)
        if text == BTN_WALLET:
            return await show_wallet(update, context)
        if text == BTN_ADMIN:
            return await show_admin(update, context)
        if text == "➕ Premium berish":
            if not is_super_admin(update.effective_user.id):
                await update.message.reply_text("Faqat super admin premium bera oladi.")
                return
            context.user_data["await"] = "admin_grant"
            await update.message.reply_text("Premium beriladigan foydalanuvchi Telegram ID sini yuboring:")
            return
        if text == "➖ Premium olish":
            if not is_super_admin(update.effective_user.id):
                await update.message.reply_text("Faqat super admin premium ola oladi.")
                return
            context.user_data["await"] = "admin_revoke"
            await update.message.reply_text("Premium olinadigan foydalanuvchi Telegram ID sini yuboring:")
            return
        if text == "👥 Premiumlar":
            if not is_super_admin(update.effective_user.id):
                await update.message.reply_text("Faqat super admin ko'ra oladi.")
                return
            return await admin_list(update, context)
        if text == "📣 Elon yuborish":
            if not is_super_admin(update.effective_user.id):
                await update.message.reply_text("Faqat super admin elon yubora oladi.")
                return
            context.user_data["await"] = "announce"
            await update.message.reply_text(
                "📣 Elon matnini yuboring — u server botning barcha "
                "foydalanuvchilariga (users jadvali) yuboriladi."
            )
            return
        if text == "💳 Karta ma'lumoti":
            if not is_super_admin(update.effective_user.id):
                await update.message.reply_text("Faqat super admin o'zgartira oladi.")
                return
            context.user_data["await"] = "card_info"
            await update.message.reply_text(
                "💳 Karta to'lov ma'lumotini yuboring — masalan, karta raqami va "
                "Click/Payme izohi. Shu matn oylik to'lovda foydalanuvchilarga ko'rinadi.\n"
                "Hozirgisi: " + (get_config("card_info") or "(yo'q)")
            )
            return
        if text == "📋 Karta to'lovlari":
            if not is_super_admin(update.effective_user.id):
                await update.message.reply_text("Faqat super admin ko'ra oladi.")
                return
            pending = list_pending_card_payments()
            if not pending:
                await update.message.reply_text("📋 Kutilayotgan karta to'lovi yo'q.")
                return
            for req in pending:
                kb = InlineKeyboardMarkup([[
                    _ibtn("✅ Tasdiqlash", callback_data=f"cardpay:approve:{req['id']}"),
                    _ibtn("❌ Rad etish", callback_data=f"cardpay:reject:{req['id']}"),
                ]])
                _purpose = "hamyon" if int(req["bot_id"]) == 0 else f"Bot #{req['bot_id']}"
                await update.message.reply_text(
                    f"💳 So'rov #{req['id']}: foydalanuvchi {req['user_id']}, "
                    f"{_purpose}, {req['amount_som']:,} so'm, {req['created_at']}",
                    reply_markup=kb,
                )
            return
        if context.user_data.get("await") == "announce":
            context.user_data.pop("await", None)
            import asyncio as _asyncio

            users = get_all_users()
            ok_count = 0
            fail_count = 0
            for u in users:
                try:
                    await context.bot.send_message(chat_id=u["user_id"], text=f"📣 Elon\n\n{text}")
                    ok_count += 1
                except Exception:
                    fail_count += 1
                await _asyncio.sleep(0.05)
            await update.message.reply_text(
                f"📣 Elon yuborildi: {ok_count} ta yetdi, {fail_count} ta yetmadi "
                f"(jami {len(users)} foydalanuvchi)."
            )
            return
        if context.user_data.get("await") == "card_info":
            context.user_data.pop("await", None)
            set_config("card_info", text)
            await update.message.reply_text("✅ Karta ma'lumoti saqlandi. Endi oylik to'lovda ko'rinadi.")
            return
        if text == "⬅️ Orqaga":
            context.user_data.pop("await", None)
            await update.message.reply_text("Asosiy menyu 👇", reply_markup=main_menu(update.effective_user.id))
            return
        if context.user_data.get("await") in ("admin_grant", "admin_revoke"):
            return await handle_admin_id(update, context, text)
        if context.user_data.get("await") == "ub_api_id":
            return await handle_ub_api_id(update, context, text)
        if context.user_data.get("await") == "ub_api_hash":
            return await handle_ub_api_hash(update, context, text)
        if context.user_data.get("await") == "ub_session":
            return await handle_ub_session(update, context, text)
        if context.user_data.get("await") == "ub_method":
            low = text.strip().lower()
            if low in ("telefon", "📱 telefon orqali", "1", "phone"):
                context.user_data["await"] = "ub_phone"
                await update.message.reply_text(
                    "Telefon raqamingizni yuboring (+998 bilan, masalan +998901234567):"
                )
                return
            if low in ("sessiya", "🔑 sessiya stringi", "2", "session"):
                context.user_data["await"] = "ub_session"
                await update.message.reply_text(
                    "Sessiya stringini yuboring.\n"
                    "Bu telefon kodi EMAS. Telethon StringSession stringi kerak."
                )
                return
            await update.message.reply_text(
                "«📱 Telefon orqali» yoki «🔑 Sessiya stringi» tugmasini bosing.",
                reply_markup=InlineKeyboardMarkup([[
                    _ibtn("📱 Telefon orqali", callback_data="ubact:phone:0"),
                    _ibtn("🔑 Sessiya stringi", callback_data="ubact:session:0"),
                ]]),
            )
            return
        if context.user_data.get("await") == "ub_qr_wait":
            await update.message.reply_text(
                "QR skanerlanishi kutilmoqda. Telegram ilovangda Sozlamalar → Qurilmalar → "
                "Qurilmani ulash dan QR ni skanerla. QR eskirsa, «👤 Userbotlar» dan qayta boshlang."
            )
            return
        if context.user_data.get("await") == "ub_phone":
            return await handle_ub_phone(update, context, text)
        if context.user_data.get("await") == "ub_code":
            return await handle_ub_code(update, context, text)
        if context.user_data.get("await") == "ub_password":
            return await handle_ub_password(update, context, text)
        if context.user_data.get("await") == "token":
            context.user_data.pop("await", None)
            return await handle_token(update, context, text)
        if context.user_data.get("await") == "code_token":
            return await handle_code_token(update, context, text)
        if context.user_data.get("await") == "code":
            await update.message.reply_text("Kod kutyapman — .py yoki .zip fayl tashlang (matn emas).")
            return
        if context.user_data.get("await") == "code_update":
            await update.message.reply_text("Kod yangilanishi kutyapman — yangi .py fayl tashlang (matn emas).")
            return
        if context.user_data.get("await") == "db_update":
            await update.message.reply_text("Baza yuklanishi kutyapman — .db fayl tashlang (matn emas).")
            return
        await update.message.reply_text("Menyudan tanlang 👇", reply_markup=main_menu(update.effective_user.id))

    async def handle_photo(update, context):
        if context.user_data.get("await") == "card_check":
            return await handle_card_check_photo(update, context)
        await update.message.reply_text("Menyudan tanlang 👇", reply_markup=main_menu(update.effective_user.id))

    async def handle_doc(update, context):
        if context.user_data.get("await") == "card_check":
            return await handle_card_check_photo(update, context)
        if context.user_data.get("await") == "code":
            return await handle_code_doc(update, context)
        if context.user_data.get("await") == "code_update":
            return await handle_code_update_doc(update, context)
        if context.user_data.get("await") == "db_update":
            return await handle_db_update_doc(update, context)
        await update.message.reply_text("Avval «📁 Bot qo'shish (kod)» tugmasini bosing, keyin fayl tashlang.")

    async def post_init(app):
        init_db()
        restore_running_bots()
        restore_running_userbots()
        # Har soatda oylik tekshiruvi: muddati tugagan botning oyligi
        # hamyondan avtomatik yechiladi, balans yetmasa bot to'xtatiladi.
        import asyncio as _asyncio

        _asyncio.create_task(monthly_wallet_loop(app))
        log.info("Server bot ishga tushdi, faol botlar qayta yoqildi")

    from telegram.ext import PreCheckoutQueryHandler

    app = ApplicationBuilder().token(SERVER_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(bot_action, pattern=r"^act:"))
    app.add_handler(CallbackQueryHandler(userbot_action, pattern=r"^ubact:"))
    app.add_handler(CallbackQueryHandler(make_choice, pattern=r"^make:"))
    app.add_handler(CallbackQueryHandler(make_feature_toggle, pattern=r"^makefeat:"))
    app.add_handler(CallbackQueryHandler(make_style_choice, pattern=r"^makestyle:"))
    app.add_handler(CallbackQueryHandler(pay_stars, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(monthly_router, pattern=r"^monthly:"))
    app.add_handler(CallbackQueryHandler(wallet_router, pattern=r"^wallet:"))
    app.add_handler(CallbackQueryHandler(cardpay_decide, pattern=r"^cardpay:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main():
    if not SERVER_BOT_TOKEN:
        print("XATO: SERVER_BOT_TOKEN env yo'q. Masalan: SERVER_BOT_TOKEN=... python server_bot.py")
        sys.exit(1)
    if not SERVER_BOT_SECRET:
        print("OGOHLANTIRISH: SERVER_BOT_SECRET yo'q — tokenlar kuchli shifrlanmaydi!")
    init_db()
    start_site_api_server()
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
