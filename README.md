# Telegram-бот с локальной LLM (Ollama + Qwen3)

Локальный Telegram-бот: **Qwen3-14B-Instruct** через Ollama (CUDA + CPU).
Модель частично выгружается на GPU, остальное считает CPU, веса живут в RAM —
задействованы все ресурсы машины. Стриминг ответов, история диалогов,
работа в личке и группах.

---

## Возможности

- 💬 Диалог в личке со стримингом ответа (сообщение обновляется по мере генерации)
- 👥 Группы: `/ask <вопрос>`, ответ на сообщение бота или упоминание `@bot`
- 🧠 История диалога на каждый чат (сохраняется в `history.json`)
- 🧹 `/clear` — очистка истории, `/help` — справка
- 🔁 Автоматические ретраи при обрывах сети до Telegram
- 🚀 systemd user-сервисы: автостарт при входе в систему

---

## Требования

- **Linux** (x86_64), systemd
- **Python 3.10+**
- `curl`, `zstd` (или `tar` с поддержкой `--zstd`)
- NVIDIA GPU — опционально (без GPU модель полностью считается на CPU)
- Модель `qwen3:14b` — ~9.3 GB на диске, ~10 GB RAM

---

## Быстрый старт

```bash
# 1. Склонировать проект
git clone <url-репозитория> tg-bot
cd tg-bot

# 2. Создать конфигурацию
cp .env.example .env
nano .env        # вписать BOT_TOKEN от @BotFather

# 3. Установить и запустить
./install.sh
```

`install.sh` сам:
1. создаст venv и установит Python-зависимости;
2. установит Ollama в `~/.local` (если его нет);
3. создаст `.env` из примера (если его нет);
4. установит и запустит systemd user-сервисы `ollama` и `tg-bot`;
5. скачает модель `qwen3:14b` (если её ещё нет).

Флаги:

```bash
./install.sh --no-model      # не качать модель
./install.sh --no-services   # не ставить systemd-юниты
```

### Запуск без systemd

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # вписать токен
.venv/bin/python bot.py
```

---

## Конфигурация (`.env`)

| Параметр | По умолчанию | Описание |
|---|---|---|
| `BOT_TOKEN` | — | Токен от @BotFather (обязательно) |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Адрес Ollama |
| `MODEL` | `qwen3:14b` | Модель Ollama |
| `TELEGRAM_API_IPS` | *(пусто)* | Запасные IP `api.telegram.org` через запятую — только если сеть блокирует Telegram |
| `SYSTEM_PROMPT` | … | Системный промпт |
| `NUM_CTX` | `16384` | Размер контекста |
| `NUM_GPU` | `-1` | Слоёв на GPU: `-1` = авто, `0` = только CPU, `N` = вручную |
| `NUM_PREDICT` | `2048` | Максимум токенов в ответе |
| `TEMPERATURE` | `0.7` | Температура |
| `TOP_P` | `0.8` | Top-p |
| `THINK` | `false` | Режим размышлений Qwen3 (`true` = глубже, но медленнее) |
| `MAX_HISTORY_MESSAGES` | `30` | Сколько сообщений помнит бот на каждый чат |
| `EDIT_INTERVAL` | `1.5` | Интервал обновления стриминга (сек) |
| `REQUEST_TIMEOUT` | `600` | Таймаут запроса к Ollama (сек) |

Дополнительные переменные для сервера Ollama (используются `install.sh`):

| Параметр | Рекомендация | Описание |
|---|---|---|
| `OLLAMA_FLASH_ATTENTION` | `1` | Flash Attention (только CUDA-совместимые GPU) |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Квантование KV-кеша — экономит VRAM |
| `OLLAMA_CONTEXT_LENGTH` | `16384` | Контекст по умолчанию на сервере |

---

## Подбор `NUM_GPU` под видеокарту

Модель весит 9.3 GB (Q4_K_M), в 6 GB VRAM помещается только часть слоёв.

| VRAM | Модель | Рекомендация |
|---|---|---|
| 6 GB (RTX A2000) | qwen3:14b | `NUM_GPU=19`, `NUM_CTX=16384`, `OLLAMA_KV_CACHE_TYPE=q8_0` (~4.5 ток/с) |
| 8–12 GB | qwen3:14b | `NUM_GPU=-1` (авто, модель почти целиком на GPU) |
| 16+ GB | qwen3:14b | `NUM_GPU=-1`, `NUM_CTX=32768` |
| Нет GPU | qwen3:14b | `NUM_GPU=0` (2–3 ток/с на 8+ ядрах) |
| Нет GPU / мало RAM | qwen3:8b | `MODEL=qwen3:8b`, `NUM_GPU=-1` |

Если Ollama падает с `cudaMalloc failed: out of memory` — уменьшите `NUM_GPU`.

---

## Управление сервисами

```bash
systemctl --user status ollama      # статус модели
systemctl --user status tg-bot      # статус бота
systemctl --user restart tg-bot     # перезапуск бота (после правки .env)
journalctl --user -u tg-bot -f      # логи бота
journalctl --user -u ollama -f      # логи Ollama
```

Автостарт при загрузке машины без логина (нужен sudo):

```bash
sudo loginctl enable-linger "$USER"
```

---

## Команды бота

| Команда | Где | Описание |
|---|---|---|
| `/start` | везде | Приветствие |
| `/help` | везде | Справка |
| `/clear` | везде | Очистить историю диалога |
| `/ask <вопрос>` | группы | Принудительный запрос к боту |
| простое сообщение | личка | Обычный диалог |
| ответ на сообщение бота / `@bot` | группы | Обращение к боту |

---

## Структура проекта

```
.
├── bot.py                # код бота
├── install.sh            # установщик
├── requirements.txt      # python-зависимости
├── .env.example          # шаблон конфигурации
├── .gitignore            # .env, history.json, .venv — не в git
├── deploy/
│   ├── ollama.service    # шаблон systemd-юнита Ollama
│   └── tg-bot.service    # шаблон systemd-юнита бота
└── README.md
```

Локально (не в git): `.env` (секреты), `.venv/`, `history.json`.

---

## Сеть и блокировки Telegram

Если сеть блокирует `api.telegram.org`:

1. Укажите в `.env` запасные IP:
   ```
   TELEGRAM_API_IPS=149.154.167.220,149.154.166.110
   ```
2. Бот ретраит поллинг бесконечно и переживает обрывы.
3. Надёжнее всего — системный VPN на машине.

---

## Смена модели

```bash
ollama pull qwen3:8b      # быстрее, меньше качество
ollama pull qwen3:32b     # умнее, медленнее, больше RAM

# в .env:
#   MODEL=qwen3:8b
#   NUM_GPU=-1

systemctl --user restart tg-bot
```

---

## Устранение неполадок

| Симптом | Решение |
|---|---|
| Бот не отвечает | `journalctl --user -u tg-bot -n 50` |
| `cudaMalloc failed: out of memory` | Уменьшите `NUM_GPU` или `NUM_CTX` |
| `Timed out` / сеть Telegram | VPN или `TELEGRAM_API_IPS` |
| Модель не грузится | `ollama list`; `ollama pull qwen3:14b` |
| Нет прав на venv | Удалите `.venv` и запустите `./install.sh` заново |
