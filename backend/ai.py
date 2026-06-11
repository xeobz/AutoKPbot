import httpx
import os

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"


async def generate_kp_text(
    make: str,
    model: str,
    year: str,
    mileage: int | None,
    color: str,
    features: list[str],
    price_eur: float,
    customs_eur: float,
    price_rub_turnkey: float,
    price_rub_util: float,
    price_rub_epts: float,
    contact: str,
    lot_number: str,
) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY", "")

    condition = "NEW" if (mileage is None or mileage == 0) else f"{mileage:,} km".replace(",", " ")
    features_text = "\n".join(f"- {f}" for f in features) if features else "Нет данных"

    total_rub = price_rub_turnkey + price_rub_util + price_rub_epts

    system_prompt = """Ты помощник автодилера. Генерируешь коммерческое предложение (КП) на русском языке строго по шаблону.
Переведи список опций с немецкого на русский, сгруппируй по категориям.
Сохраняй точно все числа и форматирование которые тебе передают.
Не добавляй лишних слов, не пиши ничего кроме самого КП."""

    user_prompt = f"""Сформируй КП строго по шаблону:

ДОСТУПЕН В ЕВРОПЕ 🇪🇺

{make} {model} 🚘

{year} / {condition} / {color if color else "—"}

Комплектация:
[переведи и сгруппируй опции по смысловым категориям: Безопасность, Мультимедиа, Комфорт, Двигатель и трансмиссия, и т.д.]

{int(total_rub):,} руб.
под ключ в мск (включая таможню РБ и комм. утиль)

{int(customs_eur):,} Евро (авто + таможня)
+ {int(price_rub_util + price_rub_epts):,} руб. утиль + ЭПТС

Связаться:
{contact}

#{lot_number}

Список опций автомобиля (немецкий, переведи):
{features_text}

Цена авто: {int(price_eur):,} EUR
Авто + таможня: {int(customs_eur):,} EUR
Цена под ключ (авто+таможня в руб.): {int(price_rub_turnkey):,} руб.
Утиль + ЭПТС: {int(price_rub_util + price_rub_epts):,} руб.
Итого: {int(total_rub):,} руб.

Замени числа в шаблоне на указанные. Верни только текст КП, без пояснений."""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost",
                "X-Title": "AutoKP Generator",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
