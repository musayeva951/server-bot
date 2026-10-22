"""
Server bot — asosiy bot (MVP, birinchi versiya).

G'oya (Umid, 2026-10-22): bitta bot "server" bo'lib ishlab turadi.
Odamlar anime bot ochish uchun alohida server qidirib yurmaydi:
  1-usul (token): foydalanuvchi o'z boti tokenini beradi ->
      server bot tokenni getMe bilan tekshiradi, shifrlab saqlaydi va
      tayyor anime bot shablonidan (child_anime_bot.py) nusxa ishga
      tushiradi, har bir bot uchun ALOHIDA SQLite baza bilan.
  2-usul (kod): token bergisi kelmagan foydalanuvchi o'z botining
      kodini (.py bitta fayl yoki .zip ichida bot.py/main.py) tashlaydi ->
      server bot uni alohida papkada saqlab, alohida jarayon sifatida
      ishga tushiradi. Kod ichida tokenning o'zi bo'lishi foydalanuvchi
      zimmasida (bot matnlarida ham shu aytiladi).

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
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- sozlamalar

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.environ.get("SERVER_DB_PATH", str(BASE_DIR / "server.db")))
SERVER_BOT_TOKEN = os.environ.get("SERVER_BOT_TOKEN", "")
SERVER_BOT_SECRET = os.environ.get("SERVER_BOT_SECRET", "")
MAX_BOTS = int(os.environ.get("MAX_BOTS", "5") or 5)
MAX_BOTS_PER_USER = int(os.environ.get("MAX_BOTS_PER_USER", "1") or 1)
MAX_CODE_SIZE = int(os.environ.get("MAX_CODE_SIZE", str(2 * 1024 * 1024)) or 2 * 1024 * 1024)
MAX_ZIP_EXTRACTED = 10 * 1024 * 1024  # zip ochilgandagi umumiy limit

CHILD_SCRIPT = BASE_DIR / "child_anime_bot.py"

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


def create_bot(owner_id, kind, token_enc="", token_hash="", code_dir="", entry_file="", bot_username=""):
    with db_conn() as c:
        cur = c.execute(
            """INSERT INTO bots(owner_id, kind, token_enc, token_hash, code_dir,
                                entry_file, bot_username, status, created_at)
               VALUES(?,?,?,?,?,?,?,'stopped',?)""",
            (owner_id, kind, token_enc, token_hash, code_dir, entry_file,
             bot_username, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


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


# ---------------------------------------------------------------- jarayon boshqaruvi

_processes: dict[int, subprocess.Popen] = {}


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
        script = CHILD_SCRIPT
        env = _clean_env({
            "CHILD_BOT_TOKEN": token,
            "CHILD_DB_PATH": str(work / "child.db"),
            "OWNER_ID": str(row["owner_id"]),
        })
        cwd = work
    else:  # kind == 'code'
        entry = Path(row["code_dir"]) / row["entry_file"] if row["code_dir"] else None
        if not entry or not entry.exists():
            return False, "Kod fayli topilmadi"
        script = entry
        env = _clean_env({
            "OWNER_ID": str(row["owner_id"]),
            "CHILD_DB_PATH": str(work / "child.db"),
            # Eslatma: kod usulida token kodning ichida bo'ladi, env orqali
            # asosiy bot tokeni ham, boshqa botlar tokenlari ham berilmaydi.
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


# ---------------------------------------------------------------- telegram qismi

def build_app():
    """python-telegram-bot ilovasini yig'adi (import shu yerda — sinov importi yengil bo'lsin)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler,
        MessageHandler, filters,
    )

    BTN_TOKEN = "➕ Bot qo'shish (token)"
    BTN_CODE = "📁 Bot qo'shish (kod)"
    BTN_MY = "🤖 Mening botlarim"
    BTN_HELP = "ℹ️ Yordam"

    def main_menu():
        return ReplyKeyboardMarkup(
            [[BTN_TOKEN, BTN_CODE], [BTN_MY, BTN_HELP]],
            resize_keyboard=True,
        )

    HELP_TEXT = (
        "ℹ️ *Yordam*\n\n"
        "Bu bot — server bot: botingizni tekin serverda ishlab turishi uchun ishlatiladi, "
        "alohida server qidirib yurmaysiz.\n\n"
        "*1-usul — token:* «➕ Bot qo'shish (token)» ni bosing va BotFather bergan tokenni yuboring. "
        "Shu token bilan tayyor anime bot nusxasi ishga tushadi: o'z katalogingiz, alohida baza.\n\n"
        "*2-usul — kod:* «📁 Bot qo'shish (kod)» ni bosib, botingiz kodini (.py fayl yoki ichida "
        "bot.py/main.py bor .zip) tashlang. Kod o'z papkasida alohida jarayon bo'lib ishlaydi. "
        "⚠️ Kod usulida token kodning ICHIDA bo'lishi sizning zimmangizda — biz tokenni koddan ajratib olmaymiz.\n\n"
        f"Cheklovlar: hozircha har foydalanuvchiga {MAX_BOTS_PER_USER} ta bot, jami {MAX_BOTS} ta bot "
        "(tekin server kuchsiz: 256 MB xotira, kuniga 1 GB trafik — shuning uchun limit bor). "
        "Fayl limiti: 2 MB.\n\n"
        "⚠️ Kod usuli ochiq ishga tushirishdir: to'liq izolyatsiya yo'q, o'z kodingiz uchun o'zingiz javobgarsiz."
    )

    async def cmd_start(update, context):
        user = update.effective_user
        add_user(user.id, user.username or "")
        await update.message.reply_text(
            "Assalomu alaykum! 👋 Bu — server bot.\n"
            "Botingizni tekin serverda ishga tushirib beraman: token bering yoki kod tashlang.",
            reply_markup=main_menu(),
        )

    async def show_help(update, context):
        await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

    # ---------- mening botlarim ----------

    def bot_buttons(bot_id: int):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Ishga tushirish", callback_data=f"act:start:{bot_id}"),
             InlineKeyboardButton("⏹ To'xtatish", callback_data=f"act:stop:{bot_id}")],
            [InlineKeyboardButton("📄 Log", callback_data=f"act:log:{bot_id}"),
             InlineKeyboardButton("🗑 O'chirish", callback_data=f"act:delask:{bot_id}")],
        ])

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
            await update.message.reply_text(
                f"🤖 Bot #{row['id']} — {name}\nUsul: {kind}\nHolat: {status}\nYaratilgan: {row['created_at']}",
                reply_markup=bot_buttons(row["id"]),
            )

    async def bot_action(update, context):
        q = update.callback_query
        await q.answer()
        _, action, raw_id = q.data.split(":", 2)
        bot_id = int(raw_id)
        row = get_bot(bot_id)
        if row is None or row["owner_id"] != q.from_user.id:
            await q.edit_message_text("Bot topilmadi yoki sizniki emas.")
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
                InlineKeyboardButton("Ha, o'chir ✅", callback_data=f"act:delyes:{bot_id}"),
                InlineKeyboardButton("Yo'q ❌", callback_data=f"act:delno:{bot_id}"),
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

    # ---------- 1-usul: token ----------

    async def ask_token(update, context):
        if count_bots_user(update.effective_user.id) >= MAX_BOTS_PER_USER:
            await update.message.reply_text(f"Limit: hozircha har foydalanuvchiga {MAX_BOTS_PER_USER} ta bot. Avval eskisini o'chiring.")
            return
        if count_bots_total() >= MAX_BOTS:
            await update.message.reply_text("Server to'lgan (tekin server kuchsiz, umumiy limit bor). Keyinroq urinib ko'ring.")
            return
        context.user_data["await"] = "token"
        await update.message.reply_text(
            "BotFather bergan tokenni yuboring.\n"
            "Token tekshiriladi (getMe), shifrlab saqlanadi va shu zahoti anime bot nusxasi ishga tushadi.\n"
            "Token logga yozilmaydi."
        )

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
            await update.effective_chat.send_message("Bu token bilan bot allaqachon qo'shilgan.")
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
        bot_id = create_bot(
            owner_id=update.effective_user.id, kind="token",
            token_enc=encrypt_token(token), token_hash=fp,
            bot_username=me.username or "",
        )
        bot_work_dir(bot_id).mkdir(parents=True, exist_ok=True)
        ok, msg = start_bot(bot_id)
        await update.effective_chat.send_message(
            f"✅ Bot qo'shildi: @{me.username}\nHolat: {msg}\n"
            "Endi o'sha botga kiring: egasi sifatida siz anime va qism qo'sha olasiz."
        )

    # ---------- 2-usul: kod ----------

    async def ask_code(update, context):
        if count_bots_user(update.effective_user.id) >= MAX_BOTS_PER_USER:
            await update.message.reply_text(f"Limit: hozircha har foydalanuvchiga {MAX_BOTS_PER_USER} ta bot. Avval eskisini o'chiring.")
            return
        if count_bots_total() >= MAX_BOTS:
            await update.message.reply_text("Server to'lgan (tekin server kuchsiz, umumiy limit bor). Keyinroq urinib ko'ring.")
            return
        context.user_data["await"] = "code"
        await update.message.reply_text(
            "Bot kodingizni tashlang: bitta .py fayl yoki ichida bot.py/main.py bor .zip (2 MB gacha).\n"
            "⚠️ Token kodning ICHIDA bo'lishi kerak — uni ajratib olmaymiz.\n"
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
        ok, msg = start_bot(bot_id)
        context.user_data.pop("await", None)
        await update.message.reply_text(f"✅ Kod qabul qilindi, bot #{bot_id} ishga tushirildi.\nHolat: {msg}\nLogni «🤖 Mening botlarim» → «📄 Log» dan ko'ring.")

    # ---------- umumiy matn yo'naltirish ----------

    async def handle_text(update, context):
        text = (update.message.text or "").strip()
        if text == BTN_TOKEN:
            return await ask_token(update, context)
        if text == BTN_CODE:
            return await ask_code(update, context)
        if text == BTN_MY:
            return await my_bots(update, context)
        if text == BTN_HELP:
            return await show_help(update, context)
        if context.user_data.get("await") == "token":
            context.user_data.pop("await", None)
            return await handle_token(update, context, text)
        if context.user_data.get("await") == "code":
            await update.message.reply_text("Kod kutyapman — .py yoki .zip fayl tashlang (matn emas).")
            return
        await update.message.reply_text("Menyudan tanlang 👇", reply_markup=main_menu())

    async def handle_doc(update, context):
        if context.user_data.get("await") == "code":
            return await handle_code_doc(update, context)
        await update.message.reply_text("Avval «📁 Bot qo'shish (kod)» tugmasini bosing, keyin fayl tashlang.")

    async def post_init(app):
        init_db()
        restore_running_bots()
        log.info("Server bot ishga tushdi, faol botlar qayta yoqildi")

    app = ApplicationBuilder().token(SERVER_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(bot_action, pattern=r"^act:"))
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
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
