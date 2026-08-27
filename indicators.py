"""
Indicadores técnicos clásicos, calculados sobre el historial de precios
diarios que ya guarda alerts_engine.py (price_history.json).

IMPORTANTE — esto no es un modelo de predicción: no proyecta precios
futuros. Son resúmenes estadísticos del comportamiento reciente
(tendencia, momentum) que se usan como contexto adicional junto a la
alerta de "bajo el promedio". Interpretación estándar de RSI (no es
garantía de nada): <30 sugiere sobreventa, >70 sugiere sobrecompra.
"""

# Numeros en formato chileno (miles con punto, decimales con coma)
# para TODO lo que lee una persona -- ver el docstring de formato.py.
import formato


def sma(prices, window):
    """Media móvil simple de los últimos 'window' precios, o None si
    no hay suficiente historial todavía."""
    if len(prices) < window:
        return None
    return sum(prices[-window:]) / window


def rsi(prices, period=14):
    """
    Relative Strength Index (método de Wilder), 0-100.
    Devuelve None si no hay suficiente historial (se necesitan al
    menos period+1 precios).
    """
    if len(prices) < period + 1:
        return None

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def summarize(prices):
    """
    Calcula todos los indicadores disponibles según cuánto historial
    haya acumulado el servicio. Los que aún no alcanzan a calcularse
    (poco historial) quedan en None -- eso es normal los primeros días.
    """
    return {
        "sma20": sma(prices, 20),
        "sma50": sma(prices, 50),
        "rsi14": rsi(prices, 14),
    }


def describe(indicators):
    """Convierte el dict de indicadores en una línea de texto legible
    para incluir en las alertas de correo/push."""
    partes = []
    if indicators.get("rsi14") is not None:
        rsi_val = indicators["rsi14"]
        lectura = ""
        if rsi_val < 30:
            lectura = " (sobrevendido)"
        elif rsi_val > 70:
            lectura = " (sobrecomprado)"
        partes.append(f"RSI(14): {rsi_val}{lectura}")
    if indicators.get("sma20") is not None:
        partes.append(f"SMA(20): {formato.num(indicators['sma20'], 0)}")
    if indicators.get("sma50") is not None:
        partes.append(f"SMA(50): {formato.num(indicators['sma50'], 0)}")
    if not partes:
        return "Aún no hay suficiente historial para calcular indicadores técnicos."
    return " · ".join(partes)
