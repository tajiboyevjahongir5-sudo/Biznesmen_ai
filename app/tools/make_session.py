"""Userbot uchun TG_SESSION yaratish (bir marta, o'z kompyuteringizda ishga tushiring).

    python -m app.tools.make_session

Skript Telegram akkauntingizga kirish uchun telefon raqami, Telegram yuborgan kod va
(yoqilgan bo'lsa) ikki bosqichli parolni so'raydi. Bular faqat Telegram serveriga
yuboriladi — hech qayerga saqlanmaydi va loglanmaydi. Natijada SESSIYA KALITI chiqadi:
uni Railway Variables dagi TG_SESSION ga qo'ying va hech kimga bermang (bu kalit
akkauntingizga to'liq kirish huquqini beradi). Kerak bo'lmay qolsa, Telegram →
Sozlamalar → Qurilmalar bo'limidan "BIZNES MIND payments" sessiyasini o'chiring.
"""

from __future__ import annotations

import asyncio
import getpass


async def _main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    print("=== BIZNES MIND AI — userbot sessiyasini yaratish ===")
    print("API ID va API HASH ni https://my.telegram.org → API development tools dan oling.\n")
    api_id = int(input("API ID (raqam): ").strip())
    api_hash = getpass.getpass("API HASH (ekranda ko'rinmaydi): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash, device_model="BIZNES MIND payments")
    await client.start(
        phone=lambda: input("Telefon raqam (+998...): ").strip(),
        code_callback=lambda: input("Telegram yuborgan kod: ").strip(),
        password=lambda: getpass.getpass("Ikki bosqichli parol (bo'lsa; ekranda ko'rinmaydi): "),
    )
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()

    print(f"\nKirildi: {me.first_name} (id {me.id})")
    print("\nQuyidagi SESSIYA KALITINI Railway → Variables → TG_SESSION ga qo'ying.")
    print("Uni chatga, GitHub ga yoki boshqa joyga YOZMANG. Nusxa olgach, terminalni tozalang.\n")
    print(session)


if __name__ == "__main__":
    asyncio.run(_main())
