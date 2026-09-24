"""
El embudo de Explorar en UNA peticion: el screener de TradingView.

POR QUE EXISTE (24-sep-2026)
=============================
Yahoo le niega el "crumb" a la IP de Render (429 en getcrumb) y sin crumb no
hay capitalizacion ni sector; el precio y las medias se bajaban de a un
historial por accion, con tope de tiempo, y no alcanzaban las 536 (quedaban
~400). Cristian pidio usar otras fuentes para que la app ande mas rapido.

El screener de TradingView responde en una sola peticion (~1 s, medido desde
un navegador) con TODAS las acciones de NYSE / Nasdaq / AMEX y exactamente
los campos de los 7 filtros de la Clase 3: precio, capitalizacion, volumen
medio de 90 dias, SMA 50, SMA 200, crecimiento del BPA diluido TTM y de las
ventas TTM, mas sector e industria. Son los mismos numeros que Cristian ve en
su screener, asi que por primera vez la app puede calzar con TradingView.

LO QUE HAY QUE SABER ANTES DE CONFIAR EN ESTO
=============================================
* NO es una API oficial ni documentada. Es el mismo servicio que usa la
  pagina del screener por dentro. TradingView puede cambiarlo o cortarlo sin
  aviso, y sus condiciones no autorizan el uso automatizado. Cristian lo
  decidio sabiendolo.
* Por eso NADA depende solo de esto: si falla (error, formato distinto, cero
  filas), `escanear()` devuelve (None, motivo) y explorar.py sigue por el
  camino de siempre (Yahoo + Nasdaq).
* Se pide como mucho una vez por corrida de Explorar (Cristian la corre a
  mano). No meter esto en ningun ciclo automatico.

EL PISO
========
Las 6.000 acciones completas serian ~1,3 MB y la app tiene sliders que se
mueven en el telefono sin volver a correr nada. Se piden solo las que pasan
un piso bastante mas bajo que los umbrales del metodo (precio >= 5, cap >=
0,5 B, volumen >= 0,3 M): ~2.400 filas. Mover un slider por debajo del piso
no trae acciones nuevas; la app lo sabe por `piso` en el diagnostico.
"""
import time

import requests

_URL = "https://scanner.tradingview.com/america/scan"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
_TIMEOUT = 30

PISO = {"precio": 5.0, "capB": 0.5, "volM": 0.3}

_COLUMNAS = ["name", "close", "market_cap_basic", "average_volume_90d_calc",
             "SMA50", "SMA200", "earnings_per_share_diluted_yoy_growth_ttm",
             "total_revenue_yoy_growth_ttm", "sector", "industry", "description"]

# Taxonomia de TradingView (FactSet) -> nombres de sector de Yahoo, que son los
# que el frontend sabe cruzar con los 11 ETF de sector. NO es 1 a 1: FactSet
# no separa "Comunicaciones" como GICS (Google y Meta caen en Technology
# Services) y pone los REIT dentro de Finance. Es una aproximacion, y cada
# ficha lo dice con fuenteSector = "tradingview".
SECTOR_A_YAHOO = {
    "Electronic Technology": "Technology",
    "Technology Services": "Technology",
    "Health Technology": "Healthcare",
    "Health Services": "Healthcare",
    "Finance": "Financial Services",
    "Energy Minerals": "Energy",
    "Utilities": "Utilities",
    "Retail Trade": "Consumer Cyclical",
    "Consumer Durables": "Consumer Cyclical",
    "Consumer Services": "Consumer Cyclical",
    "Consumer Non-Durables": "Consumer Defensive",
    "Producer Manufacturing": "Industrials",
    "Industrial Services": "Industrials",
    "Transportation": "Industrials",
    "Commercial Services": "Industrials",
    "Distribution Services": "Industrials",
    "Non-Energy Minerals": "Basic Materials",
    "Process Industries": "Basic Materials",
    "Communications": "Communication Services",
}
_INDUSTRIA_REAL_ESTATE = {"Real Estate Investment Trusts", "Real Estate Development"}


def _a_yahoo(simbolo_tv):
    """BRK.B (TradingView) -> BRK-B (Yahoo, que es como vive en toda la app)."""
    return simbolo_tv.strip().upper().replace(".", "-")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pedir(filtros, rango=6000):
    cuerpo = {
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "options": {"lang": "en"},
        "filter": [
            {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
            {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ", "AMEX"]},
        ] + filtros,
        "columns": _COLUMNAS,
        "range": [0, rango],
    }
    r = requests.post(_URL, json=cuerpo, headers=_HEADERS, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {(r.text or '')[:120]}")
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"error del screener: {str(j['error'])[:120]}")
    return j.get("data") or []


def _fila(d):
    """Una fila del screener -> el formato de `metricas` + `fund` de explorar."""
    (nombre, precio, cap, vol, sma50, sma200, crec_bpa, crec_ventas,
     sector, industria, descripcion) = (list(d) + [None] * len(_COLUMNAS))[:len(_COLUMNAS)]
    sector_y = SECTOR_A_YAHOO.get(sector or "")
    if industria in _INDUSTRIA_REAL_ESTATE:
        sector_y = "Real Estate"
    return {
        "precio": _num(precio),
        "sma50": _num(sma50),
        "sma200": _num(sma200),
        "volM": round(_num(vol) / 1e6, 2) if _num(vol) else None,
        "capB": round(_num(cap) / 1e9, 2) if _num(cap) else None,
        "crecBpa": round(_num(crec_bpa), 1) if _num(crec_bpa) is not None else None,
        "crecVentas": round(_num(crec_ventas), 1) if _num(crec_ventas) is not None else None,
        "sector": sector_y,
        "industria": industria or None,
        "nombre": descripcion or nombre,
    }


def escanear(extra=()):
    """
    ({ticker_yahoo: fila}, diagnostico) o (None, diagnostico) si fallo.

    `extra`: tickers que tienen que venir aunque esten bajo el piso (el nucleo
    del universo: S&P 500 + Nasdaq-100 + grilla), para que el control de
    cobertura no los cuente como "sin datos".
    """
    t0 = time.time()
    diag = {"fuente": "tradingview", "piso": dict(PISO)}
    try:
        filas = _pedir([
            {"left": "close", "operation": "egreater", "right": PISO["precio"]},
            {"left": "market_cap_basic", "operation": "egreater", "right": PISO["capB"] * 1e9},
            {"left": "average_volume_90d_calc", "operation": "egreater", "right": PISO["volM"] * 1e6},
        ])
        datos = {}
        for x in filas:
            d = x.get("d") or []
            if not d or not d[0]:
                continue
            datos.setdefault(_a_yahoo(d[0]), _fila(d))
        diag["sobreElPiso"] = len(datos)

        # Los del nucleo que quedaron bajo el piso: se piden por nombre.
        faltan = sorted({t.replace("-", ".") for t in extra if t not in datos})
        if faltan:
            for x in _pedir([{"left": "name", "operation": "in_range", "right": faltan}],
                            rango=len(faltan) + 50):
                d = x.get("d") or []
                if d and d[0]:
                    datos.setdefault(_a_yahoo(d[0]), _fila(d))
        diag["delNucleoBajoElPiso"] = len(faltan)
    except Exception as e:
        diag.update(ok=False, motivo=f"{type(e).__name__}: {e}",
                    segundos=round(time.time() - t0, 1))
        print(f"[tradingview] El screener no respondio: {diag['motivo']}. "
              f"Explorar sigue con Yahoo.")
        return None, diag

    # Si vino, pero casi vacio, algo cambio en el formato: mejor no usarlo que
    # filtrar el mercado entero contra columnas en blanco.
    con_medias = sum(1 for v in datos.values() if v["sma50"] and v["sma200"] and v["precio"])
    if len(datos) < 500 or con_medias < len(datos) * 0.8:
        diag.update(ok=False, motivo=(f"respuesta sospechosa: {len(datos)} filas, "
                                      f"{con_medias} con precio y medias"),
                    segundos=round(time.time() - t0, 1))
        print(f"[tradingview] {diag['motivo']}. Explorar sigue con Yahoo.")
        return None, diag

    diag.update(ok=True, motivo="ok", filas=len(datos),
                conCrecimiento=sum(1 for v in datos.values() if v["crecBpa"] is not None),
                segundos=round(time.time() - t0, 1))
    return datos, diag
