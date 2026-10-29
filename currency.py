"""Курсы валют: ЦБ РФ (раз в сутки, кэш) + конвертация в RUB.

Нужно, чтобы ВСЕГДА показывать цену в рублях — даже если игра заблокирована
в российском Steam (тогда считаем из KZT/UAH/USD по курсу).
"""
import json
import logging
import os
import time

import requests

from config import cfg

log = logging.getLogger("currency")

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_PATH = os.path.join(cfg.data_dir, "rates.json")
TTL = 24 * 3600  # сутки

# Надёжный зашитый фолбэк (RUB за 1 единицу валюты) — если API и кэш недоступны.
FALLBACK = {"USD": 92.0, "KZT": 0.175, "UAH": 2.25, "EUR": 100.0, "RUB": 1.0}

_cache: dict | None = None


def _fetch_cbr() -> dict | None:
    try:
        r = requests.get(CBR_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        out = {"RUB": 1.0}
        for code, v in (data.get("Valute") or {}).items():
            try:
                nominal = float(v.get("Nominal") or 1)
                value = float(v.get("Value"))
                out[code] = value / nominal  # RUB за 1 единицу
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if "USD" in out and out["USD"] > 0:
            return out
    except (requests.RequestException, ValueError) as e:
        log.warning("CBR недоступен: %s", cfg.mask(e))
    return None


def _load_cache() -> dict | None:
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d.get("ts", 0) < TTL and d.get("rates"):
                return d["rates"]
            return d.get("rates")  # просроченный, но лучше фолбэка
    except (OSError, ValueError):
        pass
    return None


def _save_cache(rates: dict):
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "rates": rates}, f, ensure_ascii=False)
    except OSError:
        pass


def get_rates(force: bool = False) -> dict:
    """Курсы RUB за 1 единицу. Обновление раз в сутки, иначе кэш/фолбэк."""
    global _cache
    if _cache is not None and not force:
        return _cache
    cached = _load_cache()
    if cached and not force:
        _cache = {**FALLBACK, **cached}
        return _cache
    fresh = _fetch_cbr()
    if fresh:
        _save_cache(fresh)
        _cache = {**FALLBACK, **fresh}
    else:
        _cache = {**FALLBACK, **(cached or {})}
    log.info("Курсы валют: USD=%.2f KZT=%.3f UAH=%.2f", _cache.get("USD", 0), _cache.get("KZT", 0), _cache.get("UAH", 0))
    return _cache


def to_rub(amount: float | None, currency: str) -> float | None:
    if amount is None:
        return None
    rates = get_rates()
    rate = rates.get((currency or "").upper())
    if currency == "RUB" or currency == "":
        rate = 1.0
    if not rate:
        rate = FALLBACK.get((currency or "").upper(), 1.0)
    return amount * rate


def rub_int(amount: float | None) -> int | None:
    if amount is None:
        return None
    return int(round(amount))
