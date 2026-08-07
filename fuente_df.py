"""
Adaptador para el IPSA + UF + UTM de Diario Financiero (df.cl).

POR QUE EXISTE
==============
El IPSA que entregaba Yahoo Finance (ver data_source.py) llegaba con un
`regularMarketTime` pegado durante dias para el simbolo ^IPSA, aunque el
`regularMarketPrice` si se actualizara -- una inconsistencia del propio
Yahoo para ese simbolo puntual (confirmado cruzando contra Visfin.cl, que
tambien usa datos de Yahoo). En vez de seguir ocultando el IPSA cada vez
que eso pasa, se reemplaza por completo: el IPSA de esta app YA NO viene
de Yahoo, viene de la pagina publica de indices de Diario Financiero
(https://www.df.cl/marketdata/bolsas), que se actualiza sola en cada
carga de pagina.

QUE TRAE Y QUE NO
=================
Trae: monto del IPSA, variacion % del dia, UF y UTM. Todo como texto
plano dentro del HTML de la pagina (no hace falta JavaScript para verlo,
a diferencia de otros sitios que se probaron como Visfin o BICE
Inversiones -- ver conversacion). NO trae un grafico ni el historial: la
pagina si tiene un grafico interactivo, pero ese carga sus puntos via una
llamada de JavaScript que no se pudo inspeccionar (sin navegador
disponible), asi que esta app no lo usa ni lo necesita.

SOBRE LA "HORA" DEL DATO
========================
Diario Financiero no publica la hora exacta de cada cifra en el texto
plano (el timestamp por punto solo aparece dentro de ese grafico
interactivo que no se pudo leer). Por eso `marketTime` y `fetchedAt` aca
son el mismo valor: EL MOMENTO EN QUE ESTE SERVIDOR CONSULTO LA PAGINA,
no la hora en que la bolsa fijo ese precio. La app lo rotula como
"Consultado" en vez de "Actualizado" para no insinuar mas precision de
la que hay.

FRAGILIDAD CONOCIDA
====================
Esto es leer texto de una pagina de noticias, no una API publica. Si
Diario Financiero cambia el formato de esa pagina, esta funcion puede
dejar de encontrar el numero y devolver None (nunca un dato inventado).
Revisa /diag si el IPSA deja de aparecer.
"""
from datetime import datetime, timezone
import re
import time

import requests

URL_BOLSAS = "https://www.df.cl/marketdata/bolsas"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept": "text/html",
}
_TIMEOUT = 12

_TAG_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_ESPACIOS = re.compile(r"\s+")

# "SP IPSA 10.951,36 0,37" -- nombre del indice, monto, variacion % del dia.
_RE_IPSA = re.compile(r"SP IPSA\s+(-?[\d.,]+)\s+(-?[\d.,]+)")
_RE_UF = re.compile(r"\bUF\s+\$?\s*(-?[\d.,]+)")
_RE_UTM = re.compile(r"\bUTM\s+\$?\s*(-?[\d.,]+)")
# "DOLAR $908,40" -- dolar observado (CLP=X), mismo bloque de la pagina que
# ya usan UF y UTM. Verificado contra la pagina real el 2026-08-07: el
# bloque destacado dice literalmente "DOLAR" seguido del monto en pesos.
# Esta es la fuente que pide la seccion 4.2 de la especificacion v3: usar
# el mismo origen que UF/UTM en vez de pedirle CLP=X a Yahoo.
_RE_DOLAR = re.compile(r"\bDOLAR\s+\$?\s*(-?[\d.,]+)")

# Cache muy corta compartida entre get_index() y get_uf_utm(): si server.py
# pide las dos cosas en el mismo ciclo de refresco, esto evita descargar la
# misma pagina dos veces seguidas.
_CACHE_TTL = 20
_cache = {"texto": None, "ts": 0}


def _num_cl(texto):
    """
    '10.951,36' o '71.649' o '-0,75' (formato chileno) -> float.
    None si no calza.

    OJO: el punto SIEMPRE es separador de miles en este formato, tenga o
    no coma decimal -- "71.649" es setenta y un mil, no 71,649. La primera
    version de esto solo sacaba los puntos cuando habia una coma en el
    mismo numero, asi que UTM (que a veces viene sin decimales, "71.649")
    quedaba mal leida como 71.649 (setenta y uno coma seis cuarenta y
    nueve). Ahora el punto se saca siempre, antes de mirar la coma.
    """
    if texto is None:
        return None
    t = texto.strip().replace("$", "")
    if not t:
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _texto_plano(html):
    sin_script = _TAG_SCRIPT_STYLE.sub(" ", html)
    sin_tags = _TAG.sub(" ", sin_script)
    return _ESPACIOS.sub(" ", sin_tags)


def _obtener_texto_pagina():
    ahora = time.time()
    if _cache["texto"] is not None and (ahora - _cache["ts"]) < _CACHE_TTL:
        return _cache["texto"]
    try:
        resp = requests.get(URL_BOLSAS, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        texto = _texto_plano(resp.text)
        _cache.update({"texto": texto, "ts": ahora})
        return texto
    except Exception as e:
        print(f"[fuente_df] no se pudo leer {URL_BOLSAS}: {type(e).__name__}: {e}")
        return None


def get_index():
    """
    IPSA segun Diario Financiero. Mismo "shape" que fuente_bolsa.get_index()
    y data_source._quote_de_meta() para que server.py no tenga que tratarlo
    distinto: {value, previousClose, marketTime, staleSeconds, fetchedAt}.

    `previousClose` se calcula desde la variacion % (Diario Financiero no
    publica el cierre anterior por separado): valor / (1 + var/100).
    """
    texto = _obtener_texto_pagina()
    if texto is None:
        return None

    m = _RE_IPSA.search(texto)
    if not m:
        print("[fuente_df] no se encontro 'SP IPSA' en la pagina -- "
              "puede que hayan cambiado el formato.")
        return None

    valor = _num_cl(m.group(1))
    variacion_pct = _num_cl(m.group(2))
    if valor is None:
        return None

    ahora = datetime.now(timezone.utc).isoformat()
    prev_close = None
    if variacion_pct is not None and (1 + variacion_pct / 100) != 0:
        prev_close = valor / (1 + variacion_pct / 100)

    return {
        "value": valor,
        "previousClose": prev_close,
        # No hay hora de bolsa publicada en texto plano: se usa la hora en
        # que ESTE servidor consulto la pagina. La app lo rotula como
        # "Consultado", no "Actualizado", para ser honesto con esto.
        "marketTime": ahora,
        "staleSeconds": 0,
        "fetchedAt": ahora,
        "fuenteNombre": "Diario Financiero",
    }


def get_uf_utm():
    """
    {'uf': float, 'utm': float, 'usdclp': float, 'fetchedAt': iso}
    -- lo que falte queda en None.

    `usdclp` es el dolar observado (equivalente a Yahoo CLP=X) leido del
    mismo bloque destacado de la pagina que UF y UTM -- no es el dolar
    interbancario intradia, es el que publica Diario Financiero en esa
    pagina. Igual que el IPSA, no viene con hora de bolsa propia: usa
    `fetchedAt`, el momento en que este servidor consulto la pagina.
    """
    texto = _obtener_texto_pagina()
    if texto is None:
        return {"uf": None, "utm": None, "usdclp": None, "fetchedAt": None}

    m_uf = _RE_UF.search(texto)
    m_utm = _RE_UTM.search(texto)
    m_dolar = _RE_DOLAR.search(texto)
    return {
        "uf": _num_cl(m_uf.group(1)) if m_uf else None,
        "utm": _num_cl(m_utm.group(1)) if m_utm else None,
        "usdclp": _num_cl(m_dolar.group(1)) if m_dolar else None,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }
