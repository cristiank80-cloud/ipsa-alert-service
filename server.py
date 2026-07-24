"""
Servidor (Flask) con tres funciones:
  - Recibir la suscripción push que genera la PWA en el celular
    (endpoint /subscribe), para que main.py sepa a quién avisarle.
  - Entregar datos reales de precios a la PWA (endpoint /quotes),
    para que la app muestre precios de verdad y no solo la simulación.
  - Endpoint /health para verificar que el servicio sigue vivo.

CORS: la PWA se sirve desde un dominio distinto (github.io) al de este
servidor (onrender.com) -- sin habilitar CORS explícitamente, el
navegador bloquea esas peticiones por seguridad. flask-cors lo resuelve.
"""
import os
import json
import time
from flask import Flask, request, jsonify
from flask_cors import CORS

from data_source import get_quotes, get_daily_avg, get_returns, get_index_quote, get_price_history
from main import TICKERS

app = Flask(__name__)
CORS(app)  # permite peticiones desde cualquier origen (la PWA en github.io)

SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")

# Dos caches separados, con la razon de fondo explicada abajo:
#
# 1) _price_cache: precio actual + volumen + max/min del dia. Rapido de
#    pedir (fast_info), asi que se refresca casi en cada consulta -- si
#    el mercado esta abierto, esto es lo que hace que la app se sienta
#    "en vivo" de verdad, no pegada a un numero viejo.
#
# 2) _stats_cache: promedio de 90 dias y rentabilidad 3M/1A. Esto es
#    LENTO de calcular (pide el historial completo de cada accion), y
#    ademas casi no cambia de un minuto a otro -- por eso se guarda por
#    mas tiempo, para no pagar ese costo en cada consulta de la app.
_price_cache = {"data": None, "index": None, "ts": 0}
PRICE_CACHE_TTL_SECONDS = 45  # se refresca casi en cada poll de la PWA (cada 60s)

_stats_cache = {"avgs": None, "rets": None, "ts": 0}
STATS_CACHE_TTL_SECONDS = 1800  # 30 minutos -- el promedio/rentabilidad no cambia en minutos


@app.route("/subscribe", methods=["POST"])
def subscribe():
    sub = request.get_json()
    if not sub:
        return jsonify({"status": "error", "message": "sin datos"}), 400

    subs = []
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE) as f:
            subs = json.load(f)

    if sub not in subs:
        subs.append(sub)
        with open(SUBSCRIPTIONS_FILE, "w") as f:
            json.dump(subs, f, indent=2)

    return jsonify({"status": "ok"})


@app.route("/subscriptions", methods=["GET"])
def list_subscriptions():
    """Usado por main.py (en GitHub Actions) para saber a quién avisarle por push."""
    subs = []
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE) as f:
            subs = json.load(f)
    return jsonify(subs)


@app.route("/quotes", methods=["GET"])
def quotes():
    """
    Precios reales + promedio de 90 dias para todas las acciones, desde
    Yahoo Finance. La PWA llama esto para mostrar datos de verdad en
    vez de la simulacion local.
    """
    now = time.time()

    if _stats_cache["avgs"] is None or (now - _stats_cache["ts"]) > STATS_CACHE_TTL_SECONDS:
        _stats_cache["avgs"] = get_daily_avg(TICKERS)
        _stats_cache["rets"] = get_returns(TICKERS)
        _stats_cache["ts"] = now

    if _price_cache["data"] is None or (now - _price_cache["ts"]) > PRICE_CACHE_TTL_SECONDS:
        prices = get_quotes(TICKERS)
        index = get_index_quote()
        data = {}
        for t in TICKERS:
            if t in prices:
                p = prices[t]
                volume = p.get("volume")
                monto_transado = (volume * p["price"]) if volume else None
                data[t] = {
                    "price": p["price"],
                    "avg": _stats_cache["avgs"].get(t),
                    "timestamp": p["timestamp"],
                    "dayHigh": p.get("dayHigh"),
                    "dayLow": p.get("dayLow"),
                    "volume": volume,
                    "montoTransado": monto_transado,
                    "ret3m": _stats_cache["rets"].get(t, {}).get("ret_3m"),
                    "ret1y": _stats_cache["rets"].get(t, {}).get("ret_1y"),
                }
        _price_cache["data"] = data
        _price_cache["index"] = index
        _price_cache["ts"] = now

    return jsonify({
        "quotes": _price_cache["data"],
        "index": _price_cache.get("index"),
        "cached_at": _price_cache["ts"],
        "cache_ttl_seconds": PRICE_CACHE_TTL_SECONDS,
    })


# Periodos validos (sintaxis yfinance) que acepta /history
VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "5y"}
_history_cache = {}  # {(ticker, period): {"data":..., "ts":...}}
HISTORY_CACHE_TTL = 1800  # 30 minutos -- el historial cambia poco durante el dia


@app.route("/history", methods=["GET"])
def history():
    """
    Serie de precios reales de cierre para un ticker y un periodo dado,
    para el selector de rango del grafico (1D/5D/1M/3M/6M/YTD/1A/5Y).
    Uso: /history?ticker=SQM-B&period=3mo
    """
    ticker = request.args.get("ticker", "").upper()
    period = request.args.get("period", "3mo").lower()

    if ticker not in TICKERS:
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400
    if period not in VALID_PERIODS:
        return jsonify({"error": f"period debe ser uno de {sorted(VALID_PERIODS)}"}), 400

    key = (ticker, period)
    now = time.time()
    cached = _history_cache.get(key)
    if cached is None or (now - cached["ts"]) > HISTORY_CACHE_TTL:
        data = get_price_history(ticker, period)
        _history_cache[key] = {"data": data, "ts": now}

    return jsonify({"ticker": ticker, "period": period, "points": _history_cache[key]["data"]})


@app.route("/health")
def health():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
