# 🤖 antigravity-telegram-bridge

[![CI](https://github.com/RosyidAl/agy-to-im/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/RosyidAl/agy-to-im/actions/workflows/ci-cd.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-221-brightgreen.svg)](./tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](./LICENSE)
[![systemd](https://img.shields.io/badge/supervisor-systemd--user-lightgrey.svg)](https://systemd.io/)
[![CLI](https://img.shields.io/badge/backend-Antigravity%20%28agy%29-ff69b4.svg)](https://antigravity.google)

Chat with the [Antigravity CLI](https://antigravity.google) (`agy`) from Telegram.

> **Fork notice.** This is a customized fork of [`hah23255/agy-to-im`](https://github.com/hah23255/agy-to-im) (which itself was bootstrapped from `kimi-to-im`), maintained by **Rosyid Al Ansorri** and deployed in production on an Azure VPS. The upstream project adapted the original bridge to drive Google's `agy` CLI; **this fork goes further** — see [Customizations in this fork](#customizations-in-this-fork). Each Telegram chat gets its own `agy` project/working directory so sessions persist across messages while staying isolated from one another.

**Self-hosted · single-user · Python · systemd-supervised · webhook-ready**

[English](#english) · [Български](#български)

---

## English

### What it is
A Telegram bridge daemon for the Google Antigravity CLI (`agy`), allowing persistent, isolated code editing sessions over Telegram chats. Version: `0.2.0`.

### Customizations in this fork
The biggest change is architectural: **turn execution was moved out of the polling loop into a dedicated worker task**, so the daemon keeps fetching Telegram updates while `agy` works. Before, a long turn (up to 15 min) made the bot look dead; now new messages — including `/stop` — are seen instantly and acknowledged with live queue feedback.

| Customization | What it does |
|---|---|
| 🔀 Non-blocking daemon core | Polling loop + single turn worker (FIFO). New messages get `⏳ Queued (position #N)` feedback immediately; turns run strictly one at a time per worker. |
| 📡 Live turn monitor | The placeholder message is edited in place (throttled) with a step-by-step view of the agent's tool activity, then finalized as a summary checklist. |
| ⏻ Per-chat power toggle | `/on` / `/off`; the bot also never auto-replies to offline message dumps after a restart — it wakes only on `/start`. |
| 🧠 OKF memory layer | Optional graphify-based memory vault (`extract` → `export okf`) compiled per chat directory in the background. |
| ⌨️ More commands | `/ping`, `/model`, `/mode`, `/effort`, `/cd`, `/pwd`, `/getfile`, `/mcp`, `/skills`, `/stop` in addition to the upstream set. |
| 🧩 Model catalog | Aligned with the active `agy` models, including Gemini 3.8 effort-suffix handling (`-low/-medium/-high`). |
| 🖋️ Message formatting | Markdown → Telegram HTML with automatic plain-text fallback on parse errors. |
| 📎 Media pipeline hardening | Album/burst grouping, working media debounce (0.3 s follow-up fetch), instant placeholder **before** downloads, and stray-placeholder cleanup. |
| 🚦 Queue feedback & depth guard | Configurable queue depth from `config.json`, position feedback, per-user sliding-window rate limit. |
| 🏗️ CI/CD | GitHub Actions test matrix (Python 3.11/3.12) with automatic deployment to an Azure VPS via SSH on push to `main`. |
| ⚙️ systemd autostart opt-in | `install.sh --autostart` instead of always registering the service. |

### Features
| Feature | Status |
|---|---|
| Telegram ↔ `agy` messaging | ✅ |
| Non-blocking polling loop + dedicated turn worker | ✅ |
| Per-chat project isolation (`--new-project` / `--continue`) | ✅ |
| Inline keyboard control panel (`/settings`, `/model`, `/mode`) | ✅ |
| Live turn monitor with tool-activity progress | ✅ |
| Photo + document upload support with configurable safety routing | ✅ |
| Configurable file type allowlist (pass/warn/hold/block per category) | ✅ |
| Per-user rate limiting with sliding window | ✅ |
| Memory health gate with automatic self-restart | ✅ |
| Zombie subprocess reaping + garbage collection | ✅ |
| Multi-user FIFO turn queue with position feedback | ✅ |
| Per-chat token usage tracking (`/usage`) | ✅ |
| Webhook mode with HMAC verification | ✅ |
| Health endpoint (`/health`) and Prometheus metrics (`/metrics`) | ✅ |
| `systemd --user` service with hardening | ✅ |
| Plugin tool for `agy` (`bridge start/stop/status/logs/setup`) | ✅ |
| 221 tests (green on Linux CI) | ✅ |

### Quickstart

You need: Linux with `systemd --user`, Python ≥3.11, `uv`, a working `agy` CLI on `PATH`, and an authenticated `agy` session.

1. **Get a Telegram bot token** from [@BotFather](https://t.me/BotFather).
2. **Get your Telegram user ID** from [@userinfobot](https://t.me/userinfobot).
3. **Authenticate `agy` once** in a terminal:
   ```bash
   agy
   # complete the browser OAuth flow
   ```
4. **Install the bridge.**
   ```bash
   git clone https://github.com/RosyidAl/agy-to-im.git \
     ~/.gemini/extensions/antigravity-telegram-bridge
   cd ~/.gemini/extensions/antigravity-telegram-bridge
   ./install.sh            # add --autostart to register the systemd service
   cp config.example.json config.json
   chmod 600 config.json
   $EDITOR config.json   # fill bot_token + allowed_user_ids
   systemctl --user start antigravity-telegram-bridge.service
   ```

For the full step-by-step guide see `docs/deployment.md`. Day-to-day operations are in `docs/OPERATIONS.md`.

### Configuration
```json
{
  "telegram": {
    "bot_token": "1234567890:...",
    "allowed_user_ids": [123456789],
    "allowed_chat_ids": []
  },
  "agy": {
    "chats_root": "",
    "default_workdir": "",
    "model": "",
    "mode": "code"
  }
}
```

### Text Commands
* `/start` - Welcome message
* `/ping` - Quick latency and health check
* `/help` - Usage help
* `/status` / `/info` - Session summary
* `/settings` - Inline control panel (model, mode, reset)
* `/model` - Select or view model override
* `/mode` - Switch between Code and Plan mode
* `/effort` - Set reasoning effort (low/medium/high/max)
* `/cd` - Change working directory for this chat
* `/pwd` - Show current working directory
* `/on` / `/off` - Activate / deactivate the bot for this chat
* `/stop` - Abort the active `agy` turn
* `/reset` - Start a fresh `agy` project for this chat
* `/files` - List inbox files (`/files clean` purges them)
* `/image on|off` - Toggle photo processing
* `/getfile` - Download a file from the workspace
* `/mcp` - View and manage MCP servers and tools
* `/skills` - List and inspect available skills
* `/queue` - Queue status
* `/usage` - Token usage for this chat (last turn / today / total)

### Architecture Mapping
```mermaid
flowchart LR
    User([📱 You on Telegram])
    TG[Telegram Bot API]
    Poll["polling loop<br/>getUpdates · auth · commands<br/>(never blocks on turns)"]
    Queue[("TurnQueue<br/>FIFO")]
    Worker["turn worker<br/>one agy turn at a time"]
    Agy[agy CLI subprocess<br/>per turn]
    State[(state.json<br/>chat → project dir)]
    Health[health + metrics<br/>:9100]

    User -->|message| TG
    TG -->|long-poll / webhook| Poll
    Poll -->|submit| Queue
    Queue -->|FIFO| Worker
    Worker -->|agy -p prompt --continue/--new-project| Agy
    Agy -->|stream events / reply| Worker
    Worker -->|sendMessage / editMessageText| TG
    TG -->|reply + ⏳ progress| User
    Poll <-->|read/write| State
    Worker -->|/health /metrics| Health
```

### Fault Handling Manual
| Status / Error | Root Cause | Mitigation |
| :--- | :--- | :--- |
| `ModuleNotFoundError: No module named 'httpx'` | The daemon was started using the global python binary instead of the local virtual environment `.venv`. | Ensure the systemd unit or script runs within the project's virtual environment (`.venv/bin/python`). |
| Unauthorized / Token Rejected | The Telegram Bot API token is incorrect, expired, or blocked. | Check that `config.json` contains a valid bot token from BotFather. |
| Access Denied (User ID Whitelist) | The Telegram user ID sending messages is not in the whitelisted `allowed_user_ids` config array. | Add your user ID obtained from userinfobot to `config.json` and restart the daemon. |
| `systemd --user` service fails | The systemd user daemon cannot start the service (commonly due to missing user lingering). | Run `loginctl enable-linger` as root/user to allow user session daemons to run when logged out. |
| Webhook verification fails | The incoming webhook payloads are rejected due to mismatched HMAC signatures. | Check that the webhook endpoint config matches the active bot token, and no proxy is rewriting payload bodies. |
| Chat directory traversal error | A request attempted to read/write paths outside of the whitelisted `chats_root`. | Clean state database `state.json` and ensure no inputs bypass path validation hooks in `src/state.py`. |

### Common Issues & Golden Rules
* **Golden Rule 1: Default-Deny Access Control.** Always whitelists allowed user/chat IDs. Never run public-facing bridges without a strict filter list to prevent code execution by unauthorized users.
* **Golden Rule 2: Virtual Environment Isolation.** Always execute the daemon and tools inside `.venv/` to prevent package version conflicts and runtime import issues.
* **Golden Rule 3: Webhook HMAC Protection.** Under webhook deployment, always enforce HMAC-SHA256 signature verification on incoming requests to prevent mock payload injection.
* **Golden Rule 4: Systemd User session lingering.** Always verify that `loginctl enable-linger` is active for the target user, guaranteeing the bridge daemon persists across terminal logouts.

---

## Български

### Какво представлява
Телеграм мост (daemon) за конзолния инструмент Google Antigravity CLI (`agy`), позволяващ устойчиви, изолирани сесии за редактиране на код през чатове в Telegram. Версия: `0.2.0`.

> **Забележка за форка.** Това е персонализиран форк на [`hah23255/agy-to-im`](https://github.com/hah23255/agy-to-im), поддържан от **Rosyid Al Ansorri**. Основната промяна е архитектурна: изпълнението на turn-ове е изнесено в отделен worker, така че daemon-ът продължава да чете съобщения от Telegram докато `agy` работи, а новите съобщения получават мигновена обратна връзка `⏳ Queued (position #N)`.

### Възможности
| Функция | Статус |
|---|---|
| Telegram ↔ `agy` кореспонденция | ✅ |
| Неблокиращ polling loop + отделен turn worker | ✅ |
| Изолация на проекти за всеки отделен чат (`--new-project` / `--continue`) | ✅ |
| Контролен панел с вградена клавиатура (`/settings`, `/model`, `/mode`) | ✅ |
| Монитор на активността в реално време по време на turn | ✅ |
| Поддръжка на качване на снимки и документи | ✅ |
| Многопотребителска FIFO опашка за изчакване с позиция | ✅ |
| Проследяване на потреблението на токени за чат (`/usage`) | ✅ |
| Webhook режим с HMAC верификация | ✅ |
| Здравна точка (`/health`) и Prometheus метрики (`/metrics`) | ✅ |
| `systemd --user` услуга със защити | ✅ |
| Инструмент-плъгин за `agy` (`bridge start/stop/status/logs/setup`) | ✅ |
| 221 теста (зелени на Linux CI) | ✅ |

### Бърз старт

Необходими са: Linux със `systemd --user`, Python ≥3.11, `uv`, работещ `agy` в системния път (`PATH`) и оторизирана сесия в `agy`.

1. **Вземете Telegram бот токен** от [@BotFather](https://t.me/BotFather).
2. **Вземете Вашия Telegram ID** от [@userinfobot](https://t.me/userinfobot).
3. **Оторизирайте `agy` веднъж** в конзолата:
   ```bash
   agy
   # завършете браузърния OAuth процес
   ```
4. **Инсталирайте моста.**
   ```bash
   git clone https://github.com/RosyidAl/agy-to-im.git \
     ~/.gemini/extensions/antigravity-telegram-bridge
   cd ~/.gemini/extensions/antigravity-telegram-bridge
   ./install.sh            # добавете --autostart за systemd услуга
   cp config.example.json config.json
   chmod 600 config.json
   $EDITOR config.json   # попълнете bot_token + allowed_user_ids
   systemctl --user start antigravity-telegram-bridge.service
   ```

За пълно ръководство вижте `docs/deployment.md`. Ежедневната експлоатация е описана в `docs/OPERATIONS.md`.

### Конфигурация
```json
{
  "telegram": {
    "bot_token": "1234567890:...",
    "allowed_user_ids": [123456789],
    "allowed_chat_ids": []
  },
  "agy": {
    "chats_root": "",
    "default_workdir": "",
    "model": "",
    "mode": "code"
  }
}
```

### Текстови команди
* `/start` - Добре дошли
* `/ping` - Бърза проверка на латентност и здраве
* `/help` - Помощ при ползване
* `/status` / `/info` - Обобщение на сесията
* `/settings` - Контролен панел (модел, режим, ресет)
* `/model` - Избор или преглед на модел
* `/mode` - Превключване между Code и Plan режим
* `/effort` - Задаване на усилие за разсъждение (low/medium/high/max)
* `/cd` - Смяна на работната директория за чата
* `/pwd` - Показване на текущата работна директория
* `/on` / `/off` - Активиране / деактивиране на бота за чата
* `/stop` - Прекъсване на активния `agy` turn
* `/reset` - Стартиране на нов `agy` проект за този чат
* `/files` - Списък с файлове (`/files clean` ги изтрива)
* `/image on|off` - Включване на обработка на снимки
* `/getfile` - Изтегляне на файл от работното пространство
* `/mcp` - Преглед и управление на MCP сървъри и инструменти
* `/skills` - Списък с налични умения
* `/queue` - Статус на опашката
* `/usage` - Потребление на токени за този чат (последен turn / днес / общо)

### Архитектурно описание
```mermaid
flowchart LR
    User([📱 Вие в Telegram])
    TG[Telegram Bot API]
    Poll["polling loop<br/>getUpdates · auth · команди<br/>(не блокира при turn)"]
    Queue[("TurnQueue<br/>FIFO")]
    Worker["turn worker<br/>един agy turn едновременно"]
    Agy[agy CLI подпроцес<br/>за всяка стъпка]
    State[(state.json<br/>chat → папка на проекта)]
    Health[health + metrics<br/>:9100]

    User -->|съобщение| TG
    TG -->|long-poll / webhook| Poll
    Poll -->|submit| Queue
    Queue -->|FIFO| Worker
    Worker -->|agy -p prompt --continue/--new-project| Agy
    Agy -->|събития / отговор| Worker
    Worker -->|sendMessage / editMessageText| TG
    TG -->|отговор + ⏳ прогрес| User
    Poll <-->|четене/запис| State
    Worker -->|/health /metrics| Health
```

### Ръководство за отстраняване на неизправности
| Статус / Грешка | Причина | Решение |
| :--- | :--- | :--- |
| `ModuleNotFoundError: No module named 'httpx'` | Демонът е стартиран с глобалния python, вместо с локалната виртуална среда `.venv`. | Уверете се, че systemd услугата или скриптът стартират чрез виртуалната среда (`.venv/bin/python`). |
| Невалиден токен (Rejected) | Токенът за Telegram Bot API е грешен, изтекъл или блокиран. | Проверете дали `config.json` съдържа валидния токен, получен от BotFather. |
| Отказан достъп (Whitelist) | Потребителят, изпращащ съобщения, не е в белия списък `allowed_user_ids`. | Добавете вашия ID (получен от userinfobot) в `config.json` и рестартирайте процеса. |
| Срив на `systemd --user` | Потребителският systemd не може да стартира услугата (обикновено поради липса на linger). | Изпълнете `loginctl enable-linger` като root/user, за да позволите на потребителските услуги да работят след изход. |
| Webhook верификацията се проваля | Входящите webhook съобщения се отхвърлят поради несъответствие в HMAC подписите. | Проверете дали конфигурацията съответства на активния токен и че няма прокси, което променя тялото на заявката. |
| Грешка в пътя на сесията | Опит за четене/запис извън разрешената директория `chats_root`. | Изчистете базата данни `state.json` и се уверете, че промените се правят съгласно правилата в `src/state.py`. |

### Чести проблеми и Златни правила
* **Златно правило 1: Достъп с отказ по подразбиране.** Винаги задавайте бели списъци за потребители. Никога не стартирайте публични ботове без стриктни филтри за достъп за избягване на злоупотреби.
* **Златно правило 2: Изолация на виртуалната среда.** Винаги стартирайте демона и инструментите в `.venv/`, за да избегнете конфликти в библиотеките и грешки при импортиране.
* **Златно правило 3: Webhook HMAC защита.** При webhook разгръщане винаги проверявайте подписите HMAC-SHA256 на входящите заявки, за да се предпазите от фалшиви заявки.
* **Златно правило 4: Устойчивост на потребителските сесии.** Винаги проверявайте дали е активиран linger режимът (`loginctl enable-linger`) за съответния потребител, за да работи ботът и след изход от терминала.
