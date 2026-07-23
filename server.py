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

from data_source import get_quotes, get_daily_avg
from main import TICKERS

app = Flask(__name__)
CORS(app)  # permite peticiones desde cualquier origen (la PWA en github.io)

SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")

# Cache simple en memoria para /quotes -- calcular el promedio de 90
# dias vía Yahoo Finance es lento (historial completo por accion), asi
# que no lo repetimos en cada carga de la app. Se resetea solo si el
# servidor se reinicia (ej. tras dormir por inactividad en el plan
# gratuito de Render).
_cache = {"data": None, "ts": 0}
CACHE_TTL_SECONDS = 900  # 15 minutos


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
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL_SECONDS:
        prices = get_quotes(TICKERS)
        avgs = get_daily_avg(TICKERS)
        data = {}
        for t in TICKERS:
            if t in prices:
                data[t] = {
                    "price": prices[t]["price"],
                    "avg": avgs.get(t),
                    "timestamp": prices[t]["timestamp"],
                }
        _cache["data"] = data
        _cache["ts"] = now

    return jsonify({
        "quotes": _cache["data"],
        "cached_at": _cache["ts"],
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    })


@app.route("/health")
def health():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
