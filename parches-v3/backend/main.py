"""
Punto de entrada: revisa todas las acciones y dispara alertas (correo +
push) apenas alguna cruza bajo su promedio histórico de 90 días.

Uso:
    python main.py            -> corre UN ciclo y termina (para GitHub Actions)
    python main.py --loop     -> corre en bucle infinito (para probar en tu compu)
"""
import os
import sys
import time

from data_source import get_quotes
from alerts_engine import update_and_check, get_price_series
from notify import send_email_alert, send_push_alert
import indicators
import news

TICKERS = [
    "AGUAS-A", "ANDINA-B", "BCI", "BSANTANDER", "CAP", "CCU",
    "CENCOSUD", "CHILE", "CMPC", "COLBUN", "CONCHATORO",
    "COPEC", "ECL", "ENELAM", "ENELCHILE", "ENTEL", "FALABELLA", "IAM",
    "LTM", "MALLPLAZA", "PARAUCO", "RIPLEY",
    "SMU", "SONDA", "SQM-B", "VAPORES",
    # -- Las siguientes son del IGPA (indice mas amplio), no del IPSA --
    "ANTARCHILE", "QUINENCO", "HABITAT", "CUPRUM",
    "PROVIDA", "PLANVITAL", "SK", "CAMANCHACA",
    "ALMENDRAL", "ENELGXCH", "WATTS", "CRISTALES", "BESALCO",
    "PUCOBRE", "LIPIGAS", "BLUMAR", "ORO-BLANCO", "AAISA",
    "ENJOY", "INDISA", "SOCOVESA",
    # Removidas (no existen en Yahoo Finance con .SN, confirmado en produccion):
    # CENCOSHOPP, ITAUCORP, SECURITY, BICECORP, SAAM, EMBONOR
]

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 300))


def run_once():
    quotes = get_quotes(TICKERS)
    for ticker, q in quotes.items():
        price = q["price"]
        avg, is_below, crossed_now = update_and_check(ticker, price)

        if crossed_now:
            pct_below = (1 - price / avg) * 100
            serie = get_price_series(ticker)
            indic = indicators.summarize(serie)
            indic_texto = indicators.describe(indic)

            noticias = news.get_recent_news(ticker)
            noticias_texto = news.describe(noticias)

            print(f"[ALERTA] {ticker} cruzó bajo el promedio: {price} vs {avg:.0f} ({pct_below:.1f}%) · {indic_texto}")
            if noticias:
                print(f"  {noticias_texto}")

            mensaje_extra = indic_texto + "\n\n" + noticias_texto
            send_email_alert(ticker, price, avg, pct_below, mensaje_extra)
            # Al push (celular) solo mandamos los indicadores -- las notificaciones
            # push tienen poco espacio, y si hay noticia, el correo trae el detalle.
            send_push_alert(ticker, price, avg, pct_below, indic_texto)
        elif is_below:
            print(f"[info] {ticker} sigue bajo el promedio, no se reenvía alerta.")


def loop_forever():
    print(f"Iniciando monitoreo IPSA en bucle · cada {POLL_INTERVAL_SECONDS}s · {len(TICKERS)} acciones")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[main] Error en ciclo de monitoreo: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop_forever()
    else:
        print(f"Ejecutando un ciclo único · {len(TICKERS)} acciones")
        run_once()
