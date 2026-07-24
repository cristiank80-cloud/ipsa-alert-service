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

            def _get(key_camel, key_snake):
                if hasattr(info, "get"):
                    v = info.get(key_camel)
                    if v is not None:
                        return v
                return getattr(info, key_snake, None)

            price = _get("lastPrice", "last_price")
            if price is None:
                print(f"[data_source] {t} ({ysym}): sin datos disponibles en Yahoo Finance (posiblemente el símbolo no existe o está deslistado)")
                continue

            quotes[t] = {
                "price": float(price),
                "timestamp": datetime.now().isoformat(),
                "dayHigh": _get("dayHigh", "day_high"),
                "dayLow": _get("dayLow", "day_low"),
                "volume": _get("lastVolume", "last_volume"),
            }
        except Exception as e:
            print(f"[data_source] {t} ({ysym}): sin datos disponibles en Yahoo Finance -- {type(e).__name__}: {e}")

    return quotes


def get_returns(tickers):
    """
    Rentabilidad REAL de 3 meses y 1 año, calculada desde el cierre de
    Yahoo Finance de hace ~63 y ~252 dias habiles atras vs. el cierre
    mas reciente disponible en el historial (aprox. 1 año de datos).
    """
    rets = {}
    for t in tickers:
        try:
            hist = yf.Ticker(t + SUFFIX).history(period="1y")
            closes = hist["Close"].dropna()
            if len(closes) < 5:
                continue
            last = float(closes.iloc[-1])
            idx_3m = max(0, len(closes) - 63)
            ret_3m = (last / float(closes.iloc[idx_3m])) - 1
            ret_1y = (last / float(closes.iloc[0])) - 1
            rets[t] = {"ret_3m": ret_3m, "ret_1y": ret_1y}
        except Exception as e:
            print(f"[data_source] Error obteniendo rentabilidad de {t}: {e}")
    return rets


def get_index_quote():
    """
    Valor REAL del índice IPSA (no una acción individual), directo de
    Yahoo Finance. El ticker del índice es '^IPSA' (con acento circunflejo,
    no lleva sufijo .SN como las acciones individuales).
    """
    try:
        info = yf.Ticker("^IPSA").fast_info
        price = info.get("lastPrice") if hasattr(info, "get") else getattr(info, "last_price", None)
        prev_close = info.get("previousClose") if hasattr(info, "get") else getattr(info, "previous_close", None)
        if price is None:
            return None
        return {
            "value": float(price),
            "previousClose": float(prev_close) if prev_close else None,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"[data_source] Error obteniendo el índice IPSA: {e}")
        return None


def get_price_history(ticker, period="3mo"):
    """
    Serie de precios de cierre y volumen reales para un ticker, para el
    selector de rango de período del gráfico (1D, 5D, 1M, 3M, 6M, YTD,
    1A, 5Y) y para el subgráfico de volumen.
    'period' usa la sintaxis de yfinance: 1d,5d,1mo,3mo,6mo,ytd,1y,5y.
    """
    try:
        hist = yf.Ticker(ticker + SUFFIX).history(period=period)
        hist = hist.dropna(subset=["Close"])
        if hist.empty:
            return []
        puntos = []
        for idx, row in hist.iterrows():
            vol = row.get("Volume")
            puntos.append({
                "date": idx.strftime("%Y-%m-%d"),
                "close": float(row["Close"]),
                "volume": (float(vol) if vol == vol and vol is not None else None),  # vol==vol descarta NaN
            })
        return puntos
    except Exception as e:
        print(f"[data_source] Error obteniendo historial de {ticker} ({period}): {e}")
        return []


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
