import asyncio
import csv
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
PHONE = os.getenv("TG_PHONE", "")
SESSION_NAME = os.getenv("TG_SESSION_NAME", "telethon_user")
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("TG_ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Нужно задать TG_API_ID, TG_API_HASH и TG_BOT_TOKEN в .env или переменных окружения."
    )

telethon_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# user_id -> [(group_id, group_title)]
user_group_cache: Dict[int, List[Tuple[int, str]]] = {}


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def ensure_telethon_connected() -> None:
    if telethon_client.is_connected():
        return

    await telethon_client.connect()

    if await telethon_client.is_user_authorized():
        return

    if not PHONE:
        raise RuntimeError(
            "Telethon-сессия не авторизована. Укажи TG_PHONE и один раз запусти авторизацию."
        )

    await telethon_client.send_code_request(PHONE)
    raise RuntimeError(
        "Первая авторизация Telethon не завершена. Запусти скрипт telethon_auth_once_env.py локально, "
        "введи код из Telegram, а если включён 2FA — и пароль. После этого появится session-файл, "
        "и бот сможет работать без повторной авторизации."
    )


async def get_groups() -> List[Tuple[int, str]]:
    result = await telethon_client(
        GetDialogsRequest(
            offset_date=None,
            offset_id=0,
            offset_peer=InputPeerEmpty(),
            limit=4000,
            hash=0,
        )
    )

    groups: List[Tuple[int, str]] = []
    for chat in result.chats:
        try:
            if getattr(chat, "megagroup", False):
                groups.append((chat.id, chat.title))
        except Exception:
            continue

    groups.sort(key=lambda x: x[1].lower())
    return groups


async def build_csv_for_group(group_id: int, group_title: str) -> Path:
    entity = await telethon_client.get_entity(group_id)
    participants = await telethon_client.get_participants(entity)

    safe_group_title = re.sub(r'[^A-Za-zА-Яа-я0-9._ -]+', '_', group_title).strip()
    if not safe_group_title:
        safe_group_title = f"group_{group_id}"

    tmp_dir = Path(tempfile.gettempdir())
    csv_path = tmp_dir / f"members_{safe_group_title}_{group_id}.csv"

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=",")
        writer.writerow(["user_id", "username", "name", "phone", "group"])

        for user in participants:
            username = user.username or ""
            first_name = user.first_name or ""
            last_name = user.last_name or ""
            phone = getattr(user, "phone", "") or ""
            full_name = f"{first_name} {last_name}".strip()
            writer.writerow([user.id, username, full_name, phone, group_title])

    return csv_path


@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("У тебя нет доступа к этому боту.")
        return

    await message.answer(
        "Привет! Я могу выгрузить участников твоих Telegram-групп в CSV.\n\n"
        "Команды:\n"
        "/groups — показать список доступных групп\n"
        "/help — краткая справка"
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("У тебя нет доступа к этому боту.")
        return

    await message.answer(
        "1) Отправь /groups\n"
        "2) Нажми на нужную группу\n"
        "3) Получи CSV-файл с участниками\n\n"
        "Важно: бот использует user-сессию Telethon. Он увидит только те группы, к которым привязан этот аккаунт."
    )


@dp.message(Command("groups"))
async def groups_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await message.answer("У тебя нет доступа к этому боту.")
        return

    try:
        await ensure_telethon_connected()
        groups = await get_groups()
    except Exception as e:
        logger.exception("Ошибка при получении групп")
        await message.answer(f"Не удалось получить список групп: {e}")
        return

    if not groups:
        await message.answer("Не нашёл доступных megagroup-групп в этой Telethon-сессии.")
        return

    user_group_cache[message.from_user.id] = groups

    builder = InlineKeyboardBuilder()
    for group_id, group_title in groups[:100]:
        builder.row(
            InlineKeyboardButton(
                text=group_title[:60],
                callback_data=f"export:{group_id}",
            )
        )

    await message.answer(
        f"Найдено групп: {len(groups)}. Ниже первые 100. Нажми на нужную — пришлю CSV.",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("export:"))
async def export_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id

    if not is_allowed(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer("Готовлю CSV...")

    raw_group_id = callback.data.split(":", 1)[1]
    if not raw_group_id.lstrip("-").isdigit():
        await callback.message.answer("Некорректный идентификатор группы.")
        return

    group_id = int(raw_group_id)
    cached_groups = user_group_cache.get(user_id, [])
    title_lookup = {gid: title for gid, title in cached_groups}
    group_title = title_lookup.get(group_id, f"group_{group_id}")

    try:
        await ensure_telethon_connected()
        csv_path = await build_csv_for_group(group_id, group_title)
        document = FSInputFile(csv_path)
        await callback.message.answer_document(
            document=document,
            caption=f"Готово: {group_title}",
        )
    except Exception as e:
        logger.exception("Ошибка при выгрузке участников")
        await callback.message.answer(f"Не удалось выгрузить участников: {e}")


async def main() -> None:
    try:
        await ensure_telethon_connected()
    except SessionPasswordNeededError:
        raise RuntimeError(
            "Для этой Telethon-сессии включён облачный пароль (2FA). Заверши первую авторизацию локально."
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
