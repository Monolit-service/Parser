import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
SESSION_NAME = os.getenv("TG_SESSION_NAME", "telethon_user")

if not API_ID or not API_HASH or not PHONE:
    raise RuntimeError("Нужно задать TG_API_ID, TG_API_HASH и TG_PHONE в .env или переменных окружения")


async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        print("Сессия уже авторизована.")
        await client.disconnect()
        return

    await client.send_code_request(PHONE)
    code = input("Введите код из Telegram: ").strip()

    try:
        await client.sign_in(PHONE, code)
    except SessionPasswordNeededError:
        password = input("Введите пароль 2FA: ").strip()
        await client.sign_in(password=password)

    print(f"Готово. Session-файл создан: {SESSION_NAME}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
