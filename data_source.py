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
from datetime import datetime, timezone
import math
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
# Capa de red
# --------------------------------------------------------------------------

def _chart(symbol, params, reintentos=2):
    """
    Una llamada al chart API. Devuelve el dict `result[0]` o None.
    Reintenta con espera creciente ante 429/5xx, que es como Yahoo avisa
    que le estas pidiendo demasiado rapido.
    """
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
            resp.raise_for_status()
            cuerpo = resp.json()
            error = (cuerpo.get("chart") or {}).get("error")
            if error:
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

    return {
        "avg90": avg,
        "sd90": sd,
        # Cuantas desviaciones estandar bajo su propio promedio esta hoy.
        # Este es el numero que reemplaza al umbral fijo de -4%.
        "zscore": ((ultimo - avg) / sd) if (sd and sd > 0) else None,
        "volDiaria": vol_diaria,
        "rsi14": _rsi(cierres, 14),
        "sma20": (sum(cierres[-20:]) / 20) if len(cierres) >= 20 else None,
        "sma50": (sum(cierres[-50:]) / 50) if len(cierres) >= 50 else None,
        "ret3m": ret_3m,
        "ret1y": ret_1y,
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
            "6mo": "6mo", "ytd": "ytd", "1y": "1y", "5y": "5y"}
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
