import os, re, asyncio, logging
from typing import List, Optional
from fastapi import FastAPI
from telethon import events
from telethon.sessions import StringSession
from telethon import TelegramClient

# ---------- настройки через окружение ----------
API_ID = int(os.environ["API_ID"])           # число с my.telegram.org
API_HASH = os.environ["API_HASH"]            # строка с my.telegram.org
TG_STRING_SESSION = os.environ["TG_STRING_SESSION"]  # твой длинный ключ
CHANNELS = os.getenv("CHANNELS", "").strip() # список через запятую: @chat1,@chat2
MINUS_WORDS = os.getenv("MINUS_WORDS", "")   # минус-слова через запятую

# ключевые слова и "подсказки"
KEYWORDS = [
    r"\bрепетитор[а-я]*\b", r"\bпреподавател[ья][а-я]*\b", r"\bучител[ья][а-я]*\b",
    r"\bзанятия по англ[а-я]*\b", r"\bанглийск(ий|ого|им|ом|ие)\b", r"\bангл\b",
    r"\bIELTS\b", r"\bTOEFL\b", r"\btutor\b", r"\bteacher\b", r"\benglish\b",
]
HINTS = [
    r"\bпорекомендуйте\b", r"\bможете ли порекомендовать\b",
    r"\bнужен(а|о)? репетитор\b", r"\bищу репетитора\b",
    r"\bкто может посоветовать\b",
    r"\brecommend( an? )?english (tutor|teacher)\b",
    r"\bIELTS (coach|tutor|teacher)\b",
]

def _rx_or(parts: List[str]) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE) if parts else re.compile(r"^\b$")

RX_KEY = _rx_or(KEYWORDS)
RX_HINT = _rx_or(HINTS)

MINUS = [w.strip() for w in MINUS_WORDS.split(",") if w.strip()]
RX_MINUS = _rx_or([re.escape(w) for w in MINUS]) if MINUS else None

def norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u200b", "")).strip()

def looks_like_request(text: str) -> bool:
    t = norm(text)
    if not t or not RX_KEY.search(t):
        return False
    if RX_MINUS and RX_MINUS.search(t):
        return False
    if RX_HINT.search(t):
        return True
    return bool(re.search(r"[?]|подскажите|посоветуйте|ищу|нужен|где найти", t, re.IGNORECASE))

# ---------- Telegram client ----------
client = TelegramClient(StringSession(TG_STRING_SESSION), API_ID, API_HASH)

app = FastAPI()
logger = logging.getLogger("uvicorn")
logger.setLevel(logging.INFO)

entities_cache = None  # список сущностей чатов для фильтра

async def resolve_entities():
    global entities_cache
    if not CHANNELS:
        entities_cache = None  # слушать все диалоги
        logger.info("Слушаем: ВСЕ чаты (CHANNELS пустой)")
        return
    names = [x.strip() for x in CHANNELS.split(",") if x.strip()]
    ents = []
    for name in names:
        try:
            ent = await client.get_entity(name)
            ents.append(ent)
        except Exception as e:
            logger.warning(f"Не удалось получить {name}: {e}")
    entities_cache = ents
    logger.info(f"Слушаем чаты/каналы: {len(ents)}")

def public_link(username: Optional[str], mid: int) -> str:
    return f"https://t.me/{username}/{mid}" if username else ""

@app.on_event("startup")
async def on_startup():
    await client.start()
    await resolve_entities()

    @client.on(events.NewMessage(chats=lambda _: entities_cache))
    async def handler(event):
        try:
            text = event.message.message or ""
            if not looks_like_request(text):
                return
            chat = await event.get_chat()
            username = getattr(chat, "username", None)
            title = getattr(chat, "title", username) or str(getattr(chat, "id", ""))
            link = public_link(username, event.id)

            msg = (
                "🔎 Запрос репетитора по английскому\n"
                f"👥 Чат: {title}\n"
                f"🧷 Сообщение #{event.id}\n"
                f"🔗 {link or '(приватный чат)'}\n\n"
                f"{norm(text)}"
            )
            # отправим в «Избранное» (Saved Messages)
            await client.send_message("me", msg)
            logger.info(f"[MATCH] {title} #{event.id} | {norm(text)[:120]}")

        except Exception as e:
            logger.exception(f"Ошибка обработчика: {e}")

    # держим соединение с Telegram в фоне
    asyncio.create_task(client.run_until_disconnected())
    logger.info("Клиент Telegram запущен.")

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"ok": True}
