# -*- coding: utf-8 -*-
"""
Ficha fundamental de la clase "Acciones USA" (Inversapiens), punto 3.
30-oct-2026.

QUE CALCULA
===========
Las cinco preguntas de la clase, con los cortes que acepto Cristian:

  1. ¿Crece de verdad?  ventas y BPA de cada trimestre contra el mismo del
     año anterior.  Si: los dos >= 25 % y ninguno frena dos trimestres
     seguidos.  A medias: >= 25 % pero frenando, o uno >= 25 % y el otro
     entre 15 y 25 %.  No: lo demas.
  2. ¿Gana plata con lo que vende?  margen bruto y operacional del ultimo
     año fiscal contra 3 años antes.  Si: los dos estables (+-2 puntos) o
     subiendo.  A medias: uno si y el otro no.  No: los dos cayendo.
  3. ¿La utilidad se convierte en caja?  flujo libre (operacional - capex,
     ultimos 12 meses) / utilidad.  Si: >= 80 % con flujo operacional
     positivo.  A medias: 50-80 %.  No: < 50 % o flujo libre negativo.
  4. ¿Aguanta un mal año?  deuda total / flujo libre.  Si: < 3 años o sin
     deuda.  A medias: 3-5.  No: > 5, o deuda con flujo libre negativo.
  5. ¿Usa bien el capital?  ROIC = utilidad operacional despues de impuestos
     / (deuda + patrimonio - caja).  Si: > 15 %.  A medias: 10-15 %.  No: < 10 %.

Mas la FIRMA DE CAJA (signos de los flujos operacional, de inversion y de
financiamiento, ultimos 12 meses) y el PUNTAJE (Si = 1, A medias = 1/2,
No = 0): 4 o mas = calidad, 3 a 3,5 = vigilar, menos de 3 = fuera.

Decisiones de Cristian: la firma mala SOLO MARCA (no saca a nadie); el
puntaje bajo 3 manda la accion a "Fuera por la ficha" en Explorar, que se
sigue viendo (en una primera alerta lo peor es esconder un nombre).

DE DONDE SALEN LOS NUMEROS
==========================
1. La SEC (data.sec.gov), fuente principal. Endpoint "companyconcept": un
   JSON chico por concepto contable, en vez del "companyfacts" completo de
   la empresa (varios MB, y Render gratis tiene 512 MB de memoria). Pide
   identificarse: variable de entorno SEC_USER_AGENT = "Nombre correo".
   Sin ella NO se le pide nada a la SEC (contesta 403 y puede bloquear la
   IP) y todo sale de TradingView.
2. TradingView, respaldo para lo que la SEC no trae (extranjeras que
   reportan con 20-F sin trimestrales, bancos sin margen bruto, etc.). Solo
   tiene los numeros del momento, sin historia: con el respaldo, la
   pregunta 1 no puede ver si frena, y la 2 queda sin dato.
3. Yahoo (assetProfile), solo para la descripcion de la empresa cuando la
   app no la tiene escrita.

NUNCA SE INVENTA UN NUMERO. Una pregunta sin dato queda "sin dato" y no
suma. Una ficha incompleta no saca a nadie de la lista, salvo que ni con
"Si" en todo lo que falta llegaria a 3 (entonces ya esta fuera).

COMO CORRE
==========
Bajar una ficha son ~20-30 peticiones chicas a la SEC (unos 5 s). Eso no
puede pasar dentro de una peticion de la app: el servidor tiene UN worker.
Por eso hay una COLA con un solo hilo: la app pide, recibe lo que ya este
calculado y la lista de pendientes, y vuelve a preguntar. Cache de 7 dias
en memoria y en disco (los estados financieros cambian una vez por
trimestre); un fallo se recuerda 6 horas.
"""
from collections import deque
from datetime import datetime, date, timedelta, timezone
import json
import os
import re
import threading
import time

import requests

SEC_USER_AGENT = (os.environ.get("SEC_USER_AGENT") or "").strip()
FICHAS_FILE = os.environ.get("FICHAS_FILE", "fichas.json")
TTL_OK = 7 * 86400
TTL_FALLO = 6 * 3600
_SEC_PAUSA = 0.2           # 5 peticiones por segundo; la SEC permite 10
_TIMEOUT = 20

SIN_PERFIL = set()        # server.py la llena con las acciones que sigue la app

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_CONCEPTO_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{tax}/{tag}.json"
_EDGAR_URL = "https://www.sec.gov/edgar/browse/?CIK={cik}"

# Conceptos y etiquetas candidatas, en orden de prioridad. US GAAP primero,
# IFRS despues (Novo Nordisk, AstraZeneca, TSMC, UBS reportan en IFRS).
CONCEPTOS = {
    "ventas": ["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues",
               "us-gaap:SalesRevenueNet", "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
               "us-gaap:RevenuesNetOfInterestExpense", "ifrs-full:Revenue"],
    "bpa": ["us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted",
            "ifrs-full:DilutedEarningsLossPerShare"],
    "utilidad": ["us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent",
                 "ifrs-full:ProfitLoss"],
    "bruta": ["us-gaap:GrossProfit", "ifrs-full:GrossProfit"],
    "costo": ["us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsAndServicesSold", "us-gaap:CostOfGoodsSold",
              "ifrs-full:CostOfSales"],
    "operacional": ["us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities"],
    "cfo": ["us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "ifrs-full:CashFlowsFromUsedInOperatingActivities"],
    "cfi": ["us-gaap:NetCashProvidedByUsedInInvestingActivities",
            "us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
            "ifrs-full:CashFlowsFromUsedInInvestingActivities"],
    "cff": ["us-gaap:NetCashProvidedByUsedInFinancingActivities",
            "us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
            "ifrs-full:CashFlowsFromUsedInFinancingActivities"],
    "capex": ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets",
              "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
              "ifrs-full:PurchaseOfPropertyPlantAndEquipment"],
    "impuesto": ["us-gaap:IncomeTaxExpenseBenefit", "ifrs-full:IncomeTaxExpenseContinuingOperations"],
    "antesImpuesto": ["us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                      "ifrs-full:ProfitLossBeforeTax"],
    # Balance (instantes)
    "deudaLP": ["us-gaap:LongTermDebt", "us-gaap:LongTermDebtAndCapitalLeaseObligations",
                "us-gaap:SeniorNotes", "us-gaap:ConvertibleNotesPayable", "us-gaap:LongTermNotesPayable"],
    "deudaLPnc": ["us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligationsNoncurrent",
                  "ifrs-full:NoncurrentPortionOfNoncurrentBorrowings", "ifrs-full:NoncurrentBorrowings"],
    "deudaLPc": ["us-gaap:LongTermDebtCurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
                 "ifrs-full:CurrentPortionOfNoncurrentBorrowings", "ifrs-full:CurrentBorrowings"],
    "deudaCP": ["us-gaap:ShortTermBorrowings", "us-gaap:CommercialPaper"],
    "patrimonio": ["us-gaap:StockholdersEquity",
                   "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                   "ifrs-full:EquityAttributableToOwnersOfParent", "ifrs-full:Equity"],
    "caja": ["us-gaap:CashAndCashEquivalentsAtCarryingValue",
             "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             "ifrs-full:CashAndCashEquivalents"],
}
INSTANTES = {"deudaLP", "deudaLPnc", "deudaLPc", "deudaCP", "patrimonio", "caja"}
FORMULARIOS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}

PREGUNTAS = [
    "¿Crece de verdad?",
    "¿Gana plata con lo que vende?",
    "¿La utilidad se convierte en caja?",
    "¿Aguanta un mal año?",
    "¿Usa bien el capital?",
]
REGLAS = [
    "Ventas y BPA sobre 25 % y sin frenar dos trimestres seguidos",
    "Margen bruto y operacional estables o subiendo en 3 años",
    "Flujo libre ≥ 80 % de la utilidad, generado por la operación",
    "Deuda total menor que 3 años de flujo libre",
    "ROIC sobre 15 %",
]
PUNTOS = {"si": 1.0, "medias": 0.5, "no": 0.0}


# ---------------------------------------------------------------------------
# Peticiones a la SEC
# ---------------------------------------------------------------------------
_sec_lock = threading.Lock()
_sec_ultima = [0.0]
_diag = {"secPeticiones": 0, "secErrores": 0, "ultimoErrorSec": None,
         "tvColumnas": None, "ultimoErrorTv": None, "cola": 0, "calculadas": 0}


class SinSec(Exception):
    """La SEC no se puede usar (sin identificacion, sin CIK, bloqueada)."""


def _sec_get(url):
    if not SEC_USER_AGENT:
        raise SinSec("falta la variable SEC_USER_AGENT en Render")
    with _sec_lock:
        espera = _SEC_PAUSA - (time.time() - _sec_ultima[0])
        if espera > 0:
            time.sleep(espera)
        _sec_ultima[0] = time.time()
    _diag["secPeticiones"] += 1
    r = requests.get(url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
                     timeout=_TIMEOUT)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        _diag["secErrores"] += 1
        _diag["ultimoErrorSec"] = f"HTTP {r.status_code} en {url[-80:]}"
        if r.status_code in (403, 429):
            raise SinSec(f"la SEC respondió {r.status_code}")
        return None
    return r.json()


_cik_cache = {"mapa": None, "ts": 0.0}


def cik_de(ticker):
    if _cik_cache["mapa"] is None or time.time() - _cik_cache["ts"] > 86400:
        datos = _sec_get(_TICKERS_URL) or {}
        mapa = {}
        for fila in datos.values() if isinstance(datos, dict) else []:
            t = str(fila.get("ticker") or "").upper()
            if t and fila.get("cik_str"):
                mapa.setdefault(t, int(fila["cik_str"]))
        if not mapa:
            raise SinSec("no se pudo bajar la tabla de tickers de la SEC")
        _cik_cache.update(mapa=mapa, ts=time.time())
    m = _cik_cache["mapa"]
    t = ticker.upper()
    return m.get(t) or m.get(t.replace("-", ".")) or m.get(t.replace("-", ""))


# ---------------------------------------------------------------------------
# Series a partir de los hechos XBRL
# ---------------------------------------------------------------------------
def _d(iso):
    return date.fromisoformat(iso[:10])


def _hechos(json_concepto):
    """Hechos de la UNIDAD con mas datos (USD, EUR, DKK, USD/shares...). Las
    razones de la ficha no dependen de la moneda, asi que una empresa que
    reporta en coronas danesas sirve igual."""
    unidades = (json_concepto or {}).get("units") or {}
    if not unidades:
        return []
    unidad = max(unidades, key=lambda u: (u.startswith("USD"), len(unidades[u])))
    return [h for h in unidades[unidad]
            if h.get("form") in FORMULARIOS and isinstance(h.get("val"), (int, float)) and h.get("end")]


def _dedup(hechos):
    """Un valor por periodo (inicio, fin): el de la presentacion mas nueva
    (las restataciones pisan a lo original)."""
    por = {}
    for h in hechos:
        k = (h.get("start"), h["end"])
        if k not in por or (h.get("filed") or "") >= (por[k].get("filed") or ""):
            por[k] = h
    return list(por.values())


def _dias(h):
    return (_d(h["end"]) - _d(h["start"])).days + 1 if h.get("start") else 0


def trimestres(hechos):
    """[(fin, valor)] de trimestres sueltos, de mas viejo a mas nuevo.
    Toma los de 3 meses directos y deriva el resto restando acumulados del
    mismo año (los 10-Q traen el flujo de caja acumulado: 6 y 9 meses; el
    cuarto trimestre es el año menos los 9 meses)."""
    hs = [h for h in _dedup(hechos) if h.get("start")]
    q = {}
    for h in hs:
        if 80 <= _dias(h) <= 100:
            q[h["end"]] = h["val"]
    por_inicio = {}
    for h in hs:
        if 80 <= _dias(h) <= 380:
            por_inicio.setdefault(h["start"], []).append(h)
    for grupo in por_inicio.values():
        grupo.sort(key=lambda h: h["end"])
        for a, b in zip(grupo, grupo[1:]):
            if b["end"] not in q and 80 <= (_d(b["end"]) - _d(a["end"])).days <= 100:
                q[b["end"]] = b["val"] - a["val"]
    # Cuarto trimestre cuando el 10-Q solo trae los 3 meses (sin acumulado):
    # el año menos los tres trimestres que caen dentro.
    for h in hs:
        if 350 <= _dias(h) <= 380 and h["end"] not in q:
            dentro = [v for f, v in q.items() if h["start"] < f < h["end"]]
            if len(dentro) == 3:
                q[h["end"]] = h["val"] - sum(dentro)
    return sorted(q.items())


def anuales(hechos):
    """[(fin, valor)] de años fiscales (350-380 dias)."""
    return sorted((h["end"], h["val"]) for h in _dedup(hechos) if h.get("start") and 350 <= _dias(h) <= 380)


def ultimos_4(serie):
    """Los ultimos 4 trimestres SI son consecutivos (80-100 dias entre si)."""
    if len(serie) < 4:
        return None
    u = serie[-4:]
    for (f1, _), (f2, _) in zip(u, u[1:]):
        if not 80 <= (_d(f2) - _d(f1)).days <= 100:
            return None
    return u


def ttm(hechos):
    """Suma de los ultimos 12 meses: 4 trimestres consecutivos, o si la
    empresa no reporta trimestres (20-F), el ultimo año fiscal. Devuelve
    (valor, hasta, "ttm"|"anual") o None."""
    u = ultimos_4(trimestres(hechos))
    if u and (_hoy() - _d(u[-1][0])).days <= 200:
        return sum(v for _, v in u), u[-1][0], "ttm"
    a = anuales(hechos)
    if a and (_hoy() - _d(a[-1][0])).days <= 550:
        return a[-1][1], a[-1][0], "anual"
    return None


def instante(hechos, desde=None):
    """(valor, fecha) del balance mas reciente. Con `desde` (fecha ISO), uno
    mas viejo no sirve: es una etiqueta que la empresa dejo de usar, y su
    ultimo valor es de hace años."""
    hs = [h for h in _dedup(hechos) if not h.get("start")]
    if not hs:
        return None
    h = max(hs, key=lambda x: (x["end"], x.get("filed") or ""))
    if desde and h["end"] < desde:
        return None
    return h["val"], h["end"]


def _hoy():
    return date.today()


def frescos(serie, dias):
    """La serie si su ultimo dato tiene menos de `dias`; si no, vacia."""
    return serie if serie and (_hoy() - _d(serie[-1][0])).days <= dias else []


def _reciente(hechos, dias=500):
    fines = [h["end"] for h in hechos]
    return bool(fines) and (_hoy() - _d(max(fines))).days <= dias


def concepto(cik, nombre, pedir=None, taxonomia=None):
    """Hechos del concepto, juntando hasta DOS etiquetas candidatas (las
    empresas cambian de etiqueta con los años: Revenues ->
    RevenueFromContract...). La primera con datos recientes manda en los
    periodos que se repiten. `taxonomia` ("us-gaap" o "ifrs-full") evita
    pedir etiquetas de la otra: cada una que no existe es una peticion
    perdida."""
    pedir = pedir or (lambda tax, tag: _sec_get(_CONCEPTO_URL.format(cik=cik, tax=tax, tag=tag)))
    juntos, usadas = [], []
    for cand in CONCEPTOS[nombre]:
        tax, tag = cand.split(":", 1)
        if taxonomia and tax != taxonomia:
            continue
        hs = _hechos(pedir(tax, tag))
        if not hs:
            continue
        ya = {(h.get("start"), h["end"]) for h in juntos}
        juntos += [h for h in hs if (h.get("start"), h["end"]) not in ya]
        usadas.append(cand)
        if len(usadas) == 2 or (_reciente(hs) and len(hs) >= 12):
            break
    return juntos, usadas


# ---------------------------------------------------------------------------
# Las cinco preguntas
# ---------------------------------------------------------------------------
def _pct(v):
    return None if v is None else round(v * 100, 1)


def _yoy(serie):
    """Crecimiento de cada uno de los ultimos 3 trimestres contra el mismo
    trimestre del año anterior. None donde la base es <= 0 (no se puede
    medir un % sobre una perdida)."""
    if len(serie) < 7:
        return None
    por_fecha = [(_d(f), v) for f, v in serie]
    out = []
    for fin, v in por_fecha[-3:]:
        base = next((b for f, b in por_fecha if 350 <= (fin - f).days <= 380), None)
        out.append((v / base - 1) if base and base > 0 else None)
    return out


def _frena(g):
    """Frena dos trimestres seguidos: el crecimiento baja en el ultimo y
    tambien en el anterior, cada vez en mas de 1 punto (30,2 % -> 30,0 % no
    es frenar, es ruido)."""
    return g is not None and None not in g and g[2] < g[1] - 0.01 and g[1] < g[0] - 0.01


def p1_crece(ventas_q, bpa_q, ventas_a=None, bpa_a=None):
    gv, gb = _yoy(ventas_q), _yoy(bpa_q)
    nota = None
    if gv is None and ventas_a and len(ventas_a) >= 2:
        # Sin trimestres (20-F): crecimiento anual, sin poder ver si frena.
        a, b = ventas_a[-2][1], ventas_a[-1][1]
        gv = [None, None, (b / a - 1) if a > 0 else None]
        if bpa_a and len(bpa_a) >= 2 and bpa_a[-2][1] > 0:
            gb = [None, None, bpa_a[-1][1] / bpa_a[-2][1] - 1]
        nota = "Con años fiscales (no reporta trimestres): no se puede ver si frena."
    v = gv[-1] if gv else None
    b = gb[-1] if gb else None
    det = {"ventas": _pct(v), "bpa": _pct(b),
           "ventasTrim": [_pct(x) for x in gv] if gv else None, "bpaTrim": [_pct(x) for x in gb] if gb else None}
    if v is None:
        return None, det, nota or "Sin ventas trimestrales comparables."
    if b is None:
        # BPA negativo un año atras, o sin dato: se juzga solo con ventas.
        return ("medias" if v >= 0.25 else "no"), det, (nota or "BPA sin base positiva para comparar: se mira solo ventas.")
    frena = _frena(gv) or _frena(gb)
    det["frena"] = frena
    if v >= 0.25 and b >= 0.25:
        return ("medias" if frena else "si"), det, ("Frena dos trimestres seguidos." if frena else nota)
    if (v >= 0.25 and b >= 0.15) or (b >= 0.25 and v >= 0.15):
        return "medias", det, nota
    return "no", det, nota


def p2_margenes(ventas_a, bruta_a, costo_a, oper_a):
    """Margen del ultimo año fiscal contra el de 3 años antes (o el mas
    viejo disponible, minimo 2 años atras)."""
    v = dict(ventas_a or [])
    if len(v) < 3:
        return None, {}, "Menos de 3 años fiscales para comparar."
    fines = sorted(v)
    ult = fines[-1]
    ant = fines[-4] if len(fines) >= 4 else fines[0]
    años = len(fines) - 1 - fines.index(ant)
    bru = dict(bruta_a or [])
    cos = dict(costo_a or [])

    def m_bruto(f):
        if f in bru and v.get(f):
            return bru[f] / v[f]
        if f in cos and v.get(f):
            return (v[f] - cos[f]) / v[f]
        return None
    ope = dict(oper_a or [])

    def m_oper(f):
        return ope[f] / v[f] if f in ope and v.get(f) else None
    mb1, mb0, mo1, mo0 = m_bruto(ult), m_bruto(ant), m_oper(ult), m_oper(ant)
    det = {"bruto": _pct(mb1), "brutoAntes": _pct(mb0), "operacional": _pct(mo1),
           "operacionalAntes": _pct(mo0), "años": años}
    ok = []
    if mb1 is not None and mb0 is not None:
        ok.append(mb1 - mb0 >= -0.02)
    if mo1 is not None and mo0 is not None:
        ok.append(mo1 - mo0 >= -0.02)
    if not ok:
        return None, det, "Sin margen bruto ni operacional comparables."
    nota = (f"Comparado con {años} años atrás." if años != 3 else None)
    if len(ok) == 1:
        nota = ((nota + " ") if nota else "") + "Solo hay uno de los dos márgenes."
        return ("si" if ok[0] else "no"), det, nota
    return ("si" if all(ok) else "medias" if any(ok) else "no"), det, nota


def p3_caja(cfo, capex, utilidad):
    if cfo is None or utilidad is None:
        return None, {}, "Falta el flujo operacional o la utilidad."
    fcf = cfo - (capex or 0)
    det = {"flujoLibre": fcf, "utilidad": utilidad, "flujoOperacional": cfo,
           "relacion": _pct(fcf / utilidad) if utilidad > 0 else None}
    nota = None if capex is not None else "Sin capex reportado: flujo libre = flujo operacional."
    if cfo <= 0 or fcf <= 0:
        return "no", det, "Flujo libre negativo."
    if utilidad <= 0:
        return "medias", det, "Genera caja pero la utilidad es negativa."
    r = fcf / utilidad
    return ("si" if r >= 0.8 else "medias" if r >= 0.5 else "no"), det, nota


def p4_deuda(deuda, fcf):
    if deuda is None:
        return None, {}, "Sin dato de deuda."
    det = {"deuda": deuda, "flujoLibre": fcf}
    if deuda <= 0:
        return "si", dict(det, años=0), "Sin deuda financiera reportada."
    if fcf is None:
        return None, det, "Sin flujo libre."
    if fcf <= 0:
        return "no", det, "Tiene deuda y el flujo libre es negativo."
    años = deuda / fcf
    det["años"] = round(años, 1)
    return ("si" if años < 3 else "medias" if años <= 5 else "no"), det, None


def p5_roic(oper, impuesto, antes, deuda, patrimonio, caja):
    if oper is None or patrimonio is None:
        return None, {}, "Falta la utilidad operacional o el patrimonio."
    tasa = 0.21
    if impuesto is not None and antes and antes > 0:
        tasa = min(max(impuesto / antes, 0.0), 0.35)
    capital = (deuda or 0) + patrimonio - (caja or 0)
    det = {"tasaImpuesto": _pct(tasa), "capitalInvertido": capital}
    if capital <= 0:
        return None, det, "Capital invertido negativo (mucha caja o patrimonio negativo): el ROIC no se puede leer."
    roic = oper * (1 - tasa) / capital
    det["roic"] = _pct(roic)
    return ("si" if roic > 0.15 else "medias" if roic >= 0.10 else "no"), det, None


FIRMAS = {
    "+--": "Genera, invierte y devuelve plata a accionistas o bancos. Es la que se busca.",
    "+-+": "Invierte más de lo que genera y se financia para crecer.",
    "++-": "Vende activos o inversiones para pagar deuda.",
    "+++": "Genera caja, vende activos y además se financia: acumula caja.",
    "--+": "El negocio no se paga solo: lo sostienen bancos y accionistas.",
    "-+-": "Quema caja y vende activos para sobrevivir.",
    "-++": "Quema caja y vende activos para sobrevivir.",
    "---": "Quema caja por todos lados: se está comiendo su caja.",
}


def firma_caja(cfo, cfi, cff):
    if None in (cfo, cfi, cff):
        return None
    s = "".join("+" if x >= 0 else "-" for x in (cfo, cfi, cff))
    return {"signos": s.replace("-", "−"), "texto": FIRMAS[s], "mala": cfo < 0,
            "operacional": cfo, "inversion": cfi, "financiamiento": cff}


def armar_ficha(ticker, res, firma, fuente, extra=None):
    preguntas, puntaje, resp = [], 0.0, 0
    for i, (r, det, nota, fte) in enumerate(res):
        if r is not None:
            puntaje += PUNTOS[r]
            resp += 1
        preguntas.append({"n": i + 1, "pregunta": PREGUNTAS[i], "regla": REGLAS[i],
                          "resultado": r, "puntos": PUNTOS.get(r), "datos": det, "nota": nota, "fuente": fte})
    maximo = puntaje + (5 - resp)
    if resp == 5:
        veredicto = "calidad" if puntaje >= 4 else ("vigilar" if puntaje >= 3 else "fuera")
    else:
        veredicto = "fuera" if maximo < 3 else "incompleta"
    return dict({"ticker": ticker, "disponible": resp > 0, "fuente": fuente, "preguntas": preguntas,
                 "firma": firma, "puntaje": puntaje, "respondidas": resp, "maximo": maximo,
                 "veredicto": veredicto, "calculado": datetime.now(timezone.utc).isoformat()}, **(extra or {}))


def ficha_desde_sec(ticker, cik, pedir=None):
    """La ficha con los datos de la SEC. Los None de cada pregunta los llena
    despues el respaldo de TradingView. ~20 peticiones chicas en el caso
    normal: las etiquetas alternativas se piden solo si falta la principal."""
    c = {}

    def traer(nombre, tax=None):
        c[nombre] = concepto(cik, nombre, pedir, tax)
        return c[nombre][0]

    # La utilidad decide la taxonomia (us-gaap o ifrs-full) para el resto.
    traer("utilidad")
    usadas = c["utilidad"][1]
    tax = usadas[0].split(":")[0] if usadas else None
    for k in ("ventas", "bpa", "operacional", "cfo", "cfi", "cff", "capex", "impuesto",
              "antesImpuesto", "patrimonio", "caja"):
        traer(k, tax)
    if not traer("bruta", tax):
        traer("costo", tax)
    h = {k: v[0] for k, v in c.items()}
    if not (h["ventas"] or h["utilidad"] or h["cfo"]):
        return None
    # 1: trimestres solo si estan al dia (si no, años fiscales)
    vq, bq = frescos(trimestres(h["ventas"]), 200), frescos(trimestres(h["bpa"]), 200)
    va, ba = frescos(anuales(h["ventas"]), 550), frescos(anuales(h["bpa"]), 550)
    r1 = p1_crece(vq, bq if vq else [], va, ba)
    # 2
    r2 = p2_margenes(va, anuales(h.get("bruta") or []), anuales(h.get("costo") or []),
                     anuales(h["operacional"]))
    # flujos de 12 meses
    t = {k: ttm(h[k]) for k in ("cfo", "cfi", "cff", "capex", "utilidad", "operacional", "impuesto", "antesImpuesto")}
    val = {k: (v[0] if v else None) for k, v in t.items()}
    capex = abs(val["capex"]) if val["capex"] is not None else None
    r3 = p3_caja(val["cfo"], capex, val["utilidad"])
    # balance: fecha del patrimonio como referencia de "al dia"
    pat = instante(h["patrimonio"])
    caja = instante(h["caja"])
    ref = (pat or caja or (None, None))[1]
    desde = ((_d(ref) if ref else _hoy()) - timedelta(days=100)).isoformat()
    deuda = None
    lp = instante(traer("deudaLP", tax), desde)
    if lp:
        deuda = lp[0]
    else:
        nc = instante(traer("deudaLPnc", tax), desde)
        cc = instante(traer("deudaLPc", tax), desde)
        if nc or cc:
            deuda = (nc[0] if nc else 0) + (cc[0] if cc else 0)
    cp = instante(traer("deudaCP", tax), desde)
    if deuda is None and pat:
        # Sin ninguna etiqueta de deuda al dia, pero con balance: no tiene.
        deuda = 0.0
    if deuda is not None and cp:
        deuda += cp[0]
    fcf = (val["cfo"] - (capex or 0)) if val["cfo"] is not None else None
    r4 = p4_deuda(deuda, fcf)
    r5 = p5_roic(val["operacional"], val["impuesto"], val["antesImpuesto"], deuda,
                 pat[0] if pat else None, caja[0] if caja else None)
    firma = firma_caja(val["cfo"], val["cfi"], val["cff"])
    hasta = max([v[1] for v in t.values() if v] + [""]) or None
    periodo = "anual" if any(v and v[2] == "anual" for v in t.values()) else "ttm"
    res = [(r[0], r[1], r[2], "sec" if r[0] is not None else None) for r in (r1, r2, r3, r4, r5)]
    return armar_ficha(ticker, res, firma, "sec", {
        "cik": cik, "edgar": _EDGAR_URL.format(cik=cik), "hasta": hasta, "periodo": periodo,
        "taxonomia": tax, "etiquetas": {k: v[1] for k, v in c.items() if v[1]}})


# ---------------------------------------------------------------------------
# Respaldo: TradingView
# ---------------------------------------------------------------------------
def completar_con_tv(ficha, ticker, tv):
    """Llena con TradingView las preguntas que quedaron sin dato. `tv` es la
    fila de fuente_tradingview.fundamentales(): numeros del momento, sin
    historia. Si no hay ficha de la SEC, arma una entera con esto."""
    if not tv:
        return ficha
    res = []
    base = ficha["preguntas"] if ficha else [None] * 5
    for i in range(5):
        p = base[i]
        if p and p["resultado"] is not None:
            res.append((p["resultado"], p["datos"], p["nota"], p["fuente"]))
            continue
        r = (None, {}, None)
        if i == 0 and tv.get("crecVentas") is not None:
            v, b = tv["crecVentas"] / 100, (tv.get("crecBpa") / 100 if tv.get("crecBpa") is not None else None)
            if b is None:
                r = (("medias" if v >= 0.25 else "no"), {"ventas": _pct(v)}, "Solo ventas (TradingView, 12 meses).")
            elif v >= 0.25 and b >= 0.25:
                r = ("si", {"ventas": _pct(v), "bpa": _pct(b)}, "TradingView, 12 meses: no se pudo ver si frena.")
            elif (v >= 0.25 and b >= 0.15) or (b >= 0.25 and v >= 0.15):
                r = ("medias", {"ventas": _pct(v), "bpa": _pct(b)}, "TradingView, 12 meses.")
            else:
                r = ("no", {"ventas": _pct(v), "bpa": _pct(b)}, "TradingView, 12 meses.")
        elif i == 2 and tv.get("fcf") is not None and tv.get("utilidad") is not None:
            cfo = tv.get("cfo") if tv.get("cfo") is not None else tv["fcf"]
            r = p3_caja(cfo, cfo - tv["fcf"], tv["utilidad"])
        elif i == 3 and tv.get("deuda") is not None:
            r = p4_deuda(tv["deuda"], tv.get("fcf"))
        elif i == 4 and tv.get("roic") is not None:
            roic = tv["roic"] / 100
            r = (("si" if roic > 0.15 else "medias" if roic >= 0.10 else "no"), {"roic": _pct(roic)}, "ROIC de TradingView.")
        res.append((r[0], r[1], r[2], "tradingview" if r[0] is not None else None))
    firma = (ficha or {}).get("firma") or firma_caja(tv.get("cfo"), tv.get("cfi"), tv.get("cff"))
    usadas_tv = any(x[3] == "tradingview" for x in res)
    fuente = ("sec+tradingview" if ficha and usadas_tv else (ficha or {}).get("fuente") or "tradingview")
    extra = {k: v for k, v in (ficha or {}).items()
             if k in ("cik", "edgar", "hasta", "periodo", "etiquetas")}
    if not ficha:
        extra["periodo"] = "ttm"
    return armar_ficha(ticker, res, firma, fuente, extra)


# ---------------------------------------------------------------------------
# Descripcion de la empresa (Yahoo), para las que la app no tiene escrita
# ---------------------------------------------------------------------------
_ABREV = r"(?<!\bInc)(?<!\bCorp)(?<!\bCo)(?<!\bLtd)(?<!\bU\.S)(?<!\bN\.V)(?<!\bS\.A)(?<!\bPLC)(?<!\bNo)"


def primera_oracion(texto, maximo=220):
    if not texto:
        return None
    t = " ".join(str(texto).split())
    m = re.search(_ABREV + r"\.\s+(?=[A-Z])", t)
    o = t[:m.start() + 1] if m else t
    return o if len(o) <= maximo else o[:maximo - 1].rsplit(" ", 1)[0] + "…"


def perfil_yahoo(ticker):
    try:
        import data_source
        res, _ = data_source.quote_summary(ticker, "assetProfile")
        ap = (res or {}).get("assetProfile") or {}
        resumen = primera_oracion(ap.get("longBusinessSummary"))
        if not (resumen or ap.get("industry")):
            return None
        return {"resumen": resumen, "industria": ap.get("industry"), "sector": ap.get("sector"),
                "fuente": "yahoo", "idioma": "en"}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cache, cola y el hilo que trabaja
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache = {}          # ticker -> {"ficha": {...}, "ts": epoch}
_cola = deque()
_en_cola = set()
_hilo = [None]
_cargado = [False]


def _cargar_disco():
    if _cargado[0]:
        return
    _cargado[0] = True
    try:
        with open(FICHAS_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict):
            _cache.update(d)
    except Exception:
        pass


def _guardar_disco():
    try:
        with _lock:
            copia = dict(_cache)
        with open(FICHAS_FILE, "w") as f:
            json.dump(copia, f)
    except Exception as e:
        print(f"[fichas] no se pudo guardar en disco: {e}")


def en_cache(ticker):
    _cargar_disco()
    c = _cache.get(ticker)
    if not c:
        return None
    ttl = TTL_OK if (c.get("ficha") or {}).get("disponible") else TTL_FALLO
    return c["ficha"] if time.time() - c["ts"] <= ttl else None


def calcular(ticker, tv_fn=None):
    """Una ficha completa (bloquea). La usa el hilo de la cola."""
    ficha, motivo_sec = None, None
    try:
        cik = cik_de(ticker)
        if cik:
            ficha = ficha_desde_sec(ticker, cik)
            if ficha is None:
                motivo_sec = "la SEC no tiene estados en XBRL de esta empresa"
        else:
            motivo_sec = "no está en la SEC"
    except SinSec as e:
        motivo_sec = str(e)
    except Exception as e:
        motivo_sec = f"{type(e).__name__}: {e}"
    if ficha is None or ficha["respondidas"] < 5 or ficha.get("firma") is None:
        try:
            import fuente_tradingview
            tv = (tv_fn or fuente_tradingview.fundamentales)([ticker]).get(ticker)
            ficha = completar_con_tv(ficha, ticker, tv)
        except Exception as e:
            _diag["ultimoErrorTv"] = f"{type(e).__name__}: {e}"
    if ficha is None:
        ficha = {"ticker": ticker, "disponible": False,
                 "motivo": motivo_sec or "ni la SEC ni TradingView entregaron datos",
                 "calculado": datetime.now(timezone.utc).isoformat()}
    elif motivo_sec:
        ficha["motivoSec"] = motivo_sec
    # La descripcion de Yahoo solo para las que la app no tiene escrita (las
    # que sigue Cristian vienen en español en la app): una consulta menos a
    # Yahoo por ficha, que ya nos ha negado el crumb mas de una vez.
    ficha["perfil"] = None if ticker in SIN_PERFIL else perfil_yahoo(ticker)
    return ficha


def _trabajar():
    while True:
        with _lock:
            if not _cola:
                _hilo[0] = None
                _diag["cola"] = 0
                return
            t = _cola.popleft()
            _diag["cola"] = len(_cola)
        try:
            f = calcular(t)
        except Exception as e:
            f = {"ticker": t, "disponible": False, "motivo": f"{type(e).__name__}: {e}"}
        with _lock:
            _cache[t] = {"ficha": f, "ts": time.time()}
            _en_cola.discard(t)
            _diag["calculadas"] += 1
        _guardar_disco()


def pedir(tickers, primero=False):
    """Devuelve (fichas_listas, pendientes). Lo que no esta en cache entra a
    la cola y el hilo lo calcula en segundo plano. `primero`: pasan adelante
    de la cola (lo que Cristian esta mirando: Explorar, su cartera, una
    ficha abierta), por sobre lo que pide la grilla al ir apareciendo."""
    listas, pend = {}, []
    with _lock:
        for t in tickers:
            f = en_cache(t)
            if f is not None:
                listas[t] = f
                continue
            pend.append(t)
            if t in _en_cola and primero:
                try:
                    _cola.remove(t)
                except ValueError:
                    pass
                _cola.appendleft(t)
            elif t not in _en_cola:
                _en_cola.add(t)
                (_cola.appendleft if primero else _cola.append)(t)
        _diag["cola"] = len(_cola)
        if _cola and _hilo[0] is None:
            _hilo[0] = threading.Thread(target=_trabajar, name="fichas-sec", daemon=True)
            _hilo[0].start()
    return listas, pend


def diagnostico():
    return dict(_diag, secIdentificado=bool(SEC_USER_AGENT), enCache=len(_cache),
                pendientes=sorted(_en_cola))
