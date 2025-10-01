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
    waiting_additional_decision = State()
    waiting_region = State()
    waiting_departments = State()
    waiting_confirmation = State()


REGION_OPTIONS = [
    "ВСЕ Регионы",
    "Уфа",
    "Стерлитамак",
    "Нефтекамск",
    "Екатеренбург",
]


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


def additional_decision_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("✅ Да", callback_data="additional_yes"),
        InlineKeyboardButton("❌ Нет", callback_data="additional_no"),
    )
    return keyboard


def _fetch_departments_sync() -> List[Dict[str, object]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT g.id, g.name
                FROM auth_group g
                LEFT JOIN s3app_groupsettings gs ON g.id = gs.group_id
                WHERE COALESCE(gs.show_in_bot, 1) = 1
                ORDER BY COALESCE(gs.bot_order, 0), g.name
                """
            )
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


def send_region_prompt_text() -> str:
    lines = [f"{idx}. {name}" for idx, name in enumerate(REGION_OPTIONS, start=1)]
    numbered_list = "\n".join(lines)
    return (
        "Выберите регион, отправив номер или название из списка:\n\n"
        f"{numbered_list}"
    )


async def send_region_prompt(target, state: FSMContext) -> None:
    await target.answer(send_region_prompt_text(), reply_markup=back_keyboard())


def _get_user_by_telegram_sync(telegram_id: str) -> Optional[Dict[str, object]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, first_name, last_name, middle_name, region, email
                FROM s3app_user
                WHERE telegram_id = %s
                LIMIT 1
                """,
                (telegram_id,),
            )
            user = cursor.fetchone()

            if user:
                # Получаем отделы (группы) пользователя
                cursor.execute(
                    """
                    SELECT g.name
                    FROM auth_group g
                    JOIN s3app_user_groups ug ON g.id = ug.group_id
                    WHERE ug.user_id = %s
                    ORDER BY g.name
                    """,
                    (user['id'],),
                )
                user['departments'] = [row['name'] for row in cursor.fetchall()]

            return user


async def get_user_by_telegram(telegram_id: str) -> Optional[Dict[str, object]]:
    return await asyncio.to_thread(_get_user_by_telegram_sync, telegram_id)


def _create_request_sync(full_name: str, telegram_id: str, region: str, departments: List[str], *,
                         is_additional: bool = False, target_user_id: Optional[int] = None,
                         allow_processed: bool = False) -> Tuple[int, List[str]]:
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
            if existing and existing["status"] == "processed" and not allow_processed:
                return existing["id"], ["__processed__"]

            cursor.execute(
                """
                INSERT INTO s3app_userrequest (full_name, telegram_id, region, is_additional, target_user_id, status, created_at, processed_at, processed_by_id)
                VALUES (%s, %s, %s, %s, %s, 'new', NOW(), NULL, NULL)
                """,
                (full_name, telegram_id, region, is_additional, target_user_id),
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
                SELECT id, status, region, is_additional, created_at
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
            request["is_additional"] = bool(request.get("is_additional"))
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


async def create_request(full_name: str, telegram_id: str, region: str, departments: List[str], *,
                         is_additional: bool = False, target_user_id: Optional[int] = None) -> Tuple[int, List[str]]:
    return await asyncio.to_thread(
        _create_request_sync,
        full_name,
        telegram_id,
        region,
        departments,
        is_additional=is_additional,
        target_user_id=target_user_id,
        allow_processed=is_additional,
    )


async def get_latest_request(telegram_id: str) -> Optional[Dict[str, object]]:
    return await asyncio.to_thread(_get_latest_request_sync, telegram_id)


STATUS_DISPLAY = {
    "new": "🆕 Новая",
    "pending": "⏳ Незавершена",
    "processed": "✅ Обработана",
    "rejected": "❌ Отклонена",
}


async def cmd_start(message: Message, state: FSMContext) -> None:
    logging.info(f"Команда /start от пользователя {message.from_user.id}")
    await state.finish()
    await message.answer(
        "Привет! Я бот для оформления заявок. Используйте /post_invate, чтобы отправить новую заявку, или /help для справки."
    )


async def cmd_help(message: Message) -> None:
    logging.info(f"Команда /help от пользователя {message.from_user.id}")
    await message.answer(
        "📋 Доступные команды:\n"
        "/start - Начать работу с ботом\n"
        "/post_invate - Подать новую заявку\n"
        "/status - Проверить статус заявки\n"
        "/help - Показать эту справку"
    )


async def cmd_post_invate(message: Message, state: FSMContext) -> None:
    logging.info(f"Команда /post_invate от пользователя {message.from_user.id}")

    logging.info("Получаем последнюю заявку из БД...")
    try:
        existing = await get_latest_request(str(message.from_user.id))
        logging.info(f"Заявка получена: {existing}")
    except Exception as e:
        logging.error(f"Ошибка при получении заявки: {e}")
        await message.answer("Произошла ошибка при проверке ваших заявок. Попробуйте позже.")
        return

    if existing:
        logging.info(f"Найдена существующая заявка со статусом: {existing.get('status')}")
        status = existing.get("status")
        if status in {"new", "pending"}:
            departments = ", ".join(existing.get("departments", [])) or "не указаны"
            created_at = existing["created_at"].strftime("%d.%m.%Y %H:%M") if existing.get("created_at") else "—"
            region = existing.get("region") or "—"
            lines = [
                "⚠️ У вас уже есть активная заявка.",
                "",
                f"📋 Заявка №{existing['id']}",
                f"🌍 Регион: {region}",
                f"Отделы: {departments}",
                f"Создана: {created_at}",
                "",
                "Проверьте статус командой /status",
            ]
            await message.answer("\n".join(lines))
            return
        if status == "processed":
            logging.info("Статус processed, проверяем пользователя в системе...")
            try:
                existing_user = await get_user_by_telegram(str(message.from_user.id))
                logging.info(f"Пользователь найден: {existing_user}")
            except Exception as e:
                logging.error(f"Ошибка при получении пользователя: {e}")
                await message.answer("Произошла ошибка. Попробуйте позже.")
                return

            if existing_user:
                logging.info("Предлагаем дополнительную заявку...")
                try:
                    logging.info("Сбрасываем состояние...")
                    await state.reset_state(with_data=False)
                    logging.info("Обновляем данные состояния...")
                    await state.update_data(existing_user=existing_user)
                    logging.info("Устанавливаем состояние waiting_additional_decision...")
                    await state.set_state(InviteRequestForm.waiting_additional_decision.state)
                    logging.info("Формируем текст сообщения...")
                    summary_region = existing.get("region") or existing_user.get("region") or "—"
                    # Показываем текущие отделы пользователя из БД, а не из заявки
                    summary_departments = ", ".join(existing_user.get("departments", [])) or "не указаны"
                    summary_text = [
                        "✅ Ваша предыдущая заявка уже обработана.",
                        f"🌍 Регион: {summary_region}",
                        f"📁 Текущие отделы: {summary_departments}",
                        "",
                        "Подать заявку на доступ к дополнительным отделам?",
                    ]
                    logging.info(f"Отправляем сообщение: {summary_text}")
                    await message.answer("\n".join(summary_text), reply_markup=additional_decision_keyboard())
                    logging.info("Сообщение отправлено!")
                    return
                except Exception as e:
                    logging.error(f"Ошибка при отправке сообщения о дополнительной заявке: {e}")
                    import traceback
                    traceback.print_exc()
                    await message.answer("Произошла ошибка. Попробуйте позже.")
                    return

    logging.info("Заявок не найдено, проверяем пользователя...")
    existing_user = await get_user_by_telegram(str(message.from_user.id))
    logging.info(f"Пользователь: {existing_user}")
    if existing_user:
        await state.reset_state(with_data=False)
        await state.update_data(existing_user=existing_user)
        await state.set_state(InviteRequestForm.waiting_additional_decision.state)
        summary_region = existing_user.get("region") or "—"
        summary_departments = ", ".join(existing_user.get("departments", [])) or "не указаны"
        summary_text = [
            "✅ У вас уже есть аккаунт.",
            f"🌍 Регион: {summary_region}",
            f"📁 Текущие отделы: {summary_departments}",
            "",
            "Подать заявку на доступ к дополнительным отделам?",
        ]
        await message.answer("\n".join(summary_text), reply_markup=additional_decision_keyboard())
        return

    logging.info("Начинаем новую заявку, запрашиваем имя...")
    await state.reset_state(with_data=False)
    await state.set_state(InviteRequestForm.waiting_first_name.state)
    await message.answer("Введите имя.", reply_markup=back_keyboard())
    logging.info("Запрос имени отправлен!")


async def handle_additional_decision(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    try:
        await query.message.edit_reply_markup()
    except Exception:
        pass

    data = await state.get_data()
    if query.data == "additional_yes":
        existing_user = data.get("existing_user")
        if existing_user:
            await state.update_data(
                first_name=existing_user.get("first_name", ""),
                last_name=existing_user.get("last_name", ""),
                middle_name=existing_user.get("middle_name", ""),
                region=existing_user.get("region", ""),
                is_additional=True,
                target_user_id=existing_user.get("id"),
            )
        await state.set_state(InviteRequestForm.waiting_departments.state)
        await send_departments_prompt(query.message, state)
    else:
        await state.finish()
        await query.message.answer("Операция отменена.")


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
        region = data.get("region", "")
        departments = data.get("departments", [])
        is_additional = data.get("is_additional", False)
        target_user_id = data.get("target_user_id")

        full_name_parts = [part for part in [last_name, first_name, middle_name] if part]
        full_name = " ".join(full_name_parts)
        if not full_name:
            full_name = first_name or last_name or "Не указано"
        region_to_save = region if region and region != '—' else ''

        request_id, missing = await create_request(
            full_name=full_name,
            telegram_id=str(query.from_user.id),
            region=region_to_save,
            departments=departments,
            is_additional=is_additional,
            target_user_id=target_user_id,
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
                departments_summary = ", ".join(latest.get("departments", [])) or "не указаны"
                created_at = latest["created_at"].strftime("%d.%m.%Y %H:%M") if latest["created_at"] else "—"
                region_summary = latest.get("region") or "—"
                lines = [
                    "✅ Ваша заявка уже обработана.",
                    "",
                    f"📋 Заявка №{latest['id']}",
                    f"🌍 Регион: {region_summary}",
                    f"Отделы: {departments_summary}",
                    f"Дата: {created_at}",
                ]
                await query.message.answer("\n".join(lines))
            else:
                await query.message.answer("✅ Ваша заявка уже обработана.")
            return

        lines = [f"📝 Заявка №{request_id} успешно сохранена."]
        if is_additional:
            lines.append("➕ Дополнительная заявка на новые отделы.")
        if region:
            lines.append(f"🌍 Регион: {region}")
        if departments:
            lines.append("📁 Отделы: " + ", ".join(departments))
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


async def process_first_name(message: Message, state: FSMContext) -> None:
    first_name = message.text.strip()
    if len(first_name) < 2:
        await message.answer("Пожалуйста, укажите имя полностью.")
        return
    await state.update_data(first_name=first_name, is_additional=False, target_user_id=None)
    await state.set_state(InviteRequestForm.waiting_last_name.state)
    await message.answer("Введите фамилию.", reply_markup=back_keyboard())


async def process_last_name(message: Message, state: FSMContext) -> None:
    last_name = message.text.strip()
    if len(last_name) < 2:
        await message.answer("Пожалуйста, укажите фамилию полностью.", reply_markup=back_keyboard())
        return
    await state.update_data(last_name=last_name)
    await state.set_state(InviteRequestForm.waiting_middle_name.state)
    await message.answer("Введите отчество.", reply_markup=back_keyboard())


async def process_middle_name(message: Message, state: FSMContext) -> None:
    middle_name = message.text.strip()
    if len(middle_name) < 2:
        await message.answer("Пожалуйста, укажите отчество полностью.", reply_markup=back_keyboard())
        return
    await state.update_data(middle_name=middle_name)
    await state.set_state(InviteRequestForm.waiting_region.state)
    await send_region_prompt(message, state)


async def process_region(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    region = None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(REGION_OPTIONS):
            region = REGION_OPTIONS[index - 1]
    else:
        for option in REGION_OPTIONS:
            if option.lower() == text.lower():
                region = option
                break
    if not region:
        await message.answer("Введите номер или название региона из списка.", reply_markup=back_keyboard())
        return
    await state.update_data(region=region, departments=[])
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


async def handle_back(query: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass

    if current_state == InviteRequestForm.waiting_first_name.state:
        # Отменяем заполнение заявки
        await state.finish()
        await query.message.answer("❌ Заполнение заявки отменено.")
    elif current_state == InviteRequestForm.waiting_last_name.state:
        await state.set_state(InviteRequestForm.waiting_first_name.state)
        await query.message.answer("Введите имя.", reply_markup=back_keyboard())
    elif current_state == InviteRequestForm.waiting_middle_name.state:
        await state.set_state(InviteRequestForm.waiting_last_name.state)
        await query.message.answer("Введите фамилию.", reply_markup=back_keyboard())
    elif current_state == InviteRequestForm.waiting_region.state:
        await state.set_state(InviteRequestForm.waiting_middle_name.state)
        await query.message.answer("Введите отчество.", reply_markup=back_keyboard())
    elif current_state == InviteRequestForm.waiting_departments.state:
        data = await state.get_data()
        if data.get("is_additional"):
            await state.update_data(departments=[])
            await state.set_state(InviteRequestForm.waiting_additional_decision.state)
            await query.message.answer(
                "Хотите подать заявку на доступ к дополнительным отделам?",
                reply_markup=additional_decision_keyboard()
            )
        else:
            await state.set_state(InviteRequestForm.waiting_region.state)
            await state.update_data(departments=[])
            await send_region_prompt(query.message, state)
    elif current_state == InviteRequestForm.waiting_confirmation.state:
        await state.set_state(InviteRequestForm.waiting_departments.state)
        await send_departments_prompt(query.message, state)
    else:
        await state.finish()
        await query.message.answer("❌ Заполнение заявки отменено.")


async def cmd_status(message: Message, state: FSMContext) -> None:
    logging.info(f"Команда /status от пользователя {message.from_user.id}")

    logging.info("Получаем заявку из БД...")
    try:
        request = await get_latest_request(str(message.from_user.id))
        logging.info(f"Заявка получена: {request}")
    except Exception as e:
        logging.error(f"Ошибка при получении статуса заявки: {e}")
        await message.answer("Произошла ошибка при проверке статуса. Попробуйте позже.")
        return

    if not request:
        logging.info("Заявок не найдено")
        await message.answer("📭 Заявок не найдено.")
        return

    logging.info("Формируем ответ со статусом...")
    departments = ", ".join(request.get("departments", [])) or "не указаны"
    created_at = request["created_at"].strftime("%d.%m.%Y %H:%M") if request["created_at"] else "—"
    raw_status = request["status"]
    status = STATUS_DISPLAY.get(raw_status, raw_status)
    processed = request.get("processed_departments", [])
    pending = [d for d in request.get("departments", []) if d not in processed]
    region = request.get("region") or "—"
    is_additional = request.get("is_additional", False)

    lines = [
        f"📋 Статус вашей заявки №{request['id']}",
        f"Статус: {status}",
        f"🌍 Регион: {region}",
        f"Отделы: {departments}",
    ]
    if is_additional:
        lines.append("➕ Тип: Дополнительная заявка")
    if raw_status == "pending":
        if processed:
            lines.append("✅ Уже подтвердили: " + ", ".join(processed))
        if pending:
            lines.append("⏳ Ожидаем: " + ", ".join(pending))
    elif raw_status == "processed":
        lines.append("🎉 Все отделы подтвердили заявку!")
    lines.append("")
    lines.append(f"🕒 Создана: {created_at}")

    logging.info(f"Отправляем статус заявки...")
    await message.answer("\n".join(lines))
    logging.info("Статус отправлен!")

    # Сбрасываем состояние FSM после показа статуса
    await state.finish()
    logging.info("Состояние FSM сброшено")


def main() -> None:
    token = API_TOKEN
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    bot = Bot(token=token)
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)

    logging.info("Регистрация обработчиков команд...")

    # Команды с state="*" должны быть первыми
    dp.register_message_handler(cmd_start, commands=["start"], state="*")
    logging.info("✓ Зарегистрирована команда /start")

    dp.register_message_handler(cmd_help, commands=["help"], state="*")
    logging.info("✓ Зарегистрирована команда /help")

    dp.register_message_handler(cmd_post_invate, commands=["post_invate"], state="*")
    logging.info("✓ Зарегистрирована команда /post_invate")

    dp.register_message_handler(cmd_status, commands=["status"], state="*")
    logging.info("✓ Зарегистрирована команда /status")

    # Обработчики callback запросов
    dp.register_callback_query_handler(handle_additional_decision,
                                       lambda c: c.data in {"additional_yes", "additional_no"}, state="*")
    dp.register_callback_query_handler(handle_confirmation, lambda c: c.data in {"confirm_yes", "confirm_no"},
                                       state="*")
    dp.register_callback_query_handler(handle_back, text="back", state="*")

    # Обработчики состояний (должны быть после команд)
    dp.register_message_handler(process_first_name, state=InviteRequestForm.waiting_first_name)
    dp.register_message_handler(process_last_name, state=InviteRequestForm.waiting_last_name)
    dp.register_message_handler(process_middle_name, state=InviteRequestForm.waiting_middle_name)
    dp.register_message_handler(process_region, state=InviteRequestForm.waiting_region)
    dp.register_message_handler(process_departments, state=InviteRequestForm.waiting_departments)

    logging.info("=" * 60)
    logging.info("🚀 БОТ ЗАПУЩЕН И ГОТОВ К РАБОТЕ")
    logging.info("=" * 60)
    logging.info(f"Доступные команды: /start, /help, /post_invate, /status")
    logging.info("=" * 60)

    executor.start_polling(dp, skip_updates=True)


if __name__ == "__main__":
    main()
