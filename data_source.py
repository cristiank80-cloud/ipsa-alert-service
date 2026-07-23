"""
Fuente de datos: Yahoo Finance, via la libreria 'yfinance'. No requiere
API key ni pedir acceso a nadie.

Los tickers chilenos en Yahoo Finance usan el sufijo ".SN" (Bolsa de
Santiago), ej. SQM-B.SN, FALABELLA.SN, BSANTANDER.SN. Confirmado que
existen para las 8 acciones de este proyecto.

HONESTIDAD SOBRE EL REZAGO: Yahoo etiqueta estas cotizaciones como
"Delayed Quote" (cotizacion con rezago). No publican el numero exacto
de minutos. Es gratis e inmediato, pero si en algun momento consigues
la API de la Bolsa de Santiago o de tu corredora con menor rezago,
basta con reemplazar la funcion get_quotes() de este archivo — el resto
del servicio (alerts_engine, notify, main) no necesita cambios.
"""
from datetime import datetime
import yfinance as yf

SUFFIX = ".SN"


def get_quotes(tickers):
    """
    Devuelve {ticker: {"price": float, "timestamp": str}} para cada
    nemotecnico solicitado (sin el sufijo .SN).
    """
    quotes = {}
    yahoo_symbols = [t + SUFFIX for t in tickers]

    try:
        batch = yf.Tickers(" ".join(yahoo_symbols))
    except Exception as e:
        print(f"[data_source] Error creando cliente Yahoo Finance: {e}")
        return quotes

    for t, ysym in zip(tickers, yahoo_symbols):
        try:
            info = batch.tickers[ysym].fast_info
            # Si el ticker no existe o esta deslistado, yfinance no logra
            # construir fast_info correctamente -- probamos leer un campo
            # basico primero para detectar eso con un mensaje claro.
            try:
                _ = info["lastPrice"]
            except (KeyError, Exception):
                pass
            price = info.get("lastPrice") if hasattr(info, "get") else None
            if price is None:
                price = getattr(info, "last_price", None)
            if price is None:
                print(f"[data_source] {t} ({ysym}): sin datos disponibles en Yahoo Finance (posiblemente el símbolo no existe o está deslistado)")
                continue
            quotes[t] = {
                "price": float(price),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            print(f"[data_source] {t} ({ysym}): sin datos disponibles en Yahoo Finance -- {type(e).__name__}: {e}")

    return quotes


def get_daily_avg(tickers, days=90):
    """
    Promedio real de cierre de los ultimos `days` dias, calculado
    directamente desde el historico de Yahoo Finance (no depende de
    que este servicio lleve tiempo corriendo, a diferencia del promedio
    que acumula alerts_engine.py). Mas lento que get_quotes() porque
    pide el historial completo por cada ticker -- por eso server.py lo
    cachea en vez de llamarlo en cada request.
    """
    avgs = {}
    for t in tickers:
        try:
            hist = yf.Ticker(t + SUFFIX).history(period=f"{days}d")
            if not hist.empty:
                avgs[t] = float(hist["Close"].mean())
        except Exception as e:
            print(f"[data_source] Error obteniendo promedio 90d de {t}: {e}")
    return avgs
