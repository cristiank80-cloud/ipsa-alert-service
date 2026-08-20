"""
Fuerza Relativa/Absoluta (diario) y Fases de Weinstein (semanal).

Reimplementacion en Python puro de los dos indicadores Pine Script de la
clase de Inversapiens ("Fuerza Relativa y Absoluta -- Diario" y "Weinstein
-- Fases y Confirmaciones"), para mostrarlos dentro de la app en vez de
tener que abrir TradingView aparte.

Estos dos modulos son PURAS FUNCIONES DE CALCULO: reciben series de precios
ya descargadas (mismo formato que el resto del backend: listas de
{"date": "YYYY-MM-DD", "close": float, "volume": float|None}, ordenadas de
mas antiguo a mas nuevo) y devuelven un diccionario con el diagnostico. No
hacen ninguna peticion de red -- eso lo hace server.py, reusando las mismas
funciones de data_source.py que ya usa el resto de la app.

APROXIMACIONES A PROPOSITO (por los datos que hay disponibles)
================================================================
* f5/f6 (maximos/minimos crecientes) del diario se miden sobre CIERRES, no
  sobre maximos/minimos intradia -- Yahoo no entrega ese historico en la
  serie que ya descarga esta app, y pedirlo aparte multiplicaria las
  peticiones. Es una aproximacion razonable, no identica al script original.
* La fuerza relativa (diario y semanal) alinea la serie de la accion con la
  del indice por FECHA (con tolerancia a feriados distintos), igual criterio
  que ya usa el frontend para superponer el IPSA/S&P 500 en el grafico.
* Las velas semanales agrupan por semana ISO (lunes a domingo) y toman el
  cierre del ULTIMO dia habil de cada semana. La semana EN CURSO se excluye
  siempre -- el diagnostico semanal solo mira semanas ya cerradas, para que
  nunca "repinte" a mitad de semana (ver DIFERENCIA #5 mas abajo).

DIFERENCIAS A PROPOSITO RESPECTO DE LOS SCRIPTS .pine ORIGINALES
====================================================================
Se corrigieron 5 fallas reales encontradas al revisar los dos scripts (ver
conversacion con Cristian, agosto 2026):

  1. FASE 3 (semanal) se disparaba con un solo cierre bajo la media de 30
     semanas aunque la media SIGUIERA SUBIENDO -- Weinstein tolera
     retrocesos dentro de una tendencia intacta; eso no es un techo. Aca la
     fase solo baja a "techo" cuando la media misma deja de subir.
  2. El veredicto de confirmaciones ("candidata valida") no dependia de
     estar en fase 2 -- se podia leer "FASE 1 · no compramos" junto a
     "CANDIDATA VALIDA" al mismo tiempo. Aca el veredicto exige fase 2.
  3. El caso "RECUPERANDO" (diario) no tenia condicion de apagado: una vez
     que entraba, se quedaba pintado para siempre. Aca se apaga solo si no
     hubo una recuperacion de la SMA50 en los ultimos 10 dias.
  4. Con "divergencia solo en tendencia" (el comportamiento por defecto del
     script original), una accion bajo su media larga Y su media corta no
     caia en NINGUN caso ("sin zona", sin ningun aviso) -- el peor
     escenario quedaba sin pintar. Aca ese caso es DECLIVE, evaluado primero.
  5. Repintado intrabar/intrasemana: el script original evalua sobre la
     vela en curso (se puede leer "VENDER TODO" a media manana y que se
     deshaga al cierre). Aca TODO se calcula sobre cierres diarios ya
     confirmados por Yahoo y semanas ya cerradas -- nunca sobre un dato a
     medio hacer. Si el ultimo cierre diario disponible es el de HOY y el
     mercado sigue abierto, se avisa con `velaEnCurso`.

El volumen de la confirmacion semanal #5 tambien se endurecio de 0.8x a
1.3x el promedio (ver conversacion: con 0.8x esa confirmacion casi nunca
fallaba, restando sentido al puntaje). El umbral viejo se conserva aparte
como `volumenNoSeco`, informativo, no puntua.

Todo lo demas -- el score de 8 senales del diario, los umbrales de
sobreextension (5%), los 5 puntos de agotamiento -- se dejo IGUAL al
original a proposito: es lo que Cristian ya sabe leer en TradingView.
Cambiar la metodologia de fondo (por ejemplo, que 5 de las 8 senales del
diario estan correlacionadas) es una decision aparte de portar el
indicador, y no se tocó aca.
"""
from collections import deque
from datetime import datetime, timezone


# ----------------------------------------------------------------------------
# Utilidades de series (equivalentes a ta.sma / ta.ema / ta.rsi / ta.highest
# / ta.lowest de Pine, pero calculadas de una vez sobre un arreglo completo)
# ----------------------------------------------------------------------------

def _sma_serie(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema_serie(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    seed = sum(vals[:n]) / n
    out[n - 1] = seed
    k = 2.0 / (n + 1)
    prev = seed
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_serie(vals, periodo=14):
    """RSI de Wilder, serie completa. None mientras no hay suficiente historial."""
    out = [None] * len(vals)
    if len(vals) < periodo + 1:
        return out
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    ganancias = [d if d > 0 else 0.0 for d in deltas[:periodo]]
    perdidas = [-d if d < 0 else 0.0 for d in deltas[:periodo]]
    avg_g = sum(ganancias) / periodo
    avg_p = sum(perdidas) / periodo
    out[periodo] = 100.0 if avg_p == 0 else round(100 - (100 / (1 + avg_g / avg_p)), 2)
    for i in range(periodo, len(deltas)):
        d = deltas[i]
        g = d if d > 0 else 0.0
        p = -d if d < 0 else 0.0
        avg_g = (avg_g * (periodo - 1) + g) / periodo
        avg_p = (avg_p * (periodo - 1) + p) / periodo
        out[i + 1] = 100.0 if avg_p == 0 else round(100 - (100 / (1 + avg_g / avg_p)), 2)
    return out


def _rolling_extremo(vals, n, maximo=True):
    """ta.highest/ta.lowest: extremo de una ventana de n barras terminando
    en cada indice (inclusive), via una deque monotona -- O(len(vals))."""
    out = [None] * len(vals)
    dq = deque()
    for i, v in enumerate(vals):
        while dq and (vals[dq[-1]] <= v if maximo else vals[dq[-1]] >= v):
            dq.pop()
        dq.append(i)
        while dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = vals[dq[0]]
    return out


def _alinear_indice_por_fecha(fechas, puntos_indice):
    """Para cada fecha de la accion, el cierre del indice mas reciente que
    no sea posterior a esa fecha (mismo criterio que usa el frontend para
    superponer el IPSA/S&P 500 en el grafico -- ver alinearIndicePorFecha
    en index.html). None donde todavia no hay dato del indice."""
    if not puntos_indice:
        return [None] * len(fechas)
    j = 0
    out = []
    for f in fechas:
        while j + 1 < len(puntos_indice) and puntos_indice[j + 1]["date"] <= f:
            j += 1
        out.append(puntos_indice[j]["close"] if puntos_indice[j]["date"] <= f else None)
    return out


# ----------------------------------------------------------------------------
# DIARIO -- Fuerza Relativa y Absoluta
# ----------------------------------------------------------------------------

DIAS_MIN_DIARIO = 190  # 150 (SMA larga) + 20 (pendiente RSI) + 10 (barrasDiv) + margen

_LEN_LARGA_D = 150
_LEN_CORTA_D = 50
_LEN_RAPID_D = 21
_BARRAS_PEND_D = 10
_BARRAS_ESTR_D = 10
_BARRAS_RSI_D = 20
_LOOK_RS_PEND = 21
_LOOK_RS_LID = 63
_UMBRAL_EXT = 5.0
_VENTANA_RECUPERANDO = 10


def evaluar_diario(puntos, puntos_indice=None):
    """
    puntos: serie diaria de LA ACCION, [{"date","close",...}, ...], de mas
    antigua a mas nueva.
    puntos_indice: misma forma, para el indice de referencia (SPY/S&P 500
    en EE.UU.; para Chile, ver ipsa_historico.obtener_serie_combinada()).
    Puede ser None -- la fuerza relativa queda no disponible, el resto del
    diagnostico sigue funcionando igual.
    """
    if len(puntos) < DIAS_MIN_DIARIO:
        return {
            "disponible": False,
            "motivo": f"Solo hay {len(puntos)} dias de historial; hacen falta "
                      f"al menos {DIAS_MIN_DIARIO} (150 para la media larga, "
                      f"mas margen) para que el diagnostico diario signifique algo.",
        }

    fechas = [p["date"] for p in puntos]
    cierres = [p["close"] for p in puntos]
    n = len(cierres)

    sma_larga = _sma_serie(cierres, _LEN_LARGA_D)
    sma_corta = _sma_serie(cierres, _LEN_CORTA_D)
    ema_rapid = _ema_serie(cierres, _LEN_RAPID_D)
    rsi = _rsi_serie(cierres, 14)
    max10 = _rolling_extremo(cierres, _BARRAS_ESTR_D, maximo=True)
    min10 = _rolling_extremo(cierres, _BARRAS_ESTR_D, maximo=False)

    def _score_en(i):
        """Las 8 senales de fuerza absoluta en el indice i, o None si a esa
        altura de la serie todavia falta algun insumo."""
        if i < _LEN_LARGA_D - 1 + _BARRAS_PEND_D or i < _BARRAS_RSI_D:
            return None
        if sma_larga[i] is None or sma_larga[i - _BARRAS_PEND_D] is None:
            return None
        if sma_corta[i] is None or sma_corta[i - _BARRAS_PEND_D] is None:
            return None
        if rsi[i] is None or rsi[i - _BARRAS_RSI_D] is None:
            return None
        if max10[i] is None or max10[i - _BARRAS_ESTR_D] is None:
            return None
        if min10[i] is None or min10[i - _BARRAS_ESTR_D] is None:
            return None
        f1 = cierres[i] > sma_larga[i]
        f2 = sma_corta[i] > sma_larga[i]
        f3 = sma_larga[i] > sma_larga[i - _BARRAS_PEND_D]
        f4 = sma_corta[i] > sma_corta[i - _BARRAS_PEND_D]
        f5 = max10[i] > max10[i - _BARRAS_ESTR_D]
        f6 = min10[i] > min10[i - _BARRAS_ESTR_D]
        f7 = rsi[i] > rsi[i - _BARRAS_RSI_D]
        f8 = cierres[i] > sma_corta[i]
        return {"f1": f1, "f2": f2, "f3": f3, "f4": f4, "f5": f5, "f6": f6,
                "f7": f7, "f8": f8,
                "score": sum([f1, f2, f3, f4, f5, f6, f7, f8])}

    # ---- Serie completa de "caso" por dia -----------------------------------
    # Igual calculo que el bloque "HOY" de mas abajo, pero repetido para CADA
    # dia con suficiente historial -- no solo el ultimo. Esto es lo que deja
    # pintar el fondo del grafico igual que bgcolor() en el script .pine
    # (ver /diagnostico y renderHistoryChart() en el frontend), en vez de
    # solo poder mostrar el diagnostico del dia de hoy.
    serie_casos = []
    for i in range(n):
        ch = _score_en(i)
        if ch is None:
            continue
        ch_prev = _score_en(i - _BARRAS_ESTR_D) if i - _BARRAS_ESTR_D >= 0 else None
        fc_i = ch_prev is not None and ch["score"] < ch_prev["score"]
        sc_i, sl_i = sma_corta[i], sma_larga[i]
        pcorta_i = cierres[i] < sc_i
        plarga_i = cierres[i] < sl_i
        mcruz_i = sc_i < sl_i
        de_i, sobreext_i = None, False
        if ema_rapid[i]:
            de_i = (cierres[i] - ema_rapid[i]) / ema_rapid[i] * 100
            sobreext_i = de_i > _UMBRAL_EXT
        oport_i = (ch["score"] >= 6 and not mcruz_i and not plarga_i
                   and not pcorta_i and not sobreext_i)
        recup_i = False
        for k in range(max(0, i - _VENTANA_RECUPERANDO + 1), i + 1):
            if k == 0 or sma_corta[k] is None or sma_corta[k - 1] is None:
                continue
            if not (cierres[k] > sma_corta[k] and cierres[k - 1] <= sma_corta[k - 1]):
                continue
            chk = _score_en(k)
            chk_prev = _score_en(k - _BARRAS_ESTR_D) if k - _BARRAS_ESTR_D >= 0 else None
            cae_k = chk is not None and chk_prev is not None and chk["score"] < chk_prev["score"]
            if not cae_k:
                recup_i = True
                break
        if plarga_i:
            caso_i = "DECLIVE"
        elif pcorta_i and fc_i:
            caso_i = "DIVERGENCIA"
        elif sobreext_i:
            caso_i = "SOBREEXTENDIDA"
        elif oport_i:
            caso_i = "OPORTUNIDAD"
        elif recup_i and not oport_i:
            caso_i = "RECUPERANDO"
        else:
            caso_i = None
        serie_casos.append({"date": fechas[i], "caso": caso_i})

    hoy = n - 1
    checks_hoy = _score_en(hoy)
    if checks_hoy is None:
        return {"disponible": False,
                "motivo": "No hay suficiente historial continuo para calcular "
                          "las 8 senales en el dato mas reciente."}
    score_hoy = checks_hoy["score"]

    checks_hace10 = _score_en(hoy - _BARRAS_ESTR_D)
    fuerza_cae = (checks_hace10 is not None and score_hoy < checks_hace10["score"])

    lectura_abs = ("SANA Y CON FUERZA" if score_hoy >= 6 else
                    "DEBILITANDOSE" if score_hoy >= 3 else "DEBIL")

    # ---- Fuerza relativa vs el indice --------------------------------------
    rs_disponible = False
    rs_pendiente_positiva = False
    rs_lider = False
    if puntos_indice:
        alineado = _alinear_indice_por_fecha(fechas, puntos_indice)
        rs = [(cierres[i] / alineado[i]) if alineado[i] else None for i in range(n)]
        # Solo se usa si el tramo final esta bien alineado (sin huecos grandes
        # por feriados de un solo mercado) -- exige que las ultimas 63 barras
        # (~3 meses) tengan RS calculable.
        cola = rs[-_LOOK_RS_LID:]
        if len(cola) == _LOOK_RS_LID and all(v is not None for v in cola):
            rs_disponible = True
            rs_hoy = cola[-1]
            rs_hace21 = rs[-1 - _LOOK_RS_PEND] if n > _LOOK_RS_PEND and rs[-1 - _LOOK_RS_PEND] is not None else None
            rs_pendiente_positiva = rs_hace21 is not None and rs_hoy > rs_hace21
            rs_lider = rs_hoy >= max(cola) * 0.999

    lectura_rel = ("LIDER" if rs_lider else
                    "GANANDO TERRENO" if rs_pendiente_positiva else
                    "SE QUEDA ATRAS" if rs_disponible else "SIN DATO DE INDICE")

    # ---- Sobreextension -----------------------------------------------------
    dist_ema = None
    sobreext = False
    if ema_rapid[hoy]:
        dist_ema = (cierres[hoy] - ema_rapid[hoy]) / ema_rapid[hoy] * 100
        sobreext = dist_ema > _UMBRAL_EXT

    # ---- Los dos escalones de stop ------------------------------------------
    sma_corta_hoy, sma_larga_hoy = sma_corta[hoy], sma_larga[hoy]
    perdio_corta = cierres[hoy] < sma_corta_hoy
    perdio_larga = cierres[hoy] < sma_larga_hoy
    medias_cruzadas = sma_corta_hoy < sma_larga_hoy
    bajo_ref = perdio_corta

    # ---- RECUPERANDO -- FIX #3: se apaga solo (barssince <= 10) -----------
    recuperando_reciente = False
    for i in range(max(0, n - _VENTANA_RECUPERANDO), n):
        if i == 0 or sma_corta[i] is None or sma_corta[i - 1] is None:
            continue
        cruzo = cierres[i] > sma_corta[i] and cierres[i - 1] <= sma_corta[i - 1]
        if not cruzo:
            continue
        ch = _score_en(i)
        ch_prev = _score_en(i - _BARRAS_ESTR_D)
        cae_en_ese_momento = ch is not None and ch_prev is not None and ch["score"] < ch_prev["score"]
        if not cae_en_ese_momento:
            recuperando_reciente = True
            break

    oportunidad = (score_hoy >= 6 and not medias_cruzadas and not perdio_larga
                   and not perdio_corta and not sobreext)

    # ---- Los casos -- FIX #4: DECLIVE explicito, evaluado primero ---------
    if perdio_larga:
        caso = "DECLIVE"
        caso_texto = "DECLIVE · bajo las dos medias, fuera de la tendencia"
    elif bajo_ref and fuerza_cae:
        caso = "DIVERGENCIA"
        caso_texto = "DIVERGENCIA · esperar"
    elif sobreext:
        caso = "SOBREEXTENDIDA"
        caso_texto = "SOBREEXTENDIDA · esperar"
    elif oportunidad:
        caso = "OPORTUNIDAD"
        caso_texto = "OPORTUNIDAD · comprar"
    elif recuperando_reciente and not oportunidad:
        caso = "RECUPERANDO"
        caso_texto = "RECUPERANDO · vigilar"
    else:
        caso = None
        caso_texto = "sin caso · en seguimiento"

    # ---- La decision sobre la liquidez disponible (igual que el original) --
    if perdio_larga:
        decision, regla = "VENDER TODO y reemplazar", 1
    elif perdio_corta:
        decision, regla = "VENDER LA MITAD de lo que quede", 2
    elif score_hoy <= 2:
        decision, regla = "REDUCIR · vigilar las medias", 3
    elif sobreext:
        decision, regla = "ESPERAR UNA CONSOLIDACION · no aportar", 4
    elif score_hoy <= 5:
        decision, regla = "MANTENER · no aportar", 5
    elif medias_cruzadas:
        decision, regla = "NO COMPRAR · SMA50 bajo SMA150", 6
    else:
        decision, regla = "COMPRAR · sana y sobre la SMA50", 7

    hoy_utc = datetime.now(timezone.utc).date().isoformat()
    vela_en_curso = fechas[hoy] == hoy_utc

    return {
        "disponible": True,
        "fecha": fechas[hoy],
        "velaEnCurso": vela_en_curso,
        "precio": cierres[hoy],
        "score": score_hoy,
        "checks": {k: v for k, v in checks_hoy.items() if k != "score"},
        "lecturaAbsoluta": lectura_abs,
        "fuerzaCae": fuerza_cae,
        "fuerzaRelativa": {
            "disponible": rs_disponible,
            "lectura": lectura_rel,
            "pendientePositiva": rs_pendiente_positiva,
            "lider": rs_lider,
        },
        "distanciaEMA21Pct": round(dist_ema, 2) if dist_ema is not None else None,
        "sobreextendida": sobreext,
        "stops": {
            "sma50": round(sma_corta_hoy, 4),
            "sma150": round(sma_larga_hoy, 4),
            "perdioSMA50": perdio_corta,
            "perdioSMA150": perdio_larga,
            "mediasCruzadas": medias_cruzadas,
        },
        "caso": caso,
        "casoTexto": caso_texto,
        "decision": decision,
        "regla": regla,
        "serieCasos": serie_casos,
    }


# ----------------------------------------------------------------------------
# SEMANAL -- Weinstein: Fases y Confirmaciones
# ----------------------------------------------------------------------------

SEMANAS_MIN = 95  # 30 (media) + 30 (ruptura de 30 sem) + margen para que la
                   # "memoria" de fase converja antes de la semana de hoy

_LEN_MEDIA_S = 30
_BARRAS_PEND_S = 5
_UMBRAL_PEND_S = 0.5
_LOOK_RS_S = 13
_LOOK_MAX_S = 30
_VIGENCIA_S = 8
_LEN_VOL_S = 30
_VOL_MULT_CONFIRMA = 1.3   # FIX: antes 0.8 -- ver cabecera del archivo
_VOL_MULT_SOSTENIDO = 0.8  # el umbral viejo, ahora solo informativo


def _agrupar_semanas(puntos):
    """Agrupa una serie diaria en velas semanales (semana ISO, lunes a
    domingo): cierre = el del ULTIMO dia habil de la semana, volumen = suma
    de la semana. La semana EN CURSO (todavia no cerrada) se descarta
    siempre -- ver DIFERENCIA #5 en la cabecera del archivo."""
    if not puntos:
        return []
    grupos, orden = {}, []
    for p in puntos:
        d = datetime.strptime(p["date"][:10], "%Y-%m-%d").date()
        clave = d.isocalendar()[:2]
        if clave not in grupos:
            grupos[clave] = []
            orden.append(clave)
        grupos[clave].append(p)

    hoy = datetime.now(timezone.utc).date()
    clave_hoy = hoy.isocalendar()[:2]

    semanas = []
    for clave in orden:
        if clave == clave_hoy:
            continue
        fila = grupos[clave]
        volumenes = [f["volume"] for f in fila if f.get("volume") is not None]
        semanas.append({
            "date": fila[-1]["date"],
            "close": fila[-1]["close"],
            "volume": sum(volumenes) if volumenes else None,
        })
    return semanas


def evaluar_semanal(puntos, puntos_indice=None):
    """
    puntos: serie DIARIA de la accion (se resamplea aca a semanal). Se pide
    diaria y no semanal directamente porque es la misma serie que ya usan
    el resto de las funciones del backend -- una sola descarga sirve para
    todo. puntos_indice: igual, diaria, del indice de referencia.
    """
    semanas = _agrupar_semanas(puntos)
    if len(semanas) < SEMANAS_MIN:
        return {
            "disponible": False,
            "motivo": f"Solo hay {len(semanas)} semanas cerradas de historial; "
                      f"hacen falta al menos {SEMANAS_MIN} para que la fase y "
                      f"las confirmaciones signifiquen algo.",
        }

    cierres = [s["close"] for s in semanas]
    volumenes = [s["volume"] for s in semanas]
    fechas = [s["date"] for s in semanas]
    n = len(cierres)

    media = _sma_serie(cierres, _LEN_MEDIA_S)
    pend_pct = [None] * n
    for i in range(_BARRAS_PEND_S, n):
        if media[i] is not None and media[i - _BARRAS_PEND_S]:
            pend_pct[i] = (media[i] - media[i - _BARRAS_PEND_S]) / media[i - _BARRAS_PEND_S] * 100
    subiendo = [(v is not None and v > _UMBRAL_PEND_S) for v in pend_pct]
    bajando = [(v is not None and v < -_UMBRAL_PEND_S) for v in pend_pct]

    # ---- Maquina de estados de la fase, con memoria del ciclo --------------
    # FIX #1: un retroceso dentro de fase 2 NO degrada a techo mientras la
    # media siga subiendo (rama nueva antes del "else" generico).
    fases = [0] * n
    fase = 0
    for i in range(n):
        if media[i] is None:
            fase = 0
        else:
            arriba = cierres[i] > media[i]
            if arriba and subiendo[i]:
                fase = 2
            elif (not arriba) and bajando[i]:
                fase = 4
            elif fase == 2 and subiendo[i]:
                fase = 2  # retroceso dentro de tendencia intacta -- no es techo
            else:
                if fase in (2, 3):
                    fase = 3
                elif fase in (4, 1):
                    fase = 1
                else:
                    fase = 1 if arriba else 4
        fases[i] = fase

    NOMBRES = {0: "SIN DATO", 1: "BASE", 2: "AVANCE", 3: "TECHO", 4: "DECLIVE"}
    ACCIONES = {0: "faltan semanas", 1: "no compramos", 2: "aqui y solo aqui",
                3: "preparar salida", 4: "jamas"}

    hoy = n - 1
    fase_hoy = fases[hoy]

    # ---- Fuerza relativa semanal vs el indice ------------------------------
    rs_up = rs_dn = False
    if puntos_indice:
        semanas_indice = _agrupar_semanas(puntos_indice)
        alineado = _alinear_indice_por_fecha(fechas, semanas_indice)
        rs = [(cierres[i] / alineado[i]) if alineado[i] else None for i in range(n)]
        if n > _LOOK_RS_S and rs[hoy] is not None and rs[hoy - _LOOK_RS_S] is not None:
            rs_up = rs[hoy] > rs[hoy - _LOOK_RS_S]
            rs_dn = rs[hoy] < rs[hoy - _LOOK_RS_S]

    # ---- Las 5 confirmaciones de fase 2 -------------------------------------
    c1 = cierres[hoy] > media[hoy] if media[hoy] is not None else False
    c2 = pend_pct[hoy] is not None and pend_pct[hoy] > 0

    max30 = _rolling_extremo(cierres, _LOOK_MAX_S, maximo=True)
    brk = [False] * n
    for i in range(1, n):
        if max30[i - 1] is not None:
            brk[i] = cierres[i] > max30[i - 1]
    desde_brk = None
    for atras in range(0, min(_VIGENCIA_S + 1, hoy + 1)):
        if brk[hoy - atras]:
            desde_brk = atras
            break
    c3 = desde_brk is not None and desde_brk <= _VIGENCIA_S

    c4 = rs_up

    vol_prom = _sma_serie([v if v is not None else 0.0 for v in volumenes], _LEN_VOL_S)
    vol_hoy, vol_prom_hoy = volumenes[hoy], vol_prom[hoy]
    c5 = bool(vol_hoy is not None and vol_prom_hoy and vol_hoy > vol_prom_hoy * _VOL_MULT_CONFIRMA)
    volumen_no_seco = bool(vol_hoy is not None and vol_prom_hoy and vol_hoy > vol_prom_hoy * _VOL_MULT_SOSTENIDO)
    volumen_disponible = vol_hoy is not None and vol_prom_hoy is not None

    score_conf = sum([c1, c2, c3, c4, c5])

    # ---- FIX #2: el veredicto exige estar en fase 2 -------------------------
    if fase_hoy != 2:
        veredicto = f"NO CALIFICA (fase {fase_hoy} · {NOMBRES[fase_hoy]})"
    elif score_conf >= 4:
        veredicto = "CANDIDATA VALIDA"
    elif score_conf == 3:
        veredicto = "NO ENTRA TODAVIA"
    else:
        veredicto = "ESPERAR"

    # ---- Las 5 senales de agotamiento (techo) -------------------------------
    a1 = not subiendo[hoy]
    max8 = _rolling_extremo(cierres, 8, maximo=True)
    a2 = (hoy >= 8 and max8[hoy] is not None and max8[hoy - 8] is not None
          and max8[hoy] <= max8[hoy - 8])
    a3 = rs_dn
    a4 = media[hoy] is not None and cierres[hoy] < media[hoy]
    a5 = False
    if volumen_disponible:
        ultimas10 = list(range(max(0, hoy - 9), hoy + 1))
        vol_bajistas = [volumenes[i] for i in ultimas10 if i > 0 and volumenes[i] is not None and cierres[i] < cierres[i - 1]]
        vol_alcistas = [volumenes[i] for i in ultimas10 if i > 0 and volumenes[i] is not None and cierres[i] > cierres[i - 1]]
        if vol_bajistas and vol_alcistas:
            a5 = (sum(vol_bajistas) / len(vol_bajistas)) > (sum(vol_alcistas) / len(vol_alcistas))

    score_agotamiento = sum([a1, a2, a3, a4, a5])
    techo_confirmado = fase_hoy == 3 and score_agotamiento >= 4

    return {
        "disponible": True,
        "semanaCerradaHasta": fechas[hoy],
        "semanasDeHistorial": n,
        "precio": cierres[hoy],
        "fase": fase_hoy,
        "nombreFase": NOMBRES[fase_hoy],
        "accionFase": ACCIONES[fase_hoy],
        "confirmaciones": {"c1_precioSobreMedia": c1, "c2_pendientePositiva": c2,
                            "c3_rompioMaximo30sem": c3, "c4_fuerzaRelativa": c4,
                            "c5_volumen": c5},
        "volumenNoSeco": volumen_no_seco,
        "volumenDisponible": volumen_disponible,
        "scoreConfirmaciones": score_conf,
        "veredicto": veredicto,
        "agotamiento": {"a1_mediaDejoDeSubir": a1, "a2_sinMaximosNuevos": a2,
                         "a3_pierdeFuerzaRelativa": a3, "a4_cierraBajoMedia": a4,
                         "a5_distribucion": a5},
        "scoreAgotamiento": score_agotamiento,
        "techoConfirmado": techo_confirmado,
        # Fase de CADA semana con dato (no solo la de hoy) -- el array
        # 'fases' ya se calcula completo mas arriba para que la maquina de
        # estados tenga memoria; se expone tal cual para pintar el fondo del
        # grafico igual que bgcolor() en el .pine (0 = sin dato, se omite).
        "serieFases": [{"date": fechas[i], "fase": fases[i]} for i in range(n) if fases[i] != 0],
    }
