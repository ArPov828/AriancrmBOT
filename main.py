import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta

import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from supabase import create_client, Client

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://lwsozgawasazveykkoqp.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_API_SECRET = os.getenv("BOT_API_SECRET", "")
CRM_API_BASE = "https://ariancrm.ru/api/public/bot"

if not BOT_TOKEN or not SUPABASE_KEY:
    raise ValueError("❌ Ошибка: не хватает переменных окружения! Убедитесь, что BOT_TOKEN и SUPABASE_KEY установлены в Secrets.")

# --- ИНИЦИАЛИЗАЦИЯ ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

REQUESTS_PAGE_SIZE = 5
CLIENTS_PAGE_SIZE = 5
CHECK_INTERVAL_MINUTES = 2

# Кэш последних известных статусов: {request_id: status}
status_cache: dict = {}

scheduler = AsyncIOScheduler()

# --- FSM: СОЗДАНИЕ ЗАЯВКИ ---
class NewRequest(StatesGroup):
    title = State()
    client_name = State()
    description = State()

# --- FSM: ПОИСК ---
class SearchQuery(StatesGroup):
    query = State()

# --- FSM: НАПОМИНАНИЕ ---
class SetReminder(StatesGroup):
    when = State()
    text = State()

# --- FSM: ЗАМЕТКА ---
class AddNote(StatesGroup):
    text = State()

# --- ХЕЛПЕРЫ: клавиатуры для FSM ---
def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def skip_or_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭ Пропустить")],
            [KeyboardButton(text="❌ Отменить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# --- ХЕЛПЕР: определить колонку telegram в profiles ---
TG_COL_CANDIDATES = ['telegram_chat_id', 'telegram_id', 'tg_chat_id', 'tg_id', 'tg_user_id']

def _detect_tg_col(row: dict) -> str | None:
    return next((c for c in TG_COL_CANDIDATES if c in row), None)

# --- ХЕЛПЕР: вызов Lovable CRM API ---
async def call_crm_api(action: str, payload: dict | None = None) -> dict | None:
    if not BOT_API_SECRET:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{CRM_API_BASE}/{action}",
                json=payload or {},
                headers={"X-Bot-Secret": BOT_API_SECRET}
            )
            if resp.status_code == 200:
                return resp.json()
            logging.warning(f"[api] {action} → HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        logging.warning(f"[api] {action} недоступен: {e}")
    return None

# --- ХЕЛПЕР: получить профиль по telegram chat_id ---
async def get_profile(chat_id: str):
    # Сначала пробуем Lovable API (service_role, обходит RLS)
    result = await call_crm_api("lookup-by-chat", {"chat_id": int(chat_id)})
    if result:
        # API может вернуть profile напрямую или обёрнутый
        profile = result.get("profile") or result.get("user") or (result if result.get("id") else None)
        if profile:
            return profile

    # Fallback: прямой запрос к Supabase
    for col in TG_COL_CANDIDATES:
        try:
            resp = supabase.table('profiles').select('*').eq(col, chat_id).execute()
            if resp.data:
                return resp.data[0]
        except Exception:
            continue
    return None

# --- ХЕЛПЕР: статус заявки → эмодзи ---
STATUS_EMOJI = {
    'new': '🆕',
    'in_progress': '🔄',
    'done': '✅',
    'closed': '🔒',
    'cancelled': '❌',
}

def status_label(status: str) -> str:
    emoji = STATUS_EMOJI.get(status, '📌')
    return f"{emoji} {status}"

# --- ХЕЛПЕР: форматировать одну заявку ---
def format_request(req: dict, index: int) -> str:
    title = req.get('title') or req.get('name') or f"Заявка #{req.get('id', '?')}"
    status = status_label(req.get('status', 'new'))
    client = req.get('client_name') or req.get('client') or '—'
    created = req.get('created_at', '')
    if created:
        try:
            created = datetime.fromisoformat(created.replace('Z', '+00:00')).strftime('%d.%m.%Y')
        except Exception:
            created = created[:10]
    lines = [
        f"*{index}. {title}*",
        f"Статус: {status}",
        f"Клиент: {client}",
    ]
    if created:
        lines.append(f"Дата: {created}")
    return '\n'.join(lines)

# --- ХЕЛПЕР: клавиатура пагинации заявок ---
def pagination_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = (total + REQUESTS_PAGE_SIZE - 1) // REQUESTS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"req_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"req_page:{page + 1}"))
    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"req_page:{page}")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ХЕЛПЕР: форматировать одного клиента ---
def format_client(client: dict, index: int) -> str:
    name = client.get('name') or client.get('full_name') or f"Клиент #{client.get('id', '?')}"
    phone = client.get('phone') or client.get('phone_number') or '—'
    email = client.get('email') or '—'
    company = client.get('company') or client.get('company_name') or ''
    created = client.get('created_at', '')
    if created:
        try:
            created = datetime.fromisoformat(created.replace('Z', '+00:00')).strftime('%d.%m.%Y')
        except Exception:
            created = created[:10]
    lines = [f"*{index}. {name}*"]
    if company:
        lines.append(f"Компания: {company}")
    lines.append(f"Телефон: {phone}")
    lines.append(f"Email: {email}")
    if created:
        lines.append(f"Добавлен: {created}")
    return '\n'.join(lines)

# --- ХЕЛПЕР: клавиатура пагинации клиентов ---
def clients_pagination_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = (total + CLIENTS_PAGE_SIZE - 1) // CLIENTS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"cli_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"cli_page:{page + 1}"))
    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"cli_page:{page}")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ХЕЛПЕР: показать страницу клиентов ---
async def show_clients_page(target, chat_id: str, page: int, edit: bool = False):
    profile = await get_profile(chat_id)
    if not profile:
        text = "❌ Вы не авторизованы. Используйте `/connect ваш@email.com` для привязки аккаунта."
        if edit:
            await target.edit_text(text, parse_mode="Markdown")
        else:
            await target.answer(text, parse_mode="Markdown")
        return

    user_id = profile['id']
    clients = []
    total = 0

    try:
        # Пробуем через Lovable API
        api_result = await call_crm_api("clients.list", {"user_id": user_id})
        if api_result is not None:
            all_clients = api_result.get("clients") or api_result.get("data") or (api_result if isinstance(api_result, list) else [])
            total = len(all_clients)
            offset = page * CLIENTS_PAGE_SIZE
            clients = all_clients[offset: offset + CLIENTS_PAGE_SIZE]
        else:
            # Fallback: прямой Supabase
            count_resp = supabase.table('clients').select('id', count='exact').eq('assigned_to', user_id).execute()
            total = count_resp.count if count_resp.count is not None else len(count_resp.data)
            offset = page * CLIENTS_PAGE_SIZE
            data_resp = supabase.table('clients') \
                .select('id, name, full_name, phone, phone_number, email, company, company_name, created_at') \
                .eq('assigned_to', user_id).order('created_at', desc=True) \
                .range(offset, offset + CLIENTS_PAGE_SIZE - 1).execute()
            clients = data_resp.data or []

        if total == 0:
            text = "📭 У вас пока нет клиентов."
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")]])
            if edit: await target.edit_text(text, reply_markup=keyboard)
            else:    await target.answer(text, reply_markup=keyboard)
            return

        offset = page * CLIENTS_PAGE_SIZE
        total_pages = (total + CLIENTS_PAGE_SIZE - 1) // CLIENTS_PAGE_SIZE
        header = f"👥 *Мои клиенты* (стр. {page + 1}/{total_pages}, всего: {total})\n\n"
        items = [format_client(c, offset + i + 1) for i, c in enumerate(clients)]
        text = header + '\n\n—\n\n'.join(items)
        keyboard = clients_pagination_keyboard(page, total)

        if edit: await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:    await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Ошибка при получении клиентов: {e}")
        text = f"❌ Не удалось загрузить клиентов.\n\nОшибка: `{e}`"
        if edit: await target.edit_text(text, parse_mode="Markdown")
        else:    await target.answer(text, parse_mode="Markdown")

# --- ХЕЛПЕР: показать страницу заявок ---
async def show_requests_page(target, chat_id: str, page: int, edit: bool = False):
    profile = await get_profile(chat_id)
    if not profile:
        text = "❌ Вы не авторизованы. Используйте `/connect ваш@email.com` для привязки аккаунта."
        if edit:
            await target.edit_text(text, parse_mode="Markdown")
        else:
            await target.answer(text, parse_mode="Markdown")
        return

    user_id = profile['id']
    requests_list = []
    total = 0

    try:
        # Пробуем через Lovable API
        api_result = await call_crm_api("requests.list", {"user_id": user_id})
        if api_result is not None:
            all_reqs = api_result.get("requests") or api_result.get("data") or (api_result if isinstance(api_result, list) else [])
            total = len(all_reqs)
            offset = page * REQUESTS_PAGE_SIZE
            requests_list = all_reqs[offset: offset + REQUESTS_PAGE_SIZE]
        else:
            # Fallback: прямой Supabase
            count_resp = supabase.table('requests').select('id', count='exact').eq('assigned_to', user_id).execute()
            total = count_resp.count if count_resp.count is not None else len(count_resp.data)
            offset = page * REQUESTS_PAGE_SIZE
            data_resp = supabase.table('requests') \
                .select('id, title, name, status, client_name, client, created_at') \
                .eq('assigned_to', user_id).order('created_at', desc=True) \
                .range(offset, offset + REQUESTS_PAGE_SIZE - 1).execute()
            requests_list = data_resp.data or []

        if total == 0:
            text = "📭 У вас пока нет заявок."
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать заявку", callback_data="new_request")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")],
            ])
            if edit: await target.edit_text(text, reply_markup=keyboard)
            else:    await target.answer(text, reply_markup=keyboard)
            return

        offset = page * REQUESTS_PAGE_SIZE
        total_pages = (total + REQUESTS_PAGE_SIZE - 1) // REQUESTS_PAGE_SIZE
        header = f"📋 *Мои заявки* (стр. {page + 1}/{total_pages}, всего: {total})\n\n"
        items = [format_request(req, offset + i + 1) for i, req in enumerate(requests_list)]
        text = header + '\n\n—\n\n'.join(items)
        keyboard = pagination_keyboard(page, total)

        if edit: await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:    await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Ошибка при получении заявок: {e}")
        text = f"❌ Не удалось загрузить заявки.\n\nОшибка: `{e}`"
        if edit: await target.edit_text(text, parse_mode="Markdown")
        else:    await target.answer(text, parse_mode="Markdown")

SEARCH_LIMIT = 5

async def perform_search(target, chat_id: str, query: str):
    profile = await get_profile(chat_id)
    if not profile:
        await target.answer(
            "❌ Вы не авторизованы. Используйте `/connect ваш@email.com`.",
            parse_mode="Markdown"
        )
        return

    user_id = profile['id']
    q = f"%{query}%"
    results: list[str] = []
    errors: list[str] = []

    try:
        req_resp = supabase.table('requests').select(
            'id, title, name, status, client_name, client, created_at'
        ).eq('assigned_to', user_id).or_(
            f'title.ilike.{q},name.ilike.{q},client_name.ilike.{q}'
        ).limit(SEARCH_LIMIT).execute()
        reqs = req_resp.data or []
        if reqs:
            lines = [f"📋 *Заявки ({len(reqs)}):*"]
            for i, r in enumerate(reqs, 1):
                title = r.get('title') or r.get('name') or f"#{r.get('id','?')}"
                st = status_label(r.get('status', 'new'))
                client = r.get('client_name') or r.get('client') or '—'
                lines.append(f"  {i}. *{title}*\n      {st} | {client}")
            results.append('\n'.join(lines))
    except Exception as e:
        logging.error(f"[search] requests: {e}")
        errors.append("заявки")

    try:
        cli_resp = supabase.table('clients').select(
            'id, name, full_name, phone, phone_number, email, company, company_name'
        ).eq('assigned_to', user_id).or_(
            f'name.ilike.{q},full_name.ilike.{q},email.ilike.{q},company.ilike.{q},company_name.ilike.{q}'
        ).limit(SEARCH_LIMIT).execute()
        clis = cli_resp.data or []
        if clis:
            lines = [f"👥 *Клиенты ({len(clis)}):*"]
            for i, c in enumerate(clis, 1):
                name = c.get('name') or c.get('full_name') or f"#{c.get('id','?')}"
                phone = c.get('phone') or c.get('phone_number') or '—'
                email = c.get('email') or '—'
                company = c.get('company') or c.get('company_name') or ''
                line = f"  {i}. *{name}*\n      📞 {phone}  ✉️ {email}"
                if company:
                    line += f"  🏢 {company}"
                lines.append(line)
            results.append('\n'.join(lines))
    except Exception as e:
        logging.error(f"[search] clients: {e}")
        errors.append("клиенты")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_again"),
            InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu"),
        ]
    ])

    safe_q = query.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')
    if not results:
        if errors:
            text = f"❌ Не удалось выполнить поиск ({', '.join(errors)})."
        else:
            text = f"🔍 По запросу «{safe_q}» ничего не найдено.\n\nПроверьте написание или попробуйте другой запрос."
    else:
        parts = [f"🔍 *Результаты по запросу «{safe_q}»:*\n"]
        parts.extend(results)
        if errors:
            parts.append(f"\n⚠️ Не удалось найти по: {', '.join(errors)}")
        text = '\n\n'.join(parts)

    await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)

# =====================================================================
# НАПОМИНАНИЯ
# =====================================================================

# {job_id: {chat_id, text, dt_str}}
reminder_store: dict = {}

def parse_reminder_time(raw: str) -> datetime | None:
    t = raw.strip().lower()
    now = datetime.utcnow()

    # через N минут / часов / дней
    m = re.match(r'через\s+(\d+)\s+(минут\w*|час\w*|дн\w*|ден\w*)', t)
    if m:
        n = int(m.group(1))
        u = m.group(2)
        if u.startswith('мин'):   return now + timedelta(minutes=n)
        if u.startswith('час'):   return now + timedelta(hours=n)
        if u.startswith('дн') or u.startswith('ден'): return now + timedelta(days=n)

    # завтра в ЧЧ:ММ
    m = re.match(r'завтра\s+в\s+(\d{1,2})[:\.](\d{2})', t)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        d = (now + timedelta(days=1)).date()
        return datetime(d.year, d.month, d.day, h, mn)

    # сегодня в ЧЧ:ММ  /  в ЧЧ:ММ
    m = re.match(r'(?:сегодня\s+)?в\s+(\d{1,2})[:\.](\d{2})', t)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        d = now.date()
        dt = datetime(d.year, d.month, d.day, h, mn)
        return dt if dt > now else None

    # ДД.ММ.ГГГГ ЧЧ:ММ  /  ДД.ММ.ГГГГ
    for fmt in ('%d.%m.%Y %H:%M', '%d.%m.%y %H:%M', '%d/%m/%Y %H:%M',
                '%d.%m.%Y', '%d.%m.%y'):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            pass

    return None

async def fire_reminder(chat_id: str, text: str, job_id: str):
    try:
        await bot.send_message(
            chat_id,
            f"⏰ *Напоминание!*\n\n{text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"[reminder] Ошибка отправки {job_id}: {e}")
    finally:
        reminder_store.pop(job_id, None)

# =====================================================================
# ЗАМЕТКИ
# =====================================================================

NOTES_PAGE_SIZE = 5
NOTES_TABLE_CANDIDATES = ['notes', 'telegram_notes', 'user_notes']
NOTES_TEXT_CANDIDATES  = ['text', 'content', 'body', 'note']
NOTES_USER_CANDIDATES  = ['user_id', 'profile_id', 'author_id']
NOTES_ENTITY_CANDIDATES = ['entity_type', 'type']
NOTES_ENTITY_ID_CANDIDATES = ['entity_id', 'related_id', 'reference_id']

_notes_table: str | None = None
_notes_text_col: str | None = None
_notes_user_col: str | None = None
_notes_entity_col: str | None = None
_notes_entity_id_col: str | None = None

CREATE_NOTES_SQL = (
    "CREATE TABLE notes (\n"
    "  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,\n"
    "  user_id uuid,\n"
    "  text text NOT NULL,\n"
    "  entity_type text,\n"
    "  entity_id text,\n"
    "  created_at timestamptz DEFAULT now()\n"
    ");"
)

async def detect_notes_schema():
    global _notes_table, _notes_text_col, _notes_user_col, _notes_entity_col, _notes_entity_id_col
    for table in NOTES_TABLE_CANDIDATES:
        try:
            resp = supabase.table(table).select('*').limit(1).execute()
            _notes_table = table
            if resp.data:
                cols = set(resp.data[0].keys())
                _notes_text_col     = next((c for c in NOTES_TEXT_CANDIDATES      if c in cols), None)
                _notes_user_col     = next((c for c in NOTES_USER_CANDIDATES      if c in cols), None)
                _notes_entity_col   = next((c for c in NOTES_ENTITY_CANDIDATES    if c in cols), None)
                _notes_entity_id_col= next((c for c in NOTES_ENTITY_ID_CANDIDATES if c in cols), None)
                logging.info(f"[notes] Таблица '{table}' | текст='{_notes_text_col}' | пользователь='{_notes_user_col}' | все: {sorted(cols)}")
            else:
                logging.warning(f"[notes] Таблица '{table}' пуста — определение колонок отложено")
            return
        except Exception as e:
            err = str(e)
            if any(k in err for k in ['does not exist', 'relation', '42703', '42P01', 'PGRST205', 'Could not find']):
                continue
            logging.error(f"[notes] Ошибка '{table}': {e}")
    logging.info("[notes] Таблица notes не найдена — функция будет недоступна до её создания")

async def save_note(user_id: str, text: str, entity_type: str | None = None, entity_id: str | None = None) -> bool:
    # Сначала через Lovable API
    payload: dict = {"user_id": user_id, "text": text}
    if entity_type:
        payload["entity_type"] = entity_type
    if entity_id:
        payload["entity_id"] = entity_id
    result = await call_crm_api("notes.create", payload)
    if result is not None and not result.get("error"):
        return True

    # Fallback: прямой Supabase
    if not _notes_table:
        await detect_notes_schema()
    if not _notes_table:
        return False
    record: dict = {
        (_notes_text_col or 'text'): text,
        (_notes_user_col or 'user_id'): user_id,
    }
    if entity_type and _notes_entity_col:
        record[_notes_entity_col] = entity_type
    if entity_id and _notes_entity_id_col:
        record[_notes_entity_id_col] = entity_id
    try:
        supabase.table(_notes_table).insert(record).execute()
        return True
    except Exception as e:
        logging.error(f"[notes] Ошибка сохранения: {e}")
        return False

def notes_pagination_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + NOTES_PAGE_SIZE - 1) // NOTES_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"notes_page:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"notes_page:{page+1}"))
    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="📝 Новая заметка", callback_data="new_note"),
        InlineKeyboardButton(text="◀️ В меню",         callback_data="back_to_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def show_notes_page(target, chat_id: str, page: int, edit: bool = False):
    profile = await get_profile(chat_id)
    if not profile:
        text = "❌ Вы не авторизованы."
        if edit: await target.edit_text(text)
        else:    await target.answer(text)
        return

    if not _notes_table:
        await detect_notes_schema()

    if not _notes_table:
        no_table_text = (
            "📭 Таблица заметок не найдена в базе данных.\n\n"
            "Создайте её в Supabase SQL Editor:\n\n"
            f"```sql\n{CREATE_NOTES_SQL}\n```"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")
        ]])
        if edit: await target.edit_text(no_table_text, parse_mode="Markdown", reply_markup=keyboard)
        else:    await target.answer(no_table_text, parse_mode="Markdown", reply_markup=keyboard)
        return

    user_id = profile['id']
    user_col = _notes_user_col or 'user_id'
    text_col = _notes_text_col or 'text'

    try:
        count_resp = (supabase.table(_notes_table)
                      .select('id', count='exact')
                      .eq(user_col, user_id)
                      .execute())
        total = count_resp.count if count_resp.count is not None else len(count_resp.data or [])

        if total == 0:
            text = "📭 У вас пока нет заметок.\n\nНажмите кнопку ниже, чтобы создать первую:"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Создать заметку", callback_data="new_note")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")],
            ])
            if edit: await target.edit_text(text, reply_markup=keyboard)
            else:    await target.answer(text, reply_markup=keyboard)
            return

        offset = page * NOTES_PAGE_SIZE
        total_pages = max(1, (total + NOTES_PAGE_SIZE - 1) // NOTES_PAGE_SIZE)
        data_resp = (supabase.table(_notes_table)
                     .select('*')
                     .eq(user_col, user_id)
                     .order('created_at', desc=True)
                     .range(offset, offset + NOTES_PAGE_SIZE - 1)
                     .execute())
        notes = data_resp.data or []

        lines = [f"📝 *Мои заметки* (стр. {page+1}/{total_pages}, всего: {total})\n"]
        del_buttons = []
        for i, note in enumerate(notes):
            note_text  = note.get(text_col, '—')
            note_id    = note.get('id', '')
            created_at = note.get('created_at', '')
            if created_at:
                try:
                    created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00')).strftime('%d.%m %H:%M')
                except Exception:
                    created_at = str(created_at)[:16]
            entity_type = note.get(_notes_entity_col or 'entity_type', '') if _notes_entity_col else ''
            prefix = f"[{entity_type}] " if entity_type else ""
            lines.append(f"*{offset+i+1}.* {prefix}{note_text}")
            if created_at:
                lines.append(f"   _{created_at}_")
            lines.append('')
            del_buttons.append([InlineKeyboardButton(
                text=f"🗑 Удалить #{offset+i+1}",
                callback_data=f"del_note:{note_id}:{page}"
            )])

        text = '\n'.join(lines).rstrip()
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"notes_page:{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"notes_page:{page+1}"))

        buttons = del_buttons
        if nav:
            buttons.append(nav)
        buttons.append([
            InlineKeyboardButton(text="📝 Новая заметка", callback_data="new_note"),
            InlineKeyboardButton(text="◀️ В меню",         callback_data="back_to_menu"),
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        if edit: await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:    await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"[notes] show_notes_page: {e}")
        err_text = f"❌ Ошибка при загрузке заметок: `{e}`"
        if edit: await target.edit_text(err_text, parse_mode="Markdown")
        else:    await target.answer(err_text, parse_mode="Markdown")

STATS_STATUS_ORDER = ['new', 'in_progress', 'done', 'closed', 'cancelled']

async def show_stats(target, chat_id: str, edit: bool = False):
    profile = await get_profile(chat_id)
    if not profile:
        text = "❌ Вы не авторизованы. Используйте `/connect ваш@email.com`."
        if edit:
            await target.edit_text(text, parse_mode="Markdown")
        else:
            await target.answer(text, parse_mode="Markdown")
        return

    user_id = profile['id']
    lines: list[str] = ["📊 *Статистика ArianCRM*\n"]

    try:
        req_resp = supabase.table('requests').select('id, status').eq('assigned_to', user_id).execute()
        all_reqs = req_resp.data or []
        total_reqs = len(all_reqs)
        counts: dict[str, int] = {}
        for r in all_reqs:
            s = r.get('status') or 'unknown'
            counts[s] = counts.get(s, 0) + 1
        lines.append(f"📋 *Заявки: {total_reqs}*")
        shown = set()
        for s in STATS_STATUS_ORDER:
            if s in counts:
                lines.append(f"   {status_label(s)}: {counts[s]}")
                shown.add(s)
        for s, n in counts.items():
            if s not in shown:
                lines.append(f"   {status_label(s)}: {n}")
        if not counts:
            lines.append("   Нет заявок")
    except Exception as e:
        logging.error(f"[stats] requests: {e}")
        lines.append("📋 Заявки: _ошибка загрузки_")

    try:
        cli_resp = supabase.table('clients').select('id', count='exact').eq('assigned_to', user_id).execute()
        total_clients = cli_resp.count if cli_resp.count is not None else len(cli_resp.data or [])
        lines.append(f"\n👥 *Клиенты: {total_clients}*")
    except Exception as e:
        logging.error(f"[stats] clients: {e}")
        lines.append("\n👥 Клиенты: _ошибка загрузки_")

    if _cal_table:
        try:
            today = datetime.utcnow().date()
            w_start = today - timedelta(days=today.weekday())
            w_end   = w_start + timedelta(days=6)
            q = supabase.table(_cal_table).select('id', count='exact')
            if _cal_user_col:
                q = q.eq(_cal_user_col, user_id)
            if _cal_date_col:
                q = q.gte(_cal_date_col, w_start.isoformat()).lte(_cal_date_col, w_end.isoformat())
            cal_resp = q.execute()
            cal_count = cal_resp.count if cal_resp.count is not None else len(cal_resp.data or [])
            lines.append(f"\n📅 *Задач на этой неделе: {cal_count}*")
        except Exception as e:
            logging.error(f"[stats] calendar: {e}")

    now = datetime.utcnow().strftime('%d.%m.%Y %H:%M')
    lines.append(f"\n_Обновлено: {now} UTC_")
    text = '\n'.join(lines)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="stats_refresh"),
            InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu"),
        ]
    ])

    if edit:
        await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)

# --- МЕНЮ ДО АВТОРИЗАЦИИ ---
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Войти в CRM", callback_data="login")],
        [InlineKeyboardButton(text="ℹ️ О системе", callback_data="about")],
    ])

# --- МЕНЮ ПОСЛЕ АВТОРИЗАЦИИ ---
def get_crm_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my_requests"),
         InlineKeyboardButton(text="👥 Мои клиенты", callback_data="my_clients")],
        [InlineKeyboardButton(text="📅 Мой календарь", callback_data="my_calendar"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="my_stats")],
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_from_menu"),
         InlineKeyboardButton(text="➕ Новая заявка", callback_data="new_request")],
        [InlineKeyboardButton(text="⏰ Напоминание", callback_data="new_remind"),
         InlineKeyboardButton(text="📝 Заметки",     callback_data="my_notes")],
    ])

# =====================================================================
# КОМАНДЫ
# =====================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Добро пожаловать в ArianCRM!\n\n"
        "Я — ваш мобильный помощник. Я синхронизируюсь с вашим аккаунтом в ArianCRM "
        "и позволю управлять заявками и клиентами прямо из Telegram.\n\n"
        "📌 Вы сможете работать в CRM и в телеграме и на сайте ArianCRM.ru\n\n"
        "🔗 Чтобы привязать Telegram к вашему аккаунту, зарегистрируйтесь на сайте "
        "ArianCRM.ru и в Настройках будет написана команда для работы тут в ArianCRMbot",
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

@dp.message(Command("connect"))
async def cmd_connect(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or '@' not in parts[1]:
        await message.answer(
            "❌ Укажите email.\nПример: `/connect your@email.com`",
            parse_mode="Markdown"
        )
        return

    email = parts[1].strip().lower()
    chat_id = str(message.from_user.id)
    username = message.from_user.username or ''

    await message.answer("🔄 Выполняю привязку аккаунта...", parse_mode="Markdown")

    try:
        # Шаг 1: пробуем через Lovable API (service_role, обходит RLS)
        api_result = await call_crm_api("bind-telegram", {
            "email": email,
            "chat_id": int(chat_id),
            "username": username
        })

        if api_result is not None:
            # Успех — API вернул данные без явного поля ok (или с ok=true)
            if api_result.get("ok") is not False and (api_result.get("id") or api_result.get("email") or api_result.get("ok")):
                name = api_result.get("full_name") or api_result.get("name") or api_result.get("email") or email
                logging.info(f"[connect] bind-telegram: {email} → chat_id={chat_id}")
                await message.answer(
                    f"✅ *Аккаунт успешно привязан!*\n\n"
                    f"👤 {name}\n"
                    f"📧 `{email}`\n\n"
                    "Добро пожаловать в ArianCRM! Используйте /menu для начала работы.",
                    parse_mode="Markdown",
                    reply_markup=get_crm_menu_keyboard()
                )
                return
            else:
                err = str(api_result.get("error", api_result.get("message", "")))
                if "not_found" in err or "not found" in err.lower():
                    await message.answer(
                        f"❌ Пользователь с email `{email}` не найден в системе.\n\n"
                        "Проверьте написание адреса или зарегистрируйтесь в ArianCRM.ru",
                        parse_mode="Markdown"
                    )
                    return
                elif "already" in err.lower() or "linked" in err.lower():
                    await message.answer(
                        f"ℹ️ Этот email уже привязан к другому Telegram-аккаунту.\n"
                        "Если это ваш аккаунт — обратитесь к администратору.",
                        parse_mode="Markdown"
                    )
                    return
                logging.warning(f"[connect] bind-telegram вернул: {api_result}")

        # Шаг 2: Fallback — прямой запрос (работает без RLS или с permissive политикой)
        resp = supabase.table('profiles').select('*').ilike('email', email).execute()
        if not resp.data:
            resp = supabase.table('profiles').select('*').eq('email', email).execute()

        if not resp.data:
            await message.answer(
                f"❌ Пользователь с email `{email}` не найден.\n\n"
                "Проверьте написание адреса или зарегистрируйтесь в ArianCRM.ru",
                parse_mode="Markdown"
            )
            return

        user = resp.data[0]
        user_id = user.get('id')
        tg_col = _detect_tg_col(user) or 'telegram_chat_id'

        # Уже привязан к этому же пользователю?
        if str(user.get(tg_col) or '') == chat_id:
            await message.answer(
                f"✅ Вы уже авторизованы как `{email}`!\n\nИспользуйте /menu.",
                parse_mode="Markdown",
                reply_markup=get_crm_menu_keyboard()
            )
            return

        upd = supabase.table('profiles').update({tg_col: chat_id}).eq('id', user_id).execute()
        if upd.data or upd.count:
            await message.answer(
                f"✅ *Аккаунт привязан!*\n\n📧 `{email}`\n\nИспользуйте /menu.",
                parse_mode="Markdown",
                reply_markup=get_crm_menu_keyboard()
            )
        else:
            # RLS заблокировал UPDATE — показываем SQL для ручного выполнения
            await message.answer(
                f"⚠️ Привязка не сохранилась из-за RLS в Supabase.\n\n"
                f"Выполни этот запрос в Supabase → SQL Editor:\n"
                f"```sql\n"
                f"UPDATE public.profiles\n"
                f"SET {tg_col} = '{chat_id}'\n"
                f"WHERE lower(email) = '{email}';\n"
                f"```\n"
                f"После этого отправь /start",
                parse_mode="Markdown"
            )

    except Exception as e:
        logging.error(f"[connect] Ошибка: {e}")
        await message.answer(
            f"❌ Ошибка при привязке:\n\n`{e}`",
            parse_mode="Markdown"
        )

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    await state.clear()
    profile = await get_profile(str(message.from_user.id))
    if not profile:
        await message.answer(
            "❌ Вы не авторизованы. Используйте `/connect ваш@email.com` для привязки аккаунта.",
            parse_mode="Markdown"
        )
        return
    await message.answer(
        "📋 Главное меню ArianCRM\n\nВыберите действие:",
        reply_markup=get_crm_menu_keyboard()
    )

@dp.message(Command("requests"))
async def cmd_requests(message: types.Message):
    await show_requests_page(message, str(message.from_user.id), page=0)

@dp.message(Command("clients"))
async def cmd_clients(message: types.Message):
    await show_clients_page(message, str(message.from_user.id), page=0)

@dp.message(Command("calendar"))
async def cmd_calendar(message: types.Message):
    await show_calendar_page(message, str(message.from_user.id), week_offset=0)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await show_stats(message, str(message.from_user.id))

@dp.message(Command("remind"))
async def cmd_remind(message: types.Message, state: FSMContext):
    await state.set_state(SetReminder.when)
    await message.answer(
        "⏰ *Новое напоминание*\n\n"
        "Когда напомнить? Введите дату и время:\n\n"
        "• `через 30 минут`\n"
        "• `через 2 часа`\n"
        "• `через 3 дня`\n"
        "• `сегодня в 18:00`\n"
        "• `завтра в 10:30`\n"
        "• `25.06.2026 09:00`",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.message(SetReminder.when)
async def process_remind_when(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Создание напоминания отменено.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return
    dt = parse_reminder_time(message.text or '')
    if dt is None or dt <= datetime.utcnow():
        await message.answer(
            "⚠️ Не удалось распознать время или оно уже прошло. Попробуйте ещё раз:\n\n"
            "• `через 30 минут`\n"
            "• `сегодня в 18:00`\n"
            "• `25.06.2026 09:00`",
            parse_mode="Markdown"
        )
        return
    dt_local = dt + timedelta(hours=3)
    dt_str = dt_local.strftime('%d.%m.%Y в %H:%M') + ' (МСК)'
    await state.update_data(remind_dt=dt.isoformat(), remind_dt_str=dt_str)
    await state.set_state(SetReminder.text)
    await message.answer(
        f"✅ Время: *{dt_str}*\n\nТеперь введите текст напоминания:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.message(SetReminder.text)
async def process_remind_text(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Создание напоминания отменено.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return
    remind_text = (message.text or '').strip()
    if len(remind_text) < 2:
        await message.answer("⚠️ Текст слишком короткий. Введите хотя бы 2 символа:")
        return
    data = await state.get_data()
    dt_str = data.get('remind_dt_str', '')
    dt_iso = data.get('remind_dt', '')
    await state.clear()
    await message.answer("✅ Создаю напоминание…", reply_markup=ReplyKeyboardRemove())
    dt = datetime.fromisoformat(dt_iso)
    job_id = str(uuid.uuid4())[:8]
    chat_id = str(message.from_user.id)
    reminder_store[job_id] = {'chat_id': chat_id, 'text': remind_text, 'dt_str': dt_str}
    scheduler.add_job(
        fire_reminder, 'date',
        run_date=dt,
        args=[chat_id, remind_text, job_id],
        id=job_id,
        misfire_grace_time=300,
    )
    await message.answer(
        f"⏰ *Напоминание установлено!*\n\n"
        f"📅 {dt_str}\n"
        f"📝 {remind_text}\n\n"
        f"🆔 ID: `{job_id}`  _(для отмены: /cancelremind {job_id})_",
        parse_mode="Markdown",
        reply_markup=get_crm_menu_keyboard()
    )

@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    chat_id = str(message.from_user.id)
    my = {jid: v for jid, v in reminder_store.items() if v['chat_id'] == chat_id}
    if not my:
        await message.answer(
            "⏰ У вас нет активных напоминаний.\n\nСоздать: /remind",
            reply_markup=get_crm_menu_keyboard()
        )
        return
    lines = ["⏰ *Ваши активные напоминания:*\n"]
    for jid, v in my.items():
        lines.append(f"🆔 `{jid}` — {v['dt_str']}\n   📝 {v['text']}")
    lines.append("\n_Для отмены: /cancelremind ID_")
    await message.answer('\n'.join(lines), parse_mode="Markdown")

@dp.message(Command("cancelremind"))
async def cmd_cancelremind(message: types.Message):
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("⚠️ Укажите ID напоминания: `/cancelremind abc12345`", parse_mode="Markdown")
        return
    job_id = parts[1].strip()
    chat_id = str(message.from_user.id)
    entry = reminder_store.get(job_id)
    if not entry or entry['chat_id'] != chat_id:
        await message.answer(f"❌ Напоминание `{job_id}` не найдено.", parse_mode="Markdown")
        return
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    reminder_store.pop(job_id, None)
    await message.answer(f"✅ Напоминание `{job_id}` отменено.", parse_mode="Markdown")

@dp.message(Command("note"))
async def cmd_note(message: types.Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        note_text = parts[1].strip()
        profile = await get_profile(str(message.from_user.id))
        if not profile:
            await message.answer("❌ Вы не авторизованы. Используйте `/connect ваш@email.com`.", parse_mode="Markdown")
            return
        ok = await save_note(profile['id'], note_text)
        if ok:
            await message.answer(
                f"✅ *Заметка сохранена!*\n\n📝 {note_text}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📋 Все заметки", callback_data="my_notes"),
                    InlineKeyboardButton(text="◀️ В меню",      callback_data="back_to_menu"),
                ]])
            )
        else:
            await message.answer(
                "❌ Не удалось сохранить заметку — таблица `notes` не найдена.\n\n"
                "Создайте её в Supabase SQL Editor:\n\n"
                f"```sql\n{CREATE_NOTES_SQL}\n```",
                parse_mode="Markdown"
            )
    else:
        await state.set_state(AddNote.text)
        await message.answer(
            "📝 *Новая заметка*\n\nВведите текст заметки:",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )

@dp.message(AddNote.text)
async def process_note_text(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Создание заметки отменено.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return
    note_text = (message.text or '').strip()
    if len(note_text) < 2:
        await message.answer("⚠️ Текст слишком короткий. Введите хотя бы 2 символа:")
        return
    await state.clear()
    await message.answer("💾 Сохраняю…", reply_markup=ReplyKeyboardRemove())
    profile = await get_profile(str(message.from_user.id))
    if not profile:
        await message.answer("❌ Сессия истекла. Попробуйте снова.")
        return
    ok = await save_note(profile['id'], note_text)
    if ok:
        await message.answer(
            f"✅ *Заметка сохранена!*\n\n📝 {note_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Все заметки", callback_data="my_notes"),
                InlineKeyboardButton(text="◀️ В меню",      callback_data="back_to_menu"),
            ]])
        )
    else:
        await message.answer(
            "❌ Не удалось сохранить — таблица `notes` не найдена.\n\n"
            "Создайте её в Supabase:\n\n"
            f"```sql\n{CREATE_NOTES_SQL}\n```",
            parse_mode="Markdown"
        )

@dp.message(Command("notes"))
async def cmd_notes(message: types.Message):
    await show_notes_page(message, str(message.from_user.id), page=0)

@dp.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await perform_search(message, str(message.from_user.id), parts[1].strip())
    else:
        await state.set_state(SearchQuery.query)
        await message.answer(
            "🔍 *Поиск по CRM*\n\n"
            "Введите имя клиента, название заявки или любое ключевое слово:",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )

@dp.message(SearchQuery.query)
async def process_search_query(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("🔍 Поиск отменён.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return
    query_text = (message.text or '').strip()
    await state.clear()
    await message.answer("⏳ Ищу...", reply_markup=ReplyKeyboardRemove())
    if not query_text:
        await message.answer("⚠️ Пустой запрос. Попробуйте снова: /search")
        return
    await perform_search(message, str(message.from_user.id), query_text)

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    chat_id = str(message.from_user.id)
    profile = await get_profile(chat_id)
    if profile:
        await message.answer(
            f"✅ Вы авторизованы в ArianCRM!\n\n"
            f"📧 Email: {profile.get('email', 'не указан')}\n"
            f"🆔 Telegram ID: {chat_id}\n\n"
            "Используйте /menu для управления заявками."
        )
    else:
        await message.answer(
            "❌ Вы не авторизованы.\n"
            "Используйте `/connect ваш@email.com` для привязки аккаунта.",
            parse_mode="Markdown"
        )

# =====================================================================
# FSM: СОЗДАНИЕ НОВОЙ ЗАЯВКИ
# =====================================================================

async def start_new_request(message: types.Message, state: FSMContext):
    profile = await get_profile(str(message.from_user.id))
    if not profile:
        await message.answer(
            "❌ Вы не авторизованы. Используйте `/connect ваш@email.com` для привязки аккаунта.",
            parse_mode="Markdown"
        )
        return
    await state.set_state(NewRequest.title)
    await message.answer(
        "➕ *Создание новой заявки*\n\n"
        "Шаг 1 из 3\n"
        "📝 Введите *название* заявки:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.message(Command("newrequest"))
async def cmd_newrequest(message: types.Message, state: FSMContext):
    await start_new_request(message, state)

@dp.message(NewRequest.title)
async def process_title(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Создание заявки отменено.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return

    if not message.text or len(message.text.strip()) < 2:
        await message.answer("⚠️ Название слишком короткое. Введите не менее 2 символов:")
        return

    await state.update_data(title=message.text.strip())
    await state.set_state(NewRequest.client_name)
    await message.answer(
        "Шаг 2 из 3\n"
        "👤 Введите *имя клиента*:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.message(NewRequest.client_name)
async def process_client_name(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Создание заявки отменено.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return

    if not message.text or len(message.text.strip()) < 2:
        await message.answer("⚠️ Имя слишком короткое. Введите не менее 2 символов:")
        return

    await state.update_data(client_name=message.text.strip())
    await state.set_state(NewRequest.description)
    await message.answer(
        "Шаг 3 из 3\n"
        "📄 Введите *описание* заявки или нажмите «Пропустить»:",
        parse_mode="Markdown",
        reply_markup=skip_or_cancel_keyboard()
    )

@dp.message(NewRequest.description)
async def process_description(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Создание заявки отменено.", reply_markup=ReplyKeyboardRemove())
        await message.answer("📋 Главное меню:", reply_markup=get_crm_menu_keyboard())
        return

    description = None if message.text == "⏭ Пропустить" else message.text.strip()

    data = await state.get_data()
    await state.clear()

    profile = await get_profile(str(message.from_user.id))
    if not profile:
        await message.answer("❌ Сессия истекла. Попробуйте снова.", reply_markup=ReplyKeyboardRemove())
        return

    try:
        record = {
            'title': data['title'],
            'client_name': data['client_name'],
            'assigned_to': profile['id'],
            'status': 'new',
            'created_at': datetime.utcnow().isoformat(),
        }
        if description:
            record['description'] = description

        supabase.table('requests').insert(record).execute()

        summary = (
            f"✅ *Заявка успешно создана!*\n\n"
            f"📝 Название: {data['title']}\n"
            f"👤 Клиент: {data['client_name']}\n"
        )
        if description:
            summary += f"📄 Описание: {description}\n"
        summary += f"📌 Статус: 🆕 новая"

        await message.answer(summary, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await message.answer(
            "Что дальше?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my_requests")],
                [InlineKeyboardButton(text="➕ Ещё одну заявку", callback_data="new_request")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")],
            ])
        )

    except Exception as e:
        logging.error(f"Ошибка при создании заявки: {e}")
        await message.answer(
            f"❌ Не удалось сохранить заявку. Попробуйте позже.\n\n`{e}`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )

# =====================================================================
# CALLBACKS
# =====================================================================

@dp.callback_query(F.data == "my_requests")
async def my_requests_callback(callback: types.CallbackQuery):
    await callback.answer()
    await show_requests_page(callback.message, str(callback.from_user.id), page=0, edit=False)

@dp.callback_query(F.data.startswith("req_page:"))
async def requests_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.answer()
    await show_requests_page(callback.message, str(callback.from_user.id), page=page, edit=True)

@dp.callback_query(F.data == "my_clients")
async def my_clients_callback(callback: types.CallbackQuery):
    await callback.answer()
    await show_clients_page(callback.message, str(callback.from_user.id), page=0, edit=False)

@dp.callback_query(F.data.startswith("cli_page:"))
async def clients_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.answer()
    await show_clients_page(callback.message, str(callback.from_user.id), page=page, edit=True)

@dp.callback_query(F.data == "new_request")
async def new_request_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_new_request(callback.message, state)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "📋 Главное меню ArianCRM\n\nВыберите действие:",
        reply_markup=get_crm_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "login")
async def login_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔑 Для авторизации используйте команду:\n`/connect ваш@email.com`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "about")
async def about_callback(callback: types.CallbackQuery):
    await callback.message.answer(
        "ℹ️ *ArianCRM Telegram Bot*\n\n"
        "Версия: 1.2\n"
        "Бот синхронизируется с веб-версией CRM.\n\n"
        "📌 Доступные команды:\n"
        "`/start` — Приветствие и инструкция\n"
        "`/connect email` — Привязать аккаунт\n"
        "`/menu` — Главное меню\n"
        "`/requests` — Мои заявки\n"
        "`/clients` — Мои клиенты\n"
        "`/newrequest` — Создать заявку\n"
        "`/calendar` — Мой календарь\n"
        "`/search [запрос]` — Поиск клиентов и заявок\n"
        "`/stats` — Статистика по заявкам и клиентам\n"
        "`/remind` — Установить напоминание\n"
        "`/reminders` — Мои напоминания\n"
        "`/note [текст]` — Добавить заметку\n"
        "`/notes` — Мои заметки\n"
        "`/check` — Проверить авторизацию",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "new_remind")
async def new_remind_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SetReminder.when)
    await callback.message.answer(
        "⏰ *Новое напоминание*\n\n"
        "Когда напомнить?\n\n"
        "• `через 30 минут`\n"
        "• `через 2 часа`\n"
        "• `сегодня в 18:00`\n"
        "• `завтра в 10:30`\n"
        "• `25.06.2026 09:00`",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.callback_query(F.data == "my_stats")
async def my_stats_callback(callback: types.CallbackQuery):
    await callback.answer()
    await show_stats(callback.message, str(callback.from_user.id), edit=False)

@dp.callback_query(F.data == "stats_refresh")
async def stats_refresh_callback(callback: types.CallbackQuery):
    await callback.answer("🔄 Обновляю...")
    await show_stats(callback.message, str(callback.from_user.id), edit=True)

@dp.callback_query(F.data == "search_from_menu")
async def search_from_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SearchQuery.query)
    await callback.message.answer(
        "🔍 *Поиск по CRM*\n\nВведите имя клиента, название заявки или ключевое слово:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.callback_query(F.data == "my_notes")
async def my_notes_callback(callback: types.CallbackQuery):
    await callback.answer()
    await show_notes_page(callback.message, str(callback.from_user.id), page=0, edit=False)

@dp.callback_query(F.data == "new_note")
async def new_note_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AddNote.text)
    await callback.message.answer(
        "📝 *Новая заметка*\n\nВведите текст заметки:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )

@dp.callback_query(F.data.startswith("notes_page:"))
async def notes_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.answer()
    await show_notes_page(callback.message, str(callback.from_user.id), page=page, edit=True)

@dp.callback_query(F.data.startswith("del_note:"))
async def del_note_callback(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    note_id = parts[1] if len(parts) > 1 else ''
    page    = int(parts[2]) if len(parts) > 2 else 0
    if not note_id or not _notes_table:
        await callback.answer("❌ Не удалось удалить заметку.", show_alert=True)
        return
    try:
        supabase.table(_notes_table).delete().eq('id', note_id).execute()
        await callback.answer("🗑 Заметка удалена.")
        await show_notes_page(callback.message, str(callback.from_user.id), page=page, edit=True)
    except Exception as e:
        logging.error(f"[notes] del_note: {e}")
        await callback.answer("❌ Ошибка при удалении.", show_alert=True)

@dp.callback_query(F.data == "search_again")
async def search_again_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SearchQuery.query)
    await callback.message.answer(
        "🔍 Введите новый поисковый запрос:",
        reply_markup=cancel_keyboard()
    )

@dp.callback_query(F.data == "my_calendar")
async def my_calendar_callback(callback: types.CallbackQuery):
    await callback.answer()
    await show_calendar_page(callback.message, str(callback.from_user.id), week_offset=0, edit=False)

@dp.callback_query(F.data.startswith("cal_week:"))
async def calendar_week_callback(callback: types.CallbackQuery):
    offset = int(callback.data.split(":")[1])
    await callback.answer()
    await show_calendar_page(callback.message, str(callback.from_user.id), week_offset=offset, edit=True)

# =====================================================================
# КАЛЕНДАРЬ
# =====================================================================

CALENDAR_TABLE_CANDIDATES = ['tasks', 'events', 'calendar_events', 'appointments', 'meetings']
CAL_DATE_CANDIDATES = ['due_date', 'start_date', 'date', 'scheduled_at', 'deadline', 'event_date', 'appointment_date']
CAL_TITLE_CANDIDATES = ['title', 'name', 'subject', 'task_name', 'event_name', 'description']
CAL_STATUS_CANDIDATES = ['status', 'state']

_cal_table: str | None = None
_cal_date_col: str | None = None
_cal_title_col: str | None = None
_cal_user_col: str | None = None
_cal_status_col: str | None = None

WEEKDAYS_RU = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
MONTHS_RU = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']

def fmt_date_ru(d) -> str:
    return f"{d.day} {MONTHS_RU[d.month - 1]}"

async def detect_calendar_schema():
    global _cal_table, _cal_date_col, _cal_title_col, _cal_user_col, _cal_status_col
    for table in CALENDAR_TABLE_CANDIDATES:
        try:
            resp = supabase.table(table).select('*').limit(1).execute()
            _cal_table = table
            if resp.data:
                cols = set(resp.data[0].keys())
                _cal_date_col   = next((c for c in CAL_DATE_CANDIDATES   if c in cols), None)
                _cal_title_col  = next((c for c in CAL_TITLE_CANDIDATES  if c in cols), None)
                _cal_user_col   = next((c for c in USER_CANDIDATES       if c in cols), None)
                _cal_status_col = next((c for c in CAL_STATUS_CANDIDATES if c in cols), None)
                logging.info(
                    f"[calendar] Таблица '{table}' | дата='{_cal_date_col}' | "
                    f"название='{_cal_title_col}' | пользователь='{_cal_user_col}' | "
                    f"все колонки: {sorted(cols)}"
                )
            else:
                logging.warning(f"[calendar] Таблица '{table}' найдена, но пуста — определение колонок отложено")
            return
        except Exception as e:
            err = str(e)
            if any(k in err for k in ['does not exist', 'relation', '42703', '42P01', 'PGRST205', 'Could not find']):
                logging.debug(f"[calendar] Таблица '{table}' не найдена, пробую следующую")
                continue
            logging.error(f"[calendar] Ошибка при проверке таблицы '{table}': {e}")
    logging.warning(f"[calendar] Ни одна из таблиц не найдена: {CALENDAR_TABLE_CANDIDATES}")

def _cal_extract_title(item: dict) -> str:
    for field in CAL_TITLE_CANDIDATES:
        val = item.get(field)
        if val and isinstance(val, str) and val.strip():
            return val.strip()[:80]
    return f"Задача #{item.get('id', '?')}"

def calendar_week_keyboard(week_offset: int) -> InlineKeyboardMarkup:
    buttons = [[
        InlineKeyboardButton(text="◀️ Пред.", callback_data=f"cal_week:{week_offset - 1}"),
        InlineKeyboardButton(text="🔄", callback_data=f"cal_week:{week_offset}"),
        InlineKeyboardButton(text="След. ▶️", callback_data=f"cal_week:{week_offset + 1}"),
    ]]
    if week_offset != 0:
        buttons.append([InlineKeyboardButton(text="📅 Текущая неделя", callback_data="cal_week:0")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def show_calendar_page(target, chat_id: str, week_offset: int, edit: bool = False):
    profile = await get_profile(chat_id)
    if not profile:
        text = "❌ Вы не авторизованы. Используйте `/connect ваш@email.com` для привязки аккаунта."
        if edit:
            await target.edit_text(text, parse_mode="Markdown")
        else:
            await target.answer(text, parse_mode="Markdown")
        return

    if not _cal_table:
        await detect_calendar_schema()

    if not _cal_table:
        text = (
            "📭 Раздел «Календарь» недоступен — таблица с задачами не найдена.\n\n"
            f"Поддерживаемые названия таблиц: `{', '.join(CALENDAR_TABLE_CANDIDATES)}`"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")]])
        if edit:
            await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    user_id = profile['id']
    today = datetime.utcnow().date()
    week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    week_end   = week_start + timedelta(days=6)
    week_label = f"{fmt_date_ru(week_start)} – {fmt_date_ru(week_end)} {week_end.year}"

    try:
        query = supabase.table(_cal_table).select('*')
        if _cal_user_col:
            query = query.eq(_cal_user_col, user_id)
        if _cal_date_col:
            query = (query
                     .gte(_cal_date_col, week_start.isoformat())
                     .lte(_cal_date_col, week_end.isoformat()))
        query = query.order(_cal_date_col or 'created_at')
        items = (query.execute().data or [])

        from collections import defaultdict
        days: dict = defaultdict(list)
        for item in items:
            raw = item.get(_cal_date_col, '') if _cal_date_col else ''
            day_key = str(raw)[:10] if raw else ''
            if day_key:
                days[day_key].append(item)

        header = f"📅 *Мой календарь*\n_{week_label}_\n\n"

        if not days:
            text = header + "📭 Нет задач на эту неделю."
        else:
            lines = [header.rstrip()]
            for i in range(7):
                day = week_start + timedelta(days=i)
                day_key = day.isoformat()
                if day_key not in days:
                    continue
                is_today = (day == today)
                day_header = f"*{WEEKDAYS_RU[day.weekday()]}, {fmt_date_ru(day)}*"
                if is_today:
                    day_header += " 📍"
                lines.append(day_header)
                for item in days[day_key]:
                    title = _cal_extract_title(item)
                    status_str = ''
                    if _cal_status_col and item.get(_cal_status_col):
                        status_str = f" — {status_label(item[_cal_status_col])}"
                    lines.append(f"• {title}{status_str}")
                lines.append('')
            text = '\n'.join(lines).rstrip()

        keyboard = calendar_week_keyboard(week_offset)
        if edit:
            await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"[calendar] Ошибка: {e}")
        text = f"❌ Не удалось загрузить календарь. Попробуйте позже.\n\n`{e}`"
        if edit:
            await target.edit_text(text, parse_mode="Markdown")
        else:
            await target.answer(text, parse_mode="Markdown")

# =====================================================================
# УВЕДОМЛЕНИЯ ОБ ИЗМЕНЕНИИ СТАТУСА
# =====================================================================

STATUS_RU = {
    'new': '🆕 Новая',
    'in_progress': '🔄 В работе',
    'done': '✅ Выполнена',
    'closed': '🔒 Закрыта',
    'cancelled': '❌ Отменена',
}

def status_ru(status: str) -> str:
    return STATUS_RU.get(status, f"📌 {status}")

# --- Авто-обнаруженные колонки таблицы requests ---
_req_status_col: str | None = None
_req_user_col: str | None = None

STATUS_CANDIDATES = ['status', 'state', 'request_status']
USER_CANDIDATES = ['assigned_to', 'user_id', 'created_by', 'manager_id', 'owner_id', 'profile_id', 'assignee_id']
TITLE_CANDIDATES = ['title', 'name', 'subject', 'request_name', 'heading']

def _extract_title(record: dict) -> str:
    """Извлекает название заявки из любого известного поля."""
    for field in TITLE_CANDIDATES:
        val = record.get(field)
        if val and isinstance(val, str) and val.strip():
            return val.strip()[:80]
    return f"Заявка #{record.get('id', '?')}"

async def detect_requests_schema():
    """Обнаруживает реальные названия колонок в таблице requests."""
    global _req_status_col, _req_user_col
    try:
        resp = supabase.table('requests').select('*').limit(1).execute()
        if not resp.data:
            logging.warning("[notifications] Таблица requests пуста — авто-определение отложено")
            return
        cols = set(resp.data[0].keys())
        _req_status_col = next((c for c in STATUS_CANDIDATES if c in cols), None)
        _req_user_col   = next((c for c in USER_CANDIDATES   if c in cols), None)
        logging.info(f"[notifications] Схема requests: статус='{_req_status_col}', пользователь='{_req_user_col}' | все колонки: {sorted(cols)}")
        if not _req_status_col:
            logging.warning(f"[notifications] Колонка статуса не найдена. Варианты: {STATUS_CANDIDATES}")
        if not _req_user_col:
            logging.warning(f"[notifications] Колонка пользователя не найдена. Варианты: {USER_CANDIDATES}")
    except Exception as e:
        logging.error(f"[notifications] Ошибка авто-определения схемы: {e}")

async def _fetch_requests_snapshot() -> dict:
    """Возвращает {request_id: {'status': ..., 'user_id': ...}}"""
    if not _req_status_col:
        return {}
    try:
        resp = supabase.table('requests').select('id').execute()
        if not resp.data:
            return {}
        select_cols = 'id'
        if _req_status_col:
            select_cols += f', {_req_status_col}'
        if _req_user_col:
            select_cols += f', {_req_user_col}'
        resp = supabase.table('requests').select(select_cols).execute()
        return {
            str(r['id']): {
                'status': r.get(_req_status_col, '') if _req_status_col else '',
                'user_ref': r.get(_req_user_col) if _req_user_col else None,
            }
            for r in (resp.data or [])
        }
    except Exception as e:
        logging.error(f"[notifications] Ошибка при получении заявок: {e}")
        return {}

async def _fetch_request_title(request_id: str) -> str:
    """Получает название конкретной заявки."""
    try:
        resp = supabase.table('requests').select('*').eq('id', request_id).execute()
        if resp.data:
            return _extract_title(resp.data[0])
    except Exception:
        pass
    return f"Заявка #{request_id}"

async def _get_telegram_chat_id(user_id: str) -> str | None:
    """Получить telegram_chat_id по user_id из profiles."""
    try:
        resp = supabase.table('profiles') \
            .select('telegram_chat_id') \
            .eq('id', user_id) \
            .execute()
        if resp.data:
            return resp.data[0].get('telegram_chat_id')
    except Exception as e:
        logging.error(f"[notifications] Ошибка при получении профиля {user_id}: {e}")
    return None

async def init_status_cache():
    """Определяет схему и заполняет кэш текущими статусами без отправки уведомлений."""
    global status_cache
    await detect_requests_schema()
    snapshot = await _fetch_requests_snapshot()
    status_cache = {rid: data['status'] for rid, data in snapshot.items()}
    logging.info(f"[notifications] Кэш инициализирован: {len(status_cache)} заявок")

async def check_status_changes():
    """Сравнивает текущие статусы с кэшем и отправляет уведомления об изменениях."""
    global status_cache
    # Если схема ещё не определена — пробуем снова
    if not _req_status_col:
        await detect_requests_schema()
    snapshot = await _fetch_requests_snapshot()
    if not snapshot:
        return

    changed = []
    for rid, data in snapshot.items():
        old_status = status_cache.get(rid)
        new_status = data['status']
        if old_status is not None and old_status != new_status:
            changed.append({
                'id': rid,
                'old_status': old_status,
                'new_status': new_status,
                'user_ref': data.get('user_ref'),
            })

    # Обновляем весь кэш (включая новые заявки)
    status_cache = {rid: data['status'] for rid, data in snapshot.items()}

    if not changed:
        return

    logging.info(f"[notifications] Изменилось статусов: {len(changed)}")

    # Отправляем уведомления
    for item in changed:
        if not item.get('user_ref'):
            continue
        chat_id = await _get_telegram_chat_id(str(item['user_ref']))
        if not chat_id:
            continue
        title = await _fetch_request_title(item['id'])
        text = (
            f"🔔 *Статус заявки изменился*\n\n"
            f"📝 {title}\n\n"
            f"{status_ru(item['old_status'])}  →  {status_ru(item['new_status'])}"
        )
        try:
            await bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my_requests")],
                ])
            )
        except Exception as e:
            logging.error(f"[notifications] Не удалось отправить уведомление {chat_id}: {e}")

# =====================================================================
# HEALTH CHECK HTTP СЕРВЕР (для Replit Deployment)
# =====================================================================

async def start_health_server():
    from aiohttp import web

    async def health(request):
        return web.Response(
            text='{"status":"ok","service":"ArianCRM Bot"}',
            content_type='application/json'
        )

    app = web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', 5001))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"[health] HTTP сервер запущен на порту {port}")

# =====================================================================
# ЗАПУСК
# =====================================================================

async def main():
    print("🚀 Бот ArianCRM запущен!")
    await bot.delete_webhook(drop_pending_updates=True)

    # HTTP health-check для Replit Deployment
    await start_health_server()

    # Инициализируем кэш статусов (без уведомлений при старте)
    await init_status_cache()

    # Определяем схему таблицы для календаря
    await detect_calendar_schema()

    # Определяем схему таблицы для заметок
    await detect_notes_schema()

    # Запускаем планировщик проверки изменений
    scheduler.add_job(
        check_status_changes,
        trigger='interval',
        minutes=CHECK_INTERVAL_MINUTES,
        id='check_status_changes',
        replace_existing=True,
    )
    scheduler.start()
    logging.info(f"[notifications] Планировщик запущен (интервал: {CHECK_INTERVAL_MINUTES} мин)")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
