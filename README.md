# mybot — Telegram-бот для портала Битрикс24

Сотрудник пишет боту обычным текстом, например:

> выстави счёт Русирке как в прошлый раз за поддержку, только месяц поменяй

Бот:
1. проверяет, что пишет сотрудник портала: Telegram-логин или ID должен быть в поле профиля `UF_USR_1794934830441` («telega»);
2. с помощью языковой модели (OpenAI или Claude) разбирает просьбу и через вебхук портала находит компанию, её прошлый счёт и строки в нём;
3. показывает черновик нового счёта с кнопками **Создать / Отмена**;
4. после подтверждения создаёт смарт-счёт: компания, контакт, наши реквизиты, реквизиты клиента и сделка берутся из образца, дата — сегодня, срок оплаты такой же, как у образца;
5. формирует печатную форму по шаблону Битрикса и присылает её в чат: PDF, а если портал не умеет конвертировать — DOCX.

## Как устроено

```
Telegram ──HTTPS──► nginx (b-integration.ru/mybot/) ──► 127.0.0.1:8085 (aiohttp + aiogram)
                                                         │
                                         OpenAI API ◄────┤────► bitrix.b-integration.ru/rest/<вебхук>
```

| Файл | Что делает |
|---|---|
| `bot/main.py` | вебхук Telegram, команды, кнопки подтверждения |
| `bot/staff.py` | проверка сотрудника по полю профиля (кеш на 10 мин) |
| `bot/agent.py` | цикл модели (OpenAI / Claude) с инструментами: `search_companies`, `list_invoices`, `get_invoice`, `draft_invoice` |
| `bot/invoices.py` | черновик → `crm.item.add` + `crm.item.productrow.set` + перенос реквизитов → `crm.documentgenerator.document.add` |
| `bot/bitrix.py` | REST-клиент через входящий вебхук |

Модель счёт **не создаёт** — она только готовит черновик. Создаёт его человек кнопкой.

## Установка на сервер (Ubuntu 24.04, gorodchaya.ru)

### 1. Подготовить ключи

- **Токен бота** — у [@BotFather](https://t.me/BotFather): `/newbot`.
- **Входящий вебхук Битрикса** — на портале: *Приложения → Разработчикам → Другое → Входящий вебхук*.
  Права: **CRM (crm)**, **Пользователи (user)**, **Генератор документов (documentgenerator)**.
  Вебхук создавайте от пользователя, у которого есть права на счета и компании.
- **Ключ OpenAI API** — в [platform.openai.com](https://platform.openai.com/api-keys). (Можно и Claude: `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`.)

### 2. Код и окружение

```bash
sudo apt install -y python3-venv git
sudo useradd --system --home /opt/mybot --shell /usr/sbin/nologin mybot
sudo git clone https://github.com/Timophei/mybot.git /opt/mybot
cd /opt/mybot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

### 3. Настройки (.env)

```bash
sudo cp .env.example .env
sudo nano .env
```

Заполнить:

| Переменная | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от BotFather |
| `WEBHOOK_SECRET` | результат `openssl rand -hex 32` |
| `BITRIX_WEBHOOK_URL` | `https://bitrix.b-integration.ru/rest/<id>/<код>/` |
| `OPENAI_API_KEY` | ключ OpenAI |
| `OPENAI_MODEL` | модель, которую используешь (по умолчанию `gpt-5.6-terra`) |

Остальное оставить по умолчанию. Затем закрыть файл от чужих глаз:

```bash
sudo chown -R root:mybot /opt/mybot
sudo chmod 640 /opt/mybot/.env
```

### 4. nginx

Вставить блок из `deploy/nginx-location.conf` внутрь `server { … }` сайта **b-integration.ru** (того, что слушает 443 с сертификатом):

```bash
sudo nano /etc/nginx/sites-available/<конфиг b-integration.ru>
sudo nginx -t && sudo systemctl reload nginx
```

Telegram принимает вебхуки только по HTTPS с нормальным сертификатом (Let's Encrypt подходит).

### 5. Служба

```bash
sudo cp deploy/mybot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mybot
sudo journalctl -u mybot -f          # должно появиться «Вебхук установлен: https://b-integration.ru/mybot/tg»
curl https://b-integration.ru/mybot/health   # → ok
```

### 6. Сотрудники

В профиле сотрудника на портале заполнить поле **telega**. Подходит любой вариант, можно несколько через пробел:
`ivanov`, `@ivanov`, `https://t.me/ivanov` или числовой Telegram ID (надёжнее: он не меняется).
Если пишет посторонний, бот отвечает отказом и показывает его `@логин`/ID — это значение и вписывается в поле.

## Обновление

```bash
cd /opt/mybot && sudo git pull && sudo .venv/bin/pip install -r requirements.txt && sudo systemctl restart mybot
```

## Команды бота

- `/start` — приветствие и проверка доступа
- `/new` — забыть контекст разговора
- любой текст — задача для бота

## Если что-то не так

- **«Бот только для сотрудников»** — поле telega не заполнено или заполнено не тем значением. После правки подождите до минуты: бот перечитывает список сотрудников.
- **Не тот шаблон печатной формы** — узнайте ID нужного шаблона (*CRM → Счета → Печать → Настройки шаблонов*) и пропишите `INVOICE_TEMPLATE_ID`.
- **Приходит DOCX вместо PDF** — на коробке не настроен модуль конвертации документов (модуль `transformer`).
- **Старые счета вместо смарт-счетов** — бот работает со смарт-счетами (`INVOICE_ENTITY_TYPE_ID=31`).
- Логи: `sudo journalctl -u mybot -n 200`.
