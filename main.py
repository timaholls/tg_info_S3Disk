import asyncio
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import pymysql
from aiogram import Bot, Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils import executor
from dotenv import load_dotenv

load_dotenv()

local = False

if local == True:
    API_TOKEN = os.getenv("TG_TOKEN_LOCAL")
    DB_HOST = 'localhost'
    DB_PORT = 3307
    DB_NAME = 's3_manager_local'
    DB_USER = 's3_user'
    DB_PASSWORD = 'Kfleirb_17$_'
else:
    API_TOKEN = os.getenv("TG_TOKEN")
    DB_HOST = 'localhost'
    DB_PORT = 3306
    DB_NAME = 's3_manager'
    DB_USER = 's3_user'
    DB_PASSWORD = 'Kfleirb_17$_'


def get_db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


class InviteRequestForm(StatesGroup):
    waiting_first_name = State()
    waiting_last_name = State()
    waiting_middle_name = State()
    waiting_departments = State()
    waiting_confirmation = State()


def back_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("⬅️ Назад", callback_data="back"))
    return keyboard


def confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("✅ Да", callback_data="confirm_yes"),
        InlineKeyboardButton("🔄 Нет", callback_data="confirm_no"),
    )
    return keyboard


def _fetch_departments_sync() -> List[Dict[str, object]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name FROM auth_group ORDER BY name")
            return cursor.fetchall()


async def fetch_departments() -> List[Dict[str, object]]:
    return await asyncio.to_thread(_fetch_departments_sync)


async def send_departments_prompt(target, state: FSMContext) -> None:
    departments = await fetch_departments()
    await state.update_data(departments_catalog=departments)

    if not departments:
        text = (
            "Отделы не найдены в системе. Свяжитесь с администратором для уточнения."
        )
    else:
        lines = [f"{idx}. {dept['name']}" for idx, dept in enumerate(departments, start=1)]
        numbered_list = "\n".join(lines)
        text = (
            "Выберите отделы, указав номера через запятую (например 1,3,2).\n"
            f"\n{numbered_list}"
        )

    await target.answer(text, reply_markup=back_keyboard())


def _create_request_sync(full_name: str, telegram_id: str, departments: List[str]) -> Tuple[int, List[str]]:
    missing: List[str] = []
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status, created_at
                FROM s3app_userrequest
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (telegram_id,),
            )
            existing = cursor.fetchone()
            if existing and existing["status"] in {"new", "pending"}:
                return existing["id"], ["__active__"]
            if existing and existing["status"] == "processed":
                return existing["id"], ["__processed__"]

            cursor.execute(
                """
                INSERT INTO s3app_userrequest (full_name, telegram_id, status, created_at, processed_at, processed_by_id)
                VALUES (%s, %s, 'new', NOW(), NULL, NULL)
                """,
                (full_name, telegram_id),
            )
            request_id = cursor.lastrowid

            for dept in departments:
                cursor.execute(
                    "SELECT id FROM auth_group WHERE LOWER(name) = LOWER(%s) LIMIT 1",
                    (dept,),
                )
                row = cursor.fetchone()
                if not row:
                    missing.append(dept)
                    continue
                cursor.execute(
                    """
                    INSERT IGNORE INTO s3app_userrequest_departments (userrequest_id, group_id)
                    VALUES (%s, %s)
                    """,
                    (request_id, row["id"]),
                )

        conn.commit()
    return request_id, missing


def _get_latest_request_sync(telegram_id: str) -> Optional[Dict[str, object]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status, created_at
                FROM s3app_userrequest
                WHERE telegram_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (telegram_id,),
            )
            request = cursor.fetchone()
            if not request:
                return None
            cursor.execute(
                """
                SELECT g.name
                FROM auth_group g
                JOIN s3app_userrequest_processed_departments pd ON g.id = pd.group_id
                WHERE pd.userrequest_id = %s
                ORDER BY g.name
                """,
                (request["id"],),
            )
            request["processed_departments"] = [row["name"] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT g.name
                FROM auth_group g
                JOIN s3app_userrequest_departments d ON g.id = d.group_id
                WHERE d.userrequest_id = %s
                ORDER BY g.name
                """,
                (request["id"],),
            )
            request["departments"] = [row["name"] for row in cursor.fetchall()]
            return request


async def create_request(full_name: str, telegram_id: str, departments: List[str]) -> Tuple[int, List[str]]:
    return await asyncio.to_thread(_create_request_sync, full_name, telegram_id, departments)


async def get_latest_request(telegram_id: str) -> Optional[Dict[str, object]]:
    return await asyncio.to_thread(_get_latest_request_sync, telegram_id)


STATUS_DISPLAY = {
    "new": "🆕 Новая",
    "pending": "⏳ Незавершена",
    "processed": "✅ Обработана",
    "rejected": "❌ Отклонена",
}


async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот для оформления заявок. Используйте /post_invate, чтобы отправить новую заявку, или /help для справки."
    )


async def cmd_help(message: Message) -> None:
    await message.answer(
        "Доступные команды:\n"
        "/post_invate — отправить заявку;\n"
        "/status — узнать статус последней заявки;\n"
        "/help — эта справка;\n"
        "/start — начать работу."
    )


async def cmd_post_invate(message: Message, state: FSMContext) -> None:
    existing = await get_latest_request(str(message.from_user.id))
    if existing:
        status = existing["status"]
        status_readable = STATUS_DISPLAY.get(status, status)
        departments = ", ".join(existing.get("departments", [])) or "не указаны"
        created_at = existing["created_at"].strftime("%d.%m.%Y %H:%M") if existing["created_at"] else "—"
        if status in {"new", "pending"}:
            lines = [
                "ℹ️ У вас уже есть активная заявка.",
                "",
                f"📋 Заявка №{existing['id']}",
                f"Статус: {status_readable}",
                f"Отделы: {departments}",
                f"Создана: {created_at}",
                "",
                "Проверьте статус командой /status",
            ]
            await message.answer("\n".join(lines))
            return
        if status == "processed":
            lines = [
                "✅ Ваша заявка уже обработана.",
                "",
                f"📋 Заявка №{existing['id']}",
                f"Отделы: {departments}",
                f"Дата: {created_at}",
            ]
            await message.answer("\n".join(lines))
            return

    await state.finish()
    await state.set_state(InviteRequestForm.waiting_first_name.state)
    await message.answer("Введите имя.")


async def process_first_name(message: Message, state: FSMContext) -> None:
    first_name = message.text.strip()
    if len(first_name) < 2:
        await message.answer("Пожалуйста, укажите имя полностью.")
        return
    await state.update_data(first_name=first_name)
    await state.set_state(InviteRequestForm.waiting_last_name.state)
    await message.answer("Введите фамилию.", reply_markup=back_keyboard())


async def process_last_name(message: Message, state: FSMContext) -> None:
    last_name = message.text.strip()
    if len(last_name) < 2:
        await message.answer("Пожалуйста, укажите фамилию полностью.", reply_markup=back_keyboard())
        return
    await state.update_data(last_name=last_name)
    await state.set_state(InviteRequestForm.waiting_middle_name.state)
    await message.answer("Введите отчество (или напишите 'нет').", reply_markup=back_keyboard())


async def process_middle_name(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    middle_name = "" if value.lower() == "нет" else value
    if middle_name and len(middle_name) < 2:
        await message.answer("Пожалуйста, укажите отчество полностью или напишите 'нет'.", reply_markup=back_keyboard())
        return
    await state.update_data(middle_name=middle_name)
    await state.set_state(InviteRequestForm.waiting_departments.state)
    await send_departments_prompt(message, state)


async def process_departments(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    normalized = text.replace(" ", "")
    if not normalized:
        await message.answer(
            "Укажите хотя бы один номер отдела.",
            reply_markup=back_keyboard(),
        )
        return

    if not re.fullmatch(r"\d+(,\d+)*", normalized):
        await message.answer(
            "Введите номера отделов через запятую, например 1,3,2.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    catalog: List[Dict[str, object]] = data.get("departments_catalog") or []
    if not catalog:
        await send_departments_prompt(message, state)
        return

    indexes = [int(part) for part in normalized.split(",")]
    unique_indexes = list(dict.fromkeys(indexes))

    if any(idx < 1 or idx > len(catalog) for idx in unique_indexes):
        await message.answer(
            "Указан номер вне диапазона списка. Попробуйте снова.",
            reply_markup=back_keyboard(),
        )
        return

    selected = [catalog[idx - 1]["name"] for idx in unique_indexes]
    await state.update_data(departments=selected)
    await state.set_state(InviteRequestForm.waiting_confirmation.state)
    selected_text = ", ".join(selected) if selected else "—"
    await message.answer(
        f"Вы выбрали отделы: {selected_text}. Подтвердить?",
        reply_markup=confirmation_keyboard(),
    )

async def handle_confirmation(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    try:
        await query.message.edit_reply_markup()
    except Exception:
        pass

    data = await state.get_data()
    if query.data == "confirm_yes":
        first_name = data.get("first_name", "").strip()
        last_name = data.get("last_name", "").strip()
        middle_name = data.get("middle_name", "").strip()
        departments = data.get("departments", [])
        full_name_parts = [part for part in [last_name, first_name, middle_name] if part]
        full_name = " ".join(full_name_parts)
        request_id, missing = await create_request(
            full_name=full_name,
            telegram_id=str(query.from_user.id),
            departments=departments,
        )
        await state.finish()

        if missing == ["__active__"]:
            await query.message.answer(
                "У вас уже есть активная заявка. Проверьте статус командой /status."
            )
            return
        if missing == ["__processed__"]:
            latest = await get_latest_request(str(query.from_user.id))
            if latest:
                departments = ", ".join(latest.get("departments", [])) or "не указаны"
                created_at = latest["created_at"].strftime("%d.%m.%Y %H:%M") if latest["created_at"] else "—"
                lines = [
                    "✅ Ваша заявка уже обработана.",
                    "",
                    f"📋 Заявка №{latest['id']}",
                    f"Отделы: {departments}",
                    f"Дата: {created_at}",
                ]
                await query.message.answer("\n".join(lines))
            else:
                await query.message.answer("✅ Ваша заявка уже обработана.")
            return

        lines = [
            f"📝 Заявка №{request_id} успешно сохранена.",
        ]
        filtered_missing = [name for name in missing if not name.startswith("__")]
        if filtered_missing:
            lines.append("⚠️ Не найдены отделы: " + ", ".join(filtered_missing))
        lines.append("")
        lines.append("Проверить статус можно командой /status")
        await query.message.answer("\n".join(lines))
    else:
        await state.update_data(departments=[])
        await state.set_state(InviteRequestForm.waiting_departments.state)
        await send_departments_prompt(query.message, state)


async def handle_back(query: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass

    if current_state == InviteRequestForm.waiting_last_name.state:
        await state.set_state(InviteRequestForm.waiting_first_name.state)
        await query.message.answer("Введите имя.")
    elif current_state == InviteRequestForm.waiting_middle_name.state:
        await state.set_state(InviteRequestForm.waiting_last_name.state)
        await query.message.answer("Введите фамилию.", reply_markup=back_keyboard())
    elif current_state == InviteRequestForm.waiting_departments.state:
        await state.set_state(InviteRequestForm.waiting_middle_name.state)
        await query.message.answer("Введите отчество (или напишите 'нет').", reply_markup=back_keyboard())
    elif current_state == InviteRequestForm.waiting_confirmation.state:
        await state.set_state(InviteRequestForm.waiting_departments.state)
        await send_departments_prompt(query.message, state)
    else:
        await state.set_state(InviteRequestForm.waiting_first_name.state)
        await query.message.answer("Введите имя.")


async def cmd_status(message: Message) -> None:
    request = await get_latest_request(str(message.from_user.id))
    if not request:
        await message.answer("📭 Заявок не найдено.")
        return
    departments = ", ".join(request.get("departments", [])) or "не указаны"
    created_at = request["created_at"].strftime("%d.%m.%Y %H:%M") if request["created_at"] else "—"
    raw_status = request["status"]
    status = STATUS_DISPLAY.get(raw_status, raw_status)
    processed = request.get("processed_departments", [])
    pending = [d for d in request.get("departments", []) if d not in processed]

    lines = [
        f"📋 Статус вашей заявки №{request['id']}",
        f"Статус: {status}",
        f"Отделы: {departments}",
    ]
    if raw_status == "pending":
        if processed:
            lines.append("✅ Уже подтвердили: " + ", ".join(processed))
        if pending:
            lines.append("⏳ Ожидаем: " + ", ".join(pending))
    elif raw_status == "processed":
        lines.append("🎉 Все отделы подтвердили заявку!")
    lines.append("")
    lines.append(f"🕒 Создана: {created_at}")

    await message.answer("\n".join(lines))


def main() -> None:
    token = API_TOKEN
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=token)
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)

    dp.register_message_handler(cmd_start, commands=["start"])
    dp.register_message_handler(cmd_help, commands=["help"])
    dp.register_message_handler(cmd_post_invate, commands=["post_invate"], state="*")
    dp.register_message_handler(process_first_name, state=InviteRequestForm.waiting_first_name)
    dp.register_message_handler(process_last_name, state=InviteRequestForm.waiting_last_name)
    dp.register_message_handler(process_middle_name, state=InviteRequestForm.waiting_middle_name)
    dp.register_message_handler(process_departments, state=InviteRequestForm.waiting_departments)
    dp.register_message_handler(cmd_status, commands=["status"], state="*")
    dp.register_callback_query_handler(handle_confirmation, lambda c: c.data in {"confirm_yes", "confirm_no"}, state="*")
    dp.register_callback_query_handler(handle_back, text="back", state="*")

    executor.start_polling(dp, skip_updates=True)


if __name__ == "__main__":
    main()
