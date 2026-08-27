# -*- coding: utf-8 -*-
"""
Modulo "Explorar": el metodo de revision de mercado de Cristian, corriendo
en el servidor.

QUE HACE
========
Los cinco pasos del documento "Metodo general para revisar el mercado", en
orden:

  1. SECTORES   -- 11 ETF de sector + 12 de industria, medidos contra el
                   S&P 500 a 1/3/6/12 meses. Contesta "¿donde esta entrando
                   la plata?".
  2. EMBUDO     -- los 7 filtros de TradingView sobre UNIVERSO_ANALISIS
                   (S&P 500 + Nasdaq-100 + la grilla).
  3. WEINSTEIN  -- de las que pasaron, solo fase 2 con 4 o 5 confirmaciones.
  4. FUERZA     -- score de 8 >= 6 y fuerza relativa creciente.
  5. FINALISTAS -- sin repetir industria; se queda la de mayor fuerza de
                   cada par.

QUE **NO** HACE, Y ES LO MAS IMPORTANTE DE ESTE ARCHIVO
========================================================
NO corre solo. Nunca. Este modulo se dispara unicamente cuando alguien pide
el analisis a mano desde la app, y corre en un hilo aparte con su propio
pool de hilos, mas chico que el de data_source.

Render en plan gratuito da UN worker de gunicorn. Recorrer 536 simbolos
dentro de una peticion dejaria /quotes, /health y /subscribe en cola por
minutos -- desde el celular eso se ve como 502 y "sin conexion al backend".
Ya paso cuando EE.UU. crecio de 7 a 107 instrumentos. Por eso:

  * el trabajo vive en un hilo (`iniciar()` devuelve al tiro),
  * el progreso se consulta aparte (`estado()`),
  * el resultado queda en cache 24h (`_RESULTADO`),
  * y solo puede haber UNA corrida a la vez.

CUANTA RED CUESTA UNA CORRIDA
==============================
~25 series de ETF + 536 series de un año + ~150 consultas de fundamentales
+ ~30 series de 5 años para el semanal. Entre 3 y 6 minutos con 4 hilos.
Los 4 hilos (y no los 8 de data_source) son a proposito: el ciclo automatico
de 30 minutos puede estar corriendo al mismo tiempo, y 8+8 hilos contra
Yahoo es la receta exacta para un 429.

APROXIMACIONES A PROPOSITO
===========================
* **Crecimiento de BPA e ingresos.** TradingView filtra por "TTM YoY"
  (ultimos doce meses contra los doce anteriores). Yahoo, por
  `financialData`, entrega `earningsGrowth` y `revenueGrowth`, que son
  crecimiento del ULTIMO TRIMESTRE contra el mismo trimestre del año
  anterior. No es lo mismo: el trimestral es mas ruidoso y reacciona antes.
  Se usa igual porque es lo unico que Yahoo da en una sola peticion, y
  porque para un filtro de "¿esta creciendo fuerte?" el trimestral yoy
  cumple. Los numeros NO van a calzar exactamente con TradingView, y eso hay
  que saberlo antes de comparar.
* **El orden del embudo cambia de lugar la capitalizacion.** El documento la
  pone entre los filtros de calidad, antes de la tendencia. Aca se evalua
  junto al crecimiento, porque los tres salen de la misma consulta a Yahoo y
  pedirla para los 536 costaria ~400 peticiones desperdiciadas en acciones
  que el filtro de tendencia iba a botar igual. El conjunto de filtros es el
  mismo; solo cambia en que momento se aplica uno.
* **Sector e industria** salen de `assetProfile` de Yahoo, que usa su propia
  taxonomia (no GICS). Sirve para lo que se necesita -- no llevar dos de la
  misma industria -- pero los nombres no son los del documento.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time

import requests

import data_source
import indicador_fuerza_fase


# ---------------------------------------------------------------------------
# Universo de ETF del paso 1
# ---------------------------------------------------------------------------
BENCHMARK = "SPY"

# ETFS_SECTOR y ETFS_INDUSTRIA se DEFINIAN aca hasta el 27-ago-2026. Ahora
# viven en main.py y se importan, porque desde esa fecha los 23 tambien
# estan en la grilla (TICKERS_USA) y en la lista de exclusion del embudo
# (ETFS_NO_ANALIZAR): tenerlos escritos en dos archivos era pedir que algun
# dia quedaran distintos sin que nada avisara.
#
# El import va en esta direccion y no al reves: main.py no importa nada, asi
# que no puede haber circularidad. El formato no cambio -- siguen siendo
# pares (nombre, ticker) y se usan igual mas abajo.
from main import ETFS_SECTOR, ETFS_INDUSTRIA

# Umbrales del embudo. Son los del documento; el frontend los puede mandar
# distintos para explorar, pero estos son el default.
UMBRALES = {
    "precio": 10.0,
    "capB": 2.0,
    "volM": 2.0,
    "crecimiento": 25.0,
}

TTL_RESULTADO = 24 * 3600

# CORRIDA REAL DEL 21-AGO-2026: la primera version tardo mas de 16 minutos
# solo en la etapa de bajar los 536 historiales, y el progreso se quedaba
# clavado en 18% todo ese rato. Tres cosas cambiaron por eso:
#
#   1. _WORKERS bajo de 4 a 3. En el plan gratuito de Render la instancia
#      tiene ~0.1 de CPU: cuatro hilos parseando JSON la saturaban tanto que
#      /health dejaba de responder durante la corrida -- justo lo que este
#      modulo existia para evitar. Menos hilos terminan ANTES en esta
#      maquina, porque no se pelean la CPU entre ellos ni con gunicorn.
#   2. Las series de 1 año ahora se cachean (_SERIES_CACHE). Antes cada
#      corrida volvia a bajar los 536 desde cero, aunque hubieras corrido el
#      analisis media hora antes. La segunda corrida del dia ahora es casi
#      instantanea.
#   3. El progreso avanza de a poco DENTRO de la etapa larga, en vez de
#      quedarse en un numero fijo. Un 18% que no se mueve en 16 minutos es
#      indistinguible de un cuelgue.
_WORKERS = 3

# ===========================================================================
# EL HALLAZGO DE LA PRIMERA CORRIDA REAL (21-ago-2026)
# ===========================================================================
# Se corrio el analisis completo contra Yahoo por primera vez. A los 25
# minutos seguia en la etapa de descarga, con la cuarentena vacia (o sea:
# ningun simbolo malo, solo lentitud). El diagnostico es claro:
#
#   **Yahoo estrangula las peticiones cuando le llegan cientos seguidas
#   desde la misma IP.** Con 429 y timeouts, cada ticker pasa a costar
#   decenas de segundos en vez de uno, y 536 no terminan nunca.
#
# No es un bug del codigo: es el limite real de bajar 536 historiales desde
# una IP compartida de Render sin API key. Insistir con mas hilos empeora
# las cosas -- mas 429, no menos datos.
#
# LA SOLUCION: EL ANALISIS SE CALIENTA SOLO, DE A POCO
# =====================================================
# En vez de exigir que UNA corrida baje las 536, cada corrida:
#
#   1. usa GRATIS todo lo que ya tenga en cache (12 h de vigencia),
#   2. gasta su presupuesto de tiempo bajando SOLO las que faltan,
#   3. y dice con total claridad sobre cuantas alcanzo a decidir.
#
# Asi la primera corrida cubre unas 150-250 acciones, la segunda arranca con
# esas ya listas y suma otras tantas, y a la tercera el universo esta
# completo. Un analisis honesto sobre 200 acciones sirve; uno que nunca
# termina, no.
#
# Por eso las que YA estan en cache se procesan PRIMERO: si el presupuesto
# se acaba, lo que se pierde son acciones nuevas, nunca las que ya se
# sabian.
TOPE_DESCARGA_SEG = int(__import__("os").environ.get("EXPLORAR_TOPE_SEG", 6 * 60))

# ===========================================================================
# EL CAMINO RAPIDO (ago-2026): PRECIO Y MEDIAS POR LOTE
# ===========================================================================
# Todo el bloque de arriba describe un problema que ahora tiene otra
# solucion, mucho mejor: Yahoo YA CALCULA el precio, las dos medias moviles y
# el volumen medio, y los entrega de a 40 simbolos por peticion en el mismo
# endpoint que este modulo ya usaba para la capitalizacion. El mercado
# completo pasa de 5.254 peticiones a ~131. Ver data_source.metricas_por_lote.
#
# El camino de arriba (una serie por simbolo) NO se borro: sigue siendo el
# respaldo para lo que el lote no resuelva, y sigue siendo la unica forma de
# calcular la fase de Weinstein y la fuerza relativa, que necesitan la serie
# completa. Si el endpoint por lotes se cae, todo se comporta como antes.
#
# Se puede apagar con EXPLORAR_USAR_LOTE=0 en Render sin tocar el codigo, por
# si Yahoo cambia el endpoint y hay que volver al camino viejo con urgencia.
USAR_LOTE = __import__("os").environ.get("EXPLORAR_USAR_LOTE", "1") != "0"
TAM_LOTE = int(__import__("os").environ.get("EXPLORAR_TAM_LOTE", 40))

# Cuantas acciones se verifican comparando el numero del lote contra el
# calculado desde la serie, en cada corrida. Cuesta una peticion por cada una
# y es lo unico que convierte "los numeros de Yahoo deberian calzar" en un
# dato medido. Con 0 se apaga.
TOPE_VERIFICAR = int(__import__("os").environ.get("EXPLORAR_VERIFICAR", 6))

# Cuantas "dudosas" (las que se cayeron por falta de dato, no por no cumplir)
# se diagnostican igual. Cada una cuesta una serie de 5 años, asi que no
# pueden ser todas. Se ordenan alfabeticamente para que el corte sea estable
# entre corridas y no cambie de nombres al azar.
TOPE_DUDOSAS = 15
# Cuantas acciones reciben fase de Weinstein + score. Cada una cuesta una
# serie de 5 años, asi que esto es lo que decide si la corrida dura 4 o 12
# minutos. 60 alcanza para que los filtros de la app se puedan mover con
# sentido; mas que eso es pagar mucho por acciones que casi nunca se miran.
TOPE_DIAG = int(__import__("os").environ.get("EXPLORAR_TOPE_DIAG", 60))

TTL_SERIE = 12 * 3600
_SERIES_CACHE = {}          # ticker -> {"puntos": [...], "ts": epoch}
_SERIES_LOCK = threading.Lock()


def _serie_1y(ticker, rango="1y"):
    """
    Serie con cache propio de 12h. Ver el comentario de arriba.

    OJO CON EL RANGO. Un "1y" de Yahoo trae ~251 cierres, y la ventana de 12
    meses necesita 253 (252 dias habiles + el de referencia). Resultado: la
    fuerza a 12 meses salia None SIEMPRE, y como el orden de sectores se
    calcula con ese numero, la lista de "sectores que tiran" no estaba
    ordenada por nada -- era el orden en que estan escritos en ETFS_SECTOR.
    Se veia perfectamente normal y no significaba nada.
    Por eso el benchmark y los ETF se piden con rango "2y": son 24 simbolos,
    cuesta lo mismo, y asi la ventana de 12 meses existe de verdad.
    """
    clave = f"{ticker}|{rango}"
    ahora = time.time()
    with _SERIES_LOCK:
        c = _SERIES_CACHE.get(clave)
        if c and (ahora - c["ts"]) <= TTL_SERIE:
            return c["puntos"]
    puntos = data_source.get_price_history(ticker, rango, suffix="") or []
    with _SERIES_LOCK:
        _SERIES_CACHE[clave] = {"puntos": puntos, "ts": ahora}
    return puntos


def series_en_cache():
    ahora = time.time()
    with _SERIES_LOCK:
        return sum(1 for c in _SERIES_CACHE.values() if (ahora - c["ts"]) <= TTL_SERIE)


# ---------------------------------------------------------------------------
# Utilidades de serie
# ---------------------------------------------------------------------------

def _paralelo(func, items, workers=_WORKERS):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(func, items))


def _cierres(puntos):
    return [p["close"] for p in puntos if p.get("close") is not None]


def _sma(cierres, n):
    if len(cierres) < n:
        return None
    return sum(cierres[-n:]) / n


def _ret_pct(cierres, dias):
    """Rentabilidad de los ultimos `dias` habiles. None si no hay historial."""
    if len(cierres) < dias + 1 or not cierres[-dias - 1]:
        return None
    return (cierres[-1] / cierres[-dias - 1] - 1) * 100


# Ventanas en dias habiles: ~21 por mes.
VENTANAS = [("m1", 21), ("m3", 63), ("m6", 126), ("m12", 252)]


def _fuerza_vs_benchmark(cierres, cierres_bench):
    """
    Fuerza relativa simple: cuanto rindio el ETF MENOS cuanto rindio el
    S&P 500, en el mismo plazo. Positivo = le esta ganando al mercado.

    Es una resta de rentabilidades, no el cociente de precios del indicador
    de Weinstein. Para comparar sectores entre si es lo mismo y se lee mejor:
    "+9,8%" dice cuanto le saco al indice en tres meses.
    """
    out = {}
    for clave, dias in VENTANAS:
        a = _ret_pct(cierres, dias)
        b = _ret_pct(cierres_bench, dias)
        out[clave] = None if (a is None or b is None) else round(a - b, 2)
    return out


# ---------------------------------------------------------------------------
# Fundamentales (una sola consulta por ticker)
# ---------------------------------------------------------------------------
_QS = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_MODULOS = "price,financialData,assetProfile"

# ---------------------------------------------------------------------------
# CRECIMIENTO TTM -- el que usa TradingView
# ---------------------------------------------------------------------------
# EL PROBLEMA
# ===========
# `financialData.earningsGrowth` de Yahoo es el ULTIMO TRIMESTRE contra el
# mismo trimestre del año anterior. TradingView filtra por "Crecimiento BPA
# dil., TTM YoY": los ULTIMOS DOCE MESES contra los doce anteriores. Son dos
# numeros distintos de la misma empresa, y con umbral de 25% dan listas
# distintas -- un trimestre bueno aislado pasa el filtro trimestral y no el
# TTM, y al reves.
#
# Hasta ahora esto se documentaba como "aproximacion conocida". Se puede
# hacer bien: el endpoint fundamentals-timeseries entrega la serie trimestral
# completa, y con ocho trimestres el TTM YoY sale de una resta.
#
#     TTM actual   = suma de los trimestres 1-4 (los mas recientes)
#     TTM anterior = suma de los trimestres 5-8
#     crecimiento  = TTM actual / TTM anterior - 1
#
# CUANDO NO SE PUEDE, NO SE INVENTA
# ==================================
# Si faltan trimestres, o si el TTM anterior es cero o negativo (una empresa
# que venia perdiendo plata), el porcentaje no significa nada: dividir por un
# numero negativo da un "crecimiento" con el signo dado vuelta. En esos casos
# devuelve None y el analisis cae al trimestral, DICIENDOLO en el campo
# `crecFuente`. Cada accion lleva escrito con que metodo se midio.
#
# CAMBIO (post-diagnostico real): sumar 8 trimestres nosotros mismos no
# funciona -- Yahoo solo expone ~5 trimestres de `quarterlyDilutedEPS` /
# `quarterlyTotalRevenue` por este endpoint (confirmado con NVDA en
# produccion: "5 trimestres utiles" en ambos campos, siempre, no un caso
# aislado). En vez de sumar nosotros, se pide el TTM YA CALCULADO por Yahoo
# (`trailingDilutedEPS` / `trailingTotalRevenue`): un valor por trimestre
# reportado, donde cada valor YA es la suma de los 4 trimestres previos a esa
# fecha. Con eso, comparar el TTM actual contra el de hace 4 trimestres
# necesita solo 5 puntos (el de ahora + 4 hacia atras), no 8 -- y 5 es
# justo lo que Yahoo entrega.
_TIMESERIES = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/"
               "v1/finance/timeseries/{symbol}")
_TIPOS_TTM = "trailingDilutedEPS,trailingTotalRevenue"


def _suma_ttm(valores):
    """(ttm_actual, ttm_anterior) desde una lista de TTM trailing ordenada de
    mas viejo a mas nuevo (cada valor ya es una suma de 4 trimestres, hecha
    por Yahoo). None si no hay al menos 5 puntos (el actual + 4 atras)."""
    limpios = [v for v in valores if isinstance(v, (int, float))]
    if len(limpios) < 5:
        return None, None
    return limpios[-1], limpios[-5]


def _crecimiento_ttm_uno(ticker, con_motivo=False):
    """
    Devuelve {"crecBpa":…, "crecVentas":…} en % TTM YoY, o {} si no se pudo.
    Con `con_motivo=True` devuelve (datos, motivo) -- lo usa
    /fundamentales-diag.

    EL MOTIVO NO ES ADORNO. La primera corrida con TTM devolvio
    conCrecimientoTTM = 0 sobre 104 acciones, y desde afuera era imposible
    saber si Yahoo respondia mal, si el nombre del campo estaba equivocado o
    si de verdad ninguna tenia suficiente historia. El diagnostico real
    (NVDA, en produccion) mostro la causa: `quarterlyDilutedEPS` /
    `quarterlyTotalRevenue` solo traen ~5 trimestres por este endpoint, nunca
    los 8 que la version anterior necesitaba para sumar el TTM a mano. La
    solucion no es pedir mas historia (Yahoo no la tiene) sino pedirle a
    Yahoo el TTM ya sumado (`trailing...`), que con esos mismos 5 puntos
    alcanza.
    """
    ahora = int(time.time())
    # 3 años hacia atras: sobra margen para los 5 puntos trailing que hacen
    # falta (actual + 4 trimestres atras), con holgura para reportes tardios.
    desde = ahora - int(3.2 * 365 * 24 * 3600)
    motivo = "?"
    try:
        s, crumb = data_source._asegurar_crumb()
        params = {"symbol": ticker, "type": _TIPOS_TTM,
                  "period1": desde, "period2": ahora, "merge": "false"}
        if crumb:
            params["crumb"] = crumb
        resp = s.get(_TIMESERIES.format(symbol=ticker), params=params,
                     timeout=data_source._TIMEOUT)
        if resp.status_code != 200:
            motivo = f"HTTP {resp.status_code}: {(resp.text or '')[:120]}"
            return ({}, motivo) if con_motivo else {}
        cuerpo = resp.json()
        bloques = ((cuerpo.get("timeseries") or {}).get("result") or [])
        if not bloques:
            motivo = f"200 sin result. Claves del cuerpo: {sorted(cuerpo.keys())[:6]}"
            return ({}, motivo) if con_motivo else {}
    except Exception as e:
        motivo = f"{type(e).__name__}: {e}"
        return ({}, motivo) if con_motivo else {}

    series = {}
    for b in bloques:
        for clave in ("trailingDilutedEPS", "trailingTotalRevenue"):
            filas = b.get(clave)
            if not filas:
                continue
            valores = []
            for f in filas:
                if not isinstance(f, dict):
                    continue
                v = (f.get("reportedValue") or {})
                v = v.get("raw") if isinstance(v, dict) else v
                valores.append(v if isinstance(v, (int, float)) else None)
            series[clave] = valores

    out, detalle = {}, {}
    for clave, destino in (("trailingDilutedEPS", "crecBpa"),
                           ("trailingTotalRevenue", "crecVentas")):
        vals = series.get(clave)
        utiles = [v for v in (vals or []) if isinstance(v, (int, float))]
        actual, anterior = _suma_ttm(vals or [])
        if actual is None or anterior is None:
            detalle[clave] = (f"{len(utiles)} puntos TTM útiles"
                              if vals is not None else "el campo no vino")
            continue
        # anterior <= 0: el porcentaje sale con el signo dado vuelta y seria
        # peor que no tener el dato.
        if anterior <= 0:
            detalle[clave] = f"base de comparación {round(anterior,2)} (≤ 0)"
            continue
        out[destino] = round((actual / anterior - 1) * 100, 1)
        detalle[clave] = "ok"

    motivo = "ok" if out else ("sin datos utilizables · " +
                               " · ".join(f"{k}: {v}" for k, v in detalle.items()))
    return (out, motivo) if con_motivo else out


def crecimiento_ttm(tickers):
    """
    ({ticker: {"crecBpa":…, "crecVentas":…}}, {motivo: cuantas}).

    El segundo valor es el recuento de por que fallaron las que fallaron.
    Sin eso, "0 de 104 en TTM" no dice nada accionable.
    """
    def _uno(t):
        d, m = _crecimiento_ttm_uno(t, con_motivo=True)
        return t, d, m
    datos, motivos = {}, {}
    for t, d, m in _paralelo(_uno, tickers):
        # Los motivos se agrupan por su parte estable: si vienen 104 mensajes
        # distintos por el numero de trimestres, se pierde la señal.
        clave = m.split(" · ")[0] if m else "?"
        motivos[clave] = motivos.get(clave, 0) + 1
        if d:
            datos[t] = d
    return datos, motivos


def _fundamentales_uno(ticker):
    """
    Capitalizacion, crecimiento y sector/industria en UNA peticion.

    Devuelve (ticker, datos|None, motivo). Nunca inventa: un campo que no
    viene queda en None y el embudo lo trata como "no se puede evaluar"
    (que NO es lo mismo que "no pasa" -- ver embudo()).

    El `motivo` viaja hasta el resultado del analisis. Sin el, un embudo que
    se cae entero en el filtro de capitalizacion es indistinguible de un
    mercado sin candidatas -- que es exactamente lo que paso la primera vez.
    """
    r, motivo = data_source.quote_summary(ticker, _MODULOS)
    if not r:
        return ticker, None, motivo

    price = r.get("price") or {}
    fin = r.get("financialData") or {}
    perfil = r.get("assetProfile") or {}

    def crudo(d, k):
        v = (d.get(k) or {})
        v = v.get("raw") if isinstance(v, dict) else v
        return v if isinstance(v, (int, float)) else None

    cap = crudo(price, "marketCap")
    return ticker, {
        "capB": round(cap / 1e9, 2) if cap else None,
        "crecBpa": round(crudo(fin, "earningsGrowth") * 100, 1)
                   if crudo(fin, "earningsGrowth") is not None else None,
        "crecVentas": round(crudo(fin, "revenueGrowth") * 100, 1)
                      if crudo(fin, "revenueGrowth") is not None else None,
        "sector": perfil.get("sector") or None,
        "industria": perfil.get("industry") or None,
        "nombre": price.get("longName") or price.get("shortName") or ticker,
    }, motivo


def fundamentales(tickers):
    """
    Devuelve (datos_por_ticker, diagnostico).

    DOS FUENTES PARA LA CAPITALIZACION, A PROPOSITO. La primera corrida real
    murio entera en el filtro de capitalizacion (127 de 127 sin dato), asi
    que ese campo ahora se pide por dos caminos independientes:

      1. /v7/finance/quote por LOTES -- 4 peticiones para 127 acciones, y
         trae exactamente lo que hace falta.
      2. quoteSummary uno por uno -- ademas del crecimiento y el sector.

    Si el (1) responde y el (2) no, el embudo igual puede filtrar por tamaño
    en vez de descartarlo todo. El diagnostico dice cual funciono.
    """
    caps_lote, motivos_lote = data_source.market_caps(list(tickers))

    datos, motivos_qs = {}, {}
    for t, d, motivo in _paralelo(_fundamentales_uno, tickers):
        motivos_qs[motivo] = motivos_qs.get(motivo, 0) + 1
        if d:
            datos[t] = d

    # La capitalizacion del lote rellena la que falte. No pisa la de
    # quoteSummary cuando esta vino: son el mismo dato de la misma fuente,
    # pero si por lo que sea difieren, la de la ficha completa es la buena.
    for t, cap in caps_lote.items():
        if t not in datos:
            datos[t] = {"capB": cap, "crecBpa": None, "crecVentas": None,
                        "sector": None, "industria": None, "nombre": t}
        elif datos[t].get("capB") is None:
            datos[t]["capB"] = cap

    # ---- Crecimiento TTM, que es el que usa TradingView -------------------
    # Se pide DESPUES y solo para las que ya tienen ficha: es una peticion mas
    # por accion y no vale la pena gastarla en una que ni siquiera trajo
    # capitalizacion. Lo que devuelve PISA al trimestral, porque es el numero
    # correcto -- pero el trimestral se queda como respaldo y cada accion
    # dice con cual se midio.
    for t, d in datos.items():
        d["crecFuente"] = "trimestral" if isinstance(d.get("crecBpa"), (int, float)) else None
    ttm, motivos_ttm = crecimiento_ttm(sorted(datos.keys()))
    for t, v in ttm.items():
        if t not in datos:
            continue
        if "crecBpa" in v:
            datos[t]["crecBpa"] = v["crecBpa"]
        if "crecVentas" in v:
            datos[t]["crecVentas"] = v["crecVentas"]
        if v:
            datos[t]["crecFuente"] = "ttm"

    con_cap = sum(1 for d in datos.values() if isinstance(d.get("capB"), (int, float)))
    con_crec = sum(1 for d in datos.values() if isinstance(d.get("crecBpa"), (int, float)))
    con_ttm = sum(1 for d in datos.values() if d.get("crecFuente") == "ttm")
    diagnostico = {
        "pedidos": len(tickers),
        "conCapitalizacion": con_cap,
        "conCrecimiento": con_crec,
        "conCrecimientoTTM": con_ttm,
        "conCrecimientoTrimestral": con_crec - con_ttm,
        "motivosTTM": motivos_ttm,
        "porLote": len(caps_lote),
        "motivosLote": motivos_lote,
        "motivosFicha": motivos_qs,
        "crumb": data_source.estado_crumb(),
    }
    if con_cap == 0 and tickers:
        print(f"[explorar] NINGUNA de las {len(tickers)} trajo capitalizacion. "
              f"Lote: {motivos_lote} · Ficha: {motivos_qs} · "
              f"Crumb: {data_source.estado_crumb()}")
    return datos, diagnostico


# ---------------------------------------------------------------------------
# El embudo
# ---------------------------------------------------------------------------

def _metricas_de_serie(puntos):
    """precio, sma50, sma200, volumen medio 90d. None cuando falta historial."""
    c = _cierres(puntos)
    if len(c) < 60:
        return None
    vols = [p["volume"] for p in puntos[-90:] if p.get("volume")]
    return {
        "precio": c[-1],
        "sma50": _sma(c, 50),
        "sma200": _sma(c, 200),
        "volM": round((sum(vols) / len(vols)) / 1e6, 2) if vols else None,
        "cierres": c,
    }


def _dif_pct(a, b):
    """Diferencia relativa entre dos numeros, en %. None si no se puede."""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if not b:
        return None
    return round(abs(a - b) / abs(b) * 100, 2)


def _comparar_lote(metricas, cuantas=None):
    """
    Toma una muestra de las que vinieron por lote, baja su serie y compara.

    POR QUE EXISTE
    ==============
    Los cuatro numeros del camino rapido los calcula Yahoo, no este codigo.
    Cambiar de fuente sin comprobar nada seria exactamente el tipo de cosa
    que despues aparece como "el embudo da distinto y nadie sabe por que".
    Esto lo mide en la misma corrida, sobre acciones de verdad, y lo deja
    escrito en el resultado.

    NO corta ni corrige nada: solo informa. Si un dia las diferencias se
    disparan, se ve en el resultado y ahi se decide.
    """
    cuantas = TOPE_VERIFICAR if cuantas is None else cuantas
    del_lote = sorted(t for t, m in metricas.items()
                      if (m or {}).get("fuente") == "lote")
    if not cuantas or not del_lote:
        return None
    # Las mas liquidas primero: son las que de verdad van a salir como
    # candidatas, y una diferencia ahi importa mucho mas que en una micro cap.
    muestra = sorted(del_lote,
                     key=lambda t: -((metricas[t] or {}).get("volM") or 0))[:cuantas]
    filas, difs = [], {"precio": [], "sma50": [], "sma200": [], "volM": []}
    for t in muestra:
        serie = _metricas_de_serie(_serie_1y(t))
        if not serie:
            continue
        lote = metricas[t]
        fila = {"ticker": t}
        for campo in ("precio", "sma50", "sma200", "volM"):
            d = _dif_pct(lote.get(campo), serie.get(campo))
            fila[campo] = {"lote": lote.get(campo), "serie": serie.get(campo),
                           "difPct": d}
            if d is not None:
                difs[campo].append(d)
        filas.append(fila)
    if not filas:
        return None
    peor = {c: (round(max(v), 2) if v else None) for c, v in difs.items()}
    # El precio es el que MAS puede diferir legitimamente (el del lote es
    # intradia, el de la serie es el ultimo cierre), asi que no manda en el
    # veredicto. Las medias y el volumen si tienen que calzar.
    criticos = [peor[c] for c in ("sma50", "sma200", "volM") if peor[c] is not None]
    return {
        "comparadas": len(filas),
        "peorDifPct": peor,
        "calza": bool(criticos) and max(criticos) <= 2.0,
        "detalle": filas,
        "nota": "El precio del lote es el actual (puede ser intradía) y el de "
                "la serie es el último cierre: que difieran es normal. Las "
                "medias y el volumen sí deberían calzar dentro de ~2%.",
    }


def embudo(metricas, umbrales, detalle_inicio="S&P 500 + Nasdaq-100 + tu grilla"):
    """
    Aplica los 7 filtros y devuelve (pasos, sobrevivientes_por_etapa).

    REGLA SOBRE LOS DATOS QUE FALTAN: si un campo no se pudo calcular, la
    accion NO pasa ese filtro. Es deliberado y es el lado conservador -- una
    accion sin capitalizacion conocida no es "probablemente grande", es
    desconocida, y el embudo esta para dejar pasar solo lo que se verifico.
    El conteo de descartadas por dato faltante se informa aparte, para que
    no se confunda "no cumple" con "no se sabe".
    """
    def ok(v, minimo):
        return isinstance(v, (int, float)) and v >= minimo

    pasos = []
    vivos = dict(metricas)
    pasos.append({"filtro": "Universo de partida", "detalle": detalle_inicio,
                  "quedan": len(vivos), "grupo": "inicio"})

    vivos = {t: m for t, m in vivos.items() if ok(m.get("precio"), umbrales["precio"])}
    pasos.append({"filtro": f"Precio ≥ {umbrales['precio']:g} USD", "detalle": "descarta chatarra",
                  "quedan": len(vivos), "grupo": "calidad"})

    vivos = {t: m for t, m in vivos.items() if ok(m.get("volM"), umbrales["volM"])}
    pasos.append({"filtro": f"Volumen 90 d ≥ {umbrales['volM']:g} M", "detalle": "entras y sales sin mover el precio",
                  "quedan": len(vivos), "grupo": "calidad"})

    vivos = {t: m for t, m in vivos.items()
             if m.get("sma50") and m["precio"] > m["sma50"]}
    pasos.append({"filtro": "Precio > SMA 50", "detalle": "el precio lidera",
                  "quedan": len(vivos), "grupo": "tendencia"})

    vivos = {t: m for t, m in vivos.items()
             if m.get("sma50") and m.get("sma200") and m["sma50"] > m["sma200"]}
    pasos.append({"filtro": "SMA 50 > SMA 200", "detalle": "la tendencia de fondo es alcista",
                  "quedan": len(vivos), "grupo": "tendencia"})

    return pasos, vivos


def embudo_fundamental(vivos, fund, umbrales, sin_fundamentales=None):
    """
    Segunda mitad del embudo: los tres filtros que necesitan Yahoo.

    LAS QUE NO SE PUEDEN EVALUAR NO SE PIERDEN
    ===========================================
    Antes, una accion a la que Yahoo no le entregaba la capitalizacion
    simplemente desaparecia del analisis. Para un informe eso da lo mismo;
    para lo que Cristian usa esto -- una PRIMERA ALERTA, una lista corta de
    "anda a mirar estas en TradingView" -- es lo peor que puede pasar:
    esconde nombres sin decirlo.

    Ahora esas acciones salen aparte, en `dudosas`, con el detalle de que
    dato falto. Pasaron todos los filtros que SI se pudieron medir. En
    TradingView, que es donde Cristian va a mirar igual, ese dato esta a la
    vista en dos segundos.

    SIN_FUNDAMENTALES ES OTRA COSA DISTINTA A "DUDOSA"
    ====================================================
    Un ticker en `sin_fundamentales` (Bitcoin, ver main.py) no es que Yahoo
    no le haya entregado el dato: es que el dato NO EXISTE, porque no es una
    empresa. Meterlo en `dudosas` seria mentir ("le falto medir esto") sobre
    algo que nunca se iba a poder medir. Por eso a estos tickers los tres
    filtros de esta funcion los dejan pasar directo, sin marca de duda y sin
    sumar a `sin_dato` -- el resto del embudo (precio, volumen, tendencia,
    Weinstein, fuerza relativa) SI se les aplica igual que a cualquiera.
    """
    sin_fund = set(sin_fundamentales or ())
    pasos = []
    sin_dato = {"capB": 0, "crecBpa": 0, "crecVentas": 0}
    dudosas = {}          # ticker -> [campos que faltaron]

    def filtrar(campo, minimo, etiqueta, detalle):
        nonlocal vivos
        nuevos = {}
        for t, m in vivos.items():
            if t in sin_fund:
                nuevos[t] = m
                continue
            v = (fund.get(t) or {}).get(campo)
            if not isinstance(v, (int, float)):
                sin_dato[campo] += 1
                dudosas.setdefault(t, {"metricas": m, "faltan": []})
                dudosas[t]["faltan"].append(campo)
                continue
            if v >= minimo:
                nuevos[t] = m
        vivos = nuevos
        pasos.append({"filtro": etiqueta, "detalle": detalle,
                      "quedan": len(vivos), "grupo": "crecimiento"})

    filtrar("capB", umbrales["capB"], f"Capitalización ≥ {umbrales['capB']:g} B",
            "empresas de tamaño real")
    filtrar("crecBpa", umbrales["crecimiento"], f"BPA trim. YoY ≥ {umbrales['crecimiento']:g} %",
            "gana más que hace un año")
    filtrar("crecVentas", umbrales["crecimiento"], f"Ingresos trim. YoY ≥ {umbrales['crecimiento']:g} %",
            "y vende más")

    # Una accion que fallo un filtro REAL (dato presente, no alcanza el
    # umbral) no es dudosa: no pasa, y punto. Solo quedan las que se cayeron
    # por falta de dato en algun paso.
    reales = set(vivos)
    dudosas = {t: d for t, d in dudosas.items() if t not in reales}
    return pasos, vivos, sin_dato, dudosas


# ---------------------------------------------------------------------------
# El trabajo completo, en segundo plano
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_ESTADO = {
    "estado": "inactivo",     # inactivo | corriendo | listo | error
    "etapa": "",
    "progreso": 0,            # 0-100
    "iniciado": None,
    "terminado": None,
    "error": None,
}
_RESULTADO = {"datos": None, "ts": 0}


def _set(etapa, progreso):
    with _LOCK:
        _ESTADO["etapa"] = etapa
        _ESTADO["progreso"] = progreso
    print(f"[explorar] {progreso:3d}% · {etapa}")


def estado():
    with _LOCK:
        e = dict(_ESTADO)
    edad = time.time() - _RESULTADO["ts"] if _RESULTADO["ts"] else None
    e["hayResultado"] = _RESULTADO["datos"] is not None
    e["edadResultadoSeg"] = int(edad) if edad is not None else None
    e["resultadoVencido"] = bool(edad is not None and edad > TTL_RESULTADO)
    return e


def resultado():
    return _RESULTADO["datos"]


def _analizar(universo, serie_5y, indice_5y, umbrales, nucleo=None, rotacion=None,
              sin_fundamentales=None):
    """
    El pipeline completo. `serie_5y(ticker)` y `indice_5y()` los inyecta
    server.py para reusar SU cache de 24h -- este modulo no la duplica.
    """
    t0 = time.time()

    # ---- Paso 1 · sectores e industrias -----------------------------------
    _set("Bajando el S&P 500 de referencia…", 3)
    # "2y" y no "1y": con un año justo la ventana de 12 meses nunca alcanzaba
    # (ver el comentario de _serie_1y). Son 24 simbolos en total, no cambia
    # el costo de la corrida.
    bench = _cierres(_serie_1y(BENCHMARK, "2y"))
    if len(bench) < 260:
        print(f"[explorar] AVISO: el benchmark trajo {len(bench)} cierres, "
              f"la ventana de 12 meses puede quedar incompleta.")

    def _etf(par):
        nombre, sim = par
        c = _cierres(_serie_1y(sim, "2y"))
        if len(c) < 30:
            return None
        fr = _fuerza_vs_benchmark(c, bench)
        fase = indicador_fuerza_fase.evaluar_semanal(
            data_source.get_price_history(sim, "5y", suffix="") or [], None)
        return {
            "nombre": nombre, "etf": sim, "fuerza": fr,
            "fase": (fase or {}).get("fase") if (fase or {}).get("disponible") else None,
        }

    _set("Midiendo 11 sectores contra el S&P 500…", 8)
    sectores = [x for x in _paralelo(_etf, ETFS_SECTOR) if x]
    _set("Midiendo 12 industrias…", 14)
    industrias = [x for x in _paralelo(_etf, ETFS_INDUSTRIA) if x]
    for grupo in (sectores, industrias):
        grupo.sort(key=lambda x: (x["fuerza"].get("m12") is None, -(x["fuerza"].get("m12") or 0)))

    # ---- Paso 2a · precio, volumen y medias -------------------------------
    # ESTA ERA LA ETAPA LENTA, Y YA NO LO ES.
    #
    # Antes: un historial de un año POR CADA simbolo, para sacar cuatro
    # numeros. 5.254 simbolos = 5.254 peticiones = ~50 minutos, que Render
    # gratuito no aguanta. Todo el problema de cobertura (el nucleo que no se
    # alcanzaba a revisar, la rotacion diaria del resto) salia de aca.
    #
    # Ahora: Yahoo ya calcula esos cuatro numeros y los entrega de a 40
    # simbolos por peticion, en el MISMO endpoint que este modulo ya usaba
    # para la capitalizacion. El mercado completo pasa a ~131 peticiones.
    # Ver data_source.metricas_por_lote.
    #
    # El camino viejo NO se borro: queda como respaldo para los simbolos a los
    # que el lote no les trajo los tres numeros. Si el endpoint por lotes se
    # cayera entero, `faltan` seria el universo completo y esto se comporta
    # exactamente como antes, presupuesto de tiempo incluido.
    metricas, diag_lote = {}, None
    if USAR_LOTE:
        _set(f"Precio y medias de {len(universo)} acciones, de a "
             f"{TAM_LOTE} por petición…", 20)
        try:
            metricas, diag_lote = data_source.metricas_por_lote(universo, TAM_LOTE)
        except Exception as e:
            print(f"[explorar] El camino por lotes fallo entero "
                  f"({type(e).__name__}: {e}). Sigo con el metodo de siempre.")
            metricas, diag_lote = {}, {"error": f"{type(e).__name__}: {e}"}
        print(f"[explorar] Lote: {len(metricas)} de {len(universo)} con "
              f"métricas en {(diag_lote or {}).get('peticiones', '?')} peticiones.")

    # Los que el lote no resolvio van por el camino viejo, respetando el
    # presupuesto de tiempo. El NUCLEO va primero, por lo mismo de siempre.
    faltan = [t for t in universo if t not in metricas]
    ahora = time.time()
    with _SERIES_LOCK:
        # Las claves de la cache son "TICKER|rango" desde que el benchmark y
        # los ETF se piden a 2 años (ver _serie_1y). Acá interesan SOLO las
        # de 1 año, que son las del universo: sin este filtro, comparar la
        # clave completa contra un ticker pelado no calzaría nunca y la
        # cache se veria siempre vacia.
        frescas = {clave.split("|", 1)[0]
                   for clave, c in _SERIES_CACHE.items()
                   if clave.endswith("|1y") and (ahora - c["ts"]) <= TTL_SERIE}
    en_cache = [t for t in faltan if t in frescas]
    por_bajar = [t for t in faltan if t not in frescas]
    orden = en_cache + por_bajar

    if orden:
        _set(f"{len(en_cache)} ya en caché · bajando hasta "
             f"{TOPE_DESCARGA_SEG // 60} min de las {len(por_bajar)} que faltan…", 30)
    t_desc = time.time()
    hechos, saltadas = [0], []
    lock_cnt = threading.Lock()

    def _serie_uno(t):
        # Las que estan en cache nunca se saltan: no cuestan red.
        if t not in frescas and (time.time() - t_desc) > TOPE_DESCARGA_SEG:
            saltadas.append(t)
            return t, None
        m = _metricas_de_serie(_serie_1y(t))
        if m:
            m["fuente"] = "serie"
        with lock_cnt:
            hechos[0] += 1
            n = hechos[0]
        if n % 20 == 0 or n == len(orden):
            pct = 30 + int(22 * n / max(1, len(orden)))   # 30 -> 52
            _set(f"Historiales: {n} de {len(orden)} · "
                 f"{int(time.time() - t_desc)}s", pct)
        return t, m

    if orden:
        metricas.update({t: m for t, m in _paralelo(_serie_uno, orden) if m})
    if saltadas:
        print(f"[explorar] Presupuesto de tiempo agotado: {len(saltadas)} "
              f"sin revisar de {len(universo)}. Vuelve a correr el analisis y "
              f"seguira desde donde quedo -- lo bajado queda en cache 12h.")

    # ---- La comprobacion que hace auditable el camino nuevo ---------------
    # Los numeros del lote son de Yahoo, no calculados aca. Antes de creerles
    # a ciegas sobre 5.000 acciones, se toma una muestra chica y se compara
    # contra el metodo viejo, en la misma corrida. Cuesta unas pocas
    # peticiones y es lo unico que convierte "deberia calzar" en un dato.
    comparacion = _comparar_lote(metricas) if (USAR_LOTE and metricas) else None
    # CUANTO DEL NUCLEO SE ALCANZO A REVISAR. Es la unica cifra que dice si el
    # resultado se puede comparar con corridas anteriores (y con TradingView
    # restringido a los indices): si el nucleo quedo entero, "0 candidatas"
    # significa que de verdad no hubo; si quedo a medias, no significa nada.
    nucleo_set = set(nucleo or ())
    nucleo_revisado = len([t for t in metricas if t in nucleo_set]) if nucleo_set else None
    # DOS MOTIVOS DISTINTOS, Y CONFUNDIRLOS FUE UN ERROR MIO
    # ======================================================
    # Un ticker del nucleo puede faltar en `metricas` por dos razones que no
    # se parecen en nada:
    #
    #   1. SE SALTO POR TIEMPO. Es un problema de cobertura: la proxima
    #      corrida lo agarra, porque lo ya bajado queda en cache 12 h. ESTE
    #      es el caso que hay que gritar.
    #   2. SE PIDIO Y NO HAY DATOS. Yahoo respondio que el simbolo no existe
    #      (retirado, renombrado, fusionado -- ver la cuarentena en
    #      data_source.py) o devolvio menos sesiones de las que necesita una
    #      SMA 200. Volver a ejecutar NO lo arregla NUNCA.
    #
    # Antes los dos contaban igual, asi que dos simbolos muertos dejaban el
    # indicador en cobre y el aviso de ATENCION encendidos para siempre --
    # justo el ruido que hace que despues no le creas cuando el barrido SI se
    # corto de verdad.
    nucleo_saltado = sorted(nucleo_set & set(saltadas))
    nucleo_sin_datos = sorted(nucleo_set - set(metricas) - set(saltadas))
    # "Completo" = no quedo nada del nucleo sin revisar POR TIEMPO.
    nucleo_completo = bool(nucleo_set) and not nucleo_saltado

    _set(f"{len(metricas)} con historial suficiente. Aplicando filtros de precio y tendencia…", 55)
    detalle_inicio = (
        f"{len(metricas)} revisadas de {len(universo)} del universo"
        + (f" · el núcleo ({len(nucleo_set)}) quedó "
           + (("entero" + (f", salvo {len(nucleo_sin_datos)} sin datos en Yahoo"
                           if nucleo_sin_datos else ""))
              if nucleo_completo else f"en {nucleo_revisado}") if nucleo_set else "")
    )
    pasos_a, vivos = embudo(metricas, umbrales, detalle_inicio)

    # ---- Paso 2b · capitalizacion y crecimiento ---------------------------
    # OJO: los fundamentales se piden SOLO para las que pasaron precio,
    # volumen y tendencia. Eso es lo que hace barata la corrida (unas 130
    # peticiones en vez de 534) y es tambien el motivo de que en la app los
    # umbrales de precio/volumen/tendencia se puedan mover libremente pero
    # los de capitalizacion y crecimiento solo dentro de este subconjunto:
    # aflojar un filtro de tendencia deja entrar acciones para las que nunca
    # se pidio la capitalizacion. La app lo dice en vez de mentir con un
    # numero incompleto.
    _set(f"Pidiendo fundamentales de {len(vivos)} candidatas…", 60)
    # Se guarda ANTES de que el embudo fundamental lo reduzca: es el conjunto
    # sobre el que la app puede mover los umbrales de capitalizacion y
    # crecimiento sin volver a correr nada.
    vivos_tendencia = dict(vivos)
    fund, diag_fund = fundamentales(sorted(vivos.keys()))
    # LA CAPITALIZACION YA VINO GRATIS EN EL LOTE.
    # Es el mismo endpoint que trajo precio y medias, asi que no cuesta ni una
    # peticion extra. Se usa SOLO para rellenar lo que fundamentales() no
    # trajo: si quoteSummary contesto, ese dato manda. Esto achica la lista de
    # "para mirar a mano", que se llenaba de acciones cuyo unico problema era
    # que Yahoo no habia entregado la capitalizacion por el otro camino.
    rescatadas_cap = 0
    for t in vivos:
        cap_lote = (metricas.get(t) or {}).get("capB")
        if not isinstance(cap_lote, (int, float)):
            continue
        ficha = fund.setdefault(t, {})
        if not isinstance(ficha.get("capB"), (int, float)):
            ficha["capB"] = cap_lote
            rescatadas_cap += 1
    if rescatadas_cap:
        diag_fund = dict(diag_fund or {})
        diag_fund["conCapitalizacion"] = (diag_fund.get("conCapitalizacion") or 0) + rescatadas_cap
        diag_fund["rescatadasDelLote"] = rescatadas_cap
        print(f"[explorar] {rescatadas_cap} capitalizaciones rescatadas del "
              f"lote (quoteSummary no las trajo).")
    pasos_b, vivos, sin_dato, dudosas = embudo_fundamental(
        vivos, fund, umbrales, sin_fundamentales)
    pasos = pasos_a + pasos_b
    tras_embudo = sorted(vivos.keys())
    # Las dudosas tambien se diagnostican: si ademas resultan estar en fase 2
    # con fuerza, valen mucho mas la pena que una que paso el embudo pero
    # esta en fase 4. Se acotan a TOPE_DUDOSAS para no disparar la cuenta de
    # descargas de 5 años.
    #
    # SE ORDENAN POR VOLUMEN, NO ALFABETICAMENTE. Antes era
    # `sorted(dudosas.keys())[:15]`, o sea las 15 primeras del abecedario. En
    # la primera corrida real hubo 127 dudosas y la lista que llego al
    # usuario fue ABNB, ADP, AMGN, ANET: puras A, mientras NVDA, MU y DELL
    # -- que estaban en la misma bolsa -- no se diagnosticaron nunca. Con el
    # volumen medio de 90 dias, que ya esta calculado y no cuesta ninguna
    # peticion, la lista queda encabezada por las mas liquidas, que es un
    # criterio defendible en vez de un accidente del abecedario.
    dudosas_diag = sorted(
        dudosas.keys(),
        key=lambda t: -((dudosas[t]["metricas"] or {}).get("volM") or 0),
    )[:TOPE_DUDOSAS]
    _set(f"{len(tras_embudo)} pasaron el embudo "
         f"(+{len(dudosas_diag)} sin dato completo). Ahora Weinstein…", 72)

    # ---- Pasos 3 y 4 · Weinstein y fuerza ---------------------------------
    idx = indice_5y()

    def _diag(t):
        puntos = serie_5y(t)
        if not puntos:
            return t, None
        return t, {
            "diario": indicador_fuerza_fase.evaluar_diario(puntos, idx),
            "semanal": indicador_fuerza_fase.evaluar_semanal(puntos, idx),
        }

    # A QUIENES SE LES CALCULA FASE Y FUERZA
    # =======================================
    # Antes: solo a las que pasaban el embudo COMPLETO. Eso hacia imposible
    # apagar los filtros de Weinstein/fuerza desde la app para comparar
    # contra TradingView, y tambien impedia aflojar el umbral de crecimiento
    # sin volver a correr todo: las que entraban al aflojarlo no tenian fase.
    #
    # Ahora se diagnostica a TODAS las que pasaron precio/volumen/tendencia
    # (`vivos_tendencia`), hasta TOPE_DIAG, ordenadas por volumen. Cada
    # diagnostico cuesta una serie de 5 años, asi que el tope es real y la
    # app dice cuantas quedaron sin diagnosticar en vez de esconderlo.
    candidatos_diag = sorted(
        vivos_tendencia.keys(),
        key=lambda t: -((vivos_tendencia[t] or {}).get("volM") or 0),
    )
    # Las que pasaron el embudo entero van SIEMPRE, aunque sean poco liquidas:
    # son el resultado principal y quedarse sin su fase seria absurdo.
    a_diagnosticar = list(dict.fromkeys(
        tras_embudo + candidatos_diag[:TOPE_DIAG] + dudosas_diag))
    sin_diagnosticar = [t for t in vivos_tendencia if t not in set(a_diagnosticar)]

    _set(f"Fase y fuerza de {len(a_diagnosticar)} acciones…", 74)
    diags = {t: d for t, d in _paralelo(_diag, a_diagnosticar) if d}
    _set("Cruzando fase, score y fuerza relativa…", 90)

    def _tarjeta(t, metricas_t):
        d = diags.get(t) or {}
        sem = d.get("semanal") or {}
        dia = d.get("diario") or {}
        f = (fund.get(t) or {})
        # scoreConfirmaciones lo calcula el propio indicador; contarlas de
        # nuevo aca seria duplicar su logica y arriesgarse a que las dos
        # cuentas se separen en el futuro.
        n_conf = sem.get("scoreConfirmaciones") if sem.get("disponible") else None
        return {
            "ticker": t,
            "nombre": f.get("nombre") or t,
            "sector": f.get("sector"),
            "industria": f.get("industria"),
            "precio": round(metricas_t["precio"], 2),
            "sma50": round(metricas_t["sma50"], 2) if metricas_t.get("sma50") else None,
            "capB": f.get("capB"),
            "crecBpa": f.get("crecBpa"),
            "crecVentas": f.get("crecVentas"),
            "volM": metricas_t.get("volM"),
            "fase": sem.get("fase") if sem.get("disponible") else None,
            "confirmaciones": n_conf,
            "veredictoSemanal": sem.get("veredicto"),
            "score": dia.get("score") if dia.get("disponible") else None,
            "caso": dia.get("caso"),
            "fuerzaRelativa": dia.get("fuerzaRelativa") or dia.get("fuerza_relativa"),
            # Para ir directo a mirarla en TradingView, que es donde Cristian
            # hace el analisis de verdad. Este modulo es la PRIMERA ALERTA.
            "tradingview": f"https://www.tradingview.com/symbols/{t}/",
            # Para que la app pueda decir "sin datos de empresa" en vez de
            # dejar los campos en blanco sin explicar por que -- ver
            # SIN_FUNDAMENTALES en main.py.
            "sinFundamentales": t in (sin_fundamentales or ()),
        }

    candidatas = [_tarjeta(t, vivos[t]) for t in tras_embudo]

    # ---- LAS METRICAS CRUDAS, PARA QUE LA APP PUEDA MOVER LOS FILTROS -----
    # Sin esto, cambiar un umbral obliga a volver a correr los 4-7 minutos.
    # Con esto, la app rehace el embudo entero en el telefono, al instante, y
    # puede mostrar QUIENES se cayeron en cada filtro -- que es lo que pidio
    # Cristian y lo que hacia bien el prototipo.
    #
    # Claves cortas a proposito: son ~534 filas y esto viaja por el celular.
    # t ticker · n nombre · p precio · v volumen 90d (M) · c50/c200 medias
    # cB capitalizacion (B) · gB crecimiento BPA · gV crecimiento ventas
    # gF fuente del crecimiento (ttm|trimestral) · f fase · cf confirmaciones
    # sc score 0-8 · fr fuerza relativa (1 sube, 0 no, null no se sabe)
    # se sector · in industria · dg si se le calculo fase/fuerza
    # sf si es un activo SIN_FUNDAMENTALES (Bitcoin): el telefono necesita
    # saberlo para que mover los sliders de capitalizacion/crecimiento no lo
    # bote del embudo recalculado -- esos tres filtros no se le aplican.
    def _fila(t, m):
        f = fund.get(t) or {}
        d = diags.get(t) or {}
        sem, dia = (d.get("semanal") or {}), (d.get("diario") or {})
        frd = dia.get("fuerzaRelativa") or dia.get("fuerza_relativa") or {}
        fr = None
        if frd.get("disponible"):
            fr = 1 if frd.get("pendientePositiva") else 0
        return {
            "t": t,
            "n": f.get("nombre") or t,
            "p": round(m["precio"], 2),
            "v": m.get("volM"),
            "c50": round(m["sma50"], 2) if m.get("sma50") else None,
            "c200": round(m["sma200"], 2) if m.get("sma200") else None,
            "cB": f.get("capB"),
            "gB": f.get("crecBpa"),
            "gV": f.get("crecVentas"),
            "gF": f.get("crecFuente"),
            "f": sem.get("fase") if sem.get("disponible") else None,
            "cf": sem.get("scoreConfirmaciones") if sem.get("disponible") else None,
            "sc": dia.get("score") if dia.get("disponible") else None,
            "fr": fr,
            "se": f.get("sector"),
            "in": f.get("industria"),
            "dg": 1 if t in diags else 0,
            "sf": 1 if t in (sin_fundamentales or ()) else 0,
        }

    acciones = [_fila(t, m) for t, m in sorted(metricas.items())]

    def _pasa_weinstein(c):
        return c["fase"] == 2 and isinstance(c["confirmaciones"], int) and c["confirmaciones"] >= 4

    def _pasa_fuerza(c):
        # El documento pide DOS cosas, no una: score 6-8 **y** la linea de
        # fuerza relativa subiendo. Una accion puede tener las 8 señales
        # internas ordenadas y aun asi ir mas lenta que el S&P 500 -- "sube
        # 10% en un año donde el mercado subio 25% no es fuerte, es lenta".
        # Si la fuerza relativa no se pudo calcular NO pasa, mismo criterio
        # que el resto del embudo: no se sabe no es lo mismo que si.
        if not isinstance(c["score"], (int, float)) or c["score"] < 6:
            return False
        fr = c.get("fuerzaRelativa") or {}
        return bool(fr.get("disponible") and fr.get("pendientePositiva"))

    tras_weinstein = [c for c in candidatas if _pasa_weinstein(c)]
    tras_fuerza = [c for c in tras_weinstein if _pasa_fuerza(c)]
    # Se cuenta aparte cuantas se cayeron por CADA motivo: sin esto, ver
    # "de 28 quedaron 6" no dice si el embudo esta bien calibrado o si
    # simplemente falto un dato.
    caidas = {
        "porFase": sum(1 for c in candidatas if c["fase"] != 2),
        "porConfirmaciones": sum(1 for c in candidatas
                                 if c["fase"] == 2 and not _pasa_weinstein(c)),
        "porScore": sum(1 for c in tras_weinstein
                        if not isinstance(c["score"], (int, float)) or c["score"] < 6),
        "porFuerzaRelativa": sum(1 for c in tras_weinstein
                                 if isinstance(c["score"], (int, float)) and c["score"] >= 6
                                 and not _pasa_fuerza(c)),
    }
    tras_fuerza.sort(key=lambda c: (-(c["score"] or 0), c["ticker"]))

    # ---- Paso 5 · sin repetir industria -----------------------------------
    vistas, finalistas, descartadas = {}, [], []
    for c in tras_fuerza:
        ind = c["industria"] or "(industria desconocida)"
        if ind in vistas:
            c = dict(c); c["repiteCon"] = vistas[ind]
            descartadas.append(c)
        else:
            vistas[ind] = c["ticker"]
            finalistas.append(c)

    por_sector = {}
    for c in finalistas:
        s = c["sector"] or "(sin sector)"
        por_sector[s] = por_sector.get(s, 0) + 1

    # Las dudosas que ademas resultaron tecnicamente sanas suben a su propia
    # lista: son las que mas vale la pena mirar en TradingView, porque lo
    # unico que les falta es un dato que ahi se ve de inmediato.
    NOMBRE_CAMPO = {"capB": "capitalización", "crecBpa": "crecimiento del BPA",
                    "crecVentas": "crecimiento de ingresos"}
    revisar_a_mano = []
    for t in dudosas_diag:
        c = _tarjeta(t, dudosas[t]["metricas"])
        if not (_pasa_weinstein(c) and _pasa_fuerza(c)):
            continue
        c["faltan"] = [NOMBRE_CAMPO.get(x, x) for x in dudosas[t]["faltan"]]
        revisar_a_mano.append(c)
    revisar_a_mano.sort(key=lambda c: -(c["score"] or 0))

    # Cuales activos SIN_FUNDAMENTALES (Bitcoin) se revisaron de verdad esta
    # corrida -- para la nota de abajo. Si BTC no alcanzo a bajarse por el
    # presupuesto de tiempo, no tiene sentido explicar un filtro que no se
    # le aplico a nadie.
    sin_fund_revisados = sorted(set(sin_fundamentales or ()) & set(metricas.keys()))

    # LA PRIMERA ALERTA: lo unico que hay que leer si vas apurado.
    alerta = {
        "cuantas": len(finalistas),
        "tickers": [c["ticker"] for c in finalistas],
        "revisarAMano": len(revisar_a_mano),
        "sectoresFuertes": [x["nombre"] for x in sectores[:3]],
        "industriasFuertes": [x["nombre"] for x in industrias[:3]],
        "cobertura": round(len(metricas) / max(1, len(universo)) * 100),
        "frase": (
            f"{len(finalistas)} candidata(s) sobre {len(metricas)} acciones revisadas"
            + (f", más {len(revisar_a_mano)} para mirar a mano" if revisar_a_mano else "")
            + f". Los sectores que tiran: {', '.join(x['nombre'] for x in sectores[:3])}."
        ),
    }

    _set("Listo.", 100)
    return {
        "alerta": alerta,
        "revisarAMano": revisar_a_mano,
        "generado": datetime.now(timezone.utc).isoformat(),
        "duracionSeg": int(time.time() - t0),
        "umbrales": umbrales,
        "sectores": sectores,
        "industrias": industrias,
        "embudo": {
            "pasos": pasos,
            "conHistorial": len(metricas),
            "universoTotal": len(universo),
            "revisadas": len(metricas),
            "sinRevisarPorTiempo": len(saltadas),
            "seCortoPorTiempo": bool(saltadas),
            "cobertura": round(len(metricas) / max(1, len(universo)) * 100, 1),
            "veniaEnCache": len(en_cache),
            # De donde salieron precio/medias/volumen. Ver metricas_por_lote:
            # `porLote` es el camino rapido, `porSerie` el respaldo de siempre.
            "metricasPorLote": sum(1 for m in metricas.values()
                                   if (m or {}).get("fuente") == "lote"),
            "metricasPorSerie": sum(1 for m in metricas.values()
                                    if (m or {}).get("fuente") == "serie"),
            "diagLote": diag_lote,
            "comparacionLote": comparacion,
            "sinDatoFundamental": sin_dato,
            "tickers": tras_embudo,
            # El nucleo = S&P 500 + Nasdaq-100 + la grilla. Ver el comentario
            # de nucleo_revisado: sin esto no se puede saber si un "0
            # candidatas" es un resultado o un barrido incompleto.
            "nucleoTotal": len(nucleo_set) or None,
            "nucleoRevisado": nucleo_revisado,
            "nucleoCompleto": nucleo_completo if nucleo_set else None,
            # Los que faltaron POR TIEMPO (se arregla volviendo a ejecutar) y
            # los que faltaron porque NO HAY DATO (no se arregla nunca). Ver
            # el comentario largo junto a nucleo_saltado.
            "nucleoSaltadoPorTiempo": len(nucleo_saltado) if nucleo_set else None,
            "nucleoSinDatos": nucleo_sin_datos if nucleo_set else None,
            # De donde arranco hoy el resto del mercado, y cada cuanto da la
            # vuelta completa. Ver _rotar_por_dia en universo_mercado.py.
            "rotacionMercado": rotacion,
            # Que activos de esta corrida saltaron capitalizacion/crecimiento
            # porque no aplican (Bitcoin) -- ver SIN_FUNDAMENTALES en main.py.
            "sinFundamentales": sin_fund_revisados,
        },
        # De donde salieron (o no salieron) capitalizacion y crecimiento.
        # Esto es lo que convierte "0 candidatas" en una respuesta que se
        # puede auditar: dice si el mercado no dio nada o si Yahoo no
        # contesto.
        "fuentesFundamentales": diag_fund,
        # Las metricas crudas de cada accion revisada. Es lo que deja mover
        # los umbrales en la app sin volver a correr el analisis.
        "acciones": acciones,
        "alcance": {
            "conFundamentales": sorted(vivos_tendencia.keys()),
            "diagnosticadas": sorted(diags.keys()),
            "sinDiagnosticar": sorted(sin_diagnosticar),
            "topeDiag": TOPE_DIAG,
            "nota": "Los fundamentales solo se pidieron para las que pasaron "
                    "precio/volumen/tendencia, y la fase de Weinstein solo "
                    "para las mas liquidas de esas. Aflojar un filtro mas "
                    "alla de ese conjunto necesita volver a ejecutar.",
        },
        "candidatas": candidatas,
        "caidas": caidas,
        "trasWeinstein": [c["ticker"] for c in tras_weinstein],
        "trasFuerza": [c["ticker"] for c in tras_fuerza],
        "finalistas": finalistas,
        "descartadasPorIndustria": descartadas,
        "dudosas": {t: [NOMBRE_CAMPO.get(x, x) for x in d["faltan"]]
                    for t, d in dudosas.items()},
        "reparto": por_sector,
        "notas": ([
            # Va PRIMERA cuando pasa, porque cambia el significado de todo lo
            # demas: sin capitalizacion, "0 candidatas" no quiere decir que no
            # hubiera ninguna, quiere decir que el embudo no pudo terminar.
            f"ATENCIÓN: ninguna de las {diag_fund['pedidos']} acciones que llegaron "
            f"al filtro de capitalización trajo ese dato desde Yahoo, así que el "
            f"embudo se cortó ahí y el resultado NO es comparable con TradingView. "
            f"No es que no hubiera candidatas: es que no se pudieron evaluar."
        ] if diag_fund.get("pedidos") and diag_fund.get("conCapitalizacion") == 0 else []) + ([
            # Va antes que el aviso general de cobertura porque es mas grave:
            # con el nucleo a medias, el resultado no es comparable ni con la
            # corrida de ayer ni con TradingView.
            f"ATENCIÓN: el barrido se cortó ANTES de terminar el núcleo del "
            f"universo (S&P 500 + Nasdaq-100 + tu grilla): quedaron "
            f"{len(nucleo_saltado)} de {len(nucleo_set)} sin alcanzar a "
            f"revisarse por falta de tiempo. Las candidatas de esta corrida "
            f"salen solo de esa parte, así que un número bajo acá NO quiere "
            f"decir que el mercado no tenga nada. Vuelve a ejecutar: lo ya "
            f"bajado queda en caché 12 h y la próxima corrida sigue desde "
            f"donde quedó."
        ] if nucleo_set and not nucleo_completo else []) + ([
            # Informativa, NO ATENCIÓN: volver a ejecutar no cambia nada acá.
            # Se nombran los símbolos porque la acción que corresponde es
            # editar la lista en main.py, y para eso hay que saber cuáles son.
            f"{len(nucleo_sin_datos)} símbolo(s) del núcleo se pidieron pero "
            f"Yahoo no devolvió historial usable: "
            f"{', '.join(nucleo_sin_datos[:12])}"
            f"{'…' if len(nucleo_sin_datos) > 12 else ''}. Suele ser un papel "
            f"retirado, renombrado o fusionado, o uno con menos de 200 sesiones "
            f"(no alcanza para la SMA 200). Volver a ejecutar no los recupera: "
            f"si alguno ya no existe, sácalo de la lista en main.py y dejas de "
            f"gastar una petición en él cada corrida."
        ] if nucleo_sin_datos else []) + ([
            f"Este análisis decidió sobre {len(metricas)} de las {len(universo)} "
            f"del universo ({round(len(metricas)/max(1,len(universo))*100)} %). "
            f"Quedaron {len(saltadas)} sin revisar porque se acabó el presupuesto "
            f"de tiempo: Yahoo estrangula las peticiones cuando le llegan cientos "
            f"seguidas. Vuelve a correrlo y sigue desde donde quedó — lo ya bajado "
            f"queda en caché 12 h."
        ] if saltadas else []) + ([
            # Informativa, no ATENCIÓN: esto es esperado y normal, no un
            # problema. Sin esto, "por qué salieron acciones distintas hoy
            # que ayer, si aprete lo mismo" no tiene respuesta a mano.
            f"El resto del mercado (fuera del núcleo) se cubre de a poco: hoy "
            f"le tocó desde el símbolo {rotacion['offsetHoy']} de "
            f"{rotacion['totalResto']} de esa lista, y mañana empieza en otro "
            f"punto — con el paso de hoy, una vuelta completa toma "
            f"~{rotacion['periodoDias']} días. Por eso qué candidatas "
            f"aparecen fuera del núcleo puede cambiar día a día aunque "
            f"nada más cambie."
        ] if rotacion and rotacion.get("totalResto") else []) + ([
            # Tambien informativa: explica por que estos tickers pueden
            # aparecer sin capitalizacion/crecimiento y aun asi ser
            # candidatas, sin que parezca un dato faltante de Yahoo.
            f"{', '.join(sin_fund_revisados)} no tiene capitalización bursátil ni "
            f"crecimiento de utilidades o ingresos como una empresa, así que esos "
            f"tres filtros no se le aplicaron — sí se le calculó precio, volumen, "
            f"tendencia, fase de Weinstein y fuerza relativa, igual que al resto."
        ] if sin_fund_revisados else []) + [
            (f"El crecimiento se midió en TTM (últimos 12 meses contra los 12 "
             f"anteriores, igual que TradingView) en {diag_fund.get('conCrecimientoTTM', 0)} "
             f"acciones, y en trimestral YoY en "
             f"{diag_fund.get('conCrecimientoTrimestral', 0)} donde Yahoo no entregó "
             f"los ocho trimestres. Cada acción dice cuál se le aplicó."),
            "La capitalización se evalúa junto al crecimiento y no antes de la "
            "tendencia: los tres salen de la misma consulta, y pedirla para las "
            f"{len(metricas)} costaría cientos de peticiones desperdiciadas.",
            "Una acción sin dato no pasa el filtro, pero TAMPOCO desaparece: sale "
            "en 'revisarAMano' si además está técnicamente sana, con el detalle de "
            "qué dato faltó. Para una primera alerta, esconder un nombre es peor "
            "que mostrarlo con una advertencia.",
        ],
    }


def iniciar(universo, serie_5y, indice_5y, umbrales=None, nucleo=None, rotacion=None,
            sin_fundamentales=None):
    """
    Arranca el analisis en un hilo. Devuelve (arrancó, motivo).

    Si ya hay uno corriendo NO encola otro: devuelve (False, "ya_corriendo").
    Dos corridas simultaneas serian 1.000 peticiones a Yahoo en paralelo y un
    429 garantizado.
    """
    with _LOCK:
        if _ESTADO["estado"] == "corriendo":
            return False, "ya_corriendo"
        _ESTADO.update({"estado": "corriendo", "etapa": "Arrancando…", "progreso": 0,
                        "iniciado": datetime.now(timezone.utc).isoformat(),
                        "terminado": None, "error": None})

    umb = dict(UMBRALES)
    if umbrales:
        for k in umb:
            v = umbrales.get(k)
            if isinstance(v, (int, float)) and v >= 0:
                umb[k] = float(v)

    def _correr():
        try:
            datos = _analizar(universo, serie_5y, indice_5y, umb, nucleo, rotacion,
                              sin_fundamentales)
            _RESULTADO["datos"] = datos
            _RESULTADO["ts"] = time.time()
            with _LOCK:
                _ESTADO.update({"estado": "listo",
                                "terminado": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            print(f"[explorar] FALLO: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            with _LOCK:
                _ESTADO.update({"estado": "error", "error": f"{type(e).__name__}: {e}",
                                "terminado": datetime.now(timezone.utc).isoformat()})

    threading.Thread(target=_correr, daemon=True, name="explorar").start()
    return True, "arrancado"
