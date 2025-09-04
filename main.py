import os, re, asyncio, logging
from typing import List, Optional
from fastapi import FastAPI
from telethon import events
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.errors import FloodWaitError  # <-- важно: ловим лимиты Telegram
import uvicorn

# ---------- настройки через окружение ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_STRING_SESSION = os.environ["TG_STRING_SESSION"]
CHANNELS = os.getenv("CHANNELS", "").strip()
MINUS_WORDS = os.getenv("MINUS_WORDS", "")

# ---------- ключевые группы ----------
LANG_PATTERNS = [
    r"\bанглийск\w*\b", r"\bангл\b", r"\benglish\b", r"\bIELTS\b", r"\bTOEFL\b",
    r"\bиспанск\w*\b", r"\bspanish\b", r"\bDELE\b",
    r"\bитальянск\w*\b", r"\bitalian\b", r"\bCELI\b", r"\bCILS\b",
    r"\bкитайск\w*\b", r"\bchinese\b", r"\bHSK\b",
]

ROLE_PATTERNS = [
    r"\bрепетитор\w*\b",
    r"\bпреподавател[ья]\w*\b",
    r"\bучител[ья]\w*\b",
]

SCHOOL_PATTERNS = [
    r"\bонлайн[- ]?школ\w*\b",
    r"\bкурсы?\b", r"\bзанятия\b", r"\bурок(?:и|ов)?\b",
    r"\bподготовк\w*\b",
]

HINT_PATTERNS = [
    r"\bищу\b", r"\bнужен\b", r"\bнужна\b", r"\bнужно\b",
    r"\bпорекомендуйте\b", r"\bпосоветуйте\b", r"\bкто\s+может\s+посоветовать\b",
    r"\brecommend\b", r"\blooking\s+for\b", r"\bneed\b",
]

def _rx_or(parts: List[str]) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE) if parts else re.compile(r"^\b$")

RX_LANG   = _rx_or(LANG_PATTERNS)
RX_ROLE   = _rx_or(ROLE_PATTERNS)
RX_SCHOOL = _rx_or(SCHOOL_PATTERNS)
RX_HINT   = _rx_or(HINT_PATTERNS)

MINUS = [w.strip() for w in MINUS_WORDS.split(",") if w.strip()]
RX_MINUS = _rx_or([re.escape(w) for w in MINUS]) if MINUS else None

def norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u200b", "")).strip()

def looks_like_request(text: str) -> bool:
    t = norm(text)
    if not t:
        return False
    if RX_MINUS and RX_MINUS.search(t):
        return False
    if not RX_LANG.search(t):
        return False
    if not (RX_ROLE.search(t) or RX_SCHOOL.search(t)):
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

allowed_chat_ids: Optional[set[int]] = None

async def resolve_entities():
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

# ---------- безопасная отправка в Избранное ----------
# (ограничим частоту, обработаем FloodWait и непредвиденные ошибки)
_send_lock = asyncio.Lock()  # простая серилизация отправок

async def safe_send_to_saved(text: str, max_retries: int = 5):
    attempt = 0
    async with _send_lock:  # по одному сообщению за раз
        while True:
            try:
                res = await client.send_message("me", text)
                # лёгкий троттлинг между отправками
                await asyncio.sleep(0.4)
                return res
            except FloodWaitError as e:
                wait_s = int(getattr(e, "seconds", 1)) or 1
                logger.warning(f"[FLOOD] Telegram просит подождать {wait_s}s — ждём…")
                await asyncio.sleep(wait_s + 1)
            except Exception as e:
                attempt += 1
                if attempt >= max_retries:
                    logger.exception(f"[SEND FAIL] Не удалось отправить после {attempt} попыток: {e}")
                    return None
                backoff = 2 * attempt
                logger.warning(f"[SEND RETRY] Ошибка отправки, пробуем через {backoff}s: {e}")
                await asyncio.sleep(backoff)

@app.on_event("startup")
async def on_startup():
    await client.start()
    await resolve_entities()

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
                "🔎 (новое) Запрос репетитора по языкам\n"
                f"👥 Чат: {title}\n"
                f"🧷 Сообщение #{event.id}\n"
                f"🔗 {link or '(приватный чат)'}\n\n"
                f"{norm(text)}"
            )
            await safe_send_to_saved(msg)
            logger.info(f"[MATCH] {title} #{event.id} | {norm(text)[:120]}")
        except Exception as e:
            logger.exception(f"Ошибка обработчика: {e}")

    asyncio.create_task(client.run_until_disconnected())
    logger.info("Клиент Telegram запущен.")

@app.get("/")
@app.head("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
@app.head("/health")
async def health():
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
