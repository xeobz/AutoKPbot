"""
Точка входа веб-части: API мини-аппа + отдача фронтенда.

Локально:
    cd backend
    WEB_DEV_MODE=1 WEB_DEV_USER_ID=<твой telegram id> uvicorn main:app --reload --port 8000

На сервере — systemd-юнит autokpweb.service (без dev-режима).
"""
from webapi import create_app

app = create_app()
