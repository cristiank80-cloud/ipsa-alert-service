"""
Universo completo del mercado de EE.UU. -- listas publicas de NASDAQ Trader.

POR QUE EXISTE
===============
Cristian comparo los 7 filtros del metodo corridos en TradingView contra los
mismos 7 corridos en la app, y no coincidian. La causa real (no un bug de
calculo): TradingView, cuando no le fijas un "Indice"/"Lista de seguimiento",
barre CASI TODO el mercado de EE.UU. -- varios miles de simbolos -- mientras
que la app (UNIVERSO_ANALISIS, en main.py) solo mira el S&P 500 + Nasdaq-100
+ la grilla, ~560 simbolos. Dos universos distintos, mismos filtros: resultados
distintos. Este modulo cierra esa brecha, a pedido explicito de Cristian
("Todo el mercado, acepto la espera").

DE DONDE SALE LA LISTA
=======================
NASDAQ Trader publica, gratis y sin login, el directorio de TODOS los
simbolos listados en EE.UU. (no solo los que cotizan en el Nasdaq):

  https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt   -- Nasdaq
  https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt    -- NYSE,
      NYSE American, NYSE Arca, Cboe BZX, IEX y otras plazas de EE.UU.

Son archivos de texto separados por "|", una linea por simbolo, con la
ultima linea siempre "File Creation Time: ...". Cada uno trae un campo ETF
(Y/N) explicito -- mas confiable que armar una lista a mano como la vieja
ETFS_NO_ANALIZAR de 8 nombres.

QUE SE EXCLUYE Y POR QUE
==========================
  - ETF = "Y"                    -- fondos, no acciones (mismo motivo que
                                     ETFS_NO_ANALIZAR en main.py).
  - Test Issue = "Y"             -- simbolos de prueba de la bolsa, no
                                     existen de verdad.
  - Financial Status != "N"      -- (solo nasdaqlisted.txt) "N" es
                                     "Normal"; lo demas es deficiente,
                                     atrasado en reportes o en quiebra
                                     (D/E/Q/G/H). No tiene sentido correr
                                     el metodo sobre una empresa que la
                                     propia bolsa ya marco como problema.
  - Nombre con palabras de instrumento no-accion (warrant, unit, right,
    preferred, notes, debenture, depositary...) -- son derivados o deuda,
    no la accion comun que el metodo evalua.

Lo que NO se intenta filtrar con precision: sufijos de un solo caracter en
el simbolo (la convencion de 5 letras de Nasdaq para warrants/rights/etc
es ambigua -- muchas acciones normales terminan en esas mismas letras). Se
prefiere dejar pasar algun instrumento raro de mas: el propio embudo de
explorar.py lo va a descartar en el primer filtro (precio, cap, volumen) de
todos modos, y el presupuesto de tiempo (TOPE_DESCARGA_SEG) ya esta pensado
para universos grandes -- ver el comentario ahi.

QUE HACE SI LA DESCARGA FALLA
================================
NUNCA rompe el analisis. `ampliar_universo()` devuelve el universo base
(SP500+Nasdaq100+grilla, lo que ya funcionaba) sin el mercado completo, y
dice por que en el campo "motivo". Nunca se inventa una lista.

CACHE
=====
La lista de NASDAQ Trader casi no cambia dia a dia (salen/entran unos
pocos simbolos por semana). Se descarga una vez y se guarda en memoria por
24 horas -- /explorar/run no vuelve a bajar los ~7.000 renglones de esos
dos archivos en cada corrida, solo la primera vez del dia.
"""
from datetime import datetime, timezone
import threading
import time

import requests

_HEADERS = {
    # Igual que data_source.py: sin User-Agent de navegador, NASDAQ Trader
    # tambien puede responder distinto o cortar la conexion.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
}
_TIMEOUT = 25

_URL_NASDAQ = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_URL_OTHER = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_TTL_SEG = 24 * 3600  # se vuelve a bajar como maximo una vez al dia

_PALABRAS_NO_ACCION = (
    "WARRANT", "RIGHT", "UNIT", "PREFERRED", " PFD", "DEPOSITARY",
    "NOTES", "NOTE ", "DEBENTURE", "SUBORDINATED", "TRUST PFD",
    "TRUST PREFERRED",
)

_lock = threading.Lock()
# Cache en memoria -- se pierde en cada reinicio del servidor, igual que la
# cuarentena de data_source.py, y se vuelve a armar sola en el primer
# /explorar/run despues de ese reinicio.
_cache = {
    "simbolos": None,       # set() una vez que hay una descarga exitosa
    "cuando": None,         # datetime UTC de esa descarga
    "crudo_nasdaq": 0,
    "crudo_other": 0,
    "descartados_no_accion": 0,
    "motivo_error": None,   # texto del ultimo intento fallido, si lo hay
}


def _es_accion_comun(nombre_seguridad):
    n = (nombre_seguridad or "").upper()
    return not any(p in n for p in _PALABRAS_NO_ACCION)


def _normalizar_simbolo(sym):
    """NASDAQ Trader separa las clases de accion con punto (BRK.B); Yahoo
    -- y el resto de esta app, ver SP500 en main.py -- usa guion (BRK-B)."""
    return sym.strip().upper().replace(".", "-")


def _simbolo_valido(sym):
    if not sym:
        return False
    # "$" marca acciones preferentes en el simbolo mismo (ACT Symbol de
    # otherlisted.txt, ej "AGM$C"); "." sobrante tras normalizar (series
    # con mas de un punto) es casi siempre un instrumento raro, no una
    # accion comun.
    return "$" not in sym and sym.count("-") <= 1


def _descargar(url):
    r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.text


def _parsear_nasdaqlisted(texto):
    """Symbol|Security Name|Market Category|Test Issue|Financial Status|
    Round Lot Size|ETF|NextShares"""
    simbolos, crudo, descartados = set(), 0, 0
    for linea in texto.splitlines():
        if not linea.strip() or linea.startswith("Symbol|") \
                or linea.startswith("File Creation Time"):
            continue
        campos = linea.split("|")
        if len(campos) < 7:
            continue
        crudo += 1
        symbol, nombre, _cat, test_issue, fin_status, _lote, etf = campos[:7]
        if etf.strip().upper() == "Y":
            continue
        if test_issue.strip().upper() == "Y":
            continue
        if fin_status.strip().upper() not in ("", "N"):
            continue
        if not _es_accion_comun(nombre):
            descartados += 1
            continue
        sym = _normalizar_simbolo(symbol)
        if _simbolo_valido(sym):
            simbolos.add(sym)
    return simbolos, crudo, descartados


def _parsear_otherlisted(texto):
    """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|
    Test Issue|NASDAQ Symbol"""
    simbolos, crudo, descartados = set(), 0, 0
    for linea in texto.splitlines():
        if not linea.strip() or linea.startswith("ACT Symbol|") \
                or linea.startswith("File Creation Time"):
            continue
        campos = linea.split("|")
        if len(campos) < 7:
            continue
        crudo += 1
        symbol, nombre, _exch, _cqs, etf, _lote, test_issue = campos[:7]
        if etf.strip().upper() == "Y":
            continue
        if test_issue.strip().upper() == "Y":
            continue
        # otherlisted.txt no trae Financial Status -- esa senal solo existe
        # para simbolos de Nasdaq.
        if not _es_accion_comun(nombre):
            descartados += 1
            continue
        sym = _normalizar_simbolo(symbol)
        if _simbolo_valido(sym):
            simbolos.add(sym)
    return simbolos, crudo, descartados


def _construir():
    """Descarga y filtra los dos archivos. Devuelve (set, info) o (None, info)
    si algo fallo -- nunca lanza."""
    try:
        texto_nasdaq = _descargar(_URL_NASDAQ)
        texto_other = _descargar(_URL_OTHER)
    except Exception as e:
        return None, {"motivo_error": f"{type(e).__name__}: {e}"}

    try:
        s1, crudo1, desc1 = _parsear_nasdaqlisted(texto_nasdaq)
        s2, crudo2, desc2 = _parsear_otherlisted(texto_other)
    except Exception as e:
        return None, {"motivo_error": f"parseo fallo -- {type(e).__name__}: {e}"}

    simbolos = s1 | s2
    if len(simbolos) < 1000:
        # Si NASDAQ Trader devolvio una pagina de error o un archivo
        # truncado, va a "parsear" igual (0 o pocas lineas) pero el
        # resultado es sospechosamente chico -- mejor no usarlo que usar
        # un universo incompleto sin avisar.
        return None, {
            "motivo_error": f"solo {len(simbolos)} simbolos utiles tras "
                             f"filtrar -- parece una descarga incompleta, "
                             f"se descarta y se usa el universo base.",
        }

    info = {
        "crudo_nasdaq": crudo1, "crudo_other": crudo2,
        "descartados_no_accion": desc1 + desc2,
        "motivo_error": None,
    }
    return simbolos, info


def _cache_vigente():
    if _cache["simbolos"] is None or _cache["cuando"] is None:
        return False
    return (datetime.now(timezone.utc) - _cache["cuando"]).total_seconds() < _TTL_SEG


def ampliar_universo(base, excluir=()):
    """
    Devuelve (universo_ordenado, info) uniendo `base` (lo que ya andaba --
    S&P 500 + Nasdaq-100 + grilla) con el mercado completo de NASDAQ Trader,
    menos `excluir` (ETFS_NO_ANALIZAR de main.py).

    Usa la cache de 24h si esta vigente. Si nunca se pudo descargar -- ahora
    o antes -- cae a `base` solo, y lo dice en info["motivo_error"]; el
    analisis SIEMPRE puede correr, con o sin el mercado completo.
    """
    base_set = set(base) - set(excluir)
    with _lock:
        if not _cache_vigente():
            simbolos, info = _construir()
            if simbolos is not None:
                _cache["simbolos"] = simbolos
                _cache["cuando"] = datetime.now(timezone.utc)
                _cache["crudo_nasdaq"] = info["crudo_nasdaq"]
                _cache["crudo_other"] = info["crudo_other"]
                _cache["descartados_no_accion"] = info["descartados_no_accion"]
                _cache["motivo_error"] = None
            else:
                _cache["motivo_error"] = info["motivo_error"]
                # Si habia una cache vieja (de un dia anterior) y la
                # descarga de hoy fallo, se sigue usando la vieja en vez de
                # tirarla -- mejor un universo de ayer que ninguno.
                if _cache["simbolos"] is None:
                    print(f"[universo_mercado] AVISO: no se pudo descargar "
                          f"el mercado completo -- {info['motivo_error']}. "
                          f"Corriendo solo con el universo base "
                          f"({len(base_set)} simbolos).")

        mercado = _cache["simbolos"] or set()
        completo = sorted((base_set | mercado) - set(excluir))
        info = {
            "total": len(completo),
            "solo_base": len(base_set),
            "del_mercado_completo": len(mercado - base_set),
            "fuente_mercado_completo": "NASDAQ Trader (nasdaqlisted.txt + "
                                        "otherlisted.txt)" if mercado else None,
            "descargado": _cache["cuando"].isoformat() if _cache["cuando"] else None,
            "crudo_nasdaq": _cache["crudo_nasdaq"],
            "crudo_other": _cache["crudo_other"],
            "descartados_no_accion": _cache["descartados_no_accion"],
            "motivo_error": _cache["motivo_error"],
        }
        return completo, info


def simbolos_en_cache():
    """
    Los simbolos del mercado completo que YA estan descargados, como set.
    Vacio si todavia no se pudo bajar. NO sale a la red -- se puede llamar
    desde cualquier peticion sin costo.

    La usa server.py para validar la watchlist: una candidata que Explorar
    encontro en el mercado ampliado tiene que poder mandarse a "A seguir",
    y sin esto quedaba fuera por no estar en el universo base.
    """
    with _lock:
        return set(_cache["simbolos"] or ())


def precalentar():
    """
    Deja la lista lista antes de que alguien la necesite. Pensada para
    llamarse UNA vez al arrancar el servidor, en segundo plano.

    POR QUE IMPORTA: la cache vive en memoria y se pierde en cada reinicio
    (que en el plan gratuito de Render pasa solo, por inactividad). Sin esto,
    la ventana entre el reinicio y el primer analisis dejaba la watchlist
    validandose solo contra el universo base -- y el frontend reenvia su
    watchlist cada vez que se abre la app, asi que una candidata del mercado
    ampliado se habria perdido en silencio justo ahi.

    No lanza nunca: si falla, `ampliar_universo` lo reintenta despues y el
    analisis igual corre con el universo base.
    """
    try:
        with _lock:
            if _cache_vigente():
                return False
        simbolos, info = _construir()
        with _lock:
            if simbolos is not None:
                _cache["simbolos"] = simbolos
                _cache["cuando"] = datetime.now(timezone.utc)
                _cache["crudo_nasdaq"] = info["crudo_nasdaq"]
                _cache["crudo_other"] = info["crudo_other"]
                _cache["descartados_no_accion"] = info["descartados_no_accion"]
                _cache["motivo_error"] = None
                print(f"[universo_mercado] Mercado completo listo: "
                      f"{len(simbolos)} simbolos utiles.")
                return True
            _cache["motivo_error"] = info["motivo_error"]
            print(f"[universo_mercado] No se pudo precalentar: {info['motivo_error']}")
            return False
    except Exception as e:
        print(f"[universo_mercado] Fallo el precalentado: {type(e).__name__}: {e}")
        return False


def estado_cache():
    """Como esta la cache AHORA MISMO, sin descargar nada -- para
    /universo-diag. Igual que simbolos_en_cuarentena() en data_source.py:
    solo lee, nunca sale a la red."""
    with _lock:
        return {
            "vigente": _cache_vigente(),
            "total_mercado_completo": len(_cache["simbolos"]) if _cache["simbolos"] else 0,
            "descargado": _cache["cuando"].isoformat() if _cache["cuando"] else None,
            "motivo_error": _cache["motivo_error"],
            "nota": ("Se descarga la primera vez que se pide un analisis "
                     "despues de reiniciar el servidor (o si pasaron mas de "
                     "24h desde la ultima descarga), no antes -- por eso "
                     "puede salir en 0 si todavia no se corrio "
                     "/explorar/run."),
        }
