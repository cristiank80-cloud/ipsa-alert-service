"""
Fuente de datos v3 — Yahoo Finance via el endpoint "chart" (publico, sin
API key, sin cookie ni crumb).

POR QUE SE REESCRIBIO ESTE ARCHIVO
==================================

1) fast_info.last_price NO era el precio en vivo.
   La version anterior usaba yfinance.fast_info. Por dentro, esa propiedad
   hace `history(period="1y")` y devuelve `Close.iloc[-1]`, es decir el
   ULTIMO CIERRE DIARIO. Para acciones el bar del dia se va actualizando y
   se parece al precio en vivo, pero para el indice ^IPSA el bar diario de
   Yahoo se publica tarde: por eso el indice se quedaba pegado en el valor
   de un dia anterior sin que nada lo delatara.

2) El timestamp mentia.
   Se guardaba `datetime.now()` — la hora en que el servidor pidio el dato,
   no la hora a la que corresponde el precio. Un cierre de hace tres dias
   se veia igual de fresco que uno de hace un minuto. Ahora se propaga
   `regularMarketTime`, la hora real de la bolsa, y `stale_seconds`.

3) Eran ~190 peticiones a Yahoo por refresco.
   get_quotes (47) + get_bid_ask (47, con .info que es un scrape completo)
   + get_daily_avg (47) + get_returns (47). Secuenciales. Yahoo responde
   429 (Too Many Requests) mucho antes de terminar, y yfinance se traga el
   error devolviendo vacio. Resultado: datos viejos sin aviso.

   Ahora: UNA peticion por simbolo para lo intradia (48 en paralelo, ~2s)
   y UNA peticion por simbolo para todo lo historico (promedio 90d,
   rentabilidad 3M/1A, RSI, volatilidad, serie del grafico salen todas de
   la misma serie anual, cacheada 30 min).

4) Base de precios consistente.
   Todas las estadisticas (promedio, rentabilidad, RSI, volatilidad) se
   calculan sobre cierres AJUSTADOS por dividendos y splits. En Chile los
   dividendos son altos: sin ajustar, la caida mecanica del dia ex-dividendo
   se confunde con una caida real y dispara alertas falsas.

HONESTIDAD SOBRE EL REZAGO: Yahoo entrega estas cotizaciones con rezago
(no publican de cuanto). Este modulo ya no lo esconde: cada precio viene
con su hora de bolsa y su antiguedad en segundos, y la app los muestra.
Si algun dia consigues la API de la Bolsa de Santiago o de tu corredora,
basta con reemplazar `get_market_data()` — el resto no cambia.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import threading
import time

import requests

SUFFIX = ".SN"
INDEX_SYMBOL = "^IPSA"

_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {
    # Yahoo responde 429 a clientes sin User-Agent de navegador.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_TIMEOUT = 12
_MAX_WORKERS = 8  # mas que esto y Yahoo empieza a devolver 429


# --------------------------------------------------------------------------
# AUTENTICACION DE YAHOO (cookie + crumb) -- agosto 2026
# --------------------------------------------------------------------------
# EL PROBLEMA QUE ESTO RESUELVE, MEDIDO EN PRODUCCION
# ====================================================
# La primera corrida real de Explorar sobre las 536 acciones devolvio esto:
#
#     SMA 50 > SMA 200: 127  ->  Capitalizacion >= 2 B: 0
#     sinDatoFundamental: {"capB": 127}
#
# Las 127 que llegaron al filtro de capitalizacion se cayeron ahi, TODAS, por
# falta de dato. 127 de 127 no es casualidad ni son empresas raras: NVDA, MU,
# DELL y LLY estaban entre ellas. Era el endpoint el que no contestaba.
#
# El chart API (v8/finance/chart) sigue siendo abierto -- por eso los precios
# funcionan perfecto y el analisis reviso 534 de 536 sin problema. Pero
# quoteSummary (v10) y quote (v7), que son los que traen capitalizacion,
# crecimiento y sector, ahora exigen una cookie de sesion MAS un "crumb"
# (un token corto que Yahoo entrega solo a quien ya tiene la cookie).
# Sin eso responden 401/403 y este archivo los trataba como "sin dato".
#
# COMO SE OBTIENE
# ===============
#   1. GET a fc.yahoo.com  ->  Yahoo deja las cookies de sesion.
#   2. GET a /v1/test/getcrumb con esas cookies  ->  devuelve el crumb en
#      texto plano.
#   3. Cada peticion posterior va por la MISMA session y con ?crumb=...
#
# El crumb se vence. Por eso `quote_summary()` reintenta UNA vez pidiendo
# uno nuevo cuando recibe 401/403 -- si no, el servidor quedaria caido para
# fundamentales hasta el proximo despliegue.
#
# SI ESTO FALLA, NO SE INVENTA NADA. `quote_summary()` devuelve
# (None, motivo) y el motivo viaja hasta /fundamentales-diag, para que la
# proxima vez no haya que adivinar: se pregunta.
_QUOTE_SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
# Dos fuentes de cookies, se prueban en orden. fc.yahoo.com es la que usa
# todo el mundo y responde 404 dejando las cookies igual; si esa falla (o si
# desde la IP de Render la redirigen al muro de consentimiento europeo), se
# prueba la portada de finanzas, que tambien las deja.
_COOKIE_URLS = ["https://fc.yahoo.com/", "https://finance.yahoo.com/"]
_CRUMB_TTL = 3600
_REFRESCO_MIN = 30   # ver la nota de la estampida en _asegurar_crumb()

_sesion_yf = None
_crumb = None
_crumb_ts = 0
_crumb_motivo = "todavia no se ha pedido"
_crumb_lock = threading.Lock()


def _pedir_crumb(s):
    """
    Intenta obtener el crumb con una session ya creada.
    Devuelve (crumb_o_None, motivo).

    EL 406 QUE COSTO UNA CORRIDA
    =============================
    La primera version de esto mandaba las cabeceras de _HEADERS tal cual, y
    ahi va "Accept: application/json". Pero /v1/test/getcrumb NO devuelve
    JSON: devuelve el token en TEXTO PLANO. Yahoo respondia 406 Not
    Acceptable -- "no puedo darte lo que pides en ese formato" -- y sin crumb
    todo lo demas caia con 401.
    Se vio en /fundamentales-diag: "getcrumb respondio 406". Por eso estas
    dos peticiones van con Accept: */* y no con las cabeceras por defecto.
    """
    cab = dict(_HEADERS)
    cab["Accept"] = "*/*"
    motivo = "ninguna fuente de cookies respondio"
    for cookie_url in _COOKIE_URLS:
        try:
            # Esta peticion existe SOLO para que Yahoo deje sus cookies de
            # sesion. Que responda 404 es normal y no importa: lo que
            # interesa viaja en las cabeceras Set-Cookie.
            s.get(cookie_url, headers=cab, timeout=_TIMEOUT)
        except Exception as e:
            print(f"[data_source] cookie de Yahoo ({cookie_url}): "
                  f"{type(e).__name__}: {e}")
            continue
        try:
            r = s.get(_CRUMB_URL, headers=cab, timeout=_TIMEOUT)
        except Exception as e:
            return None, f"getcrumb fallo: {type(e).__name__}: {e}"
        texto = (r.text or "").strip()
        # Un crumb real son ~11 caracteres sin espacios. Si Yahoo devuelve
        # una pagina de error viene HTML: hay que descartarlo explicitamente
        # o lo mandariamos como token en cada peticion.
        if r.status_code == 200 and texto and len(texto) <= 40 and "<" not in texto:
            return texto, "ok"
        motivo = (f"getcrumb respondio {r.status_code} con {cookie_url}: "
                  f"{texto[:120].replace(chr(10), ' ')}")
        if r.status_code != 200:
            continue   # probar la siguiente fuente de cookies
        return None, motivo
    return None, motivo


def _asegurar_crumb(forzar=False):
    """Devuelve (session, crumb_o_None). Nunca lanza."""
    global _sesion_yf, _crumb, _crumb_ts, _crumb_motivo
    with _crumb_lock:
        ahora = time.time()
        if not forzar and _crumb and (ahora - _crumb_ts) < _CRUMB_TTL:
            return _sesion_yf, _crumb

        # ESTAMPIDA DE RENOVACIONES. `forzar=True` llega desde el reintento
        # que hace cada peticion cuando recibe 401/403. Si Yahoo esta
        # rechazando el crumb, lo reciben las 127 a la vez: sin este freno,
        # 127 renovaciones simultaneas = 254 peticiones extra a Yahoo justo
        # cuando Yahoo ya nos esta diciendo que no. La primera renueva, las
        # demas reusan lo que dejo -- que es lo que se queria de todas formas.
        if forzar and _crumb_ts and (ahora - _crumb_ts) < _REFRESCO_MIN:
            return _sesion_yf, _crumb

        s = requests.Session()
        s.headers.update(_HEADERS)
        crumb, _crumb_motivo = _pedir_crumb(s)

        _sesion_yf, _crumb, _crumb_ts = s, crumb, ahora
        if crumb is None:
            print(f"[data_source] SIN CRUMB de Yahoo: {_crumb_motivo}. "
                  f"Los fundamentales (capitalizacion, crecimiento, sector) "
                  f"no se van a poder pedir.")
        return s, crumb


def estado_crumb():
    """Para /fundamentales-diag. No sale a la red."""
    return {
        "tiene": _crumb is not None,
        "motivo": _crumb_motivo,
        "edadSeg": int(time.time() - _crumb_ts) if _crumb_ts else None,
        "ttlSeg": _CRUMB_TTL,
    }


def quote_summary(symbol, modules):
    """
    quoteSummary autenticado. Devuelve (result[0] o None, motivo).

    El `motivo` es SIEMPRE informativo, incluso cuando sale bien ("200"):
    es lo que deja diagnosticar sin adivinar por que una accion no trajo
    capitalizacion.
    """
    for intento in (0, 1):
        s, crumb = _asegurar_crumb(forzar=(intento == 1))
        params = {"modules": modules}
        if crumb:
            params["crumb"] = crumb
        try:
            resp = s.get(_QUOTE_SUMMARY.format(symbol=symbol),
                         params=params, timeout=_TIMEOUT)
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

        if resp.status_code == 200:
            try:
                res = ((resp.json().get("quoteSummary") or {}).get("result") or [])
            except ValueError:
                return None, "200 pero el cuerpo no es JSON"
            return (res[0] if res else None), ("200" if res else "200 sin result")

        # 401/403 = crumb vencido o invalido. Vale la pena UN reintento con
        # crumb nuevo; mas seria martillar a Yahoo con el mismo error.
        if resp.status_code in (401, 403) and intento == 0:
            continue
        return None, f"HTTP {resp.status_code}"
    return None, "sin crumb utilizable"


_QUOTE_V7 = "https://query1.finance.yahoo.com/v7/finance/quote"


def market_caps(symbols, tam_lote=40):
    """
    Capitalizacion de MUCHOS simbolos por lote. Devuelve (dict, motivos).

    POR QUE EXISTE ADEMAS DE quote_summary()
    =========================================
    Este endpoint acepta symbols=A,B,C: 127 acciones son 4 peticiones en vez
    de 127. Y sobre todo es una SEGUNDA fuente para el unico dato que dejo el
    embudo en cero. Si quoteSummary falla pero este responde, el analisis
    igual puede filtrar por capitalizacion en vez de descartarlo todo.

    dict: {"NVDA": 5200.0, ...} en miles de millones de USD.
    """
    caps, motivos = {}, []
    lotes = [symbols[i:i + tam_lote] for i in range(0, len(symbols), tam_lote)]
    for lote in lotes:
        conseguido = False
        for intento in (0, 1):
            s, crumb = _asegurar_crumb(forzar=(intento == 1))
            params = {"symbols": ",".join(lote)}
            if crumb:
                params["crumb"] = crumb
            try:
                resp = s.get(_QUOTE_V7, params=params, timeout=_TIMEOUT)
            except Exception as e:
                motivos.append(f"{type(e).__name__}")
                break
            if resp.status_code == 200:
                try:
                    filas = ((resp.json().get("quoteResponse") or {}).get("result") or [])
                except ValueError:
                    motivos.append("200 pero el cuerpo no es JSON")
                    break
                for f in filas:
                    sim, cap = f.get("symbol"), f.get("marketCap")
                    if sim and isinstance(cap, (int, float)) and cap > 0:
                        caps[sim] = round(cap / 1e9, 2)
                conseguido = True
                break
            if resp.status_code in (401, 403) and intento == 0:
                continue
            motivos.append(f"HTTP {resp.status_code}")
            break
        if not conseguido and not motivos:
            motivos.append("lote sin respuesta")
    return caps, sorted(set(motivos))


# --------------------------------------------------------------------------
# Capa de red
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Cuarentena de simbolos muertos
# --------------------------------------------------------------------------
# El universo de analisis pasó a ser el S&P 500 + Nasdaq-100 (~536 simbolos).
# En una lista de ese tamaño SIEMPRE hay alguno que se renombro, se fusiono o
# salio de bolsa, y cada uno de esos cuesta una peticion desperdiciada cada
# vez que se recorre el universo -- para siempre, porque nada lo saca solo.
# Ya paso con ITAUCORP y SAAM en la lista chilena, y ahi eran dos sobre 50.
#
# Esto lo resuelve sin intervencion: cuando Yahoo responde que el simbolo NO
# EXISTE (no un 429, no una caida -- "no existe"), el simbolo queda anotado y
# no se vuelve a pedir mientras el proceso siga vivo.
#
# POR QUE EN MEMORIA Y NO EN DISCO
# =================================
# El disco de Render es efimero (ver el comentario de push_subscriptions.json
# en server.py): un archivo aca se borra en cada despliegue igual. Y esta
# bien que se borre -- si un simbolo vuelve a existir, o si lo comentaste mal,
# el proximo reinicio le da otra oportunidad. El costo de equivocarse es una
# pasada fallida, no un simbolo perdido para siempre.
#
# Se consulta desde /universo-diag. NO afecta al ciclo de 30 minutos salvo
# para ahorrarle peticiones.
_MUERTOS = {}          # simbolo -> motivo
_MUERTOS_LOCK = threading.Lock()

# Frases con las que Yahoo dice "ese simbolo no existe". Cualquier otro
# error (429, timeout, 503) NO manda a cuarentena: eso es Yahoo con
# problemas, no el simbolo. Confundirlos borraria medio universo en un mal
# dia de la API.
_NO_EXISTE = ("no data found", "symbol may be delisted", "not found",
              "no timezone found", "invalid symbol")


def simbolos_en_cuarentena():
    """Copia del registro de simbolos muertos. La usa /universo-diag."""
    with _MUERTOS_LOCK:
        return dict(_MUERTOS)


def _marcar_muerto(symbol, motivo):
    with _MUERTOS_LOCK:
        if symbol not in _MUERTOS:
            _MUERTOS[symbol] = motivo
            print(f"[data_source] {symbol}: EN CUARENTENA -- {motivo}. "
                  f"No se vuelve a pedir hasta que reinicie el servidor. "
                  f"Van {len(_MUERTOS)} en cuarentena.")


def _esta_muerto(symbol):
    with _MUERTOS_LOCK:
        return symbol in _MUERTOS


def _chart(symbol, params, reintentos=2):
    """
    Una llamada al chart API. Devuelve el dict `result[0]` o None.
    Reintenta con espera creciente ante 429/5xx, que es como Yahoo avisa
    que le estas pidiendo demasiado rapido.

    Si el simbolo ya esta en cuarentena, devuelve None sin salir a la red.
    """
    if _esta_muerto(symbol):
        return None
    for intento in range(reintentos + 1):
        try:
            resp = requests.get(
                _CHART.format(symbol=symbol),
                params=params,
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                if intento < reintentos:
                    time.sleep(1.5 * (intento + 1))
                    continue
                print(f"[data_source] {symbol}: Yahoo respondio {resp.status_code} "
                      f"(rate limit o caida). Se devuelve vacio, NO se inventa un precio.")
                return None
            # 404 es la respuesta de Yahoo a un simbolo que no existe. Es
            # distinto de un 429: no se reintenta, se manda a cuarentena.
            if resp.status_code == 404:
                _marcar_muerto(symbol, "404 de Yahoo (simbolo inexistente)")
                return None
            resp.raise_for_status()
            cuerpo = resp.json()
            error = (cuerpo.get("chart") or {}).get("error")
            if error:
                desc = str(error).lower()
                if any(f in desc for f in _NO_EXISTE):
                    _marcar_muerto(symbol, str(error)[:160])
                else:
                    print(f"[data_source] {symbol}: Yahoo reporto error -> {error}")
                return None
            resultados = (cuerpo.get("chart") or {}).get("result") or []
            return resultados[0] if resultados else None
        except Exception as e:
            if intento < reintentos:
                time.sleep(1.0 * (intento + 1))
                continue
            print(f"[data_source] {symbol}: fallo la peticion -- {type(e).__name__}: {e}")
            return None
    return None


def _en_paralelo(func, items):
    """Ejecuta func sobre cada item con un pool acotado de hilos."""
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        return list(pool.map(func, items))


def _limpio(v):
    """None si el valor es None, NaN o infinito. Evita meter NaN en el JSON."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------
# Cotizacion intradia (precio en vivo, con hora de bolsa)
# --------------------------------------------------------------------------

def _quote_de_meta(res):
    """
    Extrae la cotizacion de la respuesta del chart API (`res` es
    `chart.result[0]` completo, no solo `meta`).

    `regularMarketPrice` (en meta) SI es el precio en vivo, a diferencia del
    ultimo cierre diario que devolvia fast_info.

    OJO CON `regularMarketTime`: se detecto que para el indice ^IPSA este
    campo de `meta` puede quedar pegado varios dias, mientras
    `regularMarketPrice` SI se actualiza (se verifico cruzando contra
    Visfin, que tambien usa datos de Yahoo y mostraba el mismo precio con
    fecha de hoy). Es una inconsistencia del propio Yahoo para ese simbolo,
    no del precio en si. Por eso aqui se usa como respaldo el ULTIMO
    timestamp de la serie intradia (`res.timestamp`, un tick por minuto)
    cuando es mas reciente que el de meta -- eso evita marcar como "viejo"
    (y ahora directamente ocultar, ver frontend) un precio que en realidad
    es el de ahora.
    """
    meta = res.get("meta") or {}
    precio = _limpio(meta.get("regularMarketPrice"))
    if precio is None:
        return None

    epoch = meta.get("regularMarketTime")
    serie_ts = res.get("timestamp") or []
    if serie_ts:
        ultimo_ts = serie_ts[-1]
        if isinstance(ultimo_ts, (int, float)) and (not epoch or ultimo_ts > epoch):
            epoch = ultimo_ts

    hora_bolsa, antiguedad = None, None
    if isinstance(epoch, (int, float)) and epoch > 0:
        hora_bolsa = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        antiguedad = max(0, int(time.time() - epoch))

    return {
        "price": precio,
        # Hora REAL a la que corresponde el precio, segun la bolsa.
        "marketTime": hora_bolsa,
        # Cuantos segundos tiene el dato. La app pinta el semaforo con esto.
        "staleSeconds": antiguedad,
        # Hora en que ESTE servidor lo pidio (util para depurar, no para
        # decidir si el dato esta fresco).
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "previousClose": _limpio(meta.get("chartPreviousClose"))
                         or _limpio(meta.get("previousClose")),
        "dayHigh": _limpio(meta.get("regularMarketDayHigh")),
        "dayLow": _limpio(meta.get("regularMarketDayLow")),
        "volume": _limpio(meta.get("regularMarketVolume")),
        "currency": meta.get("currency"),
    }


def get_market_data(tickers, suffix=None):
    """
    Precio en vivo de las acciones, en una sola tanda paralela.
    Devuelve (quotes, index) por compatibilidad con quien ya desestructura
    la tupla, pero `index` SIEMPRE es None: el IPSA ya no se pide a Yahoo
    (^IPSA quedaba con `regularMarketTime` pegado dias enteros para ese
    simbolo puntual; ver fuente_df.py). server.py arma el indice aparte,
    directo desde Diario Financiero.

    Sacar ^IPSA de esta tanda tambien reduce las peticiones a Yahoo de 48 a
    47 por ciclo -- un poco menos de riesgo de que Yahoo responda 429.

    `suffix`: por defecto (None) usa SUFFIX (".SN", Bolsa de Santiago). Los
    tickers del ambiente 2 (EE.UU., ver main.TICKERS_USA) se piden con
    suffix="" -- Yahoo los resuelve por su simbolo tal cual, sin sufijo de
    bolsa. Este parametro no cambia el comportamiento de ningun llamado
    existente que no lo pase.
    """
    suf = SUFFIX if suffix is None else suffix
    simbolos = [t + suf for t in tickers]

    def _uno(sym):
        return sym, _chart(sym, {"range": "1d", "interval": "1m"})

    resultados = dict(_en_paralelo(_uno, simbolos))

    quotes = {}
    for t in tickers:
        res = resultados.get(t + suf)
        if not res:
            continue
        q = _quote_de_meta(res)
        if q:
            quotes[t] = q

    return quotes, None


# Alias para no romper codigo que ya llamaba get_quotes()
def get_quotes(tickers):
    return get_market_data(tickers)[0]


def get_index_quote():
    # Ya no aplica: el IPSA no sale de Yahoo. Se deja el nombre para no
    # romper un import viejo, pero devuelve None a proposito -- usa
    # fuente_df.get_index() si necesitas el IPSA de verdad.
    return None


# --------------------------------------------------------------------------
# Serie historica diaria: una sola peticion por accion, todo sale de ahi
# --------------------------------------------------------------------------

def _serie_diaria(symbol, rango="1y"):
    """
    Cierres AJUSTADOS por dividendo/split, ordenados de mas antiguo a mas
    reciente: [{"date","close","volume"}, ...].

    Se usa adjclose y no close crudo porque en Chile los dividendos son
    altos: el dia ex-dividendo el precio cae de golpe sin que la empresa
    valga menos. Sobre el precio crudo esa caida se confunde con una caida
    real y dispara una alerta falsa.
    """
    res = _chart(symbol, {"range": rango, "interval": "1d", "events": "div,splits"})
    if not res:
        return []

    fechas = res.get("timestamp") or []
    indic = res.get("indicators") or {}
    quote = (indic.get("quote") or [{}])[0]
    adj = (indic.get("adjclose") or [{}])[0].get("adjclose")
    cierres = adj if adj else quote.get("close") or []
    volumenes = quote.get("volume") or []

    puntos = []
    for i, ts in enumerate(fechas):
        c = _limpio(cierres[i]) if i < len(cierres) else None
        if c is None:
            continue  # Yahoo mete null en dias sin transaccion
        v = _limpio(volumenes[i]) if i < len(volumenes) else None
        puntos.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "close": c,
            "volume": v,
        })
    return puntos


def _rsi(cierres, periodo=14):
    """RSI de Wilder (suavizado exponencial), 0-100. None si falta historial.

    La version anterior usaba el promedio simple de las ultimas 14 variaciones,
    que no es el metodo de Wilder y da un numero distinto al de cualquier
    plataforma con la que lo compares.
    """
    if len(cierres) < periodo + 1:
        return None
    deltas = [cierres[i] - cierres[i - 1] for i in range(1, len(cierres))]

    ganancias = [d if d > 0 else 0.0 for d in deltas[:periodo]]
    perdidas = [-d if d < 0 else 0.0 for d in deltas[:periodo]]
    avg_g = sum(ganancias) / periodo
    avg_p = sum(perdidas) / periodo

    for d in deltas[periodo:]:
        g = d if d > 0 else 0.0
        p = -d if d < 0 else 0.0
        avg_g = (avg_g * (periodo - 1) + g) / periodo
        avg_p = (avg_p * (periodo - 1) + p) / periodo

    if avg_p == 0:
        return 100.0
    rs = avg_g / avg_p
    return round(100 - (100 / (1 + rs)), 1)


def _desv_estandar(valores):
    n = len(valores)
    if n < 2:
        return None
    media = sum(valores) / n
    var = sum((v - media) ** 2 for v in valores) / (n - 1)
    return math.sqrt(var)


def _extension_regresion(cierres):
    """
    Distancia del ULTIMO precio a su propia LINEA DE TENDENCIA (regresion
    lineal por minimos cuadrados sobre la ventana), medida en desviaciones
    estandar de los residuos.

    POR QUE ESTO Y NO LA DISTANCIA AL PROMEDIO
    ==========================================
    El z-score contra un promedio PLANO (avg90) tiene un problema
    matematico: una accion en tendencia alcista sostenida esta SIEMPRE por
    encima de su promedio movil. Eso no es una anomalia, es la definicion
    de tendencia. El modelo terminaba castigando a una ganadora por hacer
    exactamente lo que se espera de ella (caso PANW: -40% de puntaje por
    estar 40% sobre su media, cuando venia subiendo ordenadamente).

    Comparar contra la RECTA de regresion responde la pregunta correcta:
    "esta por encima de SU PROPIA TENDENCIA", no "esta por encima de un
    promedio que la tendencia dejo atras hace rato".

    Devuelve (z, pendiente_pct):
      z             cuantas sigmas sobre/bajo su linea de tendencia esta hoy
      pendiente_pct cuanto sube (o baja) la tendencia por dia, en % del
                    precio medio -- dato nuevo que antes no existia: dice si
                    el ancla misma va subiendo.

    Implementado en Python puro a proposito: numpy/pandas se sacaron de
    este proyecto (ver requirements.txt) porque hacian lento el arranque en
    frio de Render, que es el cuello de botella real de la app.
    """
    n = len(cierres)
    if n < 5:
        return None, None

    # Minimos cuadrados sobre x = 0..n-1. Las formulas cerradas evitan
    # tener que traer numpy solo para un polyfit de grado 1.
    sx = n * (n - 1) / 2.0
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sy = sum(cierres)
    sxy = sum(i * c for i, c in enumerate(cierres))

    denom = n * sxx - sx * sx
    if denom == 0:
        return None, None
    pendiente = (n * sxy - sx * sy) / denom
    intercepto = (sy - pendiente * sx) / n

    residuos = [c - (pendiente * i + intercepto) for i, c in enumerate(cierres)]
    sigma = _desv_estandar(residuos)
    if not sigma or sigma <= 0:
        return None, None

    z = residuos[-1] / sigma
    media = sy / n
    pendiente_pct = (pendiente / media * 100) if media else None
    return z, pendiente_pct


def _estadisticas(puntos, dias_promedio=90):
    """
    Todo lo que se puede sacar de una serie anual, calculado una sola vez.
    Incluye lo que el modelo de senales necesita para dejar de tratar por
    igual a una accion tranquila y a una que se mueve 8% al dia.
    """
    if len(puntos) < 5:
        return None

    cierres = [p["close"] for p in puntos]
    ultimo = cierres[-1]

    ventana = cierres[-dias_promedio:]
    avg = sum(ventana) / len(ventana)
    sd = _desv_estandar(ventana)

    # Retornos diarios -> volatilidad. Esto es lo que permite normalizar:
    # -4% en AGUAS-A no significa lo mismo que -4% en ENJOY.
    retornos = []
    for i in range(1, len(cierres)):
        if cierres[i - 1]:
            retornos.append(cierres[i] / cierres[i - 1] - 1)
    vol_diaria = _desv_estandar(retornos[-90:]) if len(retornos) >= 20 else None

    volumenes = [p["volume"] for p in puntos[-30:] if p["volume"]]
    vol_medio = (sum(volumenes) / len(volumenes)) if volumenes else None
    monto_medio = (vol_medio * ultimo) if vol_medio else None

    i3m = max(0, len(cierres) - 63)
    ret_3m = (ultimo / cierres[i3m] - 1) if cierres[i3m] else None
    ret_1y = (ultimo / cierres[0] - 1) if cierres[0] else None

    # Ancla nueva: distancia a la LINEA DE TENDENCIA, no al promedio plano.
    # Ver _extension_regresion() para el porque completo.
    z_reg, pendiente_pct = _extension_regresion(ventana)

    # -- Movimiento por evento (gap) ---------------------------------------
    # Busca el mayor salto diario de los ultimos 60 dias. Si supera 3 veces
    # la volatilidad diaria tipica, es casi seguro un evento puntual (un
    # reporte de resultados, una noticia) y NO una tendencia sostenida.
    # Sin esto, el modulo de tendencia lee el gap de un dia como "cambio de
    # regimen" y castiga a una accion que solo reacciono a su reporte (caso
    # CSCO: -37 de puntaje por romper sus medias al dia siguiente de un
    # reporte que batio expectativas).
    gap_pct, gap_dias_atras = None, None
    retornos_60 = retornos[-60:] if len(retornos) >= 5 else []
    if retornos_60 and vol_diaria and vol_diaria > 0:
        idx_max = max(range(len(retornos_60)), key=lambda i: abs(retornos_60[i]))
        mayor = retornos_60[idx_max]
        if abs(mayor) >= 3 * vol_diaria:
            gap_pct = mayor
            gap_dias_atras = len(retornos_60) - 1 - idx_max

    return {
        "avg90": avg,
        "sd90": sd,
        # Cuantas desviaciones estandar sobre/bajo su LINEA DE TENDENCIA
        # esta hoy (antes era contra el promedio plano -- ver
        # _extension_regresion). Si la regresion no se pudo calcular, cae
        # al metodo anterior para no quedarse sin senal.
        "zscore": z_reg if z_reg is not None else (((ultimo - avg) / sd) if (sd and sd > 0) else None),
        # Metodo con el que se calculo el zscore de arriba, para que la app
        # pueda decirlo en vez de que el usuario tenga que adivinarlo.
        "zscoreMetodo": "regresion" if z_reg is not None else "promedio",
        # Cuanto sube (o baja) la linea de tendencia por dia, en % -- dice si
        # el ancla misma va subiendo, no solo donde esta el precio respecto de ella.
        "pendientePct": pendiente_pct,
        # Distancia al promedio plano: se conserva porque varias partes de la
        # app la muestran como referencia legible ("esta 5% bajo su promedio").
        "zscorePromedio": ((ultimo - avg) / sd) if (sd and sd > 0) else None,
        "volDiaria": vol_diaria,
        "rsi14": _rsi(cierres, 14),
        "sma20": (sum(cierres[-20:]) / 20) if len(cierres) >= 20 else None,
        "sma50": (sum(cierres[-50:]) / 50) if len(cierres) >= 50 else None,
        # Igual que sma20/sma50: media simple sobre cierres diarios reales.
        # Con el "1y" que ya se pide para todo lo demas (~252 dias habiles)
        # alcanza para los 200 que necesita, sin pedir una ventana mas larga
        # a Yahoo. Si el papel tiene menos de 200 dias de historial (IPO
        # reciente, ida y vuelta, etc.) queda en None -- no se rellena con
        # un numero a medias.
        "sma200": (sum(cierres[-200:]) / 200) if len(cierres) >= 200 else None,
        "ret3m": ret_3m,
        "ret1y": ret_1y,
        # Mayor salto diario reciente atribuible a un evento puntual, y hace
        # cuantos dias habiles ocurrio. None si no hubo ninguno destacable.
        "gapPct": gap_pct,
        "gapDiasAtras": gap_dias_atras,
        "montoMedioDiario30d": monto_medio,
        "ultimoCierreDiario": ultimo,
        "fechaUltimoCierre": puntos[-1]["date"],
        "diasDeHistorial": len(cierres),
    }


def get_stats(tickers, dias_promedio=90, suffix=None, con_indice=True, index_symbol=None):
    """
    Estadisticas de todas las acciones MAS las del indice, en paralelo.
    Reemplaza a get_daily_avg() + get_returns() y ademas entrega RSI,
    volatilidad y liquidez, que antes no existian en el backend.

    Devuelve (stats_por_ticker, stats_del_indice, series_por_ticker).
    Las series se devuelven para que server.py las cachee y las reuse en
    /history y en el calculo de senales, en vez de volver a pedirlas.

    `suffix`: ver get_market_data(). `con_indice=False` evita pedir el
    indice de referencia.

    `index_symbol`: que indice pedir cuando con_indice=True. Por defecto
    INDEX_SYMBOL (^IPSA, mercado chileno). server.py pasa "^GSPC" (S&P 500)
    para el ambiente 2 (EE.UU.) -- mismo mecanismo, otro simbolo, sin tener
    que duplicar esta funcion.
    """
    suf = SUFFIX if suffix is None else suffix
    idx = index_symbol or INDEX_SYMBOL
    simbolos = [t + suf for t in tickers] + ([idx] if con_indice else [])

    def _uno(sym):
        return sym, _serie_diaria(sym, "1y")

    series = dict(_en_paralelo(_uno, simbolos))

    stats, series_por_ticker = {}, {}
    for t in tickers:
        puntos = series.get(t + suf) or []
        if not puntos:
            continue
        series_por_ticker[t] = puntos
        st = _estadisticas(puntos, dias_promedio)
        if st:
            stats[t] = st

    stats_indice = _estadisticas(series.get(idx) or [], dias_promedio) if con_indice else None
    return stats, stats_indice, series_por_ticker


def get_price_history(ticker, period="3mo", suffix=None):
    """
    Serie para el grafico. Se mantiene la firma anterior para no romper
    /history. Nota: si server.py ya tiene la serie anual cacheada, conviene
    recortarla desde ahi en vez de llamar a esta funcion.

    `suffix`: ver get_market_data(). server.py lo pasa como "" para los
    tickers del ambiente 2 (EE.UU.).
    """
    suf = SUFFIX if suffix is None else suffix
    mapa = {"1d": "1d", "5d": "5d", "1mo": "1mo", "3mo": "3mo",
            "6mo": "6mo", "ytd": "ytd", "1y": "1y", "5y": "5y", "10y": "10y"}
    rango = mapa.get(period, "3mo")
    simbolo = INDEX_SYMBOL if ticker.upper() in ("IPSA", "^IPSA") else ticker + suf

    if rango in ("1d", "5d"):
        # OJO: antes "5d" caia directo a _serie_diaria() -> interval=1d,
        # es decir SOLO ~3-5 velas (una por dia). El grafico quedaba casi
        # un segmento recto y encima todos los indicadores (SMA20, RSI14,
        # MACD) necesitan minimo 14-26 puntos, asi que desaparecian por
        # completo -- se veia "roto" aunque tecnicamente no fallaba nada.
        # Se pide intradia tambien para 5 dias (velas de 15 min, ~25-30
        # por jornada -> 100-150 puntos en total), que es lo que Yahoo
        # permite para ese rango sin devolver error.
        intervalo = "5m" if rango == "1d" else "15m"
        res = _chart(simbolo, {"range": rango, "interval": intervalo})
        if not res:
            return []
        fechas = res.get("timestamp") or []
        cierres = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        puntos = []
        for i, ts in enumerate(fechas):
            c = _limpio(cierres[i]) if i < len(cierres) else None
            if c is None:
                continue
            puntos.append({
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "close": c,
                "volume": None,
            })
        return puntos

    return _serie_diaria(simbolo, rango)


# --------------------------------------------------------------------------
# Compatibilidad con la version anterior
# --------------------------------------------------------------------------

def get_daily_avg(tickers, days=90):
    stats, _, _ = get_stats(tickers, days)
    return {t: s["avg90"] for t, s in stats.items() if s.get("avg90")}


def get_returns(tickers):
    stats, _, _ = get_stats(tickers)
    return {t: {"ret_3m": s.get("ret3m"), "ret_1y": s.get("ret1y")}
            for t, s in stats.items()}


def get_proximos_reportes(tickers, suffix=None):
    """
    Fecha del PROXIMO reporte de resultados (earnings) por ticker.

    POR QUE IMPORTA
    ===============
    Una señal tecnica a 3 dias de un reporte no es accionable: el evento
    domina cualquier lectura de precio. La app puede decir "puntaje -21" sin
    avisar que la empresa reporta el lunes, que es justamente el dato que
    cambia la decision. Con esto, signals.py puede marcarlo.

    POR QUE NO SE USA yfinance
    ==========================
    yfinance tiene `Ticker.earnings_dates`, pero arrastra pandas y numpy.
    Este proyecto lo saco a proposito (ver requirements.txt): hacia lento el
    arranque en frio de Render, que es el cuello de botella real de la app.
    El endpoint quoteSummary entrega el mismo dato con `requests` puro,
    mismo patron que ya usa _chart().

    Devuelve {"TICKER": {"fecha": "YYYY-MM-DD", "epoch": 1234567890}}.
    Los tickers sin dato simplemente no aparecen -- nunca se inventa una
    fecha, igual criterio que el resto de este modulo.
    """
    suf = SUFFIX if suffix is None else suffix

    def _uno(t):
        simbolo = t + suf
        try:
            # Pasa por quote_summary() (cookie + crumb) como todo lo demas:
            # este endpoint tambien dejo de ser abierto, asi que antes de
            # esto la fecha del proximo reporte siempre venia vacia.
            r0, _motivo = quote_summary(simbolo, "calendarEvents")
            if not r0:
                return t, None
            fechas = (((r0.get("calendarEvents") or {})
                       .get("earnings") or {}).get("earningsDate") or [])
            for f in fechas:
                epoch = f.get("raw") if isinstance(f, dict) else f
                if isinstance(epoch, (int, float)) and epoch > time.time():
                    return t, {
                        "fecha": datetime.fromtimestamp(epoch, tz=timezone.utc)
                                         .strftime("%Y-%m-%d"),
                        "epoch": int(epoch),
                    }
            return t, None
        except Exception as e:
            print(f"[data_source] {simbolo}: fallo calendarEvents -- "
                  f"{type(e).__name__}: {e}")
            return t, None

    return {t: d for t, d in _en_paralelo(_uno, tickers) if d}


def get_rango_5y(tickers, suffix=None):
    """
    Minimo y maximo de cierre de los ultimos 5 años, por ticker.

    Es una peticion APARTE de get_stats() (que solo pide 1 año) a proposito:
    5 años de historial por accion es varias veces mas pesado que 1, y esto
    se usa solo para una franja "rango 5 años" en la tarjeta -- no vale la
    pena cargar ese peso en el ciclo de 30 min que ya esta ajustado para no
    saturar al unico worker de Render (ver el comentario largo sobre esto
    en el encabezado del archivo y en server.py). server.py cachea el
    resultado de esta funcion por separado, con un TTL de un dia: el
    minimo/maximo de 5 años prácticamente no cambia de una hora a otra.

    `suffix`: igual que en get_market_data() -- "" para tickers de EE.UU.
    """
    suf = SUFFIX if suffix is None else suffix
    simbolos = [t + suf for t in tickers]

    def _uno(sym):
        return sym, _serie_diaria(sym, "5y")

    series = dict(_en_paralelo(_uno, simbolos))

    rangos = {}
    for t in tickers:
        puntos = series.get(t + suf) or []
        cierres = [p["close"] for p in puntos if p.get("close") is not None]
        if not cierres:
            continue
        rangos[t] = {"min5y": min(cierres), "max5y": max(cierres),
                     "diasDeHistorial5y": len(cierres)}
    return rangos


# --------------------------------------------------------------------------
# Historial del IPSA: ver ipsa_historico.py
# --------------------------------------------------------------------------
#
# ^IPSA en Yahoo tiene `regularMarketTime` pegado para la cotizacion en vivo
# (ver encabezado del archivo) -- pero ademas, confirmado en vivo aparte, su
# serie HISTORICA esta rota: pidas el periodo que pidas, el chart API
# devuelve un solo punto viejo en vez de la serie completa.
#
# Se probo Stooq como alternativa (descarga CSV publica, sin API key) pero
# no se pudo verificar desde el sandbox de desarrollo -- ni siquiera
# respondio a un ticker de prueba conocido. En vez de apostar a que
# funcionara recien en produccion, se uso un CSV historico real que ya
# tenia el dueño de la app (ver ipsa_historico.py, que combina ese archivo
# con el dato de HOY en vivo de fuente_df.get_index()).


def filtrar_puntos_por_periodo(puntos, period):
    """
    Recorta una serie diaria completa al período pedido por el frontend
    (mismos códigos que /history: 1d, 5d, 1mo, 3mo, 6mo, ytd, 1y, 5y, 8y,
    10y). Usa fechas de CALENDARIO, no días hábiles -- suficiente para
    dibujar un gráfico de comparación, no para calcular retornos exactos
    (para eso ya existe _estadisticas() sobre la serie de Yahoo).
    """
    if not puntos:
        return []
    if period == "1d":
        return puntos[-1:]
    if period == "5d":
        return puntos[-5:]

    dias_calendario = {
        "1mo": 30, "3mo": 90, "6mo": 182, "1y": 365,
        "5y": 5 * 365, "8y": 8 * 365, "10y": 10 * 365,
    }
    hoy = datetime.now(timezone.utc).date()
    if period == "ytd":
        corte = hoy.replace(month=1, day=1)
    elif period in dias_calendario:
        corte = hoy - timedelta(days=dias_calendario[period])
    else:
        return puntos

    corte_iso = corte.isoformat()
    return [p for p in puntos if p["date"] >= corte_iso]


def get_bid_ask(tickers):
    """
    ELIMINADO A PROPOSITO.

    La version anterior llamaba a yfinance `Ticker.info` una vez por accion.
    Cada una de esas llamadas es un scrape completo de la ficha de Yahoo:
    47 peticiones pesadas y secuenciales cada 3 minutos, que era la causa
    principal de que Yahoo respondiera 429 y todo lo demas quedara viejo.

    Y para que: Yahoo casi nunca publica bid/ask de la Bolsa de Santiago.
    En la practica venia None en casi todas. Se pagaba el costo completo
    para mostrar "no disponible".

    Se devuelve la estructura vacia para no romper server.py. Si algun dia
    quieres puntas de verdad, tienen que venir de la Bolsa de Santiago o de
    tu corredora — Yahoo no las tiene.
    """
    return {t: {"bid": None, "ask": None, "bidSize": None, "askSize": None}
            for t in tickers}
