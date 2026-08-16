"""
Capa de senales con reglas explicitas.

QUE ES Y QUE NO ES
==================
Esto NO es una recomendacion de compra o venta, y no pretende predecir
precios. Es un rankeador con reglas escritas: toma criterios que hoy estan
implicitos (o directamente ausentes) en la alerta de "-4% bajo el promedio",
los hace explicitos, les pone un numero, y muestra el porque de cada uno
para que puedas discutirle al modelo en vez de creerle.

QUE PROBLEMA RESUELVE
=====================
La regla actual es `precio < promedio_90d * 0.96`. Tiene cuatro defectos
que se ven a simple vista en la app:

1. Umbral fijo para acciones con volatilidad muy distinta.
   -4% en AGUAS-A (que se mueve ~0,8% al dia) es un evento raro. -4% en
   ENJOY (que se mueve ~5% al dia) es un martes cualquiera. Por eso el
   panel se llena siempre de las mismas acciones chicas y volatiles.
   -> Se reemplaza por el z-score: cuantas desviaciones estandar bajo su
      PROPIO promedio esta cada accion. Cada una compite contra si misma.

2. No distingue caida del mercado de caida de la accion.
   Si el IPSA cae 3%, caen 40 acciones a la vez y llegan 16 alertas que
   dicen todas lo mismo. Eso no es informacion.
   -> Se mide la fuerza relativa: cuanto se movio la accion MENOS cuanto
      se movio el indice en el mismo periodo.

3. No distingue un retroceso dentro de una tendencia alcista de una accion
   en caida libre. Comprar barato lo que sigue bajando no es comprar barato.
   -> Filtro de tendencia con SMA20/SMA50, y una bandera roja explicita
      cuando la accion viene cayendo de forma sostenida.

4. No mira liquidez.
   Una senal perfecta en una accion que transa $20 millones al dia no se
   puede ejecutar sin mover el precio en contra tuya.
   -> Filtro de monto transado promedio.

TODOS LOS UMBRALES ESTAN ARRIBA Y SE PUEDEN CAMBIAR. Si un numero te
parece mal puesto, cambialo — esa es justamente la idea de que las reglas
sean explicitas.
"""
import os
import time

# --------------------------------------------------------------------------
# Umbrales — cambialos aqui, no repartidos por el codigo
# --------------------------------------------------------------------------

# Z-score: cuantas desviaciones estandar bajo/sobre su promedio de 90 dias.
# -1,5 sigma es aprox. el 6-7% de los dias mas bajos de una accion. Es un
# evento poco comun para ESA accion, sea volatil o tranquila.
Z_COMPRA_ATENCION = float(os.environ.get("Z_COMPRA_ATENCION", -1.5))
Z_COMPRA_FUERTE   = float(os.environ.get("Z_COMPRA_FUERTE",   -2.0))
Z_VENTA_ATENCION  = float(os.environ.get("Z_VENTA_ATENCION",   1.5))
Z_VENTA_FUERTE    = float(os.environ.get("Z_VENTA_FUERTE",     2.0))

RSI_SOBREVENTA = float(os.environ.get("RSI_SOBREVENTA", 30))
RSI_SOBRECOMPRA = float(os.environ.get("RSI_SOBRECOMPRA", 70))

# Monto transado promedio diario (30 dias) en pesos. Bajo esto, la senal
# existe pero no es ejecutable sin mover el precio: se marca y se castiga.
LIQUIDEZ_MINIMA_CLP = float(os.environ.get("LIQUIDEZ_MINIMA_CLP", 150_000_000))

# Mismo criterio que LIQUIDEZ_MINIMA_CLP pero en dolares, para el ambiente
# EE.UU. (main.TICKERS_USA): ETFs grandes (VOO, VTI...) y acciones del
# S&P 500 transan varios ordenes de magnitud mas que el promedio chileno, asi
# que NO se puede reusar el umbral en CLP -- 150 millones de pesos son unos
# US$150 mil, un piso absurdamente bajo para ese mercado. US$2 millones/dia
# deja pasar cualquier nombre grande y solo filtra las acciones mas chicas
# de la lista (ej. SKWD, ERO, ENVA, PAYS).
LIQUIDEZ_MINIMA_USD = float(os.environ.get("LIQUIDEZ_MINIMA_USD", 2_000_000))

# Fuerza relativa: diferencia entre el retorno 3M de la accion y el del
# IPSA. Mas alla de esto, el movimiento es propio de la accion y no del
# mercado — conviene buscar la noticia antes de actuar.
FR_DIVERGENCIA = float(os.environ.get("FR_DIVERGENCIA", 0.10))  # 10 puntos

# Historial minimo para que el z-score signifique algo.
DIAS_MINIMOS = int(os.environ.get("DIAS_MINIMOS_SENAL", 60))

# Cuantos dias habiles despues de un salto grande (ver gapPct/gapDiasAtras en
# data_source.py) se sigue considerando que lo que movio el precio fue ESE
# evento y no una tendencia nueva. ~3 semanas de bolsa: suficiente para que
# un reporte de resultados deje de dominar la lectura tecnica.
DIAS_GAP_RECIENTE = int(os.environ.get("DIAS_GAP_RECIENTE", 15))

# Ventana antes de un reporte de resultados en la que las señales tecnicas
# se marcan como "no accionables": el evento domina cualquier lectura de
# precio, asi que la señal puede ser correcta y aun asi ser mala idea.
DIAS_ANTES_REPORTE = int(os.environ.get("DIAS_ANTES_REPORTE", 7))

DESCARGO = (
    "Esto no es una recomendacion de compra ni de venta, ni una prediccion. "
    "Es un resumen de reglas estadisticas sobre el comportamiento pasado. "
    "No conozco tu situacion financiera, tu horizonte ni tu tolerancia al "
    "riesgo, y el precio viene de Yahoo Finance con rezago. Verifica en tu "
    "corredora antes de cualquier decision."
)



def _pct(x):
    return None if x is None else round(x * 100, 1)


# --------------------------------------------------------------------------
# Evaluacion de una accion
# --------------------------------------------------------------------------

def evaluar(ticker, precio, stats, stats_indice=None, moneda="CLP", reporte=None):
    """
    Devuelve un dict con puntaje, clasificacion, razones y banderas rojas.

    puntaje: -100 (extremo caro/sobrecomprado) a +100 (extremo barato/
             sobrevendido) respecto de SU PROPIO historial. Un puntaje alto
             NO significa "buena inversion" — significa "esta lejos de su
             promedio hacia abajo". Puede ser una oportunidad o puede ser
             una empresa que se esta deteriorando. El puntaje no sabe cual.

    `moneda`: "CLP" (default, mercado chileno) o "USD" (ambiente EE.UU.) --
    solo cambia que umbral de liquidez se usa (ver LIQUIDEZ_MINIMA_CLP /
    LIQUIDEZ_MINIMA_USD): son mercados de escalas de transaccion muy
    distintas y un solo umbral no sirve para los dos.

    `reporte`: {"fecha": "YYYY-MM-DD", "epoch": ...} del proximo reporte de
    resultados, si se conoce (ver get_proximos_reportes en data_source.py).
    Una señal a pocos dias de un reporte no se borra, pero SI se marca: el
    evento domina cualquier lectura tecnica.
    """
    if not stats or precio is None:
        return None

    if stats.get("diasDeHistorial", 0) < DIAS_MINIMOS:
        return {
            "ticker": ticker,
            "clasificacion": "sin_datos",
            "puntaje": None,
            "razones": [f"Solo hay {stats.get('diasDeHistorial', 0)} dias de "
                        f"historial; se necesitan {DIAS_MINIMOS} para que el "
                        f"z-score signifique algo."],
            "banderas": [],
        }

    razones, banderas = [], []
    puntaje = 0.0

    # -- 1. Extension: distancia a su propia LINEA DE TENDENCIA -------------
    # Ojo: `zscore` ya NO es la distancia al promedio plano de 90 dias, sino
    # a la recta de regresion de esa ventana (ver _extension_regresion() en
    # data_source.py). El cambio importa: una accion en alza sostenida esta
    # SIEMPRE sobre su promedio movil -- eso es la definicion de tendencia,
    # no una anomalia -- y el modelo anterior la castigaba por eso.
    z = stats.get("zscore")
    por_regresion = stats.get("zscoreMetodo") == "regresion"
    ancla = "linea de tendencia" if por_regresion else "promedio de 90 dias"
    if z is not None:
        # +40 puntos como maximo, saturando en 3 sigma.
        puntaje += max(-40.0, min(40.0, -z * (40.0 / 3.0)))
        dist_pct = _pct(precio / stats["avg90"] - 1) if stats.get("avg90") else None
        if z <= Z_COMPRA_FUERTE:
            razones.append(
                f"Esta {abs(z):.1f} desviaciones BAJO su {ancla} "
                f"({dist_pct}% respecto del promedio de 90 dias). Para esta "
                f"accion en particular, eso es un nivel poco frecuente.")
        elif z <= Z_COMPRA_ATENCION:
            razones.append(
                f"Esta {abs(z):.1f} desviaciones bajo su {ancla} "
                f"({dist_pct}% respecto del promedio de 90 dias).")
        elif z >= Z_VENTA_FUERTE:
            razones.append(
                f"Esta {z:.1f} desviaciones SOBRE su {ancla} "
                f"({dist_pct}% respecto del promedio de 90 dias). "
                f"Historicamente se ha estirado poco mas que esto.")
        elif z >= Z_VENTA_ATENCION:
            razones.append(
                f"Esta {z:.1f} desviaciones sobre su {ancla} "
                f"({dist_pct}% respecto del promedio de 90 dias).")
        else:
            razones.append(
                f"En linea con su {ancla} ({dist_pct}% respecto del promedio "
                f"de 90 dias). Sin senal por este criterio.")

        # La pendiente dice si el ancla MISMA va subiendo o bajando. Es
        # informacion que el z solo no entrega: z=0 con pendiente +0.4%/dia
        # ("acompaña una tendencia alcista ordenada") es una situacion muy
        # distinta de z=0 con pendiente -0.4%/dia.
        pend = stats.get("pendientePct")
        if pend is not None and por_regresion:
            if pend >= 0.05:
                razones.append(f"Su linea de tendencia de 90 dias sube "
                               f"{pend:.2f}% por dia.")
            elif pend <= -0.05:
                razones.append(f"Su linea de tendencia de 90 dias baja "
                               f"{abs(pend):.2f}% por dia.")
            else:
                razones.append("Su linea de tendencia de 90 dias esta "
                               "practicamente plana.")

    # -- 2. RSI sobre cierres diarios reales --------------------------------
    # (Antes el RSI se calculaba en el celular sobre los datos que llegaban
    #  cada 60 segundos, y se reiniciaba al recargar la app. Por eso las 47
    #  tarjetas decian siempre "RSI: acumulando". Ahora viene del backend,
    #  calculado sobre cierres diarios de un ano.)
    rsi = stats.get("rsi14")
    if rsi is not None:
        if rsi <= RSI_SOBREVENTA:
            puntaje += 20
            razones.append(f"RSI(14) en {rsi}: zona de sobreventa "
                           f"(lectura convencional, bajo 30).")
        elif rsi >= RSI_SOBRECOMPRA:
            puntaje -= 20
            razones.append(f"RSI(14) en {rsi}: zona de sobrecompra "
                           f"(lectura convencional, sobre 70).")
        else:
            razones.append(f"RSI(14) en {rsi}: zona neutra.")

    # -- 3. Tendencia: retroceso sano vs. caida libre -----------------------
    # `tendencia` es la version resumida (alcista/bajista/neutra) de esta
    # misma logica, para que el frontend pueda pintar un icono en la
    # tarjeta de lista sin tener que interpretar el texto de las razones.
    sma20, sma50 = stats.get("sma20"), stats.get("sma50")
    tendencia = "neutra"

    # ¿La ruptura de medias viene de un GAP reciente (un evento puntual) o
    # de un deterioro sostenido? Si el mayor movimiento diario de los
    # ultimos 60 dias fue un salto de 3+ sigmas y ocurrio hace poco, lo que
    # rompio las medias fue ESE dia, no una tendencia. Leerlo como "caida
    # sostenida" es un falso negativo (caso CSCO: cayo el dia despues de un
    # reporte que batio expectativas, y el modelo lo marco -37).
    gap_pct = stats.get("gapPct")
    gap_dias = stats.get("gapDiasAtras")
    hubo_evento = (gap_pct is not None and gap_dias is not None
                   and gap_dias <= DIAS_GAP_RECIENTE)

    if sma20 and sma50:
        if precio < sma50 and sma20 < sma50:
            if hubo_evento:
                # Se informa, pero NO se castiga como tendencia: el gap ya
                # esta reflejado en el precio y en el z de extension.
                tendencia = "evento"
                razones.append(
                    f"Rompio sus medias, pero el movimiento viene de un salto "
                    f"de {_pct(gap_pct)}% hace {gap_dias} dias habiles "
                    f"(tipico de un reporte o una noticia puntual), no de un "
                    f"deterioro sostenido. Se evalua como evento, no como "
                    f"cambio de tendencia.")
            else:
                # Precio bajo la media larga Y media corta por debajo de la larga:
                # la caida no es un bache, es la direccion.
                puntaje -= 25
                tendencia = "bajista"
                banderas.append(
                    "CAIDA SOSTENIDA — el precio esta bajo su media de 50 dias y "
                    "la media de 20 va por debajo de la de 50. Barato aqui puede "
                    "seguir abaratandose. Un puntaje alto en esta situacion NO es "
                    "una oportunidad detectada: es una accion cayendo.")
        elif precio < sma20 and sma20 > sma50:
            puntaje += 15
            tendencia = "alcista"
            razones.append(
                "Retroceso dentro de una tendencia alcista: el precio cedio "
                "bajo su media de 20 dias, pero la media de 20 sigue sobre la "
                "de 50.")
        elif precio > sma20 > sma50:
            tendencia = "alcista"
            razones.append("Tendencia alcista intacta (precio > SMA20 > SMA50).")

    # -- 4. Fuerza relativa contra el IPSA ----------------------------------
    if stats_indice and stats.get("ret3m") is not None \
            and stats_indice.get("ret3m") is not None:
        fr = stats["ret3m"] - stats_indice["ret3m"]
        if fr <= -FR_DIVERGENCIA:
            banderas.append(
                f"Se quedo {abs(_pct(fr))} puntos ATRAS del IPSA en 3 meses "
                f"(accion {_pct(stats['ret3m'])}% vs indice "
                f"{_pct(stats_indice['ret3m'])}%). Esta cayendo por algo suyo, "
                f"no porque caiga el mercado. Busca la noticia antes de asumir "
                f"que esta barata.")
            puntaje -= 10
        elif fr >= FR_DIVERGENCIA:
            razones.append(
                f"Le lleva {_pct(fr)} puntos de ventaja al IPSA en 3 meses "
                f"(accion {_pct(stats['ret3m'])}% vs indice "
                f"{_pct(stats_indice['ret3m'])}%).")
        else:
            razones.append(
                f"Se movio parecido al IPSA en 3 meses (diferencia "
                f"{_pct(fr)} puntos): lo suyo es mercado, no algo propio.")

    # -- 4b. Proximidad a un reporte de resultados --------------------------
    # No cambia el puntaje: la lectura tecnica sigue siendo la que es. Lo
    # que cambia es si es ACCIONABLE. A pocos dias de un reporte, el evento
    # domina el precio y cualquier señal previa puede darse vuelta entera en
    # una sola sesion. Antes la app decia "puntaje -21" sin mencionar que la
    # empresa reportaba el lunes -- justo el dato que cambia la decision.
    dias_a_reporte = None
    if reporte and reporte.get("epoch"):
        faltan = (reporte["epoch"] - time.time()) / 86400.0
        if faltan >= 0:
            dias_a_reporte = int(faltan)
            if dias_a_reporte <= DIAS_ANTES_REPORTE:
                banderas.append(
                    f"REPORTA EN {dias_a_reporte} DIA{'S' if dias_a_reporte != 1 else ''} "
                    f"({reporte.get('fecha')}) — un reporte de resultados mueve el "
                    f"precio mucho mas que cualquier señal tecnica, y puede darla "
                    f"vuelta completa en una sesion. Esta lectura no es accionable "
                    f"hasta despues del reporte.")
            else:
                razones.append(f"Proximo reporte de resultados: {reporte.get('fecha')} "
                               f"(en {dias_a_reporte} dias).")

    # -- 5. Liquidez --------------------------------------------------------
    monto = stats.get("montoMedioDiario30d")
    liquidez_minima = LIQUIDEZ_MINIMA_USD if moneda == "USD" else LIQUIDEZ_MINIMA_CLP
    sufijo_moneda = "USD" if moneda == "USD" else "CLP"
    if monto is not None:
        if monto < liquidez_minima:
            puntaje *= 0.5  # la senal existe pero no se puede ejecutar limpio
            banderas.append(
                f"POCA LIQUIDEZ — transa en promedio ${monto/1e6:,.1f} millones "
                f"{sufijo_moneda} al dia. Entrar o salir puede moverte el precio "
                f"en contra, y el spread se come buena parte de la diferencia. "
                f"El puntaje se redujo a la mitad por esto.")
        else:
            razones.append(f"Liquidez razonable: ~${monto/1e6:,.1f} millones "
                           f"{sufijo_moneda} transados al dia (promedio 30 dias).")
    else:
        banderas.append("Sin dato de volumen: no se pudo verificar liquidez.")

    puntaje = round(max(-100.0, min(100.0, puntaje)), 1)

    if puntaje >= 45:
        clasif = "revisar_compra"
    elif puntaje >= 20:
        clasif = "observar_compra"
    elif puntaje <= -45:
        clasif = "revisar_venta"
    elif puntaje <= -20:
        clasif = "observar_venta"
    else:
        clasif = "neutro"

    # Una bandera de caida sostenida invalida la lectura de "candidato de
    # compra": se degrada a observacion, para que el ranking no empuje a
    # comprar justamente lo que viene cayendo.
    if clasif == "revisar_compra" and any(b.startswith("CAIDA SOSTENIDA") for b in banderas):
        clasif = "observar_compra"

    return {
        "ticker": ticker,
        "precio": precio,
        "puntaje": puntaje,
        "clasificacion": clasif,
        "tendencia": tendencia,
        "zscore": round(z, 2) if z is not None else None,
        # Contra que se comparo el precio: "regresion" (linea de tendencia,
        # el metodo bueno) o "promedio" (media plana, respaldo cuando la
        # regresion no se pudo calcular). Que la app pueda decirlo evita que
        # el usuario tenga que adivinar que significa el numero.
        "zscoreMetodo": stats.get("zscoreMetodo"),
        "pendientePct": stats.get("pendientePct"),
        "gapPct": stats.get("gapPct"),
        "gapDiasAtras": stats.get("gapDiasAtras"),
        "diasAReporte": dias_a_reporte,
        "fechaReporte": (reporte or {}).get("fecha"),
        "rsi14": rsi,
        "ret3m": stats.get("ret3m"),
        "ret1y": stats.get("ret1y"),
        "distanciaPromedio": _pct(precio / stats["avg90"] - 1) if stats.get("avg90") else None,
        "volatilidadDiaria": _pct(stats.get("volDiaria")),
        "montoMedioDiario30d": monto,
        "razones": razones,
        "banderas": banderas,
        "descargo": DESCARGO,
    }


def rankear(precios, stats, stats_indice=None, minimo_liquidez=True, moneda="CLP",
            reportes=None):
    """
    Evalua todas las acciones y devuelve dos listas ordenadas.

    `candidatos_compra` NO es una lista de compras sugeridas. Es la lista de
    acciones que estan mas lejos de su propio promedio hacia abajo, con las
    banderas rojas visibles al lado de cada una para que puedas descartarlas.

    `moneda`: ver evaluar() -- selecciona el umbral de liquidez correcto.
    """
    reportes = reportes or {}
    evaluadas = []
    for t, p in precios.items():
        ev = evaluar(t, p, stats.get(t), stats_indice, moneda=moneda,
                     reporte=reportes.get(t))
        if ev and ev.get("puntaje") is not None:
            evaluadas.append(ev)

    liquidez_minima = LIQUIDEZ_MINIMA_USD if moneda == "USD" else LIQUIDEZ_MINIMA_CLP
    ejecutables = [e for e in evaluadas
                   if not minimo_liquidez
                   or (e["montoMedioDiario30d"] or 0) >= liquidez_minima]

    compra = sorted([e for e in ejecutables if e["puntaje"] >= 20],
                    key=lambda e: -e["puntaje"])
    venta = sorted([e for e in ejecutables if e["puntaje"] <= -20],
                   key=lambda e: e["puntaje"])

    return {
        "candidatos_compra": compra,
        "candidatos_venta": venta,
        "evaluadas": len(evaluadas),
        "filtradas_por_liquidez": len(evaluadas) - len(ejecutables),
        # A diferencia de candidatos_compra/venta (solo |puntaje| >= 20),
        # esto trae TODAS las acciones evaluadas, incluidas las neutras --
        # lo usa la tarjeta de lista del frontend para mostrar puntaje/RSI/
        # tendencia de cada accion, no solo de las que ya cruzaron un umbral.
        "evaluadas_detalle": evaluadas,
        "descargo": DESCARGO,
    }


def describe(ev):
    """Una linea legible para el correo o el push."""
    if not ev or ev.get("puntaje") is None:
        return "Sin datos suficientes para evaluar."
    partes = [f"puntaje {ev['puntaje']:+.0f}"]
    if ev.get("zscore") is not None:
        partes.append(f"z {ev['zscore']:+.1f}")
    if ev.get("rsi14") is not None:
        partes.append(f"RSI {ev['rsi14']}")
    linea = " · ".join(partes)
    if ev.get("banderas"):
        linea += f" · {len(ev['banderas'])} bandera(s) roja(s)"
    return linea


# Diccionario de explicaciones para el correo y la app
GLOSARIO = {
    "banderas": {
        "CAIDA SOSTENIDA": "Precio bajo SMA50 y SMA20 bajo SMA50 — tendencia bajista establecida. No es un buen momento para comprar.",
        "LIQUIDEZ BAJA": "Volumen promedio menor que el umbral. Difícil de ejecutar sin mover el precio en contra.",
        "RSI EXTREMO": "RSI fuera de rango normal (< 10 o > 90) — sobrevendido o sobrecomprado severo.",
    },
    "puntaje": {
        "alto_positivo": "Puntaje >= 20: señal de posible compra. El precio está bajo su promedio (z-score bajo) y hay tendencia alcista.",
        "alto_negativo": "Puntaje <= -20: señal de posible venta. El precio está alto su promedio (z-score alto) y hay tendencia bajista.",
        "neutral": "Puntaje entre -20 y 20: sin señal clara. Monitorea los cambios en los próximos días.",
    },
    "criterios": {
        "z_score": "Cuántas desviaciones estándar está el precio hoy respecto de su promedio de 90 días. Z < -1.5 es bajo; Z > 1.5 es alto.",
        "rsi14": "Índice de Fuerza Relativa a 14 días. < 30 es sobrevendido; > 70 es sobrecomprado.",
        "sma": "Promedios móviles simples. SMA20: precio promedio últimos 20 días. SMA50: últimos 50 días. Si SMA20 > SMA50, tendencia alcista.",
        "fuerza_relativa": "Cómo se movió la acción MENOS cómo se movió el IPSA en el mismo período. Evita falsos positivos cuando cae todo el mercado.",
        "liquidez": "Volumen en pesos transado en promedio. Acciones con volumen bajo pueden no ejecutarse bien a buenos precios.",
    },
}
