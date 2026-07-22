"""
Mantiene un historial de precios por accion (archivo local JSON) y calcula
el promedio de los ultimos 90 dias para detectar caidas significativas.

LIMITACION IMPORTANTE: el promedio se construye con los precios que este
servicio va registrando dia a dia mientras corre. El primer dia que lo
ejecutes, el "promedio" sera simplemente el precio de ese dia (no hay
90 dias de historia todavia). Si la API de la Bolsa o de tu corredora
entrega una serie historica de cierre por accion, conviene sembrar
price_history.json con esos datos antes de partir para tener el
promedio correcto desde el dia uno.
"""
import json
import os
from datetime import datetime, timedelta

HISTORY_FILE = os.environ.get("HISTORY_FILE", "price_history.json")
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", 0.98))  # 2% bajo el promedio
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", 90))


def _load():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}


def _save(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_price_series(ticker):
    """
    Devuelve la lista de precios del ticker, ordenada de más antiguo a
    más reciente, para que los indicadores técnicos (indicators.py)
    puedan calcularse sobre ella.
    """
    data = _load()
    series = data.get(ticker, {"prices": {}})
    sorted_dates = sorted(series["prices"].keys())
    return [series["prices"][d] for d in sorted_dates]


def update_and_check(ticker, price):
    """
    Registra el precio actual del dia, actualiza el promedio de 90 dias,
    y devuelve (promedio, esta_bajo_umbral, cruzo_ahora).

    'cruzo_ahora' es True solo en el momento en que el precio pasa de
    estar sobre el umbral a estar bajo el umbral (para no reenviar la
    misma alerta en cada ciclo mientras siga bajo).
    """
    data = _load()
    today = datetime.now().strftime("%Y-%m-%d")
    series = data.get(ticker, {"prices": {}, "was_below": False})

    series["prices"][today] = price

    cutoff = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    series["prices"] = {d: p for d, p in series["prices"].items() if d >= cutoff}

    avg = sum(series["prices"].values()) / len(series["prices"])
    threshold = avg * ALERT_THRESHOLD
    is_below = price < threshold
    crossed_now = is_below and not series.get("was_below", False)

    series["was_below"] = is_below
    data[ticker] = series
    _save(data)

    return avg, is_below, crossed_now
