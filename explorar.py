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

ETFS_SECTOR = [
    ("Tecnología", "XLK"), ("Salud", "XLV"), ("Financiero", "XLF"),
    ("Consumo discrecional", "XLY"), ("Consumo básico", "XLP"),
    ("Energía", "XLE"), ("Industrial", "XLI"), ("Materiales", "XLB"),
    ("Utilities", "XLU"), ("Inmobiliario", "XLRE"), ("Comunicaciones", "XLC"),
]

ETFS_INDUSTRIA = [
    ("Semiconductores", "SMH"), ("Software", "IGV"), ("Ciberseguridad", "CIBR"),
    ("Biotecnología", "XBI"), ("Aeroespacial y defensa", "ITA"),
    ("Banca regional", "KRE"), ("Retail", "XRT"), ("Petróleo y gas E&P", "XOP"),
    ("Oro y mineras", "GDX"), ("Transporte", "IYT"),
    ("Infraestructura", "PAVE"), ("Nuclear / uranio", "URA"),
]

# Umbrales del embudo. Son los del documento; el frontend los puede mandar
# distintos para explorar, pero estos son el default.
UMBRALES = {
    "precio": 10.0,
    "capB": 2.0,
    "volM": 2.0,
    "crecimiento": 25.0,
}

TTL_RESULTADO = 24 * 3600
_WORKERS = 4          # ver el comentario de arriba: 4 y no 8, a proposito


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


def _fundamentales_uno(ticker):
    """
    Capitalizacion, crecimiento y sector/industria en UNA peticion.

    Devuelve None si Yahoo no contesta o no trae lo minimo. Nunca inventa:
    un campo que no viene queda en None y el embudo lo trata como
    "no se puede evaluar" (que NO es lo mismo que "no pasa" -- ver embudo()).
    """
    try:
        resp = requests.get(
            _QS.format(symbol=ticker),
            params={"modules": _MODULOS},
            headers=data_source._HEADERS,
            timeout=data_source._TIMEOUT,
        )
        if resp.status_code != 200:
            return ticker, None
        res = ((resp.json().get("quoteSummary") or {}).get("result") or [])
        if not res:
            return ticker, None
        r = res[0]
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
        }
    except Exception as e:
        print(f"[explorar] {ticker}: fallo quoteSummary -- {type(e).__name__}: {e}")
        return ticker, None


def fundamentales(tickers):
    return {t: d for t, d in _paralelo(_fundamentales_uno, tickers) if d}


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


def embudo(metricas, umbrales):
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
    pasos.append({"filtro": "Universo de partida", "detalle": "S&P 500 + Nasdaq-100 + tu grilla",
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


def embudo_fundamental(vivos, fund, umbrales):
    """Segunda mitad del embudo: los tres filtros que necesitan Yahoo."""
    pasos = []
    sin_dato = {"capB": 0, "crecBpa": 0, "crecVentas": 0}

    def filtrar(campo, minimo, etiqueta, detalle):
        nonlocal vivos
        nuevos = {}
        for t, m in vivos.items():
            v = (fund.get(t) or {}).get(campo)
            if not isinstance(v, (int, float)):
                sin_dato[campo] += 1
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

    return pasos, vivos, sin_dato


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


def _analizar(universo, serie_5y, indice_5y, umbrales):
    """
    El pipeline completo. `serie_5y(ticker)` y `indice_5y()` los inyecta
    server.py para reusar SU cache de 24h -- este modulo no la duplica.
    """
    t0 = time.time()

    # ---- Paso 1 · sectores e industrias -----------------------------------
    _set("Bajando el S&P 500 de referencia…", 3)
    bench = _cierres(data_source.get_price_history(BENCHMARK, "1y", suffix="") or [])
    if len(bench) < 260:
        # 252 dias habiles en un año; Yahoo a veces devuelve algunos menos.
        # Si faltan muchos, la ventana de 12 meses no se puede calcular y se
        # dice, en vez de mostrar un numero corto disfrazado de anual.
        print(f"[explorar] AVISO: el benchmark trajo {len(bench)} cierres, "
              f"la ventana de 12 meses puede quedar incompleta.")

    def _etf(par):
        nombre, sim = par
        c = _cierres(data_source.get_price_history(sim, "1y", suffix="") or [])
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

    # ---- Paso 2a · filtros que salen de la serie de precios ---------------
    _set(f"Bajando {len(universo)} historiales de un año… (esto es lo que más demora)", 18)
    def _serie_uno(t):
        return t, _metricas_de_serie(data_source.get_price_history(t, "1y", suffix="") or [])
    metricas = {t: m for t, m in _paralelo(_serie_uno, universo) if m}
    _set(f"{len(metricas)} con historial suficiente. Aplicando filtros de precio y tendencia…", 55)
    pasos_a, vivos = embudo(metricas, umbrales)

    # ---- Paso 2b · capitalizacion y crecimiento ---------------------------
    _set(f"Pidiendo fundamentales de {len(vivos)} candidatas…", 60)
    fund = fundamentales(sorted(vivos.keys()))
    pasos_b, vivos, sin_dato = embudo_fundamental(vivos, fund, umbrales)
    pasos = pasos_a + pasos_b
    tras_embudo = sorted(vivos.keys())
    _set(f"{len(tras_embudo)} pasaron el embudo. Ahora Weinstein…", 72)

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

    diags = {t: d for t, d in _paralelo(_diag, tras_embudo) if d}
    _set("Cruzando fase, score y fuerza relativa…", 90)

    candidatas = []
    for t in tras_embudo:
        d = diags.get(t) or {}
        sem = d.get("semanal") or {}
        dia = d.get("diario") or {}
        f = (fund.get(t) or {})
        # scoreConfirmaciones lo calcula el propio indicador; contarlas de
        # nuevo aca seria duplicar su logica y arriesgarse a que las dos
        # cuentas se separen en el futuro.
        n_conf = sem.get("scoreConfirmaciones") if sem.get("disponible") else None
        candidatas.append({
            "ticker": t,
            "nombre": f.get("nombre") or t,
            "sector": f.get("sector"),
            "industria": f.get("industria"),
            "precio": round(vivos[t]["precio"], 2),
            "sma50": round(vivos[t]["sma50"], 2) if vivos[t].get("sma50") else None,
            "capB": f.get("capB"),
            "crecBpa": f.get("crecBpa"),
            "crecVentas": f.get("crecVentas"),
            "volM": vivos[t].get("volM"),
            "fase": sem.get("fase") if sem.get("disponible") else None,
            "confirmaciones": n_conf,
            "veredictoSemanal": sem.get("veredicto"),
            "score": dia.get("score") if dia.get("disponible") else None,
            "caso": dia.get("caso"),
            "fuerzaRelativa": dia.get("fuerzaRelativa") or dia.get("fuerza_relativa"),
        })

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

    _set("Listo.", 100)
    return {
        "generado": datetime.now(timezone.utc).isoformat(),
        "duracionSeg": int(time.time() - t0),
        "umbrales": umbrales,
        "sectores": sectores,
        "industrias": industrias,
        "embudo": {
            "pasos": pasos,
            "conHistorial": len(metricas),
            "sinDatoFundamental": sin_dato,
            "tickers": tras_embudo,
        },
        "candidatas": candidatas,
        "caidas": caidas,
        "trasWeinstein": [c["ticker"] for c in tras_weinstein],
        "trasFuerza": [c["ticker"] for c in tras_fuerza],
        "finalistas": finalistas,
        "descartadasPorIndustria": descartadas,
        "reparto": por_sector,
        "notas": [
            "El crecimiento de BPA e ingresos es TRIMESTRAL contra el mismo trimestre "
            "del año anterior (lo que entrega Yahoo), no TTM como en TradingView. "
            "Los números no calzan exactamente; el filtro sí cumple su función.",
            "La capitalización se evalúa junto al crecimiento y no antes de la "
            "tendencia: los tres salen de la misma consulta, y pedirla para las "
            f"{len(metricas)} costaría cientos de peticiones desperdiciadas.",
            "Una acción sin dato no pasa el filtro. 'No se sabe' se cuenta aparte "
            "de 'no cumple', en sinDatoFundamental.",
        ],
    }


def iniciar(universo, serie_5y, indice_5y, umbrales=None):
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
            datos = _analizar(universo, serie_5y, indice_5y, umb)
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
