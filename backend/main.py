import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from scraper import scrape_mobile_de
from ai import generate_kp_text

load_dotenv()

app = FastAPI(title="AutoKP Generator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ParseRequest(BaseModel):
    url: str
    rate: float = 95.0
    customs_pct: float = 15.0
    util_rub: float = 4_500_000.0
    epts_rub: float = 50_000.0
    contact: str = "@Aleksandr_Montaro"
    lot_number: str = "0000"


class TableRow(BaseModel):
    make: str
    model: str
    year: str
    mileage: str
    color: str
    price_eur: float
    rate: float
    customs_pct: float
    customs_eur: float
    turnkey_rub: float
    util_rub: float
    epts_rub: float
    total_rub: float


class ParseResponse(BaseModel):
    make: str
    model: str
    year: str
    mileage: str
    color: str
    price_eur: float
    photos: list[str]
    kp_text: str
    table_row: TableRow


@app.post("/parse", response_model=ParseResponse)
async def parse_listing(req: ParseRequest):
    try:
        scraped = await scrape_mobile_de(req.url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка парсинга: {e}")

    price_eur = scraped.get("price_eur") or 0.0
    if price_eur == 0:
        raise HTTPException(status_code=422, detail="Не удалось извлечь цену из объявления")

    customs_eur = price_eur * (1 + req.customs_pct / 100)
    turnkey_rub = customs_eur * req.rate
    total_rub = turnkey_rub + req.util_rub + req.epts_rub

    mileage_val = scraped.get("mileage")
    mileage_str = "NEW" if (mileage_val is None or mileage_val == 0) else f"{int(mileage_val):,} км".replace(",", " ")

    try:
        kp_text = await generate_kp_text(
            make=scraped["make"],
            model=scraped["model"],
            year=scraped["year"],
            mileage=mileage_val,
            color=scraped.get("color", ""),
            features=scraped.get("features", []),
            price_eur=price_eur,
            customs_eur=customs_eur,
            price_rub_turnkey=turnkey_rub,
            price_rub_util=req.util_rub,
            price_rub_epts=req.epts_rub,
            contact=req.contact,
            lot_number=req.lot_number,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка AI: {e}")

    table_row = TableRow(
        make=scraped["make"],
        model=scraped["model"],
        year=scraped["year"],
        mileage=mileage_str,
        color=scraped.get("color", ""),
        price_eur=price_eur,
        rate=req.rate,
        customs_pct=req.customs_pct,
        customs_eur=round(customs_eur, 2),
        turnkey_rub=round(turnkey_rub, 0),
        util_rub=req.util_rub,
        epts_rub=req.epts_rub,
        total_rub=round(total_rub, 0),
    )

    return ParseResponse(
        make=scraped["make"],
        model=scraped["model"],
        year=scraped["year"],
        mileage=mileage_str,
        color=scraped.get("color", ""),
        price_eur=price_eur,
        photos=scraped.get("photos", []),
        kp_text=kp_text,
        table_row=table_row,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
