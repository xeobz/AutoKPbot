# Развёртывание

Два процесса на одном сервере:

| Сервис | Что это | Порт |
|---|---|---|
| `autokpbot` | телеграм-бот (long polling) | — |
| `autokpweb` | API мини-аппа + отдача фронта (uvicorn) | 127.0.0.1:8000 |

Наружу торчит только nginx с HTTPS, он проксирует на 8000.

---

## Локальный запуск (для проверки без сервера)

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
```

Бот:

```bash
python bot.py
```

Мини-апп (dev-режим — пускает без подписи Telegram, чтобы можно было открыть в обычном браузере):

```bash
WEB_DEV_MODE=1 WEB_DEV_USER_ID=<свой telegram id> uvicorn main:app --reload --port 8000
```

Открыть http://localhost:8000 — интерфейс работает, КП уходит в телеграм по-настоящему.

> **WEB_DEV_MODE обязательно выключить на сервере** — он отключает проверку подписи.

---

## Разовая настройка сервера

Всё выполняется от root на `45.136.175.125`.

### 1. Домен

A-запись `montaro.site` → `45.136.175.125` (и `www`, если нужен). Проверить:

```bash
dig +short montaro.site
```

### 2. nginx и сертификат

```bash
apt update && apt install -y nginx certbot python3-certbot-nginx
cp /opt/autokpbot/deploy/nginx-autokp.conf /etc/nginx/sites-available/autokp
ln -sf /etc/nginx/sites-available/autokp /etc/nginx/sites-enabled/autokp
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
certbot --nginx -d montaro.site -d www.montaro.site --agree-tos -m <почта> --redirect
```

Certbot сам допишет HTTPS-секцию и поставит автопродление.

### 3. Сервис мини-аппа

```bash
cp /opt/autokpbot/autokpweb.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now autokpweb
systemctl status autokpweb --no-pager
```

Проверка: `curl -s localhost:8000/health` → `{"status":"ok"}`

### 4. Включить мини-апп в боте

В GitHub → Settings → Environments → `push` → добавить секрет:

```
WEB_APP_URL = https://montaro.site
```

После следующего деплоя бот начнёт показывать кнопку «🖼 Выбрать фото» и поставит кнопку «Приложение» в меню.

> Пока секрет не задан, бот работает по-старому: фото подбираются автоматически.
> Так что домен можно подключать когда угодно, ничего не сломается.

### 5. Проверка

- `https://montaro.site` открывается в браузере;
- в боте `/app` открывает приложение;
- после расчёта приходит кнопка «Выбрать фото» → открывается галерея → КП приходит в чат.

---

## Обновление

Пуш в `main` → GitHub Actions сам подтягивает код, ставит зависимости и перезапускает оба сервиса.

## Логи

```bash
journalctl -u autokpbot -n 100 -f
journalctl -u autokpweb -n 100 -f
```
