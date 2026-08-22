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

# ===========================================================================
# UNIVERSO DE ANALISIS -- S&P 500 + Nasdaq-100
# ===========================================================================
# ESTO NO SE BARRE CADA 30 MINUTOS. Leelo antes de tocarlo.
#
# TICKERS_USA (arriba) es la GRILLA: lo que la app dibuja como tarjetas y
# refresca solo, cada ciclo. Son 172 y ese numero esta calibrado contra el
# unico worker de Render y contra el limite de 429 de Yahoo.
#
# UNIVERSO_ANALISIS (esto) es otra cosa: la lista completa sobre la que
# corre el EMBUDO del modulo Explorar, y solo cuando Cristian aprieta
# "Ejecutar analisis" a mano. Son ~560 simbolos. Meterlos en el ciclo
# automatico tumbaria el servidor -- ya paso una vez, cuando el ambiente de
# EE.UU. crecio de 7 a 107 instrumentos y get_stats bloqueaba el worker
# hasta un minuto (ver el comentario largo en server.py). Por eso viven
# separados: uno es "lo que miro todo el rato", el otro es "lo que reviso
# cuando me siento a buscar".
#
# DE DONDE SALEN
# ==============
# Componentes del S&P 500 y del Nasdaq-100 tomados de las listas publicas
# de Wikipedia (agosto 2026), no escritos de memoria. Las clases de accion
# van con guion y no con punto, que es como las pide Yahoo: BRK-B, BF-B.
#
# "Todo el Nasdaq" son ~3.000 papeles, la mayoria micro caps sin volumen
# que el embudo descartaria en el primer filtro igual. El Nasdaq-100 es la
# parte que sirve, y es lo que esta aca.
#
# SIMBOLOS QUE PUEDEN NO EXISTIR
# ===============================
# En una lista de 500 siempre hay alguno renombrado o recien salido de
# bolsa. En vez de pedirte que leas logs, data_source.py ahora los pone en
# CUARENTENA solo: el primero que responde "no existe" queda anotado y no
# se vuelve a pedir mientras el servidor siga vivo. Se consultan en
# /universo-diag.
SP500 = [
    "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE",
    "ADI", "ADM", "ADP", "ADSK", "AEE", "AEP", "AES", "AFL",
    "AIG", "AIZ", "AJG", "AKAM", "ALB", "ALGN", "ALL", "ALLE",
    "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP", "AMT", "AMZN",
    "ANET", "AON", "AOS", "APA", "APD", "APH", "APO", "APP",
    "APTV", "ARE", "ARES", "ATO", "AVB", "AVGO", "AVY", "AWK",
    "AXON", "AXP", "AZO", "BA", "BAC", "BALL", "BAX", "BBY",
    "BDX", "BEN", "BF-B", "BG", "BIIB", "BKNG", "BKR", "BLDR",
    "BLK", "BMY", "BNY", "BR", "BRK-B", "BRO", "BSX", "BX",
    "BXP", "C", "CAG", "CAH", "CARR", "CASY", "CAT", "CB",
    "CBOE", "CBRE", "CCI", "CCL", "CDNS", "CDW", "CEG", "CF",
    "CFG", "CHD", "CHRW", "CHTR", "CI", "CIEN", "CINF", "CL",
    "CLX", "CMCSA", "CME", "CMG", "CMI", "CMS", "CNC", "CNP",
    "COF", "COHR", "COIN", "COO", "COP", "COR", "COST", "CPAY",
    "CPB", "CPRT", "CPT", "CRH", "CRL", "CRM", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "CTVA", "CVNA", "CVS", "CVX",
    "D", "DAL", "DASH", "DD", "DDOG", "DE", "DECK", "DELL",
    "DG", "DGX", "DHI", "DHR", "DIS", "DLR", "DLTR", "DOC",
    "DOV", "DOW", "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN",
    "DXCM", "EA", "EBAY", "ECL", "ED", "EFX", "EG", "EIX",
    "EL", "ELV", "EME", "EMR", "EOG", "EPAM", "EQIX", "EQR",
    "EQT", "ERIE", "ES", "ESS", "ETN", "ETR", "EVRG", "EW",
    "EXC", "EXE", "EXPD", "EXPE", "EXR", "F", "FANG", "FAST",
    "FCX", "FDS", "FDX", "FE", "FFIV", "FICO", "FIS", "FISV",
    "FITB", "FIX", "FOX", "FOXA", "FRT", "FSLR", "FTNT", "FTV",
    "GD", "GDDY", "GE", "GEHC", "GEN", "GEV", "GILD", "GIS",
    "GL", "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN",
    "GRMN", "GS", "GWW", "HAL", "HAS", "HBAN", "HCA", "HD",
    "HIG", "HII", "HLT", "HON", "HOOD", "HPE", "HPQ", "HRL",
    "HSIC", "HST", "HSY", "HUBB", "HUM", "HWM", "IBKR", "IBM",
    "ICE", "IDXX", "IEX", "IFF", "INCY", "INTC", "INTU", "INVH",
    "IP", "IQV", "IR", "IRM", "ISRG", "IT", "ITW", "IVZ",
    "J", "JBHT", "JBL", "JCI", "JKHY", "JNJ", "JPM", "KDP",
    "KEY", "KEYS", "KHC", "KIM", "KKR", "KLAC", "KMB", "KMI",
    "KO", "KR", "KVUE", "L", "LDOS", "LEN", "LH", "LHX",
    "LII", "LIN", "LITE", "LLY", "LMT", "LNT", "LOW", "LRCX",
    "LULU", "LUV", "LVS", "LYB", "LYV", "MA", "MAA", "MAR",
    "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ", "MDT", "MET",
    "META", "MGM", "MKC", "MLM", "MMM", "MNST", "MO", "MOS",
    "MPC", "MPWR", "MRK", "MRNA", "MRSH", "MS", "MSCI", "MSFT",
    "MSI", "MTB", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE",
    "NEM", "NFLX", "NI", "NKE", "NOC", "NOW", "NRG", "NSC",
    "NTAP", "NTRS", "NUE", "NVDA", "NVR", "NWS", "NWSA", "NXPI",
    "O", "ODFL", "OKE", "OMC", "ON", "ORCL", "ORLY", "OTIS",
    "OXY", "PANW", "PAYX", "PCAR", "PCG", "PEG", "PEP", "PFE",
    "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR",
    "PM", "PNC", "PNR", "PNW", "PODD", "POOL", "PPG", "PPL",
    "PRU", "PSA", "PSKY", "PSX", "PTC", "PWR", "PYPL", "Q",
    "QCOM", "RCL", "REG", "REGN", "RF", "RJF", "RL", "RMD",
    "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY", "SATS",
    "SBAC", "SBUX", "SCHW", "SHW", "SJM", "SLB", "SMCI", "SNA",
    "SNDK", "SNPS", "SO", "SOLV", "SPG", "SPGI", "SRE", "STE",
    "STLD", "STT", "STX", "STZ", "SW", "SWK", "SWKS", "SYF",
    "SYK", "SYY", "T", "TAP", "TDG", "TDY", "TECH", "TEL",
    "TER", "TFC", "TGT", "TJX", "TKO", "TMO", "TMUS", "TPL",
    "TPR", "TRGP", "TRMB", "TROW", "TRV", "TSCO", "TSLA", "TSN",
    "TT", "TTD", "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER",
    "UDR", "UHS", "ULTA", "UNH", "UNP", "UPS", "URI", "USB",
    "V", "VEEV", "VICI", "VLO", "VLTO", "VMC", "VRSK", "VRSN",
    "VRT", "VRTX", "VST", "VTR", "VTRS", "VZ", "WAB", "WAT",
    "WBD", "WDAY", "WDC", "WEC", "WELL", "WFC", "WM", "WMB",
    "WMT", "WRB", "WSM", "WST", "WTW", "WY", "WYNN", "XEL",
    "XOM", "XYL", "XYZ", "YUM", "ZBH", "ZBRA", "ZTS",
]

NASDAQ100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALNY",
    "AMAT", "AMD", "AMGN", "AMZN", "APP", "ARM", "ASML", "AVGO",
    "AXON", "BKNG", "BKR", "CCEP", "CDNS", "CEG", "CHTR", "CMCSA",
    "COST", "CPRT", "CRWD", "CSCO", "CSGP", "CSX", "CTAS", "CTSH",
    "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST", "FER",
    "FTNT", "GEHC", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INSM",
    "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX",
    "MAR", "MCHP", "MDLZ", "MELI", "META", "MNST", "MPWR", "MRVL",
    "MSFT", "MSTR", "MU", "NFLX", "NVDA", "NXPI", "ODFL", "ORLY",
    "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SHOP", "SNDK", "SNPS", "STX",
    "TMUS", "TRI", "TSLA", "TTWO", "TXN", "VRSK", "VRTX", "WBD",
    "WDAY", "WDC", "WMT", "XEL", "ZS",
]

# ---------------------------------------------------------------------------
# LOS ETF NO ENTRAN AL EMBUDO
# ---------------------------------------------------------------------------
# Visto en la corrida real del 22 de agosto: en la lista "para revisar a
# mano" aparecieron VOO, VXUS y SCHD. No es un error de datos -- es que un
# ETF NO TIENE capitalizacion bursatil ni crecimiento de utilidades en el
# sentido de una empresa, asi que el embudo los marca como "sin dato" y los
# manda a revisar. Pero revisarlos no lleva a ninguna parte: no hay nada que
# revisar. Son fondos indexados, no candidatas de un metodo de seleccion de
# ACCIONES.
#
# Siguen en TICKERS_USA (la grilla): Cristian los tiene y quiere ver su
# precio todos los dias. Lo que no tiene sentido es evaluarlos con los siete
# filtros del metodo.
ETFS_NO_ANALIZAR = [
    "VOO", "VTI", "VT", "VXUS", "QQQM", "SCHD", "BND",   # nucleo de la cartera
    "ITA",                                                # sectorial de defensa
    "WT",                                                 # WisdomTree es la GESTORA (accion), no un ETF: se deja
]
# WT se saca de la lista de exclusion: es WisdomTree Inc., la empresa que
# administra los ETF, y cotiza como accion normal. Estaba puesta arriba solo
# para dejar escrito por que NO se excluye.
ETFS_NO_ANALIZAR = [t for t in ETFS_NO_ANALIZAR if t != "WT"]

# El universo real del analisis: los dos indices MAS la grilla (que trae
# cosas que no estan en ningun indice y Cristian igual quiere mirar --
# ASML, TSM, NVO, SHOP, MELI, ARM y las cinco chicas que pidio), MENOS los
# ETF, por lo que dice el comentario de arriba.
UNIVERSO_ANALISIS = sorted(
    (set(SP500) | set(NASDAQ100) | set(TICKERS_USA)) - set(ETFS_NO_ANALIZAR))
