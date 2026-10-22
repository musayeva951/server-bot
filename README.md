# Server bot (MVP — birinchi versiya)

Bitta Telegram boti "server" bo'lib ishlaydi: odamlar anime bot ochish
uchun alohida server qidirib yurmaydi.

## Ikki usul

|  | 1-usul: token | 2-usul: kod |
|---|---|---|
| Foydalanuvchi nima beradi | BotFather tokeni | `.py` fayl yoki `.zip` (ichida `bot.py`/`main.py`) |
| Nima ishga tushadi | Tayyor anime bot shabloni (`child_anime_bot.py`) | Foydalanuvchining o'z kodi |
| Baza | Har bot uchun alohida `child.db` | Kod o'zi hal qiladi (`CHILD_DB_PATH` env beriladi) |
| Token qayerda | Server botda shifrlanib saqlanadi | **Kodning ichida bo'lishi foydalanuvchi zimmasida** |

## Tuzilma

```
server-bot/
  server_bot.py       — asosiy bot: menyu, token/kod qabul qilish,
                        jarayonlarni ishga tushirish/to'xtatish, log ko'rish
  child_anime_bot.py  — tayyor anime bot shabloni (token usuli uchun)
  requirements.txt    — python-telegram-bot, cryptography
  .env.example        — env misoli (faqat placeholder!)
  server.db           — asosiy baza (ish vaqtida yasaladi, git ga kirmaydi)
  data/bots/<id>/     — har bir bot papkasi: child.db, bot.log, code/ (kod usulida)
  tests/test_local.py — tarmoqsiz lokal sinovlar
```

Asosiy baza (`server.db`) jadvallari:
- `users` — foydalanuvchilar
- `bots` — owner_id, turi (`token`/`code`), shifrlangan token (ochiq token
  **yo'q**, takrorni topish uchun alohida hash), kod papkasi, username,
  holat, yaratilgan vaqt

## Qanday ishga tushirish (lokal)

```bash
cd server-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SERVER_BOT_TOKEN="BotFather bergan server bot tokeni" \
SERVER_BOT_SECRET="uzun tasodifiy maxfiy so'z" \
.venv/bin/python server_bot.py
```

Sinovlar (Telegram kerak emas):

```bash
.venv/bin/python tests/test_local.py
python3 -m py_compile server_bot.py child_anime_bot.py
```

## FadeHost ga chiqarish (qisqa)

1. BotFather da server bot uchun alohida bot oching, tokenini oling
   (tokenni chatga yozmang — panel env iga o'zingiz kiriting).
2. Bu papkani alohida GitHub repo qiling (`.env`, `server.db`, `data/`
   repo ga **kirmasin** — `.gitignore` tayyor).
3. FadeHost da ikkinchi hisob bilan (hisob boshiga 1 ta bepul bot)
   yangi ilova yarating, repo ni ulang.
4. Panel env iga yozing: `SERVER_BOT_TOKEN`, `SERVER_BOT_SECRET`,
   kerak bo'lsa `MAX_BOTS=5`, `SERVER_DB_PATH=/data/server.db`,
   `DATA_DIR=/data` (baza va botlar papkasi doimiy diskda qolsin).
5. Start buyrug'i: `python server_bot.py`. Deploy dan keyin server botga
   `/start` yuborib tekshiring.

Eslatma: FadeHost bepul tarifda GitHub orqali kirishda "Confirm access"
email tasdig'i kerak bo'lishi mumkin — uni hisob egasining o'zi o'tadi.

## Cheklovlar

- Tekin server kuchsiz: **256 MB xotira, kuniga 1 GB trafik**. Shuning
  uchun umumiy limit bor: `MAX_BOTS` (default 5), har foydalanuvchiga
  hozircha 1 ta bot (`MAX_BOTS_PER_USER`).
- Kod fayli limiti: **2 MB**; zip ochilganda 10 MB.
- Har bir bola-jarayonga yumshoq xotira limiti (512 MB) qo'yilgan, lekin
  bu to'liq izolyatsiya emas.
- Video trafik ko'p ketsa, kunlik 1 GB tez tugaydi — botlar soni
  ko'paysa, pullik/kuchli server kerak bo'ladi.

## Xavfsizlik

- Tokenlar logga yozilmaydi; xabar bilan kelgan token darhol
  o'chirishga harakat qilinadi.
- Tokenlar bazada `SERVER_BOT_SECRET` dan yasalgan **Fernet** kaliti
  bilan shifrlanadi. Bu secret yo'qolsa, saqlangan tokenlarni ochib
  bo'lmaydi — botlarni qayta qo'shishga to'g'ri keladi.
- Har bir bola-jarayonga **toza env** beriladi: asosiy bot tokeni va
  secret bola jarayonga tushmaydi (lokal sinovda tekshirilgan).
- Kod usuli = begona kodni ochiq ishga tushirish. Tekin serverda
  konteyner/sandbox yo'q: alohida papka + toza env + xotira limiti
  qo'llangan, lekin kod usulini faqat ishonchli odamlarga oching,
  kod egasi o'z kodi uchun o'zi javobgar.

## Bola (child) anime botda nima bor

- Foydalanuvchi: `/start`, anime ro'yxati, qidiruv (nom yozsa), qismlar,
  video ko'rish
- Egasi (admin): "Anime qo'shish", "Qism qo'shish" (video qabul qilib
  `file_id` saqlaydi — video shu botga yuborilgani uchun `file_id`
  shu botda ishlaydi)
- MVP ga kirmaydi: majburiy obuna, premium/to'lov, postlar, sayt API
