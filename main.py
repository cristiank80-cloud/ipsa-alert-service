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
# ---------------------------------------------------------------------------
# ETF DE SECTOR E INDUSTRIA -- una sola lista para toda la app
# ---------------------------------------------------------------------------
# Estas dos listas VIVIAN en explorar.py. Se movieron aca el 27-ago-2026,
# cuando los 21 que faltaban entraron a la grilla.
#
# POR QUE SE MOVIERON
# ===================
# Si se quedaban alla, agregar un sector nuevo obligaba a editar TRES
# listas: la de explorar.py, TICKERS_USA aca, y stocksUSA en el frontend.
# Olvidar una no da error: da una tarjeta que nunca recibe precio, o un
# simbolo que el servidor pide cada media hora y nadie ve. Ese desajuste
# silencioso ya nos costo caro con la grilla y el frontend, y no habia
# ninguna razon para repetirlo. Ahora explorar.py las IMPORTA de aca:
#
#     from main import ETFS_SECTOR, ETFS_INDUSTRIA
#
# Esa direccion es la unica que se puede: main.py no importa NADA (mira el
# docstring de arriba, se dejo asi a proposito), asi que no hay import
# circular posible. Al reves si lo habria, porque explorar.py arrastra
# requests, data_source e indicador_fuerza_fase.
#
# EL FORMATO ES (nombre, ticker) y lo pide explorar.py: el nombre es la
# etiqueta que se muestra en el paso 1, y el ticker es lo que se le manda a
# Yahoo. Agregar un sector aca lo mete solo en los tres lugares: en el paso
# 1 de Explorar, en la grilla (TICKERS_USA, mas abajo) y en la lista de
# exclusion del embudo (ETFS_NO_ANALIZAR). Lo unico que sigue quedando a
# mano es la entrada del frontend, que es otro archivo.
_ETFS_SECTOR_CRUDO = [
    ("Tecnología", "XLK"), ("Salud", "XLV"), ("Financiero", "XLF"),
    ("Consumo discrecional", "XLY"), ("Consumo básico", "XLP"),
    ("Energía", "XLE"), ("Industrial", "XLI"), ("Materiales", "XLB"),
    ("Utilities", "XLU"), ("Inmobiliario", "XLRE"), ("Comunicaciones", "XLC"),
]

_ETFS_INDUSTRIA_CRUDO = [
    ("Semiconductores", "SMH"), ("Software", "IGV"), ("Ciberseguridad", "CIBR"),
    ("Biotecnología", "XBI"), ("Aeroespacial y defensa", "ITA"),
    ("Banca regional", "KRE"), ("Retail", "XRT"), ("Petróleo y gas E&P", "XOP"),
    ("Oro y mineras", "GDX"), ("Transporte", "IYT"),
    ("Infraestructura", "PAVE"), ("Nuclear / uranio", "URA"),
]

# Los nombres publicos que importa explorar.py.
ETFS_SECTOR = _ETFS_SECTOR_CRUDO
ETFS_INDUSTRIA = _ETFS_INDUSTRIA_CRUDO

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

    # --- Cripto (agosto 2026, a pedido de Cristian) ---
    # Yahoo entrega BTC-USD por el mismo endpoint de precio/historial que
    # cualquier accion (ver data_source.get_market_data, suffix=""), asi que
    # no necesita ningun codigo aparte para aparecer en la grilla ni para
    # que Explorar le baje el historial. Lo que SI es distinto: no tiene
    # capitalizacion bursatil ni crecimiento de utilidades/ingresos como una
    # empresa, asi que esos tres filtros de Explorar no se le pueden aplicar
    # -- ver SIN_FUNDAMENTALES mas abajo. Lo que SI se le calcula igual que
    # al resto: precio, volumen, tendencia, fase de Weinstein y fuerza
    # relativa contra el indice.
    "BTC-USD",

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

    # -----------------------------------------------------------------------
    # AGREGADAS A PEDIDO DE CRISTIAN (27-ago-2026)
    # -----------------------------------------------------------------------
    # De la lista que paso venian diez nombres, pero DELL, LLY, MA y V ya
    # estaban en la grilla desde antes -- no se repiten aca (repetirlos
    # rompe el assert de mas abajo). Estas seis son las que faltaban de
    # verdad.
    #
    # TRES SON ETF, y por eso ademas van a ETFS_NO_ANALIZAR mas abajo: se
    # ven en la grilla, pero no entran al embudo de Explorar (un fondo no
    # tiene capitalizacion ni crecimiento de utilidades que filtrar).
    #
    #   ARKK -- ARK Innovation. Fondo de gestion activa de tecnologia.
    #   IYT  -- iShares U.S. Transportation. OJO: este simbolo YA se usaba
    #           en explorar.py como el ETF de la industria "Transporte" del
    #           paso 1. Que ahora tambien este en la grilla no lo rompe:
    #           son dos usos distintos del mismo simbolo y el historial se
    #           cachea igual, pero si algun dia se saca de aca, NO tocar el
    #           de explorar.py.
    #   URSP -- ProShares Ultra S&P 500 Equal Weight. Es APALANCADO 2x, la
    #           unica excepcion a la regla de "nada apalancado" que dice el
    #           comentario del bloque de las 100 grandes. Entra porque
    #           Cristian lo pidio, pero amplifica al doble las subidas Y las
    #           bajadas: la alerta de caida se le va a disparar mucho mas
    #           seguido que al resto de la grilla, y no significa lo mismo.
    #
    # LAS OTRAS TRES SON ACCIONES NORMALES y si entran al embudo completo:
    #
    #   BUSE -- First Busey, banca regional (Nasdaq).
    #   HNGE -- Hinge Health, salud digital (NYSE). Salio a bolsa en mayo de
    #           2025, asi que recien ahora tiene el año de historial que
    #           necesitan la fase de Weinstein y la fuerza relativa. Si el
    #           embudo la deja en "sin dato", es por eso y no por un error.
    #   UBS  -- UBS Group AG, banca global (la accion que cotiza en NYSE).
    "ARKK", "BUSE", "HNGE", "IYT", "UBS", "URSP",

    # -----------------------------------------------------------------------
    # LOS ETF DE SECTOR E INDUSTRIA (27-ago-2026)
    # -----------------------------------------------------------------------
    # Son los mismos 23 que Explorar mide en el paso 1 -- ITA e IYT ya
    # estaban en la grilla desde antes, asi que aca van los 21 restantes.
    # NO se escriben a mano: salen de ETFS_SECTOR y ETFS_INDUSTRIA, que
    # ahora viven mas abajo en este mismo archivo. Ver el comentario de esas
    # listas para saber por que se movieron desde explorar.py.
    #
    # POR QUE ENTRAN A LA GRILLA
    # ==========================
    # El metodo de Cristian empieza por el sector: primero se mira que
    # sector esta fuerte y despues se busca la accion adentro. Hasta ahora
    # esa lectura solo existia dentro de Explorar, que se corre a mano. En
    # la grilla se ven todos los dias, al lado de las acciones.
    #
    # LO QUE ESTO CUESTA
    # ==================
    # La grilla pasa de 179 a 200 simbolos: ~12% mas peticiones a Yahoo en
    # cada ciclo de 30 minutos. Sigue debajo del techo con el que se venia
    # trabajando, pero es el limite: si hay que agregar mas cosas, primero
    # sacar algo. El sintoma de haberse pasado son los 429 de Yahoo y las
    # tarjetas que se quedan en "Calculando..." (ver
    # data_source._MAX_WORKERS = 8, puesto justo por esto).
    #
    # Ninguno entra al embudo de Explorar: ETFS_NO_ANALIZAR los excluye a
    # todos automaticamente, mas abajo.
]

# Los 23 ETF del paso 1 se agregan aca y no escritos a mano arriba: los que
# ya estaban en la lista literal (ITA e IYT) se saltan, y el orden en que
# entran es el de ETFS_SECTOR/ETFS_INDUSTRIA, que es como se dibujan las
# tarjetas -- tiene que calzar con stocksUSA del frontend.
#
# OJO: se filtra por "no estaba ya" y NO se deduplica la lista entera a
# proposito. El assert de mas abajo tiene que seguir cachando un simbolo
# repetido escrito a mano en el bloque literal; si aca se dedujera todo, ese
# error se taparia solo y nunca lo veriamos.
TICKERS_USA += [t for _, t in _ETFS_SECTOR_CRUDO + _ETFS_INDUSTRIA_CRUDO
                if t not in TICKERS_USA]

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
ETFS_NO_ANALIZAR = (
    ["VOO", "VTI", "VT", "VXUS", "QQQM", "SCHD", "BND"]   # nucleo de la cartera
    + ["ARKK"]                                            # gestion activa de tecnologia
    + ["URSP"]                                            # apalancado 2x sobre el S&P equiponderado
    # Los 23 del paso 1 de Explorar (incluye ITA y IYT, que antes estaban
    # escritos a mano aca). Se leen de la lista de arriba a proposito: asi
    # agregar un sector nuevo NO obliga a acordarse de excluirlo tambien.
    + [t for _, t in ETFS_SECTOR]
    + [t for _, t in ETFS_INDUSTRIA]
)
# WT NO va en esta lista: es WisdomTree Inc., la empresa que administra los
# ETF, y cotiza como accion normal. Se deja escrito porque es el error facil
# de cometer al leer la lista rapido.
_vistos = set()
ETFS_NO_ANALIZAR = [t for t in ETFS_NO_ANALIZAR
                    if not (t in _vistos or _vistos.add(t))]
del _vistos

# El universo real del analisis: los dos indices MAS la grilla (que trae
# cosas que no estan en ningun indice y Cristian igual quiere mirar --
# ASML, TSM, NVO, SHOP, MELI, ARM y las cinco chicas que pidio), MENOS los
# ETF, por lo que dice el comentario de arriba.
UNIVERSO_ANALISIS = sorted(
    (set(SP500) | set(NASDAQ100) | set(TICKERS_USA)) - set(ETFS_NO_ANALIZAR))

# ---------------------------------------------------------------------------
# ACTIVOS SIN FUNDAMENTALES DE EMPRESA (Bitcoin, y lo que se agregue despues)
# ---------------------------------------------------------------------------
# Distinto de ETFS_NO_ANALIZAR de arriba -- ojo con la diferencia:
#
#   ETFS_NO_ANALIZAR   = NO entra al embudo de Explorar. Se ve el precio en
#                         la grilla y nada mas: ni fase de Weinstein, ni
#                         fuerza relativa, ni los siete filtros del metodo.
#   SIN_FUNDAMENTALES  = SI entra al embudo entero -- precio, volumen,
#                         tendencia, fase de Weinstein, fuerza relativa --
#                         pero SOLO salta los tres filtros que necesitan que
#                         el ticker sea una empresa: capitalizacion
#                         bursatil, crecimiento del BPA, crecimiento de
#                         ingresos. Una criptomoneda no tiene nada de eso
#                         (no hay "utilidades" ni "acciones en circulacion"),
#                         asi que exigirselo la sacaria del embudo por un
#                         motivo que no tiene que ver con si es una buena
#                         entrada tecnica o no.
#
# A diferencia de un ETF (que es un fondo, no algo que "elegir" con este
# metodo), Bitcoin SI es candidato de una primera alerta tecnica -- por eso
# entra al embudo completo en vez de quedar excluido como los ETF.
SIN_FUNDAMENTALES = {"BTC-USD"}
