#!/usr/bin/env python3
"""Telegram-бот с локальной LLM через Ollama.

Настройки берутся из .env (или переменных окружения).
Запуск: .venv/bin/python bot.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

import httpx
import telegram.error
from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
HISTORY_PATH = BASE_DIR / "history.json"

DEFAULTS: dict[str, Any] = {
    "BOT_TOKEN": "",
    "OLLAMA_URL": "http://127.0.0.1:11434",
    "MODEL": "qwen3:14b",
    # Запасные IP для api.telegram.org (через запятую). Пусто = обычный DNS.
    # Нужно только в сетях, где DNS/сеть блокирует Telegram.
    "TELEGRAM_API_IPS": "",
    "SYSTEM_PROMPT": (
        "Ты — дружелюбный и полезный ассистент в Telegram. "
        "Отвечай всегда на языке собеседника (по умолчанию — на русском). "
        "Отвечай кратко и по делу, если не просят подробностей."
    ),
    "NUM_CTX": 16384,
    # -1 = автоматически (Ollama сам распределит слои между GPU и CPU)
    "NUM_GPU": -1,
    "NUM_PREDICT": 2048,
    "TEMPERATURE": 0.7,
    "TOP_P": 0.8,
    "THINK": False,
    "MAX_HISTORY_MESSAGES": 30,
    "EDIT_INTERVAL": 1.5,
    "REQUEST_TIMEOUT": 600,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    env_from_file: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env_from_file[k.strip()] = v.strip().strip('"').strip("'")

    int_keys = {"NUM_CTX", "NUM_GPU", "NUM_PREDICT", "MAX_HISTORY_MESSAGES", "REQUEST_TIMEOUT"}
    float_keys = {"TEMPERATURE", "TOP_P", "EDIT_INTERVAL"}
    bool_keys = {"THINK"}

    # Приоритет: .env > переменные окружения (чтобы чужие переменные
    # вроде MODEL из shell-профиля не ломали настройки бота).
    for key in cfg:
        if key in env_from_file:
            val: Any = env_from_file[key]
        elif key in os.environ:
            val = os.environ[key]
        else:
            continue
        if key in int_keys:
            cfg[key] = int(val)
        elif key in float_keys:
            cfg[key] = float(val)
        elif key in bool_keys:
            cfg[key] = str(val).lower() in ("1", "true", "yes", "on")
        else:
            cfg[key] = val
    return cfg


CFG = load_config()

# Подменяем DNS-резолвинг api.telegram.org на рабочие IP:
# в этой сети DNS отдаёт заблокированный адрес.
_TG_API_HOST = "api.telegram.org"
_TG_API_IPS = [
    ip.strip() for ip in str(CFG["TELEGRAM_API_IPS"]).split(",") if ip.strip()
]
_orig_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    if host == _TG_API_HOST and _TG_API_IPS:
        results = []
        for ip in _TG_API_IPS:
            try:
                results.extend(_orig_getaddrinfo(ip, *args, **kwargs))
            except Exception:
                continue
        return results
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo


class RetryHTTPXRequest(HTTPXRequest):
    """HTTPXRequest с ретраями — сеть до Telegram нестабильна.

    Для getUpdates (поллинг) ретраим бесконечно: бот переживает
    длительные обрывы сети. Для остальных запросов — ограниченное
    число попыток, чтобы не подвешивать обработку сообщений.
    """

    def __init__(self, *args, retries: int = 5, retry_delay: float = 2.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._retries = retries
        self._retry_delay = retry_delay

    async def do_request(self, *args, **kwargs):
        url = str(args[0]) if args else ""
        is_polling = "getUpdates" in url
        max_attempts = None if is_polling else self._retries + 1
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            try:
                return await super().do_request(*args, **kwargs)
            except (telegram.error.TimedOut, httpx.TransportError):
                attempt += 1
                if max_attempts is not None and attempt >= max_attempts:
                    raise
                delay = min(self._retry_delay * attempt, 10.0)
                await asyncio.sleep(delay)


# История диалогов: chat_id -> [{"role": "user"/"assistant", "content": "..."}]
_history: dict[int, list[dict[str, str]]] = {}


def load_history() -> None:
    global _history
    if HISTORY_PATH.exists():
        try:
            raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            _history = {int(k): v for k, v in raw.items() if isinstance(v, list)}
        except Exception:
            _history = {}


def save_history() -> None:
    try:
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_history, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def trim_history(chat_id: int) -> None:
    msgs = _history.get(chat_id, [])
    max_messages = int(CFG["MAX_HISTORY_MESSAGES"])
    while len(msgs) > max_messages:
        msgs.pop(0)
    total = sum(len(m["content"]) for m in msgs)
    while total > 24000 and len(msgs) > 2:
        total -= len(msgs[0]["content"])
        msgs.pop(0)
    _history[chat_id] = msgs


async def safe_edit(message: Any, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def generate_reply(
    update: Update, chat_id: int, user_text: str
) -> None:
    """Генерирует ответ со стримингом в редактируемое сообщение."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": str(CFG["SYSTEM_PROMPT"])}
    ]
    messages.extend(_history.get(chat_id, []))
    messages.append({"role": "user", "content": user_text})

    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    placeholder = await update.effective_message.reply_text("⏳ Думаю…")

    full: list[str] = []
    last_edit = 0.0
    interval = float(CFG["EDIT_INTERVAL"])
    answer = ""

    try:
        timeout = httpx.Timeout(float(CFG["REQUEST_TIMEOUT"]), connect=15.0)
        options: dict[str, Any] = {
            "num_ctx": int(CFG["NUM_CTX"]),
            "num_predict": int(CFG["NUM_PREDICT"]),
            "temperature": float(CFG["TEMPERATURE"]),
            "top_p": float(CFG["TOP_P"]),
        }
        # num_gpu: -1 = авто (не передаём), 0 = только CPU, N = слоёв на GPU
        if int(CFG["NUM_GPU"]) >= 0:
            options["num_gpu"] = int(CFG["NUM_GPU"])
        payload = {
            "model": CFG["MODEL"],
            "messages": messages,
            "stream": True,
            "think": bool(CFG["THINK"]),
            "options": options,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f'{str(CFG["OLLAMA_URL"]).rstrip("/")}/api/chat', json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    piece = chunk.get("message", {}).get("content")
                    if piece:
                        full.append(piece)
                        now = time.monotonic()
                        if now - last_edit >= interval:
                            last_edit = now
                            await safe_edit(placeholder, "".join(full) + " ▌")
        answer = "".join(full).strip() or "🤖 Пустой ответ модели."
        await safe_edit(placeholder, answer)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(placeholder, f"⚠️ Ошибка генерации: {exc}")
        return

    hist = _history.setdefault(chat_id, [])
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": answer})
    trim_history(chat_id)
    save_history()


def is_mentioned(message: Any, bot_username: str) -> bool:
    text = message.text or ""
    return f"@{bot_username}" in text


# ---------- Обработчики команд ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я локальный ИИ-ассистент (Qwen3-14B на Ollama).\n\n"
        "Просто напишите сообщение — отвечу. "
        "В группах используйте /ask <вопрос> или упоминание @бот.\n\n"
        "/help — справка\n"
        "/clear — очистить историю диалога"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>Локальный ИИ-бот</b>\n\n"
        "• В личке: просто пишите сообщение.\n"
        "• В группе: /ask &lt;вопрос&gt;, ответ на сообщение бота "
        "или упоминание @бот.\n"
        "• /clear — очистить историю диалога.\n\n"
        "Модель: Qwen3-14B (Ollama, GPU + CPU).",
        parse_mode="HTML",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _history.pop(chat_id, None)
    save_history()
    await update.message.reply_text("🧹 История диалога очищена.")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <вопрос> — принудительный запрос к боту (в группах)."""
    if not context.args:
        await update.message.reply_text("Использование: /ask <ваш вопрос>")
        return
    user_text = " ".join(context.args)
    await generate_reply(update, update.effective_chat.id, user_text)


# ---------- Обработчики сообщений ----------

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    await generate_reply(update, update.effective_chat.id, update.message.text)


async def handle_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    bot_username = (context.bot.username or "").lower()
    text = update.message.text

    # Отвечаем в группе, если: упомянули бота, ответили на сообщение бота
    if is_mentioned(update.message, context.bot.username or ""):
        # убираем упоминание из текста запроса
        cleaned = text.replace(f"@{context.bot.username}", "").strip()
        if not cleaned:
            cleaned = "Привет! Чем помочь?"
        await generate_reply(update, update.effective_chat.id, cleaned)
        return

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        if update.message.reply_to_message.from_user.id == context.bot.id:
            await generate_reply(update, update.effective_chat.id, text)
            return


def main() -> None:
    token = str(CFG.get("BOT_TOKEN", "")).strip()
    if not token:
        print("❌ Не задан BOT_TOKEN. Добавьте его в файл .env и запустите снова.")
        raise SystemExit(1)

    load_history()

    request = RetryHTTPXRequest(
        connect_timeout=10.0,
        read_timeout=90.0,
        write_timeout=30.0,
        pool_timeout=10.0,
        retries=5,
        retry_delay=2.0,
    )
    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_private,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            handle_group,
        )
    )

    print(
        f"✅ Бот запущен. Модель: {CFG['MODEL']}, "
        f"Ollama: {CFG['OLLAMA_URL']}, контекст: {CFG['NUM_CTX']}"
    )
    # Короткий long-poll (10с) + бесконечные ретраи (в т.ч. bootstrap getMe) —
    # переживает нестабильную сеть до Telegram.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=10,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
