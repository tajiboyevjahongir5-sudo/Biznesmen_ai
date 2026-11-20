# BIZNES MIND AI

Telegram bot ko'rinishidagi AI biznes maslahatchisi. Bot foydalanuvchi bilan menyusiz,
jonli inson kabi suhbatlashadi, g'oyani savollar orqali chuqur o'rganadi, talab, raqobat
va muvaffaqiyatsizlik sabablarini tanqidiy tahlil qiladi va oxirida boshqa AI larda
ishlatish uchun tayyor **SUPER PROMPT** beradi.

Qo'shimcha ravishda bot **pullik g'oyalar kanali**ni yuritadi:

- har kuni izlanish o'tkazib, talabi yuqori va raqobati past g'oyalarni topadi;
- g'oyalarni kanalga joylaydi, post ostidagi tugma botga o'tib o'sha g'oya promptini beradi;
- kanalga kirish uchun to'lovni **noyob summa** orqali avtomatik tekshiradi va **bir martalik** havola beradi.

## Hozir nima ishlaydi (haqiqiy holat)

| Funksiya | Holat |
|---|---|
| Menyusiz tabiiy suhbat, 6–12 ta moslashtirilgan savol | Ishlaydi (Gemini yoki OpenAI kaliti kerak) |
| Tahlil: talab, raqobat, "nima uchun muvaffaqiyatsiz bo'lishi mumkin", optimist/skeptik bahsi, yakuniy qaror | Ishlaydi — hozircha **bitta** agent chaqiruvida |
| SUPER PROMPT yaratish | Ishlaydi |
| Internet qidiruvi | Faqat `AI_PROVIDER=openai` va `RESEARCH_PROVIDER=openai_web` bo'lsa. Gemini rejimida o'chiq. Aks holda bot buni ochiq aytadi va da'volarni "taxmin" deb belgilaydi |
| Kunlik g'oya izlanishi + kanalga joylash | Ishlaydi (admin tasdig'i bilan yoki avtomatik) |
| Kanal posti tugmasi → botda prompt | Ishlaydi (faqat kanal a'zolariga) |
| To'lov: narx + 1..99 noyob summa, 5 daqiqa taymer, to'lov kanalini kuzatish, bir martalik havola | Ishlaydi; bank xabari formati sizning bankingizda tekshirilishi kerak |
| Admin panel (Telegram buyruqlari), kanal/karta/narxni botdan kiritish | Ishlaydi |
| "Har kuni o'rganish" | Har kungi izlanish xulosalari DB ga yoziladi va keyingi tahlillarda kontekst sifatida beriladi. Bu model qayta o'qitilishi emas |
| Python orqali moliyaviy model, PDF/DOCX eksport, FastAPI, Redis, Alembic, Docker | **Hali qilinmagan** (keyingi bosqichlar) |

## Arxitektura

```
app/
  main.py                 ishga tushirish, DI, polling, fon vazifalari
  config.py               .env sozlamalari (SecretStr)
  agents/                 interviewer, analyst, prompt_engineer, idea_scout
  prompts/                agentlar tizim ko'rsatmalari (.txt)
  core/
    orchestrator.py       suhbat -> tahlil -> prompt (chaqiruvlar soni cheklangan)
    security.py           input tekshiruvi, prompt injection himoyasi, rate limit
    logging.py            tokenlarni yashiruvchi logging
    text.py               Telegram HTML formatlash, uzun xabarlarni bo'lish
  services/
    ai_service.py         Gemini/OpenAI: timeout, retry, limitlar, structured output
    runtime_settings.py   admin paneldan kiritiladigan sozlamalar (DB da)
    payment_service.py    noyob summa rezervatsiyasi va moslashtirish
    payment_parser.py     bank xabaridan summa (regex, AI ishlatilmaydi)
    channel_service.py    kunlik izlanish va kanalga joylash
    scheduler.py          kunlik vazifa va to'lov tozalagich
  bot/
    handlers/             user, payment, admin, errors
    middlewares/          foydalanuvchini aniqlash, bloklash, rate limit
  database/               SQLAlchemy 2 modellar va repository
tests/                    pytest (barcha tashqi API lar mock)
```

## O'rnatish (Windows 11, PowerShell)

Python 3.12+ kerak.

```powershell
cd D:\EXE\Deep-Live-Cam\biznes-mind-ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
notepad .env
```

> Agar `Activate.ps1` bloklansa: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### .env ni to'ldirish

1. **BOT_TOKEN** — Telegram'da [@BotFather](https://t.me/BotFather) → `/newbot` → tokenni `.env` ga yozing.
2. **GEMINI_API_KEY** — https://aistudio.google.com/apikey dan oling (Google hisobingiz bilan).
   Bepul tarifda kunlik so'rovlar limiti bor; ko'p foydalanuvchi bo'lsa, AI Studio'da billing yoqiladi.
3. **GEMINI_MODEL** — AI Studio'da ko'rsatilgan joriy model nomi. Kodga qattiq yozilmagan.
   Model band (503) bo'lsa, bot `GEMINI_FALLBACK_MODELS` dagi zaxira modellarga avtomatik o'tadi;
   `AI_REASONING_EFFORT=low` javobni tezlashtiradi. Admin `/aitest` har bir modelni alohida sinaydi.
4. **ADMIN_IDS** — o'z Telegram ID ingiz (masalan, @userinfobot orqali bilib olasiz).

OpenAI ishlatmoqchi bo'lsangiz: `AI_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL`.

Tokenlarni hech qachon chatga, Git ga yoki skrinshotga qo'ymang.

### Kanal va to'lovni sozlash (botning o'zida, admin panel orqali)

Bu sozlamalar `.env` ga yozilmaydi — botda admin sifatida kiritasiz va ular bazada saqlanadi.

1. Ikki **yopiq** kanal yarating:
   - **G'oyalar kanali** — pullik kanal.
   - **To'lov kanali** — bank kirim xabarlari tushadigan kanal. Bu kanalga faqat siz yoki
     ishonchli avtomatika post yoza olishi kerak — u yerdagi har bir xabar to'lov deb hisoblanadi.
2. Botga `/link_ideas` yuboring. Botni g'oyalar kanaliga admin qiling ("Post messages" va
   "Invite users via link" huquqlari bilan) va bot bergan `BMA-XXXXXXXX` kodini kanalga post qiling.
   Bot kanalni ulab, sizga xabar beradi. Kod 10 daqiqa va faqat bir marta ishlaydi.
3. To'lov kanali uchun ham xuddi shunday: `/link_payment`.
4. `/card 8600 1234 5678 9012`, `/card_holder Ism Familiya`, `/price 50000`, `/pay_minutes 5`.
5. `/settings` — hammasi to'g'ri kiritilganini ko'rasiz ("To'lov tizimi: tayyor").
6. Bank bildirishnomalarini to'lov kanaliga tushirish yo'lini o'zingiz tashkil qilasiz
   (masalan, telefondagi SMS/bildirishnoma forward ilovasi orqali). Telegram boti boshqa
   botlarning shaxsiy chatini o'qiy olmaydi.
7. Bankingiz xabari namunasini to'lov kanaliga yuborib tekshiring. Format tanilmasa,
   `PAYMENT_AMOUNT_REGEX` o'zgaruvchisini sozlang.

**To'lov qanday ishlaydi:** foydalanuvchiga `50 000 + N` (N = 1..99) summa beriladi.
Bir vaqtda bir xil summa ikki kishiga berilmaydi (kod + DB unique index). 5 daqiqa ichida
aynan shu summa kelsa — bir martalik (`member_limit=1`), 24 soatlik havola yuboriladi.
Vaqt tugasa summa bo'shaydi. Kech kelgan yoki noto'g'ri summalar **avtomatik tasdiqlanmaydi** —
adminga xabar boradi va admin `/confirm id` bilan tasdiqlaydi.

Cheklov: bir vaqtda maksimal 99 ta kutilayotgan to'lov. Barqarorroq yechim uchun keyinchalik
Click/Payme rasmiy merchant API sini ulash tavsiya etiladi.

## Ishga tushirish

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.main
```

Ma'lumotlar `data\biznes_mind.db` (SQLite) da, loglar `logs\bot.log` da saqlanadi.

## Railway'ga deploy

1. Railway'da yangi loyiha → **Deploy from GitHub repo** → shu repozitoriyani tanlang.
2. Loyihaga **PostgreSQL** qo'shing (New → Database → PostgreSQL). Railway'da fayl tizimi
   doimiy emas, shuning uchun SQLite ishlatmang.
3. Bot servisining **Variables** bo'limida kamida `BOT_TOKEN`, `ADMIN_IDS`, `GEMINI_API_KEY`,
   `GEMINI_MODEL` ni kiriting (qolganlari ixtiyoriy, `.env.example` ga qarang) va
   `DATABASE_URL=${{Postgres.DATABASE_URL}}` deb yozing (`postgresql://` avtomatik `asyncpg` ga moslanadi).
   `LOG_FILE` ni bo'sh qoldiring — loglar Railway konsolida ko'rinadi.
4. Start buyrug'i `railway.json` da: `python -m app.main`. Bot polling rejimida ishlaydi,
   shuning uchun **faqat 1 replika** bo'lishi kerak (ikkita nusxa Telegram'da konflikt beradi).

## Botdan foydalanish

Foydalanuvchi: shunchaki g'oyasini yozadi. Qo'shimcha buyruqlar: `/new`, `/cancel`, `/history`,
`/prompt 12`, `/report 12` (to'liq hisobot fayli), `/delete 12`, `/kanal`, `/help`.

Tahlil oxirida bot moliyaviy modelni (marja, zararsizlik nuqtasi, 3 ssenariy, qoplash muddati)
**Python bilan hisoblaydi** — AI faqat kirish raqamlarini beradi. To'liq hisobot HTML fayl
sifatida yuboriladi: brauzerda ochiladi va "Chop etish → PDF" bilan PDF qilib saqlanadi.

AI band bo'lsa, xabar navbatga olinadi va bot 1, 3 va 7 daqiqadan keyin qayta urinib,
javobni o'zi yuboradi. Doimiy xatolar haqida adminlarga xabar boradi (15 daqiqada bir marta).

Admin: `/admin` (statistika va AI xarajati), `/research` (izlanishni hozir boshlash),
`/ideas`, `/publish id`, `/reject id`, `/payments`, `/confirm id`, `/block tg_id`,
`/unblock tg_id`, `/promo`, `/settings` (kanal, karta, narx, kunlik g'oyalar va boshqalar),
`/stats` (foydalanuvchilar, faollik, konversiya, tushum, obunachilar),
`/broadcast` (xabarga reply qilib — hammaga yuborish, tasdiqlash bilan),
`/sub_days 30` (oylik obuna; muddat tugashidan 3 kun oldin eslatma, tugagach kanaldan
chiqariladi; `0` — umrbod kirish).

Ma'lumotlar bazasi: yangi versiyada qo'shilgan ustunlar ishga tushishda avtomatik qo'shiladi
(mavjud ma'lumotlar o'chirilmaydi).

## Testlar

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
python -m ruff check .
```

Testlarda haqiqiy API kalitlari ishlatilmaydi — Gemini, OpenAI va Telegram mock qilingan.

## Xavfsizlik

- Tokenlar faqat `.env` da; `.gitignore` uni chiqarib tashlaydi. Loglarda token/kalit
  ko'rinishidagi har qanday satr avtomatik `[YASHIRILDI]` ga almashtiriladi.
- Foydalanuvchi matni loglanmaydi; foydalanuvchi ID si loglarda xeshlangan.
- Foydalanuvchi va internet matnlari AI ga alohida `<blok>` ichida "ishonchsiz ma'lumot"
  sifatida beriladi; blokni yopuvchi teglar zararsizlantiriladi.
- Karta raqami/parol yozilsa, bot uni saqlamaydi va ogohlantiradi.
- Rate limit, bir foydalanuvchiga bir vaqtda bitta AI so'rovi, kunlik foydalanuvchi va global
  limitlar, maksimal token, timeout va retry.
- Bank xabarlari faqat regex bilan o'qiladi va hech qachon AI ga yuborilmaydi.
- Har bir loyiha so'rovida egalik tekshiriladi; `/delete id ha` loyihani to'liq o'chiradi.

## Zaxira nusxa (SQLite)

```powershell
New-Item -ItemType Directory -Force backups
Copy-Item data\biznes_mind.db "backups\biznes_mind_$(Get-Date -Format yyyyMMdd_HHmm).db"
```

Tiklash: botni to'xtating va nusxani `data\biznes_mind.db` ga qaytarib nusxalang.

## Xatolarni tuzatish

| Belgisi | Sabab |
|---|---|
| `XATO: .env faylida ... to'ldirilmagan` | BOT_TOKEN / GEMINI_API_KEY / GEMINI_MODEL kiritilmagan |
| "AI xizmati sozlanmagan" | Kalit noto'g'ri yoki model nomi mavjud emas |
| "AI xizmatining limiti tugagan" / "hozir band" | API balansi yoki Gemini bepul tarif limiti tugagan |
| Kanal ulanmadi | Bot kanalda admin emas yoki kerakli huquqlar berilmagan; yangi kod oling |
| To'lov tasdiqlanmayapti | Bot to'lov kanalida admin emas yoki bank xabari formati tanilmadi |
| Havola yaratilmadi | Bot g'oyalar kanalida "invite users" huquqiga ega emas |
