"""
Server bot — asosiy bot (MVP, birinchi versiya).

G'oya (Umid, 2026-10-22): bitta bot "server" bo'lib ishlab turadi.
Odamlar anime bot ochish uchun alohida server qidirib yurmaydi:
  1-usul (token): foydalanuvchi o'z boti tokenini beradi ->
      server bot tokenni getMe bilan tekshiradi, shifrlab saqlaydi va
      tayyor anime bot shablonidan (child_anime_bot.py) nusxa ishga
      tushiradi, har bir bot uchun ALOHIDA SQLite baza bilan.
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        "bot.py/main.py bor .zip) tashlang, keyin shu kod uchun bot tokenini yuboring. Token getMe bilan "
        "tekshirilib, shifrlab saqlanadi va ishga tushirishda BOT_TOKEN env orqali kodga beriladi. "
        "Kod o'z papkasida alohida jarayon bo'lib ishlaydi.\n\n"
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

    def bot_buttons(bot_id: int, kind: str = "", has_token: bool = True):
        rows = [
            [InlineKeyboardButton("▶️ Ishga tushirish", callback_data=f"act:start:{bot_id}"),
             InlineKeyboardButton("⏹ To'xtatish", callback_data=f"act:stop:{bot_id}")],
            [InlineKeyboardButton("📄 Log", callback_data=f"act:log:{bot_id}"),
             InlineKeyboardButton("🗑 O'chirish", callback_data=f"act:delask:{bot_id}")],
        ]
        # Kod botiga token hali kiritilmagan bo'lsa — alohida tugma ko'rsatamiz.
        if kind == "code" and not has_token:
            rows.append([InlineKeyboardButton("🔑 Token kiritish", callback_data=f"act:codetoken:{bot_id}")])
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
            await update.message.reply_text(
                f"🤖 Bot #{row['id']} — {name}\nUsul: {kind}\nHolat: {status}\nYaratilgan: {row['created_at']}",
                reply_markup=bot_buttons(row["id"], kind=row["kind"], has_token=bool(row["token_enc"])),
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
        if action == "codetoken":
            if row["kind"] != "code":
                await q.edit_message_text(f"Bot #{bot_id}: token faqat kod usulidagi botlar uchun kiritiladi.")
                return
            context.user_data["await"] = "code_token"
            context.user_data["code_bot_id"] = bot_id
            await q.message.reply_text(
                f"Bot #{bot_id} uchun tokenni yuboring.\n"
                "Token getMe bilan tekshirilib, shifrlab saqlanadi va ishga tushirishda "
                "BOT_TOKEN env orqali kodga beriladi. Token chatdan o'chirishga harakat qilinadi."
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
        context.user_data["await"] = "code_token"
        context.user_data["code_bot_id"] = bot_id
        await update.message.reply_text(
            f"✅ Kod qabul qilindi, bot #{bot_id} yaratildi.\n"
            "Endi shu kod uchun bot tokenini yuboring — token tekshirilib, shifrlab saqlanadi, "
            "so'ng bot BOT_TOKEN env bilan ishga tushadi."
        )

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
            await update.effective_chat.send_message("Avval kod botini tanlang — «🤖 Mening botlarim» dan «🔑 Token kiritish» ni bosing.")
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
        if text == BTN_HELP:
            return await show_help(update, context)
        if context.user_data.get("await") == "token":
            context.user_data.pop("await", None)
            return await handle_token(update, context, text)
        if context.user_data.get("await") == "code_token":
            return await handle_code_token(update, context, text)
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
    start_site_api_server()
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
