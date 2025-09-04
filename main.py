import os, re, asyncio, logging
from typing import List, Optional
from fastapi import FastAPI
from telethon import events
from telethon.sessions import StringSession
from telethon import TelegramClient
import uvicorn
from datetime import datetime, timedelta, timezone  # для бэскана истории

# ---------- настройки через окружение ----------
API_ID = int(os.environ["API_ID"])           # число с my.telegram.org
API_HASH = os.environ["API_HASH"]            # строка с my.telegram.org
TG_STRING_SESSION = os.environ["TG_STRING_SESSION"]  # твой длинный ключ
CHANNELS = os.getenv("CHANNELS", "").strip() # список через запятую: @chat1,@chat2
MINUS_WORDS = os.getenv("MINUS_WORDS", "")   # минус-слова через запятую (например: "школа,класс,обед,столовая,домашка,ученик,директор")

# ---------- ключевые группы ----------
LANG_PATTERNS = [
    # английский
    r"\bанглийск\w*\b", r"\bангл\b", r"\benglish\b", r"\bIELTS\b", r"\bTOEFL\b",
    # испанский
    r"\bиспанск\w*\b", r"\bspanish\b", r"\bDELE\b",
    # итальянский
    r"\bитальянск\w*\b", r"\bitalian\b", r"\bCELI\b", r"\bCILS\b",
    # китайский
    r"\bкитайск\w*\b", r"\bchinese\b", r"\bHSK\b",
]

ROLE_PATTERNS = [
    r"\bрепетитор\w*\b",
    r"\bпреподавател[ья]\w*\b",
    r"\bучител[ья]\w*\b",  # оставим, но оно само по себе не триггерит без языка + намерения
]

SCHOOL_PATTERNS = [
    r"\bонлайн[- ]?школ\w*\b",
    r"\bкурсы?\b", r"\bзанятия\b", r"\bурок(?:и|ов)?\b",
    r"\bподготовк\w*\b",  # подготовка к экзаменам и т.п.
]

HINT_PATTERNS = [
    r"\bищу\b", r"\bнужен\b", r"\bнужна\b", r"\bнужно\b",
    r"\bпорекомендуйте\b", r"\bпосоветуйте\b", r"\bкто\s+может\s+посоветовать\b",
    r"\brecommend\b", r"\blooking\s+for\b", r"\bneed\b",
]

def _rx_or(parts: List[str]) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE) if parts else re.compile(r"^\b$")

# компилированные регэкспы
RX_LANG   = _rx_or(LANG_PATTERNS)
RX_ROLE   = _rx_or(ROLE_PATTERNS)
RX_SCHOOL = _rx_or(SCHOOL_PATTERNS)
RX_HINT   = _rx_or(HINT_PATTERNS)

# минус-слова из окружения
MINUS = [w.strip() for w in MINUS_WORDS.split(",") if w.strip()]
RX_MINUS = _rx_or([re.escape(w) for w in MINUS]) if MINUS else None

def norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u200b", "")).strip()

def looks_like_request(text: str) -> bool:
    """
    Жёсткий фильтр:
    - обязательно есть один из языков (RX_LANG)
    - и (роль репетитора/препода ИЛИ школа/курсы)  => RX_ROLE or RX_SCHOOL
    - и выражена интенция (RX_HINT ИЛИ вопросительный знак/слова-триггеры)
    - не содержит минус-слов
    """
    t = norm(text)
    if not t:
        return False
    if RX_MINUS and RX_MINUS.search(t):
        return False

    if not RX_LANG.search(t):
        return False

    has_role_or_school = bool(RX_ROLE.search(t) or RX_SCHOOL.search(t))
    if not has_role_or_school:
        return False

    hinted = bool(
        RX_HINT.search(t) or
        re.search(r"[?]|подскажите|посоветуйте|ищу|нужен|нужна|нужно|порекоменд(уй|уйте)", t, re.IGNORECASE)
    )
    return hinted

# ---------- Telegram client ----------
client = TelegramClient(StringSession(TG_STRING_SESSION), API_ID, API_HASH)

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn")

# будем хранить разрешённые chat_id (если CHANNELS пустой — слушаем все)
allowed_chat_ids: Optional[set[int]] = None

async def resolve_entities():
    """Заполняем allowed_chat_ids из переменной CHANNELS.
    Если CHANNELS пуст — слушаем все чаты."""
    global allowed_chat_ids
    if not CHANNELS:
        allowed_chat_ids = None
        logger.info("Слушаем: ВСЕ чаты (CHANNELS пустой)")
        return

    names = [x.strip() for x in CHANNELS.split(",") if x.strip()]
    ids = set()
    for name in names:
        try:
            ent = await client.get_entity(name)
            ids.add(getattr(ent, "id", None))
        except Exception as e:
            logger.warning(f"Не удалось получить {name}: {e}")
    allowed_chat_ids = {i for i in ids if i is not None}
    logger.info(f"Слушаем чаты/каналы: {len(allowed_chat_ids)}")

def public_link(username: Optional[str], mid: int) -> str:
    return f"https://t.me/{username}/{mid}" if username else ""

# ---------- сканирование истории ----------
async def scan_recent(days: int = 4, max_per_chat: int = 2000) -> int:
    """
    Пройтись по чатам/каналам и найти сообщения за последние `days` дней,
    похожие на запрос репетитора. Возвращает число совпадений.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # набираем список сущностей для прохода
    entities = []
    if allowed_chat_ids is None:
        async for d in client.iter_dialogs():
            if getattr(d, "is_group", False) or getattr(d, "is_channel", False):
                entities.append(d.entity)
    else:
        for cid in allowed_chat_ids:
            try:
                entities.append(await client.get_entity(cid))
            except Exception as e:
                logger.warning(f"Не удалось получить entity {cid}: {e}")

    total = 0
    for ent in entities:
        title = getattr(ent, "title", getattr(ent, "username", None)) or str(getattr(ent, "id", ""))
        username = getattr(ent, "username", None)

        async for m in client.iter_messages(ent, limit=max_per_chat):
            if not m or not m.date:
                continue
            if m.date < cutoff:
                break  # дальше сообщения ещё старше

            text = m.message or ""
            if not looks_like_request(text):
                continue

            link = public_link(username, m.id)
            msg = (
                "🔎 (история) Запрос репетитора по языкам\n"
                f"👥 Чат: {title}\n"
                f"🧷 Сообщение #{m.id}\n"
                f"🕒 {m.date.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
                f"🔗 {link or '(приватный чат)'}\n\n"
                f"{norm(text)}"
            )
            await client.send_message("me", msg)
            total += 1
            await asyncio.sleep(0.2)  # щадим лимиты

    logger.info(f"[SCAN] Найдено совпадений: {total} (за {days} дн.)")
    return total

@app.on_event("startup")
async def on_startup():
    await client.start()
    await resolve_entities()

    # обработчик новых сообщений (онлайн-режим)
    @client.on(events.NewMessage)
    async def handler(event):
        try:
            if allowed_chat_ids is not None and event.chat_id not in allowed_chat_ids:
                return

            text = event.message.message or ""
            if not looks_like_request(text):
                return

            chat = await event.get_chat()
            username = getattr(chat, "username", None)
            title = getattr(chat, "title", username) or str(getattr(chat, "id", ""))
            link = public_link(username, event.id)

            msg = (
                "🔎 Запрос репетитора по языкам\n"
                f"👥 Чат: {title}\n"
                f"🧷 Сообщение #{event.id}\n"
                f"🔗 {link or '(приватный чат)'}\n\n"
                f"{norm(text)}"
            )
            await client.send_message("me", msg)
            logger.info(f"[MATCH] {title} #{event.id} | {norm(text)[:120]}")
        except Exception as e:
            logger.exception(f"Ошибка обработчика: {e}")

    # Telethon в фоне
    asyncio.create_task(client.run_until_disconnected())
    logger.info("Клиент Telegram запущен.")

    # разовый бэскан истории за последние 4 дня при старте
    asyncio.create_task(scan_recent(days=4))

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"ok": True}

if __name__ == "__main__":
    # Render задаёт PORT; по умолчанию 10000
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
