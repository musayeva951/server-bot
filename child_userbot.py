"""
Server botning userbot shabloni — alohida bo'lim uchun.
Bu fayl to'g'ridan-to'g'ri ishga tushirilmaydi, server bot uni
subprocess sifatida ishga tushiradi va env orqali quyidagilarni beradi:
  USERBOT_API_ID, USERBOT_API_HASH, USERBOT_SESSION (Telethon StringSession),
  OWNER_ID, USERBOT_DB_PATH
Sessiya stringi = hisob kaliti. Uni hech kimga berma, kodga yozma.
Buyruqlar faqat egasi o'zi yozgan xabarlarda, nuqta (.) bilan ishlaydi.
Spam/flood funksiyalari yo'q.
"""
import asyncio
import ast
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ.get("USERBOT_API_ID", "0") or 0)
API_HASH = os.environ.get("USERBOT_API_HASH", "")
SESSION = os.environ.get("USERBOT_SESSION", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
DB_PATH = os.environ.get("USERBOT_DB_PATH", "userbot.db")
PRODUCTS_FILE = os.environ.get("USERBOT_PRODUCTS_FILE", "products.json")

state = {"afk": False, "afk_reason": "", "afk_replied": set(), "sales": False, "last_auto": 0.0}
SALES_KEYWORDS = ["narx", "qancha", "sotib", "olmoq", "buyurtma", "yetkaz", "dostavka", "price", "купить", "цена"]
YORDAM = """.yordam — shu ro'yxat
.ping — tezlik
.alive — ishlayaptimi?
.vaqt — Toshkent vaqti
.men — mening profilim
.id — ID (reply bilan odam ID si)
.afk [sabab] / .afkoff — band rejimi
.eslat <daqiqa> <matn> — eslatma
.note <nom> <matn> / .notes / .noteol <nom> / .notedel <nom>
.calc <ifoda> — hisoblash
.katta / .kichik / .teskari <matn>
.yoz <matn> — yozayotgandek animatsiya
.tozalash <son> — o'z xabarlarimni o'chirish (1..20)
.stat — shu chat statistikasi
.sales on/off — savdo rejimi (products.json asosida)
.hisobot — hisobot"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS notes (name TEXT PRIMARY KEY, text TEXT, ts TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sent_log (id INTEGER PRIMARY KEY, chat_id INTEGER, chat_title TEXT, text TEXT, ts TEXT, kind TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS inquiries (id INTEGER PRIMARY KEY, chat_id INTEGER, chat_title TEXT, sender_id INTEGER, text TEXT, ts TEXT, answered INTEGER DEFAULT 0)")
    conn.commit()
    return conn


def load_products():
    try:
        with open(PRODUCTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def product_answer(text, products):
    low = text.lower()
    for p in products:
        keys = [str(p.get("name", "")).lower()] + [str(k).lower() for k in p.get("keywords", [])]
        if any(k and k in low for k in keys):
            lines = [str(p.get("name", ""))]
            if p.get("price"):
                lines.append(f"Narxi: {p['price']}")
            if p.get("info"):
                lines.append(str(p["info"]))
            if p.get("delivery"):
                lines.append(f"Yetkazish: {p['delivery']}")
            return "\n".join(lines)
    return None


def safe_calc(expr):
    if not re.fullmatch(r"[0-9+\-*/().\s]+", expr or ""):
        raise ValueError("Faqat raqam va + - * / ( ) bo'lishi kerak.")
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd, ast.Load)):
            raise ValueError("Ruxsat etilmagan ifoda.")
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})


def now_uz():
    return datetime.now(timezone(timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")


async def remind(client, minutes, text, chat_id):
    await asyncio.sleep(minutes * 60)
    await client.send_message(chat_id, f"⏰ Eslatma: {text}")


async def main():
    if not API_ID or not API_HASH or not SESSION:
        raise SystemExit("USERBOT_API_ID, USERBOT_API_HASH, USERBOT_SESSION kerak.")
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

    @client.on(events.NewMessage(outgoing=True))
    async def commands(event):
        text = (event.raw_text or "").strip()
        if not text.startswith("."):
            return
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        async def edit(msg):
            try:
                await event.edit(msg)
            except Exception:
                await event.reply(msg)

        if cmd == ".yordam":
            await edit(YORDAM)
        elif cmd == ".ping":
            s = time.time()
            await event.edit("🏓 Ping...")
            await event.edit(f"🏓 Ping: {int((time.time() - s) * 1000)} ms")
        elif cmd == ".alive":
            await edit("✅ Userbot ishlayapti. Buyruqlar: .yordam")
        elif cmd == ".vaqt":
            await edit(f"🕐 Toshkent vaqti: {now_uz()}")
        elif cmd == ".men":
            me = await client.get_me()
            await edit(f"👤 {me.first_name or ''}\nUsername: @{me.username or '-'}\nID: {me.id}")
        elif cmd == ".id":
            await edit(f"Chat ID: {event.chat_id}")
        elif cmd == ".afk":
            state["afk"] = True; state["afk_reason"] = arg; state["afk_replied"] = set()
            await edit(f"💤 AFK yoqildi. Sabab: {arg or 'aytilmagan'}")
        elif cmd == ".afkoff":
            state["afk"] = False; state["afk_replied"] = set()
            await edit("✅ AFK o'chirildi.")
        elif cmd == ".eslat":
            m = re.match(r"(\d+)\s+(.+)", arg)
            if not m:
                await edit("Foydalanish: .eslat <daqiqa> <matn>")
            else:
                asyncio.create_task(remind(client, int(m.group(1)), m.group(2), event.chat_id))
                await edit(f"⏰ {m.group(1)} daqiqadan keyin eslataman: {m.group(2)}")
        elif cmd == ".note":
            m = re.match(r"(\S+)\s+(.+)", arg, re.DOTALL)
            if not m:
                await edit("Foydalanish: .note <nom> <matn>")
            else:
                conn = db(); conn.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?)", (m.group(1), m.group(2), now_uz())); conn.commit(); conn.close()
                await edit(f"💾 Saqlandi: {m.group(1)}")
        elif cmd == ".notes":
            conn = db(); rows = conn.execute("SELECT name, ts FROM notes ORDER BY name").fetchall(); conn.close()
            await edit("📝 Eslatmalar:\n" + ("\n".join(f"- {n} ({t})" for n, t in rows) if rows else "Bo'sh."))
        elif cmd == ".noteol":
            conn = db(); row = conn.execute("SELECT text FROM notes WHERE name=?", (arg.strip(),)).fetchone(); conn.close()
            await edit(row[0] if row else "Topilmadi.")
        elif cmd == ".notedel":
            conn = db(); conn.execute("DELETE FROM notes WHERE name=?", (arg.strip(),)); conn.commit(); conn.close()
            await edit(f"🗑 O'chirildi: {arg.strip()}")
        elif cmd == ".calc":
            try:
                await edit(f"🧮 {arg} = {safe_calc(arg)}")
            except Exception as e:
                await edit(f"Xato: {e}")
        elif cmd == ".katta":
            await edit(arg.upper())
        elif cmd == ".kichik":
            await edit(arg.lower())
        elif cmd == ".teskari":
            await edit(arg[::-1])
        elif cmd == ".yoz":
            typed = ""
            for ch in arg:
                typed += ch
                try:
                    await event.edit(typed + "▌")
                except Exception:
                    pass
                await asyncio.sleep(0.08)
            await event.edit(arg)
        elif cmd == ".tozalash":
            try:
                n = max(1, min(20, int(arg.strip() or "5")))
            except ValueError:
                n = 5
            deleted = 0
            async for msg in client.iter_messages(event.chat_id, limit=100):
                if msg.out and msg.id != event.id:
                    try:
                        await msg.delete(); deleted += 1
                    except Exception:
                        pass
                    if deleted >= n:
                        break
            await edit(f"🗑 {deleted} ta o'z xabarim o'chirildi.")
        elif cmd == ".stat":
            total = mine = 0
            async for msg in client.iter_messages(event.chat_id, limit=200):
                total += 1
                if msg.out:
                    mine += 1
            await edit(f"📊 Oxirgi 200 ta ichida:\nJami: {total}\nMening xabarlarim: {mine}\nBoshqalar: {total - mine}")
        elif cmd == ".sales":
            state["sales"] = arg.lower() == "on"
            await edit(f"Savdo rejimi: {'on' if state['sales'] else 'off'}")
        elif cmd == ".hisobot":
            conn = db()
            total_sent = conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0]
            rows = conn.execute("SELECT chat_title, text, ts, kind FROM sent_log ORDER BY id DESC LIMIT 15").fetchall()
            inq = conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
            unans = conn.execute("SELECT chat_title, text, ts FROM inquiries WHERE answered=0 ORDER BY id DESC LIMIT 15").fetchall()
            conn.close()
            out = [f"📋 Hisobot — {now_uz()}", f"Avtomatik yozganlarim: {total_sent}", f"Savdo so'rovlari: {inq}", "", "Oxirgi yozganlarim:"]
            for title, t, ts, kind in rows:
                out.append(f"- [{kind}] {title}: {t[:100]} ({ts[:16]})")
            out.append(""); out.append("Javobsiz savdo so'rovlari:")
            for title, t, ts in unans:
                out.append(f"- {title}: {t[:100]} ({ts[:16]})")
            await edit("\n".join(out))

    @client.on(events.NewMessage(incoming=True))
    async def auto(event):
        if event.out:
            return
        if state["afk"] and event.is_private and event.chat_id not in state["afk_replied"]:
            if time.time() - state["last_auto"] >= 3:
                state["last_auto"] = time.time(); state["afk_replied"].add(event.chat_id)
                reason = f" Sabab: {state['afk_reason']}" if state["afk_reason"] else ""
                await event.reply(f"💤 Hozir bandman, keyin javob beraman.{reason}")
                return
        if state["sales"]:
            text = event.raw_text or ""
            if not text:
                return
            if time.time() - state["last_auto"] < 3:
                await asyncio.sleep(3)
            state["last_auto"] = time.time()
            ans = product_answer(text, load_products())
            title = getattr(event.chat, "title", None) or getattr(event.chat, "username", None) or str(event.chat_id)
            conn = db()
            if ans:
                await event.reply(ans)
                conn.execute("INSERT INTO sent_log VALUES (NULL,?,?,?,?,?)", (event.chat_id, title, ans, now_uz(), "sales"))
                conn.execute("INSERT INTO inquiries VALUES (NULL,?,?,?,?,?,1)", (event.chat_id, title, event.sender_id, text, now_uz()))
            elif any(k in text.lower() for k in SALES_KEYWORDS):
                conn.execute("INSERT INTO inquiries VALUES (NULL,?,?,?,?,?,0)", (event.chat_id, title, event.sender_id, text, now_uz()))
            conn.commit(); conn.close()

    await client.start()
    print("Server userbot shabloni ishga tushdi.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
