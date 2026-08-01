"""
Servidor v3 (Flask).

CAMBIOS RESPECTO DE LA VERSION ANTERIOR
=======================================

1) /run-check ya no puede fallar en silencio.
   Antes, si Yahoo devolvia 429, get_quotes() retornaba {} , el bucle no
   entraba nunca, y el endpoint respondia HTTP 200 con "checked": 0. El
   cron externo veia un 200 y quedaba conforme. Nadie se enteraba de nada.
   Ahora devuelve HTTP 503 y, tras varios ciclos fallidos seguidos, manda
   un correo de alarma. Un servicio de monitoreo que vigile este endpoint
   ahora si te va a avisar.

2) Latido diario (/resumen-diario).
   El sistema anterior solo sabia hablar cuando algo andaba mal. El
   silencio era ambiguo: podia significar "no cruzo nada" o "todo esta
   roto". Hoy paso lo segundo y no habia forma de distinguirlo. Ahora
   llega un correo todos los dias habiles al cierre, aunque no haya
   ninguna alerta. Si un dia no llega ese correo, sabes que algo se rompio.

3) Las suscripciones push se guardan por endpoint, no por objeto completo.
   El disco de Render en plan gratuito es EFIMERO: se borra en cada
   reinicio y en cada despliegue. La app en el celular guardaba
   "notificaciones activas" en localStorage y nunca revalidaba, asi que
   mostraba el visto verde mientras el servidor tenia la lista vacia.
   Ese es el motivo mas probable de que hoy no te llegara ningun push.
   Con el parche del frontend, la PWA se re-suscribe sola cada vez que la
   abres; aqui se deduplica por endpoint para que no se acumule basura.

4) /quotes entrega la frescura real de cada dato.
   Cada precio viaja con marketTime (hora de la bolsa) y staleSeconds. El
   indice viaja con su propio estado: si no se pudo obtener, se dice
   explicitamente en vez de dejar que la app muestre el valor anterior
   como si fuera de ahora.

5) /signals: la capa de senales con reglas explicitas (ver signals.py).
"""
import os
import json
import time
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS

from data_source import get_market_data, get_stats, get_price_history
from main import TICKERS
import fuente_bolsa
import fuente_df
import news
import notify
import signals

app = Flask(__name__)
CORS(app)

SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")
CHECK_SECRET = os.environ.get("CHECK_SECRET")

PRICE_CACHE_TTL = int(os.environ.get("PRICE_CACHE_TTL", 45))
STATS_CACHE_TTL = int(os.environ.get("STATS_CACHE_TTL", 1800))

_price_cache = {"quotes": None, "index": None, "ts": 0, "fuente": None}
_stats_cache = {"stats": None, "indice": None, "series": None, "ts": 0}
_news_cache = {}
NEWS_CACHE_TTL = 1800

# Salud del servicio: lo que permite distinguir "no paso nada" de "esta roto".
_salud = {
    "ultimo_check_ok": None,      # epoch del ultimo /run-check con datos
    "fallos_seguidos": 0,
    "ultimo_error": None,
    "alarma_enviada": False,
    "checks_totales": 0,
}
FALLOS_PARA_ALARMA = int(os.environ.get("FALLOS_PARA_ALARMA", 3))


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _refrescar_stats(forzar=False):
    ahora = time.time()
    if forzar or _stats_cache["stats"] is None or (ahora - _stats_cache["ts"]) > STATS_CACHE_TTL:
        stats, indice, series = get_stats(TICKERS)
        if stats:  # solo se pisa la cache si vino algo; si Yahoo fallo, se
            _stats_cache.update({"stats": stats, "indice": indice,  # conserva lo viejo
                                 "series": series, "ts": ahora})
    return _stats_cache


def _obtener_precios():
    """
    Elige la fuente de precios para las 47 ACCIONES.

    Si BOLSA_API_KEY esta definida, se usa la API oficial de la Bolsa de
    Santiago: datos de lo que se esta transando de verdad, y con puntas de
    compra/venta, que Yahoo no publica para el mercado chileno.

    Si esa fuente no responde (cuota agotada, clave vencida, caida), cae de
    vuelta a Yahoo automaticamente en vez de dejarte sin datos. La app
    muestra la hora real del dato en cualquiera de los dos casos, asi que
    siempre sabes de cuando es lo que estas viendo.

    El IPSA es aparte: independiente de cual de las dos fuentes de arriba
    se use para las acciones, el indice SIEMPRE viene de Diario Financiero
    (fuente_df.py), nunca de Yahoo. Ver fuente_df.py para el porque.
    """
    if fuente_bolsa.disponible():
        quotes = fuente_bolsa.get_quotes(TICKERS)
        if quotes:
            fuente = "bolsa_de_santiago"
        else:
            print("[server] La API de la Bolsa no respondio; se usa Yahoo como respaldo.")
            quotes, _ = get_market_data(TICKERS)
            fuente = "yahoo"
    else:
        quotes, _ = get_market_data(TICKERS)
        fuente = "yahoo"

    index = fuente_df.get_index()
    return quotes, index, fuente


def _refrescar_precios(forzar=False):
    ahora = time.time()
    if forzar or _price_cache["quotes"] is None or (ahora - _price_cache["ts"]) > PRICE_CACHE_TTL:
        quotes, index, fuente = _obtener_precios()
        _price_cache["fuente"] = fuente
        if quotes:
            _price_cache.update({"quotes": quotes, "index": index, "ts": ahora})
        elif _price_cache["quotes"] is not None:
            # No se pisa con vacio: se conserva lo ultimo bueno, pero la
            # antiguedad real viaja en cada precio, asi que la app lo sabe.
            print("[server] get_market_data no devolvio nada; se conserva la cache anterior.")
    return _price_cache


# --------------------------------------------------------------------------
# Suscripciones push
# --------------------------------------------------------------------------

def _leer_subs():
    if os.path.exists(SUBSCRIPTIONS_FILE):
        try:
            with open(SUBSCRIPTIONS_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[server] Archivo de suscripciones ilegible: {e}")
    return []


@app.route("/subscribe", methods=["POST"])
def subscribe():
    """
    Recibe (o revalida) la suscripcion push de un dispositivo.

    La PWA ahora llama esto CADA VEZ que se abre, no solo la primera. Es a
    proposito: el disco de Render se borra en cada reinicio, y sin esto la
    suscripcion desaparece del servidor mientras el celular sigue creyendo
    que las notificaciones estan activas.
    """
    sub = request.get_json(silent=True)
    if not sub or not sub.get("endpoint"):
        return jsonify({"status": "error", "message": "falta el endpoint"}), 400

    subs = _leer_subs()
    # Deduplicar por endpoint: es el identificador unico del dispositivo.
    # Comparar el objeto completo fallaba si cambiaba el orden de las claves.
    subs = [s for s in subs if s.get("endpoint") != sub["endpoint"]]
    subs.append(sub)

    try:
        with open(SUBSCRIPTIONS_FILE, "w") as f:
            json.dump(subs, f, indent=2)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok", "suscripciones": len(subs)})


@app.route("/subscriptions", methods=["GET"])
def list_subscriptions():
    return jsonify(_leer_subs())


# --------------------------------------------------------------------------
# Datos para la app
# --------------------------------------------------------------------------

@app.route("/quotes", methods=["GET"])
def quotes():
    st = _refrescar_stats()
    pc = _refrescar_precios()

    stats = st["stats"] or {}
    quotes_raw = pc["quotes"] or {}

    data = {}
    for t in TICKERS:
        q = quotes_raw.get(t)
        if not q:
            continue
        s = stats.get(t, {})
        volumen = q.get("volume")
        data[t] = {
            "price": q["price"],
            "avg": s.get("avg90"),
            # Frescura REAL, no la hora en que el servidor pidio el dato.
            "marketTime": q.get("marketTime"),
            "staleSeconds": q.get("staleSeconds"),
            "fetchedAt": q.get("fetchedAt"),
            "previousClose": q.get("previousClose"),
            "dayHigh": q.get("dayHigh"),
            "dayLow": q.get("dayLow"),
            "volume": volumen,
            "montoTransado": (volumen * q["price"]) if volumen else None,
            "ret3m": s.get("ret3m"),
            "ret1y": s.get("ret1y"),
            # Indicadores calculados sobre cierres diarios reales de un ano,
            # no sobre los ticks de 60 segundos del celular.
            "rsi14": s.get("rsi14"),
            "sma20": s.get("sma20"),
            "sma50": s.get("sma50"),
            "zscore": round(s["zscore"], 2) if s.get("zscore") is not None else None,
            "volDiaria": s.get("volDiaria"),
            "montoMedioDiario30d": s.get("montoMedioDiario30d"),
            # Yahoo no publica puntas de la Bolsa de Santiago. Se declara
            # explicitamente en vez de gastar 47 peticiones para mostrar
            # "no disponible" (ver data_source.get_bid_ask).
            # Con la API de la Bolsa estas vienen con datos; con Yahoo, en None.
            "bid": q.get("bid"), "ask": q.get("ask"),
            "bidSize": q.get("bidSize"), "askSize": q.get("askSize"),
            "puntasDisponibles": bool(q.get("bid") or q.get("ask")),
        }

    index = pc.get("index")
    indicadores = fuente_df.get_uf_utm()
    return jsonify({
        "quotes": data,
        "index": index,
        # Si el indice no llego, la app NO debe mostrar el valor anterior
        # como si fuera de ahora. Este flag existe para eso.
        "indexDisponible": index is not None,
        # UF y UTM, tambien de Diario Financiero (ver fuente_df.py).
        "uf": indicadores.get("uf"),
        "utm": indicadores.get("utm"),
        "recibidos": len(data),
        "esperados": len(TICKERS),
        "cached_at": pc["ts"],
        "cache_ttl_seconds": PRICE_CACHE_TTL,
        "fuente": pc.get("fuente", "yahoo"),
        "serverTime": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/signals", methods=["GET"])
def signals_endpoint():
    """
    Ranking con reglas explicitas. NO es una lista de compras sugeridas:
    cada item trae sus razones y sus banderas rojas para que puedas
    descartarlo. Ver el descargo en signals.py.
    """
    st = _refrescar_stats()
    pc = _refrescar_precios()
    if not pc["quotes"]:
        return jsonify({"error": "sin precios disponibles ahora mismo"}), 503

    precios = {t: q["price"] for t, q in pc["quotes"].items()}
    resultado = signals.rankear(precios, st["stats"] or {}, st["indice"])
    resultado["serverTime"] = datetime.now(timezone.utc).isoformat()
    return jsonify(resultado)


@app.route("/signal", methods=["GET"])
def signal_uno():
    ticker = request.args.get("ticker", "").upper()
    if ticker not in TICKERS:
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400
    st = _refrescar_stats()
    pc = _refrescar_precios()
    q = (pc["quotes"] or {}).get(ticker)
    if not q:
        return jsonify({"error": "sin precio disponible para esa accion"}), 503
    return jsonify(signals.evaluar(ticker, q["price"],
                                   (st["stats"] or {}).get(ticker), st["indice"]))


VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "5y"}
_history_cache = {}
HISTORY_CACHE_TTL = 1800


@app.route("/history", methods=["GET"])
def history():
    ticker = request.args.get("ticker", "").upper()
    period = request.args.get("period", "3mo").lower()
    if ticker not in TICKERS and ticker not in ("IPSA", "^IPSA"):
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400
    if period not in VALID_PERIODS:
        return jsonify({"error": f"period debe ser uno de {sorted(VALID_PERIODS)}"}), 400

    # Si la serie anual ya esta en cache, se recorta de ahi en vez de
    # volver a pedirsela a Yahoo. Menos peticiones = menos rate limiting.
    st = _refrescar_stats()
    dias = {"1mo": 21, "3mo": 63, "6mo": 126, "ytd": None, "1y": 252}.get(period)
    serie = (st.get("series") or {}).get(ticker)
    if serie and dias and len(serie) >= dias:
        return jsonify({"ticker": ticker, "period": period,
                        "points": serie[-dias:], "origen": "cache"})

    key = (ticker, period)
    ahora = time.time()
    c = _history_cache.get(key)
    if c is None or (ahora - c["ts"]) > HISTORY_CACHE_TTL:
        _history_cache[key] = {"data": get_price_history(ticker, period), "ts": ahora}
    return jsonify({"ticker": ticker, "period": period,
                    "points": _history_cache[key]["data"], "origen": "yahoo"})


@app.route("/news", methods=["GET"])
def news_endpoint():
    ticker = request.args.get("ticker", "").upper()
    if ticker not in TICKERS:
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400
    ahora = time.time()
    c = _news_cache.get(ticker)
    if c is None or (ahora - c["ts"]) > NEWS_CACHE_TTL:
        _news_cache[ticker] = {"data": news.get_recent_news(ticker), "ts": ahora}
    return jsonify({"ticker": ticker, "items": _news_cache[ticker]["data"]})


# --------------------------------------------------------------------------
# Chequeo periodico y alertas
# --------------------------------------------------------------------------

_alert_state = {}


def _direccion_senal(ev):
    """
    'compra', 'venta' o None, segun el puntaje de signals.evaluar().

    Antes esto era un umbral aparte (-4% fijo, despues z <= -1.5) que solo
    miraba caidas. Ahora usa el MISMO corte que ya arma candidatos_compra/
    candidatos_venta en signals.rankear() (|puntaje| >= 20): lo que dispara
    el correo y el push es exactamente lo que el panel "Analisis del
    momento" de la app ya te muestra, en vez de una regla aparte que podia
    quedar desincronizada.

    Excepcion: si la accion trae la bandera "CAIDA SOSTENIDA", NO se avisa
    aunque el puntaje cruce el umbral de compra. Esa bandera ya le resta
    puntaje en signals.evaluar(), pero no siempre alcanza para bajarla de
    20 -- y avisar "posible compra" de algo que esta cayendo de forma
    sostenida es justo la senal enganosa que la bandera existe para marcar.
    """
    if not ev or ev.get("puntaje") is None:
        return None
    if any(b.startswith("CAIDA SOSTENIDA") for b in ev.get("banderas", [])):
        return None
    if ev["puntaje"] >= 20:
        return "compra"
    if ev["puntaje"] <= -20:
        return "venta"
    return None


@app.route("/run-check", methods=["GET", "POST"])
def run_check():
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    _salud["checks_totales"] += 1
    st = _refrescar_stats()
    quotes, _, _fuente = _obtener_precios()

    # --- Fallo ruidoso -----------------------------------------------------
    # Antes esto devolvia 200 con "checked": 0 y el cron quedaba conforme.
    if not quotes:
        _salud["fallos_seguidos"] += 1
        _salud["ultimo_error"] = "get_market_data devolvio vacio (Yahoo 429 o caido)"
        print(f"[run-check] SIN DATOS. Fallos seguidos: {_salud['fallos_seguidos']}")

        if _salud["fallos_seguidos"] >= FALLOS_PARA_ALARMA and not _salud["alarma_enviada"]:
            _salud["alarma_enviada"] = True
            try:
                notify.send_raw_email(
                    "IPSA Monitor: el servicio dejo de recibir datos",
                    f"Llevo {_salud['fallos_seguidos']} chequeos seguidos sin poder "
                    f"obtener precios de Yahoo Finance.\n\n"
                    f"Mientras esto pase NO vas a recibir alertas — pero "
                    f"tampoco significa que no este pasando nada en el mercado.\n\n"
                    f"Ultimo chequeo con datos: {_salud['ultimo_check_ok']}\n"
                    f"Error: {_salud['ultimo_error']}\n\n"
                    f"Revisa /diag en el servidor.")
            except Exception as e:
                print(f"[run-check] No se pudo enviar la alarma: {e}")

        return jsonify({
            "estado": "sin_datos",
            "fallos_seguidos": _salud["fallos_seguidos"],
            "detalle": _salud["ultimo_error"],
        }), 503

    _salud["fallos_seguidos"] = 0
    _salud["alarma_enviada"] = False
    _salud["ultimo_check_ok"] = datetime.now(timezone.utc).isoformat()
    _salud["ultimo_error"] = None

    stats = st["stats"] or {}
    alertadas = []

    for t in TICKERS:
        q, s = quotes.get(t), stats.get(t)
        if not q or not s:
            continue
        precio = q["price"]
        ev = signals.evaluar(t, precio, s, st["indice"])
        direccion = _direccion_senal(ev)
        anterior = _alert_state.get(t)
        _alert_state[t] = direccion
        # Solo avisa al ENTRAR a una direccion nueva (compra o venta), no en
        # cada chequeo mientras se mantenga ahi, y no si vuelve a "neutro".
        if not direccion or direccion == anterior:
            continue

        cuerpo = _texto_alerta(ev)
        try:
            noticias = news.get_recent_news(t)
            if noticias:
                cuerpo += "\n\n" + news.describe(noticias)
        except Exception as e:
            print(f"[run-check] noticias de {t}: {e}")

        try:
            notify.send_alert(t, direccion, precio, s.get("avg90"), cuerpo)
        except Exception as e:
            print(f"[run-check] correo {t}: {e}")
        try:
            notify.send_push_alert(t, direccion, precio, s.get("avg90"), signals.describe(ev))
        except Exception as e:
            print(f"[run-check] push {t}: {e}")

        alertadas.append({"ticker": t, "direccion": direccion, "precio": precio,
                          "puntaje": ev.get("puntaje") if ev else None,
                          "banderas": len(ev.get("banderas", [])) if ev else 0})

    return jsonify({
        "estado": "ok",
        "evaluadas": len([t for t in TICKERS if t in quotes]),
        "esperadas": len(TICKERS),
        "alertas_disparadas": alertadas,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _texto_alerta(ev):
    if not ev:
        return ""
    lineas = []
    if ev.get("banderas"):
        lineas.append("BANDERAS ROJAS:")
        lineas += [f"  - {b}" for b in ev["banderas"]]
        lineas.append("")
    lineas.append("Por que aparecio:")
    lineas += [f"  - {r}" for r in ev.get("razones", [])]
    lineas.append("")
    lineas.append("CONCEPTOS CLAVE:")
    glosario = signals.GLOSARIO
    if isinstance(glosario, dict):
        # Si es diccionario, genera un texto legible
        if "criterios" in glosario:
            for clave, desc in glosario.get("criterios", {}).items():
                lineas.append(f"  {clave.replace('_', ' ').upper()}: {desc}")
    lineas.append("")
    lineas.append(signals.DESCARGO)
    return "\n".join(lineas)


@app.route("/resumen-diario", methods=["GET", "POST"])
def resumen_diario():
    """
    LATIDO. Este correo llega TODOS los dias habiles, haya o no alertas.

    Es la pieza que faltaba: el sistema anterior solo hablaba cuando algo
    cruzaba el umbral, asi que el silencio era ambiguo — podia ser "no paso
    nada" o "esta todo roto". Con esto, si un dia no llega el resumen, ya
    sabes que hay que revisar el servicio.

    Programalo en el cron externo una vez al dia, despues del cierre.
    """
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    st = _refrescar_stats(forzar=True)
    quotes, index, _fuente = _obtener_precios()

    if not quotes:
        try:
            notify.send_raw_email(
                "IPSA Monitor: resumen diario SIN DATOS",
                "No pude obtener precios hoy. El servicio esta vivo (este correo "
                "llego), pero la fuente de datos no responde. Revisa /diag.")
        except Exception as e:
            print(f"[resumen] {e}")
        return jsonify({"estado": "sin_datos"}), 503

    precios = {t: q["price"] for t, q in quotes.items()}
    r = signals.rankear(precios, st["stats"] or {}, st["indice"])

    lineas = [f"Resumen IPSA · {datetime.now().strftime('%d/%m/%Y')}", ""]
    if index:
        edad = index.get("staleSeconds")
        lineas.append(f"IPSA: {index['value']:,.0f}"
                      + (f" (dato de hace {edad//60} min)" if edad else ""))
    else:
        lineas.append("IPSA: NO DISPONIBLE hoy (Diario Financiero no respondio).")
    lineas.append(f"Acciones con precio: {len(quotes)} de {len(TICKERS)}")
    lineas.append("")

    def _bloque(titulo, items):
        out = [titulo]
        if not items:
            out.append("  (ninguna cumple los criterios hoy)")
        for e in items[:5]:
            out.append(f"  {e['ticker']}: {e['precio']:,.0f} · puntaje {e['puntaje']:+.0f} "
                       f"· z {e['zscore']:+.1f} · RSI {e['rsi14']}")
            for b in e.get("banderas", []):
                out.append(f"      ! {b.split('—')[0].strip()}")
        out.append("")
        return out

    lineas += _bloque("Mas lejos bajo su promedio (revisar, NO comprar a ciegas):",
                      r["candidatos_compra"])
    lineas += _bloque("Mas estiradas sobre su promedio:", r["candidatos_venta"])
    if r["filtradas_por_liquidez"]:
        lineas.append(f"({r['filtradas_por_liquidez']} acciones quedaron fuera del "
                      f"ranking por transar muy poco al dia.)")
        lineas.append("")
    lineas.append(signals.GLOSARIO)
    lineas.append("")
    lineas.append(signals.DESCARGO)

    texto = "\n".join(lineas)
    try:
        notify.send_raw_email("IPSA Monitor · resumen del dia", texto)
    except Exception as e:
        print(f"[resumen] no se pudo enviar: {e}")
        return jsonify({"estado": "error_envio", "detalle": str(e)}), 500

    return jsonify({"estado": "enviado", "texto": texto})


# --------------------------------------------------------------------------
# Diagnostico
# --------------------------------------------------------------------------

@app.route("/diag", methods=["GET"])
def diag():
    """Todo lo que necesitas para saber si esto esta sano, en una pantalla."""
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    pc = _price_cache
    index = pc.get("index")
    return jsonify({
        "salud": _salud,
        "precios_en_cache": len(pc.get("quotes") or {}),
        "precios_esperados": len(TICKERS),
        "edad_cache_precios_seg": int(time.time() - pc["ts"]) if pc["ts"] else None,
        "indice": {
            "fuente": "diario_financiero",
            "disponible": index is not None,
            "valor": index.get("value") if index else None,
            # No es la hora de bolsa: Diario Financiero no la publica en
            # texto plano. Es el momento en que ESTE servidor consulto la
            # pagina -- ver fuente_df.py.
            "hora_consulta": index.get("marketTime") if index else None,
            "antiguedad_seg": index.get("staleSeconds") if index else None,
        },
        "uf_utm": fuente_df.get_uf_utm(),
        "stats_en_cache": len(_stats_cache.get("stats") or {}),
        "edad_cache_stats_seg": int(time.time() - _stats_cache["ts"]) if _stats_cache["ts"] else None,
        "suscripciones_push": len(_leer_subs()),
        "aviso_disco": ("El disco de Render en plan gratuito es EFIMERO: las "
                        "suscripciones se borran en cada reinicio y despliegue. "
                        "Si este numero es 0, abre la PWA en el celular y se "
                        "vuelve a registrar sola."),
        "push": notify.vapid_diagnostico(),
        "correo_configurado": bool(os.environ.get("RESEND_API_KEY")
                                   and os.environ.get("EMAIL_TO")),
        "modo_alerta": "compra_venta (signals.py, |puntaje| >= 20 -- mismo corte que candidatos_compra/venta)",
        "fuente_de_precios": pc.get("fuente", "(aun sin consultar)"),
        "bolsa_de_santiago": fuente_bolsa.diagnostico(),
    })


@app.route("/diag-bolsa", methods=["GET"])
def diag_bolsa():
    """
    Respuesta cruda de la API de la Bolsa de Santiago. Usalo la primera vez
    que configures BOLSA_API_KEY, para ver como se llaman de verdad los
    campos y ajustar _MAPA_CAMPOS en fuente_bolsa.py si algo no calza.
    """
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401
    return jsonify(fuente_bolsa.diagnostico())


@app.route("/push-debug", methods=["GET"])
def push_debug():
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401
    return jsonify(notify.vapid_diagnostico())


@app.route("/push-test", methods=["GET", "POST"])
def push_test():
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401
    enviados, fallidos, detalle = notify.enviar_push({
        "title": "IPSA Monitor",
        "body": "Prueba de notificacion: el push quedo funcionando.",
        "ticker": "test",
    })
    return jsonify({"enviados": enviados, "fallidos": fallidos,
                    "detalle": detalle, "suscripciones": len(_leer_subs())})


@app.route("/email-test", methods=["GET", "POST"])
def email_test():
    """Faltaba el equivalente de /push-test para el correo."""
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401
    try:
        ok = notify.send_raw_email(
            "IPSA Monitor: prueba de correo",
            "Si estas leyendo esto, el canal de correo funciona.")
        return jsonify({"enviado": bool(ok)})
    except Exception as e:
        return jsonify({"enviado": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
