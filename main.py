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

# ---------------------------------------------------------------------------
# Ambiente 1 (Chile · CLP)
# ---------------------------------------------------------------------------
# REDUCIDO A 5 NOMBRES (agosto 2026, a pedido de Cristian).
#
# POR QUE
# =======
# Cristian esta migrando su cartera de Chile a EE.UU. Las unicas posiciones
# chilenas que le quedan -- y que esta cerrando, no abriendo -- son estas
# cinco. Barrer los 50 papeles chilenos de antes gastaba ~50 peticiones a
# Yahoo en cada ciclo para mirar acciones que no va a comprar. Ese
# presupuesto de peticiones es exactamente el que necesitaba el Nasdaq-100
# que se agrega mas abajo: el ciclo dura lo mismo que antes, pero mirando
# cosas que si le sirven.
#
# LO QUE ESTO NO HACE
# ===================
# No borra nada. Las 45 que salieron quedan aca abajo comentadas, en el
# mismo orden, y volver a activar cualquiera es descomentar una linea. El
# buscador de la app sigue encontrando cualquier ticker que este en esta
# lista -- si algun dia Cristian quiere mirar SQM-B otra vez, se
# descomenta y listo.
#
# OJO AL DESPLEGAR: si tienes una posicion registrada en "Mis movimientos"
# de una accion que NO este en esta lista, esa posicion deja de mostrarse
# en "Mi Cartera" (la app arma la grilla desde aca). Los movimientos NO se
# pierden -- siguen en el respaldo y en el historial -- pero la fila
# desaparece hasta que el ticker vuelva a la lista. Antes de subir esto,
# revisa que tus posiciones chilenas abiertas sean solo estas cinco.
TICKERS = [
    "COPEC", "FALABELLA", "RIPLEY", "LTM", "SOCOVESA",
    # -- Sacadas del barrido en agosto 2026 (ver comentario de arriba).
    #    Estaban todas verificadas contra Yahoo Finance y funcionaban; se
    #    quitaron por presupuesto de peticiones, no porque fallaran.
    # "AGUAS-A", "ANDINA-B", "BCI", "BSANTANDER", "CAP", "CCU",
    # "CENCOSUD", "CHILE", "CMPC", "COLBUN", "CONCHATORO",
    # "ECL", "ENELAM", "ENELCHILE", "ENTEL", "IAM",
    # "MALLPLAZA", "PARAUCO",
    # "SMU", "SONDA", "SQM-B", "VAPORES",
    # -- Las siguientes eran del IGPA (indice mas amplio), no del IPSA --
    # "ANTARCHILE", "QUINENCO", "HABITAT", "CUPRUM",
    # "PROVIDA", "PLANVITAL", "SK", "CAMANCHACA",
    # "ALMENDRAL", "ENELGXCH", "WATTS", "CRISTALES", "BESALCO",
    # "PUCOBRE", "LIPIGAS", "BLUMAR", "ORO-BLANCO", "AAISA",
    # "ENJOY", "INDISA",
    # "ITAUCL", "CENCOSHOPP", "ILC", "SALFACORP", "SMSAAM",
    # Removidas antes de esto (no existen en Yahoo Finance con .SN,
    # confirmado en produccion): ITAUCORP, SECURITY, BICECORP, SAAM, EMBONOR
]

# Ambiente 2 (EE.UU. · USD) -- especificacion v3, seccion 2.3. Nucleo de
# ETFs sugerido; ajustar segun preferencia real del usuario. Estos NO
# llevan sufijo .SN al pedirlos a Yahoo (ver data_source.get_market_data
# con suffix="").
TICKERS_USA = [
    # --- Nucleo de ETF (los 7 que ya estaban) ---
    "VOO", "VTI", "VT", "VXUS", "QQQM", "SCHD",
    "BND",
    # --- 100 acciones grandes de EE.UU. ---
    # Elegidas del catalogo de Racional que me pasaste, priorizando
    # capitalizacion de mercado y peso en el S&P 500. Se dejaron fuera a
    # proposito: ETF apalancados/inversos (2X, 3X, "Short"), warrants,
    # papeles OTC y tickers marcados .OLD en ese catalogo -- Honeywell
    # aparecia como "HON.OLD", que es un simbolo retirado, asi que no se
    # incluyo en vez de arriesgar un 404 permanente.
    #
    # OJO con Berkshire: el catalogo lo lista como "BRK.B", pero Yahoo usa
    # guion en las clases de accion. Aca va "BRK-B" porque este texto se
    # manda tal cual como simbolo a Yahoo (ver data_source.get_market_data).
    # Block aparece como "XYZ": cambio su simbolo desde "SQ" en enero 2025.
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
    "TSLA", "AVGO", "TSM", "ORCL", "NFLX", "AMD",
    "CRM", "ADBE", "INTC", "QCOM", "TXN", "AMAT",
    "LRCX", "KLAC", "MU", "ASML", "ARM", "PLTR",
    "NOW", "INTU", "IBM", "CSCO", "ACN", "UBER",
    "SHOP", "PANW", "CRWD", "SNOW", "ANET", "MRVL",
    "DELL", "SMCI", "COIN", "SPOT", "ABNB", "BKNG",
    "MELI", "XYZ", "PYPL", "BRK-B", "JPM", "V",
    "MA", "BAC", "WFC", "GS", "MS", "C",
    "BLK", "AXP", "SCHW", "SPGI", "KKR", "BX",
    "LLY", "JNJ", "UNH", "ABBV", "MRK", "TMO",
    "ABT", "PFE", "AMGN", "DHR", "ISRG", "NVO",
    "VRTX", "BMY", "MDT", "WMT", "COST", "PG",
    "HD", "KO", "PEP", "MCD", "NKE", "SBUX",
    "DIS", "PM", "TGT", "LOW", "TJX", "XOM",
    "CVX", "COP", "CAT", "BA", "GE", "RTX",
    "UNP", "LMT", "DE", "LIN",
    # Agregado a pedido: ETF sectorial de aeroespacial/defensa.
    "ITA",
    # Agregadas a pedido (lista revisada una por una contra lo que ya
    # estaba, solo se suman las que faltaban -- ANET y LLY ya estaban):
    # Skyward Specialty Insurance, Ero Copper, Enova International,
    # Paysign, WisdomTree.
    "SKWD", "ERO", "ENVA", "PAYS", "WT",

    # -----------------------------------------------------------------------
    # NASDAQ-100 (agosto 2026)
    # -----------------------------------------------------------------------
    # Agregado con el presupuesto de peticiones que liberaron los 45 papeles
    # chilenos que salieron de TICKERS. Son SOLO los miembros del indice que
    # NO estaban ya en la lista de arriba -- AAPL, MSFT, NVDA, AMZN, GOOGL,
    # META, TSLA, AVGO, NFLX, AMD, COST, PEP, ADBE, CSCO, INTU, TXN, QCOM,
    # AMAT, LRCX, KLAC, MU, ASML, ARM, PLTR, PANW, CRWD, ABNB, BKNG, MELI,
    # PYPL, ISRG, AMGN, VRTX, SBUX, LIN, MRVL, INTC, APP y ANET ya estaban,
    # asi que no se repiten.
    #
    # POR QUE ESTOS Y NO OTROS
    # ========================
    # Es un indice publico y estable: sus miembros se pueden verificar, a
    # diferencia de inventar una lista "de crecimiento" a ojo. Se dejaron
    # FUERA a proposito los casos donde el simbolo pudo cambiar y un 404
    # permanente costaria una peticion en cada ciclo para siempre (mismo
    # problema que ya paso con ITAUCORP y SAAM en Chile): ANSS (Ansys fue
    # absorbida por Synopsys en 2025) y WBD (se dividio en dos empresas).
    # Si alguna de estas igual falla, data_source.py lo imprime en el log
    # con el nombre del simbolo -- revisa el log del primer despliegue y
    # comenta la que aparezca.
    #
    # HON va con su simbolo real. En el catalogo de Racional aparecia como
    # "HON.OLD" y por eso se habia dejado fuera en v3; ese sufijo era del
    # catalogo, no de Yahoo, donde Honeywell es simplemente HON.
    "GOOG", "ADI", "ADP", "ADSK", "AEP", "AXON", "AZN",
    "BIIB", "BKR", "CCEP", "CDNS", "CDW", "CEG", "CHTR",
    "CMCSA", "CPRT", "CSGP", "CSX", "CTAS", "CTSH", "DASH",
    "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST", "FTNT",
    "GEHC", "GFS", "GILD", "HON", "IDXX", "KDP", "KHC",
    "LULU", "MAR", "MCHP", "MNST", "MSTR", "NXPI", "ODFL",
    "ON", "ORLY", "PAYX", "PCAR", "PDD", "REGN", "ROP",
    "ROST", "SNPS", "TEAM", "TMUS", "TTD", "TTWO", "VRSK",
    "WDAY", "XEL", "ZS",
]

# Control de tamaño: si esta lista crece mucho mas, el ciclo de fondo
# empieza a rozar el limite de 429 de Yahoo (data_source._MAX_WORKERS = 8
# esta puesto justo por eso). Cualquier ampliacion futura -- el S&P 500
# completo, por ejemplo -- deberia ir a un universo aparte que solo se
# recorra cuando Cristian pida el analisis a mano, NO en el ciclo de 30
# minutos que alimenta la grilla.
assert len(set(TICKERS_USA)) == len(TICKERS_USA), \
    "TICKERS_USA tiene simbolos repetidos: " + ", ".join(
        sorted({t for t in TICKERS_USA if TICKERS_USA.count(t) > 1}))