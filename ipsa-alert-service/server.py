"""
Servidor mínimo (Flask) con dos funciones:
  - Recibir la suscripción push que genera la PWA en el celular
    (endpoint /subscribe), para que main.py sepa a quién avisarle.
  - Endpoint /health para verificar que el servicio sigue vivo,
    útil si lo despliegas en Render/Railway/etc.
"""
import os
import json
from flask import Flask, request, jsonify

app = Flask(__name__)
SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")


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
    """
    Usado por main.py (corriendo en GitHub Actions, en otra máquina) para
    leer a quién avisarle por push -- no puede leer el archivo local de
    este servidor directamente, así que lo pide por HTTP.
    """
    subs = []
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE) as f:
            subs = json.load(f)
    return jsonify(subs)


@app.route("/health")
def health():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
