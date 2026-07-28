"""
Adaptador para la API oficial de la Bolsa de Santiago.

POR QUE EXISTE
==============
Yahoo Finance entrega los datos chilenos con rezago no declarado, y para la
Bolsa de Santiago no publica puntas de compra/venta (bid/ask) practicamente
nunca. Si lo que quieres es lo que se esta transando de verdad, hay que ir a
la fuente.

La Bolsa de Comercio de Santiago tiene una API para desarrolladores en
https://startup.bolsadesantiago.com — hay que SOLICITAR una api key con el
equipo que la mantiene. Existe ademas un SDK oficioso en Python
(pip install bolsa-stgo, github.com/LautaroParada/bolsa-santiago).

NO PUDE VERIFICAR EL COSTO ni las condiciones de uso: la pagina es una
aplicacion JavaScript y no se deja leer desde aca. Averigualo antes de
apoyarte en esto. Tambien tiene limite diario de peticiones (el propio
endpoint get_request_usuario te dice cuantas te quedan).

QUE ENTREGA QUE YAHOO NO
========================
  get_indices_rv       valor de los indices + variacion + volumen
  get_instrumentos_rv  apertura, maximo, minimo, volumen por instrumento
  get_puntas_rv        PUNTAS DE COMPRA Y VENTA  <-- esto Yahoo no lo tiene
  get_transacciones_rv ultimas transacciones
  get_resumen_accion   ficha detallada de una accion

COMO SE ACTIVA
==============
1. Consigue la api key en https://startup.bolsadesantiago.com
2. En Render, define la variable de entorno:  BOLSA_API_KEY=tu_clave
3. Listo. server.py la usa sola y deja Yahoo como respaldo.

Si BOLSA_API_KEY no esta definida, este modulo se desactiva solo y todo
sigue funcionando con Yahoo exactamente como hasta ahora. No rompe nada.

ADVERTENCIA IMPORTANTE
======================
Los nombres de los campos que devuelve la API (NEMO, PRECIO_CIERRE, etc.)
estan escritos aqui segun la documentacion del SDK, pero NO los he podido
probar contra la API real porque no tengo la clave. Cuando la consigas,
llama primero a /diag-bolsa para ver la respuesta cruda y ajusta el mapeo
de _MAPA_CAMPOS si algun nombre no coincide. Preferi dejarlo explicito y
en un solo lugar antes que esconder adivinanzas por todo el codigo.
"""
import os
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("BOLSA_API_KEY")
BASE = os.environ.get("BOLSA_API_BASE", "https://api.bolsadesantiago.com/api")
TIMEOUT = 12

# Nombres de campo esperados. Ajustalos aqui si la API real usa otros.
_MAPA_CAMPOS = {
    "nemo":        ("NEMO", "nemo", "Nemo"),
    "precio":      ("PRECIO_CIERRE", "PRECIO", "precio", "PRECIO_ULTIMO"),
    "cierre_ant":  ("PRECIO_CIERRE_ANT", "CIERRE_ANTERIOR", "precioCierreAnterior"),
    "maximo":      ("PRECIO_MAXIMO", "MAXIMO", "maximo"),
    "minimo":      ("PRECIO_MINIMO", "MINIMO", "minimo"),
    "volumen":     ("UN_TRANSADAS", "VOLUMEN", "volumen"),
    "monto":       ("MONTO_TRANSADO", "MONTO", "monto"),
    "hora":        ("HORA", "FEC_HORA", "hora", "TIMESTAMP"),
    "compra":      ("PRECIO_COMPRA", "PUNTA_COMPRA", "precioCompra"),
    "venta":       ("PRECIO_VENTA", "PUNTA_VENTA", "precioVenta"),
}


def disponible():
    return bool(API_KEY)


def _campo(fila, clave):
    """Busca un campo probando los nombres alternativos conocidos."""
    for nombre in _MAPA_CAMPOS.get(clave, ()):
        if nombre in fila and fila[nombre] not in (None, ""):
            return fila[nombre]
    return None


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        # La API puede devolver "1.234,56" (formato chileno) o "1234.56".
        t = str(v).strip().replace("$", "").replace(" ", "")
        if "," in t and "." in t:
            t = t.replace(".", "").replace(",", ".")
        elif "," in t:
            t = t.replace(",", ".")
        return float(t)
    except (TypeError, ValueError):
        return None


def _get(ruta, params=None):
    if not API_KEY:
        return None
    try:
        resp = requests.get(
            f"{BASE}/{ruta.lstrip('/')}",
            headers={"Authorization": f"Bearer {API_KEY}",
                     "Accept": "application/json"},
            params=params or {},
            timeout=TIMEOUT,
        )
        if resp.status_code == 401:
            print("[bolsa] 401: la BOLSA_API_KEY no fue aceptada.")
            return None
        if resp.status_code == 429:
            print("[bolsa] 429: se acabo la cuota diaria de peticiones.")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[bolsa] fallo {ruta}: {type(e).__name__}: {e}")
        return None


def get_quotes(tickers):
    """
    Mismo formato de salida que data_source.get_quotes(), para que sea
    intercambiable sin tocar nada mas.

    Ventaja frente a Yahoo: una sola peticion trae TODAS las acciones
    (get_instrumentos_rv devuelve el mercado completo), y trae las puntas.
    """
    datos = _get("RV_Instrumentos") or _get("instrumentos_rv")
    if not datos:
        return {}

    filas = datos if isinstance(datos, list) else (datos.get("listaResult")
                                                   or datos.get("data") or [])
    puntas = _puntas_por_nemo()
    ahora = datetime.now(timezone.utc)
    quotes = {}

    for fila in filas:
        nemo = _campo(fila, "nemo")
        if not nemo or nemo not in tickers:
            continue
        precio = _num(_campo(fila, "precio"))
        if precio is None:
            continue
        p = puntas.get(nemo, {})
        quotes[nemo] = {
            "price": precio,
            "marketTime": ahora.isoformat(),   # ver nota abajo
            "staleSeconds": 0,
            "fetchedAt": ahora.isoformat(),
            "previousClose": _num(_campo(fila, "cierre_ant")),
            "dayHigh": _num(_campo(fila, "maximo")),
            "dayLow": _num(_campo(fila, "minimo")),
            "volume": _num(_campo(fila, "volumen")),
            # Esto es lo que Yahoo nunca te va a dar:
            "bid": p.get("bid"),
            "ask": p.get("ask"),
            "puntasDisponibles": bool(p),
        }
    # NOTA: si la API devuelve una hora propia por instrumento (campo HORA),
    # usala en vez de "ahora" — es mas honesto. Lo dejo asi porque no pude
    # verificar el formato exacto que entrega.
    return quotes


def _puntas_por_nemo():
    datos = _get("RV_Puntas") or _get("puntas_rv")
    if not datos:
        return {}
    filas = datos if isinstance(datos, list) else (datos.get("listaResult")
                                                   or datos.get("data") or [])
    out = {}
    for fila in filas:
        nemo = _campo(fila, "nemo")
        if not nemo:
            continue
        out[nemo] = {"bid": _num(_campo(fila, "compra")),
                     "ask": _num(_campo(fila, "venta"))}
    return out


def get_index():
    """Valor del IPSA directo de la bolsa, no de Yahoo."""
    datos = _get("RV_Indices") or _get("indices_rv")
    if not datos:
        return None
    filas = datos if isinstance(datos, list) else (datos.get("listaResult")
                                                   or datos.get("data") or [])
    for fila in filas:
        nombre = str(fila.get("CODIGO") or fila.get("NOMBRE")
                     or fila.get("nombre") or "").upper()
        if "IPSA" in nombre:
            valor = _num(fila.get("VALOR") or fila.get("PUNTOS")
                         or fila.get("valor"))
            if valor is None:
                continue
            ahora = datetime.now(timezone.utc).isoformat()
            return {"value": valor,
                    "previousClose": _num(fila.get("CIERRE_ANTERIOR")),
                    "marketTime": ahora, "staleSeconds": 0, "fetchedAt": ahora}
    return None


def diagnostico():
    """Respuesta cruda de la API, para ajustar _MAPA_CAMPOS la primera vez."""
    if not API_KEY:
        return {"activa": False,
                "motivo": "BOLSA_API_KEY no esta definida",
                "como_activar": ("Pide la clave en https://startup.bolsadesantiago.com "
                                 "y definela como variable de entorno BOLSA_API_KEY "
                                 "en Render.")}
    indices = _get("RV_Indices") or _get("indices_rv")
    instrumentos = _get("RV_Instrumentos") or _get("instrumentos_rv")
    return {
        "activa": True,
        "base": BASE,
        "indices_ok": indices is not None,
        "instrumentos_ok": instrumentos is not None,
        # Los nombres de campo reales, para corregir _MAPA_CAMPOS si hace falta.
        "muestra_indices": (indices if isinstance(indices, list) else
                            (indices or {}).get("listaResult", []))[:2],
        "muestra_instrumentos": (instrumentos if isinstance(instrumentos, list) else
                                 (instrumentos or {}).get("listaResult", []))[:2],
    }
