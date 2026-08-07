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
    # Agregadas en v3 (bloque 9 de la spec) -- verificadas una por una contra
    # Yahoo Finance antes de sumarlas (busqueda web, no se pudo hacer curl
    # directo desde este sandbox por el mismo bloqueo de red que afecta a
    # data_source.py; ver comentario ahi):
    #   ITAUCL.SN     -> Banco Itaú Chile (el simbolo viejo ITAUCORP.SN fue
    #                     renombrado a ITAUCL.SN en 2023, por eso el intento
    #                     anterior con "ITAUCORP" fallaba)
    #   CENCOSHOPP.SN -> Cencosud Shopping S.A. (existe, separado de
    #                     CENCOMALLS.SN que es el mismo emisor con otro simbolo)
    #   ILC.SN        -> Inversiones La Construcción S.A.
    #   SALFACORP.SN  -> SalfaCorp S.A.
    #   SMSAAM.SN     -> Sociedad Matriz SAAM S.A. (reintentado como SMSAAM,
    #                     el simbolo viejo "SAAM" no existia)
    "ITAUCL", "CENCOSHOPP", "ILC", "SALFACORP", "SMSAAM",
    # Removidas (no existen en Yahoo Finance con .SN, confirmado en produccion):
    # ITAUCORP, SECURITY, BICECORP, SAAM, EMBONOR
]

# Ambiente 2 (EE.UU. · USD) -- especificacion v3, seccion 2.3. Nucleo de
# ETFs sugerido; ajustar segun preferencia real del usuario. Estos NO
# llevan sufijo .SN al pedirlos a Yahoo (ver data_source.get_market_data
# con suffix="").
TICKERS_USA = [
    "VOO", "VTI", "VT", "VXUS", "QQQM", "SCHD", "BND",
]
