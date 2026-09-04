#!/usr/bin/env bash
# ============================================================
#  Установщик Telegram-бота с локальной LLM (Ollama + Qwen3)
#  Запуск:  ./install.sh [--no-model] [--no-services]
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
VENV_DIR="$PROJECT_DIR/.venv"

SKIP_MODEL=0
SKIP_SERVICES=0
for arg in "$@"; do
    case "$arg" in
        --no-model)    SKIP_MODEL=1 ;;
        --no-services) SKIP_SERVICES=1 ;;
        -h|--help)
            echo "Использование: ./install.sh [--no-model] [--no-services]"
            echo "  --no-model     не скачивать модель"
            echo "  --no-services  не устанавливать systemd-юниты"
            exit 0 ;;
        *) echo "Неизвестный аргумент: $arg" >&2; exit 1 ;;
    esac
done

info()  { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- Python ----------
command -v python3 >/dev/null || fail "python3 не найден. Установите python3 (>= 3.10)."
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Нужен python3 >= 3.10 (найдено $PY_MAJOR.$PY_MINOR)"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "Создаю виртуальное окружение..."
    python3 -m venv "$VENV_DIR"
fi
info "Устанавливаю Python-зависимости..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
ok "Python-окружение готово"

# ---------- Ollama (user-space) ----------
if ! command -v ollama >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/ollama" ]; then
    info "Ollama не найден — устанавливаю в пользовательское пространство..."
    command -v curl >/dev/null || fail "Нужен curl."
    OLLAMA_TAG=$(curl -fsSL https://api.github.com/repos/ollama/ollama/releases/latest \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
    OLLAMA_TARBALL="/tmp/ollama-${OLLAMA_TAG}.tar.zst"
    curl -fL --retry 3 -o "$OLLAMA_TARBALL" \
        "https://github.com/ollama/ollama/releases/download/${OLLAMA_TAG}/ollama-linux-amd64.tar.zst"
    mkdir -p "$HOME/.local/lib/ollama" "$HOME/.local/bin"
    if command -v zstd >/dev/null 2>&1; then
        zstd -dc "$OLLAMA_TARBALL" | tar -x -C "$HOME/.local/lib/ollama"
    elif tar --zstd -tf "$OLLAMA_TARBALL" >/dev/null 2>&1; then
        tar --zstd -xf "$OLLAMA_TARBALL" -C "$HOME/.local/lib/ollama"
    else
        rm -f "$OLLAMA_TARBALL"
        fail "Нужен zstd (или tar с поддержкой --zstd). Установите zstd и повторите."
    fi
    rm -f "$OLLAMA_TARBALL"
    ln -sf "$HOME/.local/lib/ollama/bin/ollama" "$HOME/.local/bin/ollama"
    ok "Ollama $OLLAMA_TAG установлен в ~/.local"
else
    ok "Ollama уже установлен: $(command -v ollama || echo "$HOME/.local/bin/ollama")"
fi

# ---------- .env ----------
if [ ! -f "$ENV_FILE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "Создан $ENV_FILE из .env.example"
else
    info "$ENV_FILE уже существует — не трогаю"
fi

# ---------- systemd-юниты ----------
if [ "$SKIP_SERVICES" -eq 0 ]; then
    info "Устанавливаю systemd user-юниты..."
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"

    # Доп. переменные окружения для ollama.service из .env
    EXTRA_ENV=""
    for var in OLLAMA_FLASH_ATTENTION OLLAMA_KV_CACHE_TYPE OLLAMA_CONTEXT_LENGTH; do
        val=$(grep -E "^${var}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
        if [ -n "$val" ]; then
            EXTRA_ENV="${EXTRA_ENV}Environment=${var}=${val}"$'\n'
        fi
    done
    awk -v extra="$EXTRA_ENV" '
        { gsub(/@@OLLAMA_EXTRA_ENV@@/, extra); print }
    ' "$PROJECT_DIR/deploy/ollama.service" > "$SYSTEMD_DIR/ollama.service"
    sed -e "s|@@PROJECT_DIR@@|$PROJECT_DIR|g" \
        "$PROJECT_DIR/deploy/tg-bot.service" > "$SYSTEMD_DIR/tg-bot.service"

    systemctl --user daemon-reload
    systemctl --user enable --now ollama.service
    ok "Сервис ollama.service установлен и запущен"

    # Ждём готовности API
    for _ in $(seq 1 30); do
        curl -sf "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1 && break
        sleep 1
    done
else
    info "Пропускаю установку systemd-юнитов (--no-services)"
fi

# ---------- Модель ----------
# Модель берём из .env (не из переменной окружения MODEL — она может быть занята)
MODEL=$(grep -E "^MODEL=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
[ -n "$MODEL" ] || MODEL="qwen3:14b"
if [ "$SKIP_MODEL" -eq 0 ]; then
    OLLAMA_BIN="$(command -v ollama || echo "$HOME/.local/bin/ollama")"
    if [ -x "$OLLAMA_BIN" ]; then
        if ! "$OLLAMA_BIN" list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
            info "Скачиваю модель $MODEL (может занять время)..."
            "$OLLAMA_BIN" pull "$MODEL"
            ok "Модель $MODEL готова"
        else
            ok "Модель $MODEL уже есть"
        fi
    else
        warn "Ollama недоступен — модель не скачана. Выполните позже: ollama pull $MODEL"
    fi
else
    info "Пропускаю загрузку модели (--no-model)"
fi

# ---------- Запуск бота ----------
TOKEN=$(grep -E "^BOT_TOKEN=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
if [ -z "$TOKEN" ]; then
    warn "BOT_TOKEN пуст. Отредактируйте $ENV_FILE и запустите:"
    warn "    systemctl --user restart tg-bot"
else
    systemctl --user enable --now tg-bot.service
    ok "Бот запущен"
fi

cat <<EOF

Готово!
  Конфигурация: $ENV_FILE
  Логи бота:    journalctl --user -u tg-bot -f
  Логи модели:  journalctl --user -u ollama -f

Автозапуск при загрузке машины (опционально, нужен sudo):
  sudo loginctl enable-linger "$USER"
EOF
