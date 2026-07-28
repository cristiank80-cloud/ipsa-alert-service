"""
Lista compartida de tickers.

ANTES este archivo tambien era un punto de entrada de linea de comandos
(`python main.py --loop`) que corria su propio ciclo de alertas por fuera
del servidor Flask. Ese camino quedo roto por el rediseño de v3 -- llamaba
a data_source.get_quotes() con el formato viejo, a alerts_engine.py (logica
de umbral -4% que ya no se usa) y a notify.send_email_alert()/
send_push_alert() con firmas de 4 argumentos que signals.py y notify.py ya
no tienen (ver notify.send_alert(), bidireccional).

Como server.py SOLO necesita `from main import TICKERS` para armar la
grilla de la app, y el resto de este archivo nunca se ejecuta bajo
gunicorn (no es `__main__`), se dejo unicamente la lista: mantener el
codigo viejo generaba un riesgo real -- si alguna de esas funciones dejaba
de existir con ese nombre (como paso ahora, send_email_alert -> send_alert),
el `import` de arriba fallaba y tumbaba TODO el servidor al desplegar,
aunque nadie fuera a correr `python main.py --loop` nunca.
"""

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
