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

# Fuerza relativa: diferencia entre el retorno 3M de la accion y el del
# IPSA. Mas alla de esto, el movimiento es propio de la accion y no del
# mercado — conviene buscar la noticia antes de actuar.
FR_DIVERGENCIA = float(os.environ.get("FR_DIVERGENCIA", 0.10))  # 10 puntos

# Historial minimo para que el z-score signifique algo.
DIAS_MINIMOS = int(os.environ.get("DIAS_MINIMOS_SENAL", 60))

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

def evaluar(ticker, precio, stats, stats_indice=None):
    """
    Devuelve un dict con puntaje, clasificacion, razones y banderas rojas.

    puntaje: -100 (extremo caro/sobrecomprado) a +100 (extremo barato/
             sobrevendido) respecto de SU PROPIO historial. Un puntaje alto
             NO significa "buena inversion" — significa "esta lejos de su
             promedio hacia abajo". Puede ser una oportunidad o puede ser
             una empresa que se esta deteriorando. El puntaje no sabe cual.
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

    # -- 1. Distancia al propio promedio, normalizada por volatilidad -------
    z = stats.get("zscore")
    if z is not None:
        # +40 puntos como maximo, saturando en 3 sigma.
        puntaje += max(-40.0, min(40.0, -z * (40.0 / 3.0)))
        dist_pct = _pct(precio / stats["avg90"] - 1) if stats.get("avg90") else None
        if z <= Z_COMPRA_FUERTE:
            razones.append(
                f"Esta {abs(z):.1f} desviaciones BAJO su promedio de 90 dias "
                f"({dist_pct}%). Para esta accion en particular, eso es un "
                f"nivel poco frecuente.")
        elif z <= Z_COMPRA_ATENCION:
            razones.append(
                f"Esta {abs(z):.1f} desviaciones bajo su promedio de 90 dias "
                f"({dist_pct}%).")
        elif z >= Z_VENTA_FUERTE:
            razones.append(
                f"Esta {z:.1f} desviaciones SOBRE su promedio de 90 dias "
                f"({dist_pct}%). Historicamente se ha estirado poco mas que esto.")
        elif z >= Z_VENTA_ATENCION:
            razones.append(
                f"Esta {z:.1f} desviaciones sobre su promedio de 90 dias "
                f"({dist_pct}%).")
        else:
            razones.append(f"Cerca de su promedio de 90 dias ({dist_pct}%). "
                           f"Sin senal por este criterio.")

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
    sma20, sma50 = stats.get("sma20"), stats.get("sma50")
    if sma20 and sma50:
        if precio < sma50 and sma20 < sma50:
            # Precio bajo la media larga Y media corta por debajo de la larga:
            # la caida no es un bache, es la direccion.
            puntaje -= 25
            banderas.append(
                "CAIDA SOSTENIDA — el precio esta bajo su media de 50 dias y "
                "la media de 20 va por debajo de la de 50. Barato aqui puede "
                "seguir abaratandose. Un puntaje alto en esta situacion NO es "
                "una oportunidad detectada: es una accion cayendo.")
        elif precio < sma20 and sma20 > sma50:
            puntaje += 15
            razones.append(
                "Retroceso dentro de una tendencia alcista: el precio cedio "
                "bajo su media de 20 dias, pero la media de 20 sigue sobre la "
                "de 50.")
        elif precio > sma20 > sma50:
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

    # -- 5. Liquidez --------------------------------------------------------
    monto = stats.get("montoMedioDiario30d")
    if monto is not None:
        if monto < LIQUIDEZ_MINIMA_CLP:
            puntaje *= 0.5  # la senal existe pero no se puede ejecutar limpio
            banderas.append(
                f"POCA LIQUIDEZ — transa en promedio ${monto/1e6:,.0f} millones "
                f"al dia. Entrar o salir puede moverte el precio en contra, y "
                f"el spread se come buena parte de la diferencia. El puntaje "
                f"se redujo a la mitad por esto.")
        else:
            razones.append(f"Liquidez razonable: ~${monto/1e6:,.0f} millones "
                           f"transados al dia (promedio 30 dias).")
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
        "zscore": round(z, 2) if z is not None else None,
        "rsi14": rsi,
        "distanciaPromedio": _pct(precio / stats["avg90"] - 1) if stats.get("avg90") else None,
        "volatilidadDiaria": _pct(stats.get("volDiaria")),
        "montoMedioDiario30d": monto,
        "razones": razones,
        "banderas": banderas,
        "descargo": DESCARGO,
    }


def rankear(precios, stats, stats_indice=None, minimo_liquidez=True):
    """
    Evalua todas las acciones y devuelve dos listas ordenadas.

    `candidatos_compra` NO es una lista de compras sugeridas. Es la lista de
    acciones que estan mas lejos de su propio promedio hacia abajo, con las
    banderas rojas visibles al lado de cada una para que puedas descartarlas.
    """
    evaluadas = []
    for t, p in precios.items():
        ev = evaluar(t, p, stats.get(t), stats_indice)
        if ev and ev.get("puntaje") is not None:
            evaluadas.append(ev)

    ejecutables = [e for e in evaluadas
                   if not minimo_liquidez
                   or (e["montoMedioDiario30d"] or 0) >= LIQUIDEZ_MINIMA_CLP]

    compra = sorted([e for e in ejecutables if e["puntaje"] >= 20],
                    key=lambda e: -e["puntaje"])
    venta = sorted([e for e in ejecutables if e["puntaje"] <= -20],
                   key=lambda e: e["puntaje"])

    return {
        "candidatos_compra": compra,
        "candidatos_venta": venta,
        "evaluadas": len(evaluadas),
        "filtradas_por_liquidez": len(evaluadas) - len(ejecutables),
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
