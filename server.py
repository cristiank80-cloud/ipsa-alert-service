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
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from flask_cors import CORS

from data_source import (get_market_data, get_stats, get_price_history,
                         get_rango_5y, get_proximos_reportes,
                         filtrar_puntos_por_periodo,
                         simbolos_en_cuarentena,
                         market_caps, estado_crumb)
from main import TICKERS, TICKERS_USA, UNIVERSO_ANALISIS, ETFS_NO_ANALIZAR
import fuente_bolsa
import fuente_df
import ipsa_historico
import news
import notify
import signals
import indicador_fuerza_fase
import explorar
import universo_mercado

app = Flask(__name__)
CORS(app)

SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")
# Precios objetivo por accion, definidos por el usuario en el modulo "Mi
# Cartera" del frontend ("avisar si sube/baja a"). Mismo patron que
# SUBSCRIPTIONS_FILE: disco EFIMERO en el plan gratuito de Render, se
# pierde en cada reinicio/despliegue. El frontend se auto-cura igual que
# hace con la suscripcion push: reenvia su copia de localStorage completa
# cada vez que abre la app y cada vez que el usuario edita un objetivo, asi
# que el servidor se repuebla solo sin que Cristian tenga que hacer nada.
OBJETIVOS_FILE = os.environ.get("OBJETIVOS_FILE", "alertas_precio.json")
CHECK_SECRET = os.environ.get("CHECK_SECRET")

# --------------------------------------------------------------------------
# WATCHLIST PERSONAL -- las candidatas de Explorar que NO estan en la grilla
# --------------------------------------------------------------------------
# El problema que resuelve: el modulo Explorar barre 536 acciones, pero la
# grilla que recibe precio cada ciclo son 172. Cuando Explorar encontraba una
# candidata fuera de esas 172, no habia forma de vigilarla -- ni precio en la
# app, ni alerta de "esperando entrada", ni push. La candidata quedaba como
# un nombre en una lista y nada mas.
#
# Ahora el frontend manda su watchlist a POST /watchlist y esos simbolos se
# suman a la tanda de EE.UU. Sin maquinaria nueva: entran por el mismo
# get_market_data() y el mismo get_stats() que ya corren, asi que aparecen
# solos en /quotes-usa Y en _evaluar_objetivos() (o sea, el push funciona
# igual que para cualquier accion de la grilla).
#
# EL TOPE ES LO QUE HACE QUE ESTO SEA SEGURO. Con WATCHLIST_MAX en 15, la
# tanda de EE.UU. pasa de 172 a 187 simbolos como maximo (+8,7%). Sin tope,
# el frontend podria mandar las 536 y volveriamos exactamente al problema que
# la separacion grilla/universo existia para evitar (ver main.py). Ademas
# solo se aceptan simbolos que esten en UNIVERSO_ANALISIS: no es un campo de
# texto libre apuntando a Yahoo.
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.json")
WATCHLIST_MAX = int(os.environ.get("WATCHLIST_MAX", 15))

PRICE_CACHE_TTL = int(os.environ.get("PRICE_CACHE_TTL", 45))
STATS_CACHE_TTL = int(os.environ.get("STATS_CACHE_TTL", 1800))

_price_cache = {"quotes": None, "index": None, "ts": 0, "fuente": None}
_stats_cache = {"stats": None, "indice": None, "series": None, "ts": 0}
# Cache separada para el ambiente 2 (EE.UU.). Ver seccion 2.3 de la spec v3:
# mismo mecanismo que Chile, pero sin indice propio (el S&P 500 no sale de
# fuente_df) y sin pasar por fuente_bolsa (esa API es solo Bolsa de Santiago).
_price_cache_usa = {"quotes": None, "index": None, "ts": 0}
_stats_cache_usa = {"stats": None, "indice": None, "series": None, "ts": 0}
# S&P 500 como referencia del ambiente 2. A diferencia del IPSA, este SI se
# pide a Yahoo con el mecanismo generico get_market_data() -- el bug de
# "regularMarketTime pegado" que obligo a sacar el IPSA de Yahoo era
# especifico de ^IPSA (confirmado cruzando contra Visfin, ver
# fuente_df.py); ^GSPC es uno de los simbolos mas liquidos que existen y no
# se ha observado ese problema en el. Igual viaja con staleSeconds real, asi
# que si algun dia se queda pegado, la app lo va a mostrar viejo en vez de
# ocultarlo silenciosamente.
INDEX_SYMBOL_USA = "^GSPC"
_news_cache = {}
NEWS_CACHE_TTL = 1800

# Rango de 5 años (min/max) para la franja nueva de la tarjeta de lista.
# Cache PROPIA y separada de _stats_cache/_stats_cache_usa (que solo piden
# 1 año) porque 5 años de historial por accion es varias veces mas pesado
# -- ver el comentario en get_rango_5y() de data_source.py. TTL de un dia:
# el minimo/maximo de 5 años no cambia de una hora a otra, asi que no vale
# la pena refrescarlo en el mismo ciclo de 30 min que precios/stats.
RANGO5Y_CACHE_TTL = 24 * 3600
_rango5y_cache = {"chile": {"data": None, "ts": 0}, "usa": {"data": None, "ts": 0}}

# Proximo reporte de resultados por ticker (ver get_proximos_reportes en
# data_source.py). Cache de 12h: una fecha de earnings no cambia de una hora
# a otra, y son 107+ peticiones extra a Yahoo, asi que no tiene sentido
# pedirlas en el ciclo de precios. Solo EE.UU. por ahora: Yahoo casi nunca
# publica calendarEvents para los nemotecnicos de la Bolsa de Santiago.
REPORTES_CACHE_TTL = 12 * 3600
_reportes_cache = {"usa": {"data": None, "ts": 0}}

# --------------------------------------------------------------------------
# Diagnostico Fuerza/Weinstein (ver indicador_fuerza_fase.py) -- serie diaria
# de 5 años POR TICKER, para poder resamplear a semanal (30+30 semanas de
# ventana no entran en el "1y" que ya cachea _stats_cache/_stats_cache_usa).
#
# A DIFERENCIA de esas caches (que refrescan TODO el universo cada 30 min en
# segundo plano), esta es PEREZOSA: solo se pide la serie de un ticker la
# PRIMERA VEZ que alguien abre su diagnostico, y de ahi queda cacheada 24h.
# Con 131 tickers en total, bajarlos todos igual que /rango5y seria una
# peticion 5 años mas por ticket sin que nadie la haya pedido -- innecesario
# si Cristian solo mira el diagnostico de unas pocas acciones a la vez.
#
# Mismo patron que ya usa /history (get_price_history dentro del ciclo de
# peticion, sincrono): UNA peticion a Yahoo por un solo simbolo, no la
# descarga masiva que causaba el bloqueo de 107 historiales. Ver el
# comentario largo sobre "POR QUE LAS ACTUALIZACIONES CORREN EN SEGUNDO
# PLANO" mas arriba -- esto es deliberadamente distinto porque es un solo
# simbolo, poco frecuente (un usuario abriendo un detalle a la vez).
DIAGNOSTICO_CACHE_TTL = 24 * 3600
_serie5y_cache = {}  # ticker -> {"puntos": [...], "ts": epoch}

# Indice de referencia para la fuerza relativa del diagnostico, TAMBIEN de
# 5 años: para Chile sale de ipsa_historico (CSV local + hoy en vivo, cero
# peticiones nuevas a Yahoo -- ver ese modulo); para EE.UU. sale de Yahoo
# (^GSPC, que a diferencia de ^IPSA SI tiene historico sano, ver
# data_source.py) y se cachea 24h una sola vez para TODO el ambiente, no por
# ticker -- todas las acciones de EE.UU. comparten el mismo S&P 500.
_indice5y_cache = {"chile": {"puntos": None, "ts": 0}, "usa": {"puntos": None, "ts": 0}}


def _serie5y_ticker(ticker, es_usa):
    ahora = time.time()
    c = _serie5y_cache.get(ticker)
    if c is not None and (ahora - c["ts"]) <= DIAGNOSTICO_CACHE_TTL:
        return c["puntos"]
    puntos = get_price_history(ticker, "5y", suffix=("" if es_usa else None))
    _serie5y_cache[ticker] = {"puntos": puntos, "ts": ahora}
    return puntos


def _indice5y(mercado):
    ahora = time.time()
    c = _indice5y_cache[mercado]
    if c["puntos"] is not None and (ahora - c["ts"]) <= DIAGNOSTICO_CACHE_TTL:
        return c["puntos"]
    if mercado == "chile":
        puntos = ipsa_historico.obtener_serie_combinada()
    else:
        puntos = get_price_history("^GSPC", "5y", suffix="")
    if puntos:
        _indice5y_cache[mercado] = {"puntos": puntos, "ts": ahora}
    return puntos


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
#
# POR QUE LAS ACTUALIZACIONES CORREN EN SEGUNDO PLANO
# ===================================================
# Render en plan gratuito levanta UN solo worker de gunicorn, y ese worker
# atiende una peticion a la vez. Mientras estuvo con 47 acciones chilenas y
# 7 ETF, refrescar la cache dentro de la peticion tardaba poco y no se
# notaba.
#
# Al pasar el ambiente de EE.UU. de 7 a 107 instrumentos, `get_stats` empezo
# a bajar 107 historiales de un año completo cada media hora. Eso deja al
# unico worker ocupado hasta un minuto, y durante ese rato TODO lo demas
# queda haciendo cola detras: /quotes, /health y -- lo importante para las
# notificaciones -- /subscribe. Desde el celular eso se ve como "sin
# conexion al backend" y como un 502, aunque el servidor este vivo: no esta
# caido, esta ocupado.
#
# Ahora las peticiones NUNCA esperan a Yahoo. Devuelven al tiro lo que haya
# en cache (aunque este vacio o viejo) y, si corresponde, disparan la
# actualizacion en un hilo aparte. La app ya sabe mostrar "Cargando datos
# reales..." y la antiguedad real de cada dato, asi que no se inventa nada:
# solo se deja de bloquear el servidor para conseguirlo.

_locks_refresco = {}
_locks_refresco_mutex = threading.Lock()


def _en_segundo_plano(nombre, funcion):
    """
    Corre `funcion` en un hilo aparte, garantizando que no haya dos
    actualizaciones del mismo tipo a la vez (si ya hay una en curso, esta
    llamada no hace nada en vez de encolar otra descarga de 107 historiales).
    """
    with _locks_refresco_mutex:
        lock = _locks_refresco.setdefault(nombre, threading.Lock())
    if not lock.acquire(blocking=False):
        return False  # ya hay una actualizacion de este tipo corriendo

    def _correr():
        try:
            funcion()
        except Exception as e:
            print(f"[server] Fallo la actualizacion en segundo plano '{nombre}': "
                  f"{type(e).__name__}: {e}")
        finally:
            lock.release()

    threading.Thread(target=_correr, name=f"refresco-{nombre}", daemon=True).start()
    return True


def _refrescar_stats(forzar=False):
    ahora = time.time()
    if forzar or _stats_cache["stats"] is None or (ahora - _stats_cache["ts"]) > STATS_CACHE_TTL:
        def _trabajo():
            stats, indice, series = get_stats(TICKERS)
            if stats:  # solo se pisa la cache si vino algo; si Yahoo fallo, se
                _stats_cache.update({"stats": stats, "indice": indice,  # conserva lo viejo
                                     "series": series, "ts": time.time()})
        if forzar:
            _trabajo()          # /run-check y el resumen diario SI necesitan el dato ya
        else:
            _en_segundo_plano("stats_chile", _trabajo)
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
        def _trabajo():
            quotes, index, fuente = _obtener_precios()
            _price_cache["fuente"] = fuente
            if quotes:
                _price_cache.update({"quotes": quotes, "index": index, "ts": time.time()})
            elif _price_cache["quotes"] is not None:
                # No se pisa con vacio: se conserva lo ultimo bueno, pero la
                # antiguedad real viaja en cada precio, asi que la app lo sabe.
                print("[server] get_market_data no devolvio nada; se conserva la cache anterior.")
        if forzar:
            _trabajo()
        else:
            _en_segundo_plano("precios_chile", _trabajo)
    return _price_cache


# --------------------------------------------------------------------------
# Ambiente 2 (EE.UU. · USD) -- mismo patron de cache que Chile, pero mas
# simple: siempre Yahoo (no hay "API de la Bolsa" equivalente para EE.UU.
# en este proyecto), sin indice propio, sin fuente_bolsa/fuente_df.
# --------------------------------------------------------------------------

def _refrescar_stats_usa(forzar=False):
    # ESTE es el que provocaba el problema: 107 historiales de un año.
    # Siempre en segundo plano, incluso con forzar=True -- no hay ningun
    # camino en el que valga la pena tener el servidor entero detenido un
    # minuto por las estadisticas de EE.UU.
    #
    # con_indice=True + index_symbol=INDEX_SYMBOL_USA (bloque "alertas EE.UU."):
    # ahora SI se pide el S&P 500 en la misma tanda paralela, porque
    # signals.rankear() lo necesita para la "fuerza relativa" (punto 4 de
    # signals.py) igual que ya lo hace para Chile con el IPSA. Es una
    # peticion mas sobre 107 (108 en total), no cambia el riesgo de 429.
    ahora = time.time()
    if forzar or _stats_cache_usa["stats"] is None or (ahora - _stats_cache_usa["ts"]) > STATS_CACHE_TTL:
        def _trabajo():
            # La watchlist entra aca tambien, no solo en los precios. Sin
            # esto una accion de watchlist llegaria con precio pero sin avg90
            # ni sma50, y el detalle de la app se queda en "Cargando datos
            # reales..." para siempre -- mostraria el nombre sin poder
            # mostrar nada mas, que es peor que no mostrarlo.
            stats, indice, series = get_stats(_tickers_usa_con_watchlist(), suffix="",
                                               con_indice=True,
                                               index_symbol=INDEX_SYMBOL_USA)
            if stats:
                _stats_cache_usa.update({"stats": stats, "indice": indice,
                                         "series": series, "ts": time.time()})
        _en_segundo_plano("stats_usa", _trabajo)
    return _stats_cache_usa


def _refrescar_precios_usa(forzar=False):
    ahora = time.time()
    if forzar or _price_cache_usa["quotes"] is None or (ahora - _price_cache_usa["ts"]) > PRICE_CACHE_TTL:
        def _trabajo():
            # El indice viaja en la MISMA tanda paralela que los ETFs (igual
            # que se hizo para las acciones chilenas) -- no una peticion aparte.
            quotes, _ = get_market_data(
                _tickers_usa_con_watchlist() + [INDEX_SYMBOL_USA], suffix="")
            if quotes:
                indice = quotes.pop(INDEX_SYMBOL_USA, None)
                _price_cache_usa.update({"quotes": quotes, "index": indice, "ts": time.time()})
            elif _price_cache_usa["quotes"] is not None:
                print("[server] get_market_data (USA) no devolvio nada; se conserva la cache anterior.")
        _en_segundo_plano("precios_usa", _trabajo)
    return _price_cache_usa


def _refrescar_rango5y(mercado):
    """
    mercado: "chile" o "usa". Mismo patron que _refrescar_stats_usa --
    SIEMPRE en segundo plano (nunca bloquea la peticion), y si todavia no
    hay nada en cache, /rango5y devuelve data=None y el frontend se queda
    sin esa franja hasta el proximo refresco, en vez de esperar.
    """
    cache = _rango5y_cache[mercado]
    ahora = time.time()
    if cache["data"] is not None and (ahora - cache["ts"]) <= RANGO5Y_CACHE_TTL:
        return cache

    def _trabajo():
        if mercado == "usa":
            data = get_rango_5y(TICKERS_USA, suffix="")
        else:
            data = get_rango_5y(TICKERS)
        if data:
            cache.update({"data": data, "ts": time.time()})
    _en_segundo_plano(f"rango5y_{mercado}", _trabajo)
    return cache


def _refrescar_reportes_usa():
    """
    Fechas del proximo reporte de resultados para TICKERS_USA. Mismo patron
    que _refrescar_rango5y: siempre en segundo plano, y si todavia no hay
    nada, signals.rankear() simplemente no marca ningun reporte (en vez de
    esperar). Nunca bloquea la peticion.
    """
    cache = _reportes_cache["usa"]
    ahora = time.time()
    if cache["data"] is not None and (ahora - cache["ts"]) <= REPORTES_CACHE_TTL:
        return cache

    def _trabajo():
        data = get_proximos_reportes(TICKERS_USA, suffix="")
        if data:
            cache.update({"data": data, "ts": time.time()})
    _en_segundo_plano("reportes_usa", _trabajo)
    return cache


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


def _leer_objetivos():
    """{"TICKER": {"sube": float|None, "baja": float|None, "mercado": "CLP"|"USD"}}"""
    if os.path.exists(OBJETIVOS_FILE):
        try:
            with open(OBJETIVOS_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[server] Archivo de objetivos de precio ilegible: {e}")
    return {}


def _leer_watchlist():
    """
    Lista de simbolos extra que se suman a la tanda de EE.UU. Siempre
    devuelve una lista limpia: solo simbolos del universo de analisis, sin
    repetidos, sin los que ya estan en la grilla, y como maximo WATCHLIST_MAX.

    Se filtra AL LEER y no solo al escribir a proposito: el archivo vive en
    el disco efimero de Render y podria quedar de una version anterior con
    otro tope o con simbolos que desde entonces salieron del universo.
    """
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE) as f:
            crudo = json.load(f)
    except Exception as e:
        print(f"[server] Archivo de watchlist ilegible: {e}")
        return []
    if not isinstance(crudo, list):
        return []
    return _limpiar_watchlist(crudo)


def _limpiar_watchlist(simbolos):
    universo = set(UNIVERSO_ANALISIS)
    en_grilla = set(TICKERS_USA)
    limpia, vistos = [], set()
    for s in simbolos:
        if not isinstance(s, str):
            continue
        t = s.strip().upper()
        # Ya estar en la grilla no es un error del frontend: es el caso
        # normal cuando una candidata que estaba fuera despues entra. Se
        # ignora en silencio porque ya recibe precio por el camino de siempre.
        if not t or t in vistos or t in en_grilla or t not in universo:
            continue
        vistos.add(t)
        limpia.append(t)
        if len(limpia) >= WATCHLIST_MAX:
            break
    return limpia


def _tickers_usa_con_watchlist():
    """La grilla MAS la watchlist. Es lo que se le pide a Yahoo cada ciclo."""
    return TICKERS_USA + _leer_watchlist()


@app.route("/vapid-public-key", methods=["GET"])
def vapid_public_key():
    """
    Entrega la clave publica VAPID que corresponde a la privada que este
    servidor tiene configurada.

    POR QUE EXISTE
    ==============
    Hasta ahora la clave publica estaba escrita a mano en el HTML de la PWA
    y la privada vivia en las variables de entorno de Render. Son dos copias
    del mismo dato en dos lugares distintos, y se desincronizaron: el HTML
    tenia una clave (BOQ6bc...) y el servidor derivaba otra (BKLnGp...).

    Con claves distintas el navegador SI deja suscribirse y SI dice que todo
    salio bien -- el celular no tiene como saber cual es la clave correcta.
    El push se cae despues, en silencio, cuando el servidor lo firma con una
    identidad que no calza con la de la suscripcion y el servicio de push
    (Apple/Google) lo rechaza. Por eso el boton decia "activas" y nunca
    llegaba nada: no habia ningun error visible en ninguna de las dos
    puntas.

    Publicando la clave aca, la PWA la pide al arrancar y siempre usa la del
    servidor. Deja de haber dos copias que puedan discrepar.

    Es PUBLICA a proposito -- va dentro de cada suscripcion push y cualquiera
    que abra la app la ve. Lo secreto es la privada, que no sale de aca.
    """
    diag = notify.vapid_diagnostico()
    clave = diag.get("publica_derivada")
    if not clave:
        return jsonify({
            "error": "el servidor no tiene una clave VAPID valida configurada",
            "detalle": diag.get("error"),
        }), 503
    return jsonify({"key": clave})


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
    """
    OJO: esto devuelve las suscripciones COMPLETAS, con sus claves `p256dh`
    y `auth`. Con esas dos claves cualquiera puede mandarle notificaciones
    a ese dispositivo. Estaba abierto sin token -- ahora pide el mismo
    CHECK_SECRET que el resto de los endpoints de administracion.
    Para diagnosticar sin exponer nada, usa /push-debug.
    """
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401
    return jsonify(_leer_subs())


@app.route("/alertas-precio", methods=["POST"])
def guardar_alertas_precio():
    """
    Recibe la copia COMPLETA de los precios objetivo que el usuario tiene
    guardados en localStorage (modulo "Mi Cartera") y reemplaza el archivo
    entero -- igual criterio que /subscribe: el frontend es la fuente de
    verdad, el servidor solo necesita una copia fresca para poder avisar
    aunque la app este cerrada. Se llama al abrir la app y cada vez que se
    edita un objetivo (ver sincronizarAlertasPrecio() en el frontend).

    Body esperado: {"objetivos": {"TICKER": {"sube": 123.4, "baja": null,
    "mercado": "CLP"}, ...}}
    """
    body = request.get_json(silent=True) or {}
    objetivos = body.get("objetivos")
    if not isinstance(objetivos, dict):
        return jsonify({"status": "error", "message": "falta 'objetivos' (objeto)"}), 400

    limpio = {}
    for ticker, obj in objetivos.items():
        if not isinstance(obj, dict):
            continue
        sube = obj.get("sube")
        baja = obj.get("baja")
        mercado = obj.get("mercado") if obj.get("mercado") in ("CLP", "USD") else None
        if sube is None and baja is None:
            continue  # sin objetivo activo, no hay nada que guardar/chequear
        limpio[ticker] = {
            "sube": float(sube) if isinstance(sube, (int, float)) else None,
            "baja": float(baja) if isinstance(baja, (int, float)) else None,
            "mercado": mercado,
        }

    try:
        with open(OBJETIVOS_FILE, "w") as f:
            json.dump(limpio, f, indent=2)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok", "objetivos": len(limpio)})


@app.route("/alertas-precio", methods=["GET"])
def ver_alertas_precio():
    """Diagnostico -- mismo token que /subscriptions. No hay datos sensibles aca."""
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401
    return jsonify(_leer_objetivos())


@app.route("/watchlist", methods=["POST"])
def guardar_watchlist():
    """
    Recibe la watchlist COMPLETA del frontend y reemplaza el archivo -- mismo
    criterio que /alertas-precio: el telefono es la fuente de verdad, el
    servidor solo necesita una copia fresca para saber a quien pedirle precio.

    Body: {"tickers": ["VRT", "AXON", ...]}

    La respuesta dice explicitamente que fue ACEPTADO y que fue RECHAZADO,
    con el motivo. Si el frontend manda 30 simbolos y el tope son 15, tiene
    que poder decirselo al usuario en vez de que 15 desaparezcan en silencio.
    """
    body = request.get_json(silent=True) or {}
    pedidos = body.get("tickers")
    if not isinstance(pedidos, list):
        return jsonify({"status": "error", "message": "falta 'tickers' (lista)"}), 400

    aceptados = _limpiar_watchlist(pedidos)
    universo, en_grilla = set(UNIVERSO_ANALISIS), set(TICKERS_USA)
    rechazados = {}
    for s in pedidos:
        if not isinstance(s, str):
            continue
        t = s.strip().upper()
        if not t or t in aceptados:
            continue
        if t in en_grilla:
            rechazados[t] = "ya esta en la grilla (ya recibe precio)"
        elif t not in universo:
            rechazados[t] = "no esta en el universo de analisis"
        else:
            rechazados[t] = f"se paso del tope de {WATCHLIST_MAX}"

    # OJO CON EL forzar=True DE MAS ABAJO. La app manda su watchlist CADA VEZ
    # que se abre (para repoblar el servidor despues de un despliegue, porque
    # el disco de Render es efimero). Si cada uno de esos POST forzara un
    # refresco de estadisticas, abrir la app tres veces seguidas dispararia
    # tres tandas de ~190 historiales de un año contra Yahoo -- exactamente
    # el 429 que todo este archivo trata de evitar.
    #
    # Por eso se compara con lo que ya habia: si la lista viene igual (el
    # caso normal, que es "la app se abrio"), no se fuerza nada y el ciclo
    # sigue su ritmo. Solo se fuerza cuando de verdad cambio.
    anterior = _leer_watchlist()
    cambio = anterior != aceptados

    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(aceptados, f, indent=2)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    if cambio:
        # Que el proximo /quotes-usa ya los traiga, sin esperar a que venza
        # el TTL: si no, el usuario agrega una accion y la ve "sin precio"
        # hasta 45 segundos despues, que parece que no funciono.
        _refrescar_precios_usa(forzar=True)
        _refrescar_stats_usa(forzar=True)

    return jsonify({"status": "ok", "aceptados": aceptados, "cambio": cambio,
                    "rechazados": rechazados, "tope": WATCHLIST_MAX})


@app.route("/watchlist", methods=["GET"])
def ver_watchlist():
    """Que simbolos extra esta siguiendo el servidor ahora mismo."""
    actual = _leer_watchlist()
    return jsonify({"tickers": actual, "total": len(actual), "tope": WATCHLIST_MAX,
                    "grilla_usa": len(TICKERS_USA),
                    "pedidos_por_ciclo": len(TICKERS_USA) + len(actual) + 1})


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
            "sma200": s.get("sma200"),
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
        # UF, UTM y dolar observado (CLP=X), los tres de Diario Financiero
        # (ver fuente_df.py). El dolar es el que habilita el ambiente 2 y
        # los totales consolidados de la seccion 4.2 de la especificacion.
        "uf": indicadores.get("uf"),
        "utm": indicadores.get("utm"),
        "usdclp": indicadores.get("usdclp"),
        "recibidos": len(data),
        "esperados": len(TICKERS),
        "cached_at": pc["ts"],
        "cache_ttl_seconds": PRICE_CACHE_TTL,
        "fuente": pc.get("fuente", "yahoo"),
        "serverTime": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/quotes-usa", methods=["GET"])
def quotes_usa():
    """
    Igual que /quotes pero para el ambiente 2 (EE.UU. · USD, ver main.
    TICKERS_USA). Sin indice propio (el S&P 500 no sale de Diario
    Financiero) y sin puntas de compra/venta (Yahoo tampoco las publica
    para EE.UU. en el plan gratuito que usa esta app). El frontend debe
    convertir con el mismo usdclp que ya recibe de /quotes -- no hay un
    segundo tipo de cambio aca.
    """
    st = _refrescar_stats_usa()
    pc = _refrescar_precios_usa()

    stats = st["stats"] or {}
    quotes_raw = pc["quotes"] or {}

    # La watchlist viaja en la MISMA respuesta que la grilla, no en un
    # endpoint aparte: para el frontend son acciones con precio igual que
    # cualquier otra, y ya vienen en la misma tanda. Lo unico que cambia es
    # que van marcadas, para que la app sepa que NO debe dibujarles tarjeta
    # en la grilla de EE.UU. -- solo existen para "esperando entrada" y para
    # que se puedan abrir en el detalle.
    watch = _leer_watchlist()
    data = {}
    for t in TICKERS_USA + watch:
        q = quotes_raw.get(t)
        if not q:
            continue
        s = stats.get(t, {})
        volumen = q.get("volume")
        data[t] = {
            "price": q["price"],
            "avg": s.get("avg90"),
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
            "rsi14": s.get("rsi14"),
            "sma20": s.get("sma20"),
            "sma50": s.get("sma50"),
            "sma200": s.get("sma200"),
            "zscore": round(s["zscore"], 2) if s.get("zscore") is not None else None,
            "volDiaria": s.get("volDiaria"),
            "montoMedioDiario30d": s.get("montoMedioDiario30d"),
            "bid": None, "ask": None, "bidSize": None, "askSize": None,
            "puntasDisponibles": False,
            "esWatchlist": t in watch,
        }

    indice = pc.get("index")
    return jsonify({
        "quotes": data,
        "index": {
            "value": indice["price"],
            "previousClose": indice.get("previousClose"),
            "marketTime": indice.get("marketTime"),
            "staleSeconds": indice.get("staleSeconds"),
            "fetchedAt": indice.get("fetchedAt"),
            "fuenteNombre": "Yahoo Finance (^GSPC)",
        } if indice else None,
        "indexDisponible": indice is not None,
        "recibidos": len(data),
        "esperados": len(TICKERS_USA) + len(watch),
        "watchlist": watch,
        "cached_at": pc["ts"],
        "cache_ttl_seconds": PRICE_CACHE_TTL,
        "fuente": "yahoo",
        "serverTime": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/signals", methods=["GET"])
def signals_endpoint():
    """
    Ranking con reglas explicitas. NO es una lista de compras sugeridas:
    cada item trae sus razones y sus banderas rojas para que puedas
    descartarlo. Ver el descargo en signals.py.

    `mercado=usa` (antes devolvia 404 a proposito -- ver INFORME.md /
    historial de este archivo): ahora SI evalua TICKERS_USA con las mismas
    reglas que Chile, usando el S&P 500 (^GSPC) como indice de referencia
    para la fuerza relativa y LIQUIDEZ_MINIMA_USD en vez de
    LIQUIDEZ_MINIMA_CLP (ver signals.py). El frontend ya estaba preparado
    para recibir 200 aca (ver fetchSignals() en index.html) -- no hace
    falta tocarlo.
    """
    mercado = request.args.get("mercado", "").lower()
    if mercado == "usa":
        st = _refrescar_stats_usa()
        pc = _refrescar_precios_usa()
        if not pc["quotes"]:
            return jsonify({"error": "sin precios disponibles ahora mismo",
                            "mercado": "usa"}), 503
        precios = {t: q["price"] for t, q in pc["quotes"].items()}
        reportes = _refrescar_reportes_usa()["data"] or {}
        resultado = signals.rankear(precios, st["stats"] or {}, st.get("indice"),
                                    moneda="USD", reportes=reportes)
        resultado["mercado"] = "usa"
        resultado["serverTime"] = datetime.now(timezone.utc).isoformat()
        return jsonify(resultado)

    st = _refrescar_stats()
    pc = _refrescar_precios()
    if not pc["quotes"]:
        return jsonify({"error": "sin precios disponibles ahora mismo"}), 503

    precios = {t: q["price"] for t, q in pc["quotes"].items()}
    resultado = signals.rankear(precios, st["stats"] or {}, st["indice"])
    resultado["serverTime"] = datetime.now(timezone.utc).isoformat()
    return jsonify(resultado)


@app.route("/rango5y", methods=["GET"])
def rango5y_endpoint():
    """
    Minimo y maximo de cierre de 5 años por ticker, para la franja "Rango 5
    años" de la tarjeta de lista (ver get_rango_5y() en data_source.py y
    _refrescar_rango5y() mas arriba). Igual que /signals, nunca bloquea: si
    todavia no hay nada calculado devuelve data=null y dispara el calculo
    en segundo plano, y el frontend simplemente omite esa franja hasta el
    proximo refresco.
    """
    mercado = "usa" if request.args.get("mercado", "").lower() == "usa" else "chile"
    cache = _refrescar_rango5y(mercado)
    return jsonify({"mercado": mercado, "data": cache["data"], "ts": cache["ts"]})


@app.route("/signal", methods=["GET"])
def signal_uno():
    ticker = request.args.get("ticker", "").upper()
    es_usa = ticker in TICKERS_USA
    if ticker not in TICKERS and not es_usa:
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400
    st = _refrescar_stats_usa() if es_usa else _refrescar_stats()
    pc = _refrescar_precios_usa() if es_usa else _refrescar_precios()
    q = (pc["quotes"] or {}).get(ticker)
    if not q:
        return jsonify({"error": "sin precio disponible para esa accion"}), 503
    moneda = "USD" if es_usa else "CLP"
    reporte = (_refrescar_reportes_usa()["data"] or {}).get(ticker) if es_usa else None
    return jsonify(signals.evaluar(ticker, q["price"],
                                   (st["stats"] or {}).get(ticker), st.get("indice"),
                                   moneda=moneda, reporte=reporte))


@app.route("/diagnostico", methods=["GET"])
def diagnostico():
    """
    Fuerza Relativa/Absoluta (diario) + Fases de Weinstein (semanal), ver
    indicador_fuerza_fase.py. Es el puerto de los dos indicadores .pine que
    Cristian usa en TradingView (clase de Inversapiens) -- mismo espiritu
    que /signal, pero con la logica de esos dos scripts en vez del modelo
    de puntaje compuesto de signals.py. Son dos lecturas DISTINTAS y
    complementarias, no reemplazan una a la otra.

    Devuelve {"diario": {...}, "semanal": {...}}, cada uno con
    "disponible": false y un "motivo" si todavia no hay suficiente
    historial (algo esperado los primeros minutos despues de que alguien
    abre por primera vez el detalle de un ticker que nunca se habia
    consultado -- ver _serie5y_ticker() mas arriba).
    """
    ticker = request.args.get("ticker", "").upper()
    es_usa = ticker in TICKERS_USA
    if ticker not in TICKERS and not es_usa:
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400

    mercado = "usa" if es_usa else "chile"

    # Una sola serie de 5 años para AMBOS calculos (bajo demanda, cache 24h
    # -- ver _serie5y_ticker mas arriba). El semanal SIEMPRE la necesita (30+
    # 30 semanas de ventana no entran en 1 año); el diario le alcanzaba con
    # 1 año, pero usar la misma de 5 aca tambien deja que "serieCasos" cubra
    # todo lo que el frontend pueda llegar a pedir para pintar el fondo del
    # grafico (hasta el boton "5A"), sin una segunda fuente de datos.
    puntos_5y = _serie5y_ticker(ticker, es_usa)
    indice_puntos = _indice5y(mercado)

    diario = indicador_fuerza_fase.evaluar_diario(puntos_5y or [], indice_puntos)
    semanal = indicador_fuerza_fase.evaluar_semanal(puntos_5y or [], indice_puntos)

    return jsonify({
        "ticker": ticker,
        "mercado": mercado,
        "diario": diario,
        "semanal": semanal,
        "serverTime": datetime.now(timezone.utc).isoformat(),
    })


VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "5y", "10y"}
# "8y" NO esta aca a proposito: Yahoo Finance no tiene ese rango nativo (sus
# rangos validos son 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max). El boton "8A"
# del frontend pide "10y" y recorta los ultimos 8 años en el navegador --
# mismo truco que ya usaba el rango personalizado Desde/Hasta.
_history_cache = {}
HISTORY_CACHE_TTL = 1800

# Serie del IPSA para /history: ver ipsa_historico.py. No necesita cache
# propio aca -- el CSV se cachea en memoria adentro de ese modulo (se lee
# una sola vez), y el dato de HOY viaja por fuente_df.get_index(), que ya
# tiene su propio TTL. Pedirla de nuevo en cada request es barato.

# Alias para pedir el S&P 500 (referencia del ambiente EE.UU., ver
# INDEX_SYMBOL_USA) por /history -- mismo patron que "IPSA"/"^IPSA" para el
# indice chileno. Lo usa el frontend para la comparacion "tu cartera vs. el
# indice" (seccion nueva de "Mi Cartera").
INDICE_USA_ALIASES = ("^GSPC", "SP500")


@app.route("/history", methods=["GET"])
def history():
    ticker = request.args.get("ticker", "").upper()
    period = request.args.get("period", "3mo").lower()
    es_usa = ticker in TICKERS_USA
    es_indice_usa = ticker in INDICE_USA_ALIASES
    if (ticker not in TICKERS and not es_usa
            and ticker not in ("IPSA", "^IPSA") and not es_indice_usa):
        return jsonify({"error": f"ticker '{ticker}' no reconocido"}), 400
    if period not in VALID_PERIODS:
        return jsonify({"error": f"period debe ser uno de {sorted(VALID_PERIODS)}"}), 400

    # El IPSA es aparte: Yahoo tiene rota su serie historica (ver el
    # comentario largo en data_source.py). Para este simbolo puntual
    # /history sale del CSV historico local + el dato de hoy en vivo (ver
    # ipsa_historico.py) en vez de pedirselo a Yahoo.
    if ticker in ("IPSA", "^IPSA"):
        completa = ipsa_historico.obtener_serie_combinada()
        puntos = filtrar_puntos_por_periodo(completa, period)
        return jsonify({"ticker": "IPSA", "period": period, "points": puntos,
                        "origen": "csv_local" if completa else "no_disponible"})

    # Si la serie anual ya esta en cache, se recorta de ahi en vez de
    # volver a pedirsela a Yahoo. Menos peticiones = menos rate limiting.
    # Las series de ambiente 2 viven en una cache separada (ver
    # _refrescar_stats_usa) porque no comparten ciclo con las de Chile.
    st = _refrescar_stats_usa() if (es_usa or es_indice_usa) else _refrescar_stats()
    dias = {"1mo": 21, "3mo": 63, "6mo": 126, "ytd": None, "1y": 252}.get(period)
    serie = (st.get("series") or {}).get(ticker)
    if serie and dias and len(serie) >= dias:
        return jsonify({"ticker": ticker, "period": period,
                        "points": serie[-dias:], "origen": "cache"})

    key = (ticker, period)
    ahora = time.time()
    c = _history_cache.get(key)
    if c is None or (ahora - c["ts"]) > HISTORY_CACHE_TTL:
        # get_price_history() ya sabe mapear "IPSA"/"^IPSA" -> INDEX_SYMBOL
        # (^IPSA) internamente; para el S&P 500 se pasa el simbolo real
        # (^GSPC) directo, porque data_source.py no conoce el alias "SP500".
        simbolo_pedido = INDEX_SYMBOL_USA if es_indice_usa else ticker
        suf = "" if (es_usa or es_indice_usa) else None
        _history_cache[key] = {"data": get_price_history(simbolo_pedido, period, suffix=suf), "ts": ahora}
    return jsonify({"ticker": ticker, "period": period,
                    "points": _history_cache[key]["data"], "origen": "yahoo"})


@app.route("/news", methods=["GET"])
def news_endpoint():
    ticker = request.args.get("ticker", "").upper()
    if ticker not in TICKERS and ticker not in TICKERS_USA:
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
# Mismo patron que _alert_state, pero separado para TICKERS_USA -- aunque
# hoy ningun ticker se repite entre los dos mercados, mantenerlos aparte
# evita que un futuro choque de simbolos (ej. alguien agrega a mano el mismo
# nemotecnico a los dos universos) pise el estado de "compra"/"venta" del
# otro mercado.
_alert_state_usa = {}

# ---- Alertas acumuladas para el correo consolidado -------------------------
# Antes cada senal de compra/venta y cada precio objetivo mandaba su PROPIO
# correo apenas se disparaba. Un dia de movimiento amplio de mercado (varias
# decenas de acciones cruzando su umbral casi juntas -- mas facil aun con
# TICKERS_USA sumado) manda decenas de correos en minutos: eso fue lo que
# paso el dia que se agrego el bloque de EE.UU.
#
# /run-check sigue corriendo tan seguido como antes (no se puede espaciar
# sin arriesgarse a perder un cruce), pero ya NO manda correo por cada uno:
# junta cada alerta en esta lista en memoria, y un endpoint aparte
# (/enviar-digesto) -- llamado por el cron externo SOLO a horas fijas, ver
# monitor.yml -- manda UN correo con todo lo acumulado desde el ultimo envio
# y vacia la lista. El push SI sigue siendo inmediato (no se toco): son
# notificaciones cortas que no llenan una bandeja de entrada como un correo.
_alertas_pendientes = []

# A que hora (Chile) mandar el digesto, y con que huso horario.
#
# ANTES esto se decidia en monitor.yml con horas fijas en UTC -- y Chile
# cambia de huso horario dos veces al año (horario de invierno/verano), asi
# que habia que editar el cron a mano cada vez o se empezaba a desfasar.
#
# AHORA la decision de "es una de las horas que corresponde" se toma ACA,
# con la hora real de Chile via zoneinfo (America/Santiago) -- ese modulo
# conoce el cambio de horario solo, a traves de la base de datos de husos
# horarios de IANA (paquete `tzdata` en requirements.txt), la misma que
# usan los sistemas operativos y los navegadores. monitor.yml solo necesita
# llamar a /enviar-digesto seguido durante una ventana ANCHA en UTC que
# cubra ambos horarios -- nunca mas hay que tocarlo por un cambio de hora.
TZ_CHILE = ZoneInfo("America/Santiago")
HORAS_DIGESTO_CHILE = set(range(9, 19))  # 9:00, 10:00, ..., 18:00

# Ultima hora (Chile) en que YA se mando un digesto, para no reenviar si el
# cron externo llama /enviar-digesto varias veces dentro de la misma hora
# (corre cada 10 minutos). En memoria a proposito, igual que _alert_state:
# si el servidor se reinicia a mitad de una hora ya despachada, en el peor
# caso se manda un digesto de mas esa hora (o uno vacio) -- nunca uno de
# menos, y de todas formas ya no son 100 correos sueltos.
_ultima_hora_digesto_enviada = None  # "AAAA-MM-DD-HH" en hora de Chile

# Mismo mecanismo que arriba, aplicado a /resumen-diario (el latido de una
# vez al dia): antes tambien dependia de una hora fija en UTC en monitor.yml
# con el mismo problema de horario de verano/invierno. Aca solo importa la
# HORA (no el minuto exacto -- el cron externo puede llegar unos minutos
# tarde) y que no se haya mandado ya hoy.
HORA_RESUMEN_DIARIO_CHILE = 16  # ~16:30 Chile, igual que antes
_ultimo_dia_resumen_enviado = None  # "AAAA-MM-DD" en hora de Chile

# Estado en memoria de los precios objetivo ("Mi Cartera"): guarda si CADA
# objetivo (ticker + direccion "sube"/"baja") ya estaba cruzado en el
# chequeo anterior, para avisar solo al ENTRAR al cruce -- mismo criterio
# que _alert_state de arriba. Vive en memoria (no en OBJETIVOS_FILE) a
# proposito: si se pierde en un redeploy, en el peor caso se manda UN aviso
# de mas la primera vez que vuelve a cruzar, nunca uno de menos.
_estado_objetivos = {}


def _evaluar_objetivos(quotes_map, mercado):
    """
    Revisa los precios objetivo guardados via POST /alertas-precio contra
    los precios actuales de ESTE mercado ('CLP' o 'USD') y dispara push +
    acumula el correo cuando el precio CRUZA un objetivo (no en cada chequeo
    mientras se mantenga cruzado -- igual criterio que las señales de zscore).
    El correo sale consolidado por /enviar-digesto, igual que las señales.

    Corre para los dos mercados igual que los dos bucles de senales tecnicas
    de mas abajo (Chile y EE.UU.): el objetivo lo pone el usuario a mano, no
    depende de indicadores tecnicos, asi que no tiene la limitacion que
    tenian esos bucles antes de que EE.UU. tuviera su propia evaluacion.
    """
    objetivos = _leer_objetivos()
    disparadas = []
    for ticker, obj in objetivos.items():
        if obj.get("mercado") and obj.get("mercado") != mercado:
            continue
        q = quotes_map.get(ticker)
        precio = q.get("price") if isinstance(q, dict) else None
        if precio is None:
            continue

        for direccion, campo in (("sube", "sube"), ("baja", "baja")):
            objetivo = obj.get(campo)
            if objetivo is None:
                continue
            cruzado_ahora = precio >= objetivo if direccion == "sube" else precio <= objetivo
            clave = f"{ticker}:{direccion}"
            estaba_cruzado = _estado_objetivos.get(clave, False)
            _estado_objetivos[clave] = cruzado_ahora
            if not (cruzado_ahora and not estaba_cruzado):
                continue

            monto = precio - objetivo
            pct = (monto / objetivo * 100) if objetivo else None
            try:
                notify.send_price_target_push(ticker, direccion, precio, objetivo, monto, pct, mercado)
            except Exception as e:
                print(f"[run-check] push de objetivo {ticker}: {e}")

            _alertas_pendientes.append({
                "tipo": "objetivo", "mercado": mercado, "ticker": ticker, "direccion": direccion,
                "precio": precio, "objetivo": objetivo, "monto": monto, "pct": pct,
            })

            disparadas.append({"ticker": ticker, "direccion": direccion, "precio": precio,
                               "objetivo": objetivo, "monto": monto, "pct": pct, "mercado": mercado})
    return disparadas


def _direccion_senal(ev):
    """
    'compra', 'venta' o None, segun signals.candidato_fuerte().

    Antes esto era un umbral aparte (-4% fijo, despues z <= -1.5), y mas
    tarde uso el puntaje compuesto (|puntaje| >= 20). Ese segundo umbral
    quedo DESACTUALIZADO cuando se endurecio el filtro de candidatos_compra/
    candidatos_venta en signals.rankear() (ver RSI_CANDIDATO_*/Z_CANDIDATO_ABS
    y candidato_fuerte() en signals.py): con |puntaje| >= 20 el correo y el
    push seguian disparando para decenas de tickers aunque el panel "Analisis
    del momento" ya mostrara una lista mucho mas corta -- exactamente la
    desincronizacion que ese ajuste queria evitar.

    Ahora usa el MISMO gate que arma esas listas: exige RSI extremo Y
    z-score extremo A LA VEZ (signals.candidato_fuerte()), no el puntaje
    compuesto solo. Asi lo que dispara el correo y el push es exactamente
    lo que el panel ya te muestra como candidato fuerte.

    Excepcion: si la accion trae la bandera "CAIDA SOSTENIDA", NO se avisa
    aunque cumpla el gate de compra. Esa bandera ya le resta puntaje en
    signals.evaluar(), pero eso no bloquea el gate por RSI/z-score -- y
    avisar "posible compra" de algo que esta cayendo de forma sostenida es
    justo la senal enganosa que la bandera existe para marcar.
    """
    if not ev or ev.get("puntaje") is None:
        return None
    if any(b.startswith("CAIDA SOSTENIDA") for b in ev.get("banderas", [])):
        return None
    if signals.candidato_fuerte(ev, es_venta=False):
        return "compra"
    if signals.candidato_fuerte(ev, es_venta=True):
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

        # El correo YA NO sale de aca -- se acumula en _alertas_pendientes y
        # sale consolidado por /enviar-digesto (ver ese endpoint mas abajo).
        try:
            notify.send_push_alert(t, direccion, precio, s.get("avg90"), signals.describe(ev))
        except Exception as e:
            print(f"[run-check] push {t}: {e}")

        _alertas_pendientes.append({
            "tipo": "senal", "mercado": "CLP", "ticker": t, "direccion": direccion,
            "precio": precio, "resumen": signals.describe(ev),
        })

        alertadas.append({"ticker": t, "direccion": direccion, "precio": precio,
                          "puntaje": ev.get("puntaje") if ev else None,
                          "banderas": len(ev.get("banderas", [])) if ev else 0,
                          "mercado": "CLP"})

    # ---- Senales tecnicas (signals.py) para EE.UU. -------------------------
    # Mismo criterio que el bucle de Chile de arriba (mismo _direccion_senal,
    # mismo gate candidato_fuerte()), pero sobre TICKERS_USA y con la cache
    # separada del ambiente 2. A proposito NUNCA se fuerza aca una descarga
    # bloqueante de 107 historiales dentro del ciclo de peticion (ver el
    # bloque "POR QUE LAS ACTUALIZACIONES CORREN EN SEGUNDO PLANO" mas
    # arriba): _refrescar_stats_usa() sin forzar=True solo dispara un
    # refresco en segundo plano si esta vencida la cache, y este bucle
    # evalua con lo que YA este en cache ahora mismo (puede quedar vacio los
    # primeros minutos despues de un despliegue, igual que le pasa hoy a
    # /signals?mercado=usa).
    st_usa = _refrescar_stats_usa()
    _refrescar_precios_usa()
    stats_usa = st_usa["stats"] or {}
    quotes_usa_map = _price_cache_usa["quotes"] or {}
    for t in TICKERS_USA:
        q, s = quotes_usa_map.get(t), stats_usa.get(t)
        if not q or not s:
            continue
        precio = q["price"]
        ev = signals.evaluar(t, precio, s, st_usa.get("indice"), moneda="USD")
        direccion = _direccion_senal(ev)
        anterior = _alert_state_usa.get(t)
        _alert_state_usa[t] = direccion
        if not direccion or direccion == anterior:
            continue

        # Igual que en el bucle de Chile: el correo se acumula, no se manda aca.
        try:
            notify.send_push_alert(t, direccion, precio, s.get("avg90"), signals.describe(ev), mercado="USD")
        except Exception as e:
            print(f"[run-check] push {t}: {e}")

        _alertas_pendientes.append({
            "tipo": "senal", "mercado": "USD", "ticker": t, "direccion": direccion,
            "precio": precio, "resumen": signals.describe(ev),
        })

        alertadas.append({"ticker": t, "direccion": direccion, "precio": precio,
                          "puntaje": ev.get("puntaje") if ev else None,
                          "banderas": len(ev.get("banderas", [])) if ev else 0,
                          "mercado": "USD"})

    # ---- Precios objetivo del usuario ("Mi Cartera") -----------------------
    # Cubre Chile (con los `quotes` recien obtenidos arriba) Y EE.UU. En
    # EE.UU. NUNCA se fuerza una descarga bloqueante de 107 tickers dentro
    # de esta peticion (ver el bloque "POR QUE LAS ACTUALIZACIONES CORREN EN
    # SEGUNDO PLANO" mas arriba) -- solo se pide que refresque en segundo
    # plano si esta vencida, y se evalua con lo que haya en cache ahora
    # mismo (puede ser de hasta PRICE_CACHE_TTL segundos atras).
    objetivos_disparados = _evaluar_objetivos(quotes, "CLP")
    _refrescar_precios_usa()
    objetivos_disparados += _evaluar_objetivos(_price_cache_usa["quotes"] or {}, "USD")

    return jsonify({
        "estado": "ok",
        "evaluadas": len([t for t in TICKERS if t in quotes]),
        "esperadas": len(TICKERS),
        "evaluadas_usa": len([t for t in TICKERS_USA if t in quotes_usa_map and t in stats_usa]),
        "esperadas_usa": len(TICKERS_USA),
        "alertas_disparadas": alertadas,
        "objetivos_disparados": objetivos_disparados,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _fmt_monto_digesto(valor, mercado):
    """CLP sin decimales, USD con 2 -- mismo criterio que notify._fmt_monto,
    copiado aca en vez de importado porque es privado de ese modulo."""
    if mercado == "USD":
        return f"US$ {valor:,.2f}"
    return f"CL$ {valor:,.0f}"


def _texto_digesto(alertas):
    """
    Arma el cuerpo del correo consolidado a partir de lo acumulado en
    _alertas_pendientes desde el ultimo envio. Una linea por alerta -- con
    varias decenas acumuladas (dia de mercado con movimiento amplio) un
    correo con el detalle completo de cada una, como mandaba send_alert()
    antes por separado, seria ilegible. Se agrupa por mercado y tipo para
    poder escanearlo rapido y decidir a cuales vale la pena entrarle en la
    app -- el detalle completo (banderas, razones) sigue disponible ahi,
    tocando el ticker.
    """
    senales = [a for a in alertas if a["tipo"] == "senal"]
    objetivos = [a for a in alertas if a["tipo"] == "objetivo"]

    lineas = [f"Resumen de alertas · {datetime.now().strftime('%d/%m/%Y %H:%M')}", ""]

    def _bloque_senales(titulo, mercado):
        items = [a for a in senales if a["mercado"] == mercado]
        if not items:
            return []
        out = [titulo]
        for a in items:
            palabra = "posible venta" if a["direccion"] == "venta" else "posible compra"
            out.append(f"  {a['ticker']} ({palabra}): "
                       f"{_fmt_monto_digesto(a['precio'], mercado)} · {a['resumen']}")
        out.append("")
        return out

    lineas += _bloque_senales("SEÑALES · CHILE (CLP)", "CLP")
    lineas += _bloque_senales("SEÑALES · EE.UU. (USD)", "USD")

    if objetivos:
        lineas.append("PRECIOS OBJETIVO ALCANZADOS")
        for a in objetivos:
            verbo = "subió a" if a["direccion"] == "sube" else "bajó a"
            pct_txt = (f"{'+' if a['pct'] is not None and a['pct'] >= 0 else ''}{a['pct']:.1f}%"
                       if a["pct"] is not None else "s/d")
            lineas.append(f"  {a['ticker']} {verbo} {_fmt_monto_digesto(a['precio'], a['mercado'])} "
                          f"(objetivo {_fmt_monto_digesto(a['objetivo'], a['mercado'])}, {pct_txt})")
        lineas.append("")

    lineas.append(f"👉 Abrir la app: {notify.FRONTEND_URL}")
    lineas.append("")
    lineas.append(signals.DESCARGO)
    return "\n".join(lineas)


@app.route("/enviar-digesto", methods=["GET", "POST"])
def enviar_digesto():
    """
    Manda UN correo con todas las alertas (señales de compra/venta y precios
    objetivo cruzados) acumuladas desde el último envío, y vacía la cola.

    El cron externo (monitor.yml, job "digesto-alertas") llama esto seguido
    -- cada 10 minutos, dentro de una ventana ANCHA en UTC que cubre 9:00 a
    18:00 Chile tanto en horario de invierno como de verano. La decisión de
    si REALMENTE corresponde mandar el correo ahora se toma AQUÍ, con la
    hora real de Chile (ver TZ_CHILE/HORAS_DIGESTO_CHILE más arriba) -- así
    monitor.yml no necesita editarse nunca más por el cambio de hora.
    /run-check sigue corriendo cada 10 minutos igual que antes para no
    perder ningún cruce; simplemente ya no manda correo él mismo (ver
    _alertas_pendientes más arriba).

    Si no se acumuló nada, NO manda correo -- evita el "sin novedades" cada
    hora. El latido diario de /resumen-diario sigue siendo el que garantiza
    que llegue algo aunque de verdad no haya pasado nada en el día.
    """
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    global _alertas_pendientes, _ultima_hora_digesto_enviada

    ahora_chile = datetime.now(TZ_CHILE)
    # Defensivo: monitor.yml ya restringe a lunes-viernes, pero si algun dia
    # se llama a mano (workflow_dispatch) un sabado, mejor no mandar nada.
    if ahora_chile.weekday() >= 5 or ahora_chile.hour not in HORAS_DIGESTO_CHILE:
        return jsonify({"estado": "fuera_de_horario", "hora_chile": ahora_chile.isoformat()})

    clave_hora = ahora_chile.strftime("%Y-%m-%d-%H")
    if clave_hora == _ultima_hora_digesto_enviada:
        return jsonify({"estado": "ya_enviado_esta_hora", "hora_chile": ahora_chile.isoformat()})
    _ultima_hora_digesto_enviada = clave_hora

    if not _alertas_pendientes:
        return jsonify({"estado": "sin_novedades", "enviadas": 0, "hora_chile": ahora_chile.isoformat()})

    pendientes, _alertas_pendientes = _alertas_pendientes, []
    n = len(pendientes)
    texto = _texto_digesto(pendientes)
    try:
        ok = notify.send_raw_email(f"IPSA Monitor · {n} alerta{'s' if n != 1 else ''}", texto)
    except Exception as e:
        # No se pierden las alertas si el envío falla: vuelven a la cola
        # para el próximo intento en vez de desaparecer en silencio.
        _alertas_pendientes = pendientes + _alertas_pendientes
        print(f"[enviar-digesto] fallo el envio: {e}")
        return jsonify({"estado": "error_envio", "detalle": str(e)}), 500

    if not ok:
        # send_raw_email devuelve False (sin excepcion) cuando el correo no
        # esta configurado (falta RESEND_API_KEY o EMAIL_TO) -- mismo caso,
        # se reencolan para cuando se configure.
        _alertas_pendientes = pendientes + _alertas_pendientes

    return jsonify({"estado": "enviado" if ok else "no_configurado", "enviadas": n})


def _texto_alerta(ev):
    """
    Cuerpo detallado (banderas + razones + glosario) para una alerta
    individual. Ya no se usa desde /run-check (que ahora acumula un resumen
    de una linea en _alertas_pendientes, ver _texto_digesto) -- se deja
    definida por si algun dia se quiere un correo de detalle a pedido para
    un ticker puntual.
    """
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

    El cron externo llama esto seguido dentro de una ventana ancha en UTC
    (ver monitor.yml) -- la hora real de Chile (HORA_RESUMEN_DIARIO_CHILE)
    decide aca adentro si corresponde mandarlo, igual que /enviar-digesto,
    asi que tampoco necesita tocarse por el cambio de horario.
    """
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    global _ultimo_dia_resumen_enviado
    ahora_chile = datetime.now(TZ_CHILE)
    hoy = ahora_chile.strftime("%Y-%m-%d")
    if ahora_chile.weekday() >= 5 or ahora_chile.hour != HORA_RESUMEN_DIARIO_CHILE:
        return jsonify({"estado": "fuera_de_horario", "hora_chile": ahora_chile.isoformat()})
    if hoy == _ultimo_dia_resumen_enviado:
        return jsonify({"estado": "ya_enviado_hoy", "hora_chile": ahora_chile.isoformat()})
    # Se marca ANTES de intentar (no solo si sale bien): la ventana del cron
    # dura mas de una hora y _refrescar_stats(forzar=True) bloquea -- no
    # conviene reintentarlo cada 10 minutos toda la ventana si Yahoo esta
    # caido. Si falla, igual llega el correo de "resumen SIN DATOS" de abajo.
    _ultimo_dia_resumen_enviado = hoy

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

    # Agregar GLOSARIO (puede ser diccionario o string)
    glosario = signals.GLOSARIO
    if isinstance(glosario, dict):
        lineas.append("CONCEPTOS CLAVE:")
        if "criterios" in glosario:
            for clave, desc in glosario.get("criterios", {}).items():
                lineas.append(f"  {clave.replace('_', ' ').upper()}: {desc}")
    else:
        lineas.append(glosario)

    lineas.append("")
    lineas.append(signals.DESCARGO)

    texto = "\n".join(lineas)
    try:
        notify.send_raw_email("IPSA Monitor · resumen del dia", texto)
    except Exception as e:
        print(f"[resumen] no se pudo enviar: {e}")
        return jsonify({"estado": "error_envio", "detalle": str(e)}), 500

    return jsonify({"estado": "enviado", "texto": texto})


@app.route("/ipsa-valor-hoy", methods=["GET"])
def ipsa_valor_hoy():
    """
    Valor del IPSA de HOY (hora Chile), en JSON simple -- pensado para que
    lo lea el workflow de GitHub Actions (monitor.yml, job
    "guardar-ipsa-historico") y agregue una fila nueva a
    ipsa_historico.csv una vez al dia. Ver la seccion "COMO SE RELLENA EL
    CSV PERMANENTEMENTE" en ipsa_historico.py para el porque.

    No hace que el workflow tenga que scrapear Diario Financiero de nuevo:
    reusa fuente_df.get_index(), que ya tiene su propia cache de 20
    segundos, asi que llamar esto seguido no cuesta nada extra.

    503 si Diario Financiero no respondio -- el workflow debe leerlo como
    "hoy todavia no hay nada que guardar", no como una falla del servicio.
    """
    if CHECK_SECRET and request.args.get("token") != CHECK_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    index = fuente_df.get_index()
    if not index or index.get("value") is None:
        return jsonify({"estado": "sin_dato"}), 503

    hoy = datetime.now(TZ_CHILE).strftime("%Y-%m-%d")
    return jsonify({"estado": "ok", "fecha": hoy, "valor": round(index["value"], 2)})


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
        "modo_alerta": "compra_venta (signals.candidato_fuerte(): RSI y z-score extremos a la vez -- mismo gate que candidatos_compra/venta; cubre TICKERS y TICKERS_USA)",
        "alertas_pendientes_de_correo": len(_alertas_pendientes),
        "ultima_hora_digesto_enviada": _ultima_hora_digesto_enviada,
        "hora_chile_ahora": datetime.now(TZ_CHILE).isoformat(),
        "aviso_digesto": ("El correo de senales/objetivos ya NO sale de /run-check: se acumula aca y "
                          "sale consolidado desde /enviar-digesto, de 9:00 a 18:00 Chile cada hora en "
                          "punto (hora real via zoneinfo, no requiere ajuste por horario de verano)."),
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
    info = notify.vapid_diagnostico()

    # Ficha por dispositivo suscrito, SIN exponer las claves. Lo unico que
    # se necesita para diagnosticar es a que servicio de push pertenece
    # cada suscripcion: el host dice si es un iPhone, un Chrome de
    # escritorio o un Firefox, y con eso se sabe donde mirar cuando el
    # envio sale bien pero la notificacion no aparece.
    servicios = {
        "web.push.apple.com": "iPhone/iPad/Mac (Safari · PWA en pantalla de inicio)",
        "fcm.googleapis.com": "Chrome / Edge / Android",
        "updates.push.services.mozilla.com": "Firefox",
        "wns2-": "Windows (Edge antiguo)",
    }
    fichas = []
    for sub in _leer_subs():
        ep = sub.get("endpoint") or ""
        host = ep.split("/")[2] if "://" in ep else "?"
        cual = next((v for k, v in servicios.items() if k in host), "desconocido")
        keys = sub.get("keys") or {}
        fichas.append({
            "servicio": host,
            "probablemente": cual,
            # Solo la cola del endpoint, suficiente para distinguir dos
            # dispositivos entre si sin publicar el identificador entero.
            "endpoint_termina_en": ep[-12:] if ep else None,
            "trae_claves": bool(keys.get("p256dh") and keys.get("auth")),
        })
    info["dispositivos"] = fichas
    return jsonify(info)


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


@app.route("/universo-diag", methods=["GET"])
def universo_diag():
    """
    Estado del universo: cuantos simbolos hay en cada lista y cuales
    quedaron en cuarentena por no existir en Yahoo.

    POR QUE EXISTE
    ==============
    El universo de analisis pasó a ser el S&P 500 + Nasdaq-100 (~536
    simbolos). En una lista de ese tamaño siempre hay alguno renombrado o
    salido de bolsa, y antes la unica forma de enterarse era leer los logs
    de Render linea por linea. Esto lo responde en una peticion.

    `cuarentena` se llena sola: data_source.py anota el simbolo la primera
    vez que Yahoo responde que no existe, y deja de pedirlo. La lista se
    vacia en cada reinicio del servidor a proposito -- si un simbolo vuelve
    a existir, o si se marco por error, el proximo despliegue le da otra
    oportunidad (ver el comentario de _MUERTOS en data_source.py).

    NO hace ninguna peticion a Yahoo: solo lee lo que ya se sabe.
    """
    muertos = simbolos_en_cuarentena()
    solo_analisis = sorted(set(UNIVERSO_ANALISIS) - set(TICKERS_USA))
    watch = _leer_watchlist()
    return jsonify({
        "grilla": {
            "chile": len(TICKERS),
            "usa": len(TICKERS_USA),
            "nota": "Lo que la app dibuja como tarjetas y refresca cada ciclo.",
        },
        "watchlist": {
            "tickers": watch,
            "total": len(watch),
            "tope": WATCHLIST_MAX,
            "peticiones_usa_por_ciclo": len(TICKERS_USA) + len(watch) + 1,
            "nota": "Candidatas de Explorar que NO estan en la grilla. Reciben "
                    "precio en la misma tanda que la grilla (por eso el push de "
                    "precio objetivo les funciona igual), pero la app no les "
                    "dibuja tarjeta. El +1 de las peticiones es el S&P 500.",
        },
        "analisis": {
            "total": len(UNIVERSO_ANALISIS),
            "solo_en_analisis": len(solo_analisis),
            "nota": ("Universo BASE del embudo (S&P 500 + Nasdaq-100 + la "
                     "grilla). /explorar/run lo amplia con el mercado "
                     "completo -- ver 'mercado_completo' aca abajo. Solo se "
                     "recorre cuando se pide el analisis a mano, NUNCA en "
                     "el ciclo automatico."),
        },
        "mercado_completo": dict(
            universo_mercado.estado_cache(),
            nota_extra=("Se une al universo base en cada /explorar/run "
                        "(sin duplicar, sin ETF). Si 'motivo_error' no es "
                        "null, la ultima corrida uso solo el universo base "
                        "-- revisa ese texto."),
        ),
        "cuarentena": {
            "total": len(muertos),
            "simbolos": muertos,
            "nota": ("Simbolos que Yahoo dice que no existen. Se dejan de "
                     "pedir hasta el proximo reinicio. Si alguno de estos te "
                     "importa, revisa si cambio de simbolo y corrigelo en "
                     "main.py Y en index.html."),
        },
    })


# ==========================================================================
# EXPLORAR -- el metodo completo, a pedido
# ==========================================================================
# Ver explorar.py para el pipeline. Aca solo viven los tres endpoints que lo
# disparan y lo consultan.
#
# POR QUE SON TRES Y NO UNO
# =========================
# Una corrida tarda entre 3 y 6 minutos (536 historiales + fundamentales).
# Con UN worker de gunicorn, hacer eso dentro de la peticion dejaria la app
# entera colgada todo ese rato -- y ademas el celular cortaria por timeout
# mucho antes. Asi que: /explorar/run arranca y devuelve al tiro,
# /explorar/estado dice como va, y /explorar/resultado entrega lo ultimo que
# se calculo.

@app.route("/explorar/run", methods=["POST", "GET"])
def explorar_run():
    """
    Arranca el analisis. Devuelve de inmediato -- NO espera el resultado.

    Si ya hay uno corriendo devuelve 409 en vez de encolar otro: dos
    corridas simultaneas son ~1.000 peticiones en paralelo a Yahoo y un 429
    garantizado para todo el servidor, incluido el ciclo normal de precios.

    Parametros opcionales (para mover los umbrales del embudo desde la app):
      ?precio=10&capB=2&volM=2&crecimiento=25

    UNIVERSO: se amplia UNIVERSO_ANALISIS (S&P 500 + Nasdaq-100 + grilla)
    con el mercado completo de EE.UU. via universo_mercado.py -- a pedido
    de Cristian, para que el embudo mire lo mismo que TradingView mira
    cuando no se le fija un indice. Si esa descarga falla (o nunca se pudo
    hacer), sigue con el universo base solo: nunca bloquea la corrida. Ver
    universo_mercado.py para el detalle.
    """
    def _num(nombre):
        v = request.args.get(nombre)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    umbrales = {k: _num(k) for k in ("precio", "capB", "volM", "crecimiento")}
    universo, info_universo = universo_mercado.ampliar_universo(
        UNIVERSO_ANALISIS, ETFS_NO_ANALIZAR)
    arranco, motivo = explorar.iniciar(
        universo, lambda t: _serie5y_ticker(t, True),
        lambda: _indice5y("usa"), umbrales)

    if not arranco:
        return jsonify({"arrancado": False, "motivo": motivo,
                        "estado": explorar.estado()}), 409
    return jsonify({"arrancado": True, "universo": len(universo),
                    "universoInfo": info_universo,
                    "estado": explorar.estado()})


@app.route("/fundamentales-diag", methods=["GET"])
def fundamentales_diag():
    """
    Por que una accion no trae capitalizacion / crecimiento / sector.

    POR QUE EXISTE
    ==============
    La primera corrida real de Explorar devolvio 0 candidatas, y el embudo
    mostraba: "SMA 50 > SMA 200: 127 -> Capitalizacion >= 2 B: 0". Las 127
    se cayeron por falta de dato, no por ser chicas. Desde afuera eso es
    indistinguible de "el mercado no dio nada", y para saber cual de las dos
    era hubo que leer codigo.

    Esto lo contesta en una peticion: prueba las DOS fuentes para el mismo
    ticker y devuelve el codigo de respuesta de cada una, mas el estado del
    crumb (el token de sesion que Yahoo ahora exige para estos endpoints).

        /fundamentales-diag?ticker=NVDA

    SI HACE PETICIONES A LA RED -- es un diagnostico, no un endpoint de la
    app. No lo llames en bucle.
    """
    ticker = (request.args.get("ticker") or "NVDA").strip().upper()

    # OJO: data_source se importa con nombres sueltos (ver el import de
    # arriba), no como modulo. Escribir `data_source.market_caps(...)` acá
    # tumba la ruta con NameError -- ya pasó una vez con
    # simbolos_en_cuarentena en /universo-diag.
    caps, motivos_lote = market_caps([ticker])
    _t, datos, motivo_ficha = explorar._fundamentales_uno(ticker)
    # El crecimiento TTM es la tercera fuente y tiene su propio endpoint en
    # Yahoo, asi que puede fallar por su cuenta -- de hecho es lo que paso en
    # la corrida del 22/08: capitalizacion y ficha funcionaban y el TTM daba
    # 0 de 104, sin ninguna pista de por que.
    ttm, motivo_ttm = explorar._crecimiento_ttm_uno(ticker, con_motivo=True)

    return jsonify({
        "ticker": ticker,
        "crumb": estado_crumb(),
        "porTTM": {
            "datos": ttm,
            "funciono": bool(ttm),
            "motivo": motivo_ttm,
        },
        "porLote": {
            "capB": caps.get(ticker),
            "motivos": motivos_lote,
            "funciono": ticker in caps,
        },
        "porFicha": {
            "motivo": motivo_ficha,
            "funciono": datos is not None,
            "datos": datos,
        },
        "comoLeerlo": {
            "crumb.tiene=false": "Yahoo no entrego el token de sesion. Ninguna "
                                 "de las dos fuentes va a funcionar; mira "
                                 "crumb.motivo.",
            "HTTP 401 o 403": "El crumb existe pero Yahoo lo rechaza.",
            "HTTP 429": "Demasiadas peticiones. Espera unos minutos.",
            "las dos funcionan": "El problema no es de red: es que esa accion "
                                 "de verdad no publica el dato.",
        },
    })


@app.route("/explorar/estado", methods=["GET"])
def explorar_estado():
    """Como va la corrida. Barato: no toca la red, solo lee variables."""
    return jsonify(explorar.estado())


@app.route("/explorar/resultado", methods=["GET"])
def explorar_resultado():
    """
    El ultimo analisis calculado, completo. Queda en memoria 24h.

    Si nunca se corrio devuelve 404 con `hayResultado: false` -- la app
    muestra "sin datos todavia" y el boton de correr, en vez de un error.
    Si el servidor se reinicio, el resultado se pierde: es memoria, igual
    que las suscripciones de push (disco efimero de Render). Correr de nuevo
    tarda lo mismo que la primera vez.
    """
    datos = explorar.resultado()
    if datos is None:
        return jsonify({"hayResultado": False, "estado": explorar.estado()}), 404
    return jsonify({"hayResultado": True, "estado": explorar.estado(), "analisis": datos})


@app.route("/explorar/resumen", methods=["GET"])
def explorar_resumen():
    """
    Lo mismo que /explorar/resultado pero SIN el array `acciones`.

    POR QUE EXISTE
    ==============
    `acciones` trae las ~534 filas con las metricas crudas de cada accion.
    Eso es lo que le permite a la app rehacer el embudo al instante cuando
    mueves un umbral, y tiene que estar. Pero pesa unos 100 KB, y deja
    /explorar/resultado inservible para mirarlo a ojo en el navegador: el
    resumen queda enterrado bajo cientos de filas.

    Esto devuelve solo la parte que se lee: la alerta, el embudo paso a paso,
    las finalistas, las de revisar a mano y de donde salieron los datos.
    """
    datos = explorar.resultado()
    if datos is None:
        return jsonify({"hayResultado": False, "estado": explorar.estado()}), 404
    liviano = {k: v for k, v in datos.items() if k != "acciones"}
    liviano["accionesRevisadas"] = len(datos.get("acciones") or [])
    liviano["nota"] = ("Resumen sin el detalle por accion. El analisis completo, "
                       "con las metricas de cada una, esta en /explorar/resultado.")
    return jsonify({"hayResultado": True, "estado": explorar.estado(),
                    "analisis": liviano})


@app.route("/diagnostico-lote", methods=["GET"])
def diagnostico_lote():
    """
    /diagnostico para varios tickers de una vez: ?tickers=AAPL,MSFT,NVDA

    Reusa la MISMA cache de 5 años de /diagnostico (_serie5y_cache, 24h), asi
    que pedir en lote diez acciones que ya se miraron una por una no cuesta
    ni una peticion nueva a Yahoo.

    Tope de 40 por llamada. No es capricho: cada ticker que NO este en cache
    son 5 años de historial, y cuarenta de esos ya son varios segundos de
    worker ocupado. La app pagina si necesita mas.
    """
    crudo = request.args.get("tickers", "")
    pedidos = [t.strip().upper() for t in crudo.split(",") if t.strip()]
    if not pedidos:
        return jsonify({"error": "falta ?tickers=A,B,C"}), 400
    if len(pedidos) > 40:
        return jsonify({"error": f"maximo 40 por llamada, pediste {len(pedidos)}"}), 400

    conocidos = set(TICKERS) | set(TICKERS_USA) | set(UNIVERSO_ANALISIS)
    salida, desconocidos = {}, []
    for t in pedidos:
        if t not in conocidos:
            desconocidos.append(t)
            continue
        es_usa = t not in TICKERS
        puntos = _serie5y_ticker(t, es_usa)
        idx = _indice5y("usa" if es_usa else "chile")
        salida[t] = {
            "mercado": "usa" if es_usa else "chile",
            "diario": indicador_fuerza_fase.evaluar_diario(puntos or [], idx),
            "semanal": indicador_fuerza_fase.evaluar_semanal(puntos or [], idx),
        }
    return jsonify({"diagnosticos": salida, "desconocidos": desconocidos,
                    "serverTime": datetime.now(timezone.utc).isoformat()})


@app.route("/health")
def health():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
