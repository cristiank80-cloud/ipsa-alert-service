"""
Historico del IPSA desde un archivo CSV local (fecha;valor).

POR QUE EXISTE ESTE ARCHIVO
============================
El historico real del IPSA es dificil de conseguir gratis y en vivo:
Yahoo tiene rota la serie historica de ^IPSA (chart API devuelve un solo
punto viejo para cualquier periodo -- ver el comentario largo en
data_source.py) y Stooq no se pudo verificar desde el entorno de
desarrollo (no respondio ni a un ticker de prueba conocido). El dueño de
la app ya tenia un historico real exportado a mano (Bolsa de Santiago /
Investing.com), asi que se uso ese en vez de seguir apostando a una
fuente externa que podria no funcionar.

ipsa_historico.csv vive junto a este archivo, formato "fecha;valor" con
un dato por dia habil (mismo formato con el que se subio la primera vez).
Para refrescarlo, se reemplaza ese archivo por una exportacion mas nueva
con el mismo formato -- no hace falta tocar el codigo.

COMO SE MANTIENE ACTUALIZADO EL DATO DE HOY
=============================================
El disco de Render es EFIMERO: se borra en cada reinicio y en cada
despliegue (ver el comentario largo sobre esto en server.py). Por eso
este modulo NUNCA escribe de vuelta en el CSV -- cualquier cosa que
agregara ahi se perderia en el proximo redeploy sin avisar, lo que es
peor que no guardar nada.

En cambio, la estrategia es mas simple y sobrevive a cualquier reinicio:

  1) ipsa_historico.csv es la base FIJA, tal cual se subio. Congelada en
     la fecha de su ultimo dato.
  2) Cada vez que alguien pide el historico, este modulo le agrega ENCIMA
     el valor de HOY, tomado en vivo de la misma fuente que ya usa el
     banner del IPSA (fuente_df.get_index()) -- ese SI se pide fresco en
     cada llamada, nunca se guarda en disco.

Resultado: el grafico siempre trae el dato de HOY real y actualizado. El
tramo entre la fecha del CSV y hoy (si el archivo lleva un tiempo sin
renovarse) queda con un salto -- se cierra solo la proxima vez que se
suba un CSV mas nuevo, no requiere cambios de codigo.
"""
import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo

_RUTA_CSV = os.path.join(os.path.dirname(__file__), "ipsa_historico.csv")
_TZ_CHILE = ZoneInfo("America/Santiago")

_cache = {"puntos": None}


def _cargar_csv():
    """
    Lee y parsea ipsa_historico.csv UNA sola vez por arranque del
    servidor -- son ~9 mil filas que no cambian entre requests, no vale
    la pena re-leer el archivo en cada llamada.

    Devuelve la lista ordenada de MAS VIEJO a MAS NUEVO (el archivo viene
    al reves, mas nuevo primero), formato compatible con el resto de la
    app: [{"date": "YYYY-MM-DD", "close": float}, ...].
    """
    if _cache["puntos"] is not None:
        return _cache["puntos"]

    puntos = []
    try:
        with open(_RUTA_CSV, encoding="utf-8") as f:
            lector = csv.reader(f, delimiter=";")
            next(lector, None)  # salta el encabezado "fecha;valor"
            for fila in lector:
                if len(fila) < 2:
                    continue
                fecha, valor = fila[0].strip(), fila[1].strip()
                try:
                    puntos.append({"date": fecha, "close": float(valor)})
                except ValueError:
                    continue
    except FileNotFoundError:
        print(f"[ipsa_historico] no se encontro {_RUTA_CSV} -- el grafico "
              "de IPSA para Chile quedara sin historial de fondo (solo el "
              "dato de hoy, si esta disponible).")
        _cache["puntos"] = []
        return _cache["puntos"]
    except Exception as e:
        print(f"[ipsa_historico] fallo al leer {_RUTA_CSV} -- "
              f"{type(e).__name__}: {e}")
        _cache["puntos"] = []
        return _cache["puntos"]

    puntos.sort(key=lambda p: p["date"])
    _cache["puntos"] = puntos
    print(f"[ipsa_historico] {len(puntos)} datos cargados desde "
          f"ipsa_historico.csv ({puntos[0]['date']} a {puntos[-1]['date']})"
          if puntos else "[ipsa_historico] el CSV no trajo filas validas.")
    return puntos


def obtener_serie_combinada():
    """
    Serie completa del IPSA para el grafico de comparacion: el CSV
    congelado + el valor de HOY en vivo (hora de Chile, no UTC -- si se
    usara UTC el dato de la tarde/noche en Chile quedaria etiquetado con
    la fecha de mañana). Misma fuente que ya usa el banner del IPSA
    (Diario Financiero), asi que no agrega ninguna peticion nueva a la
    red que no se estuviera haciendo ya.

    Si hoy ya esta en el CSV (por ejemplo, se subio el archivo hoy
    mismo), se reemplaza ese punto en vez de duplicarlo.
    """
    import fuente_df  # import tardio: evita import circular con server.py

    base = list(_cargar_csv())  # copia -- no mutar el cache compartido
    try:
        indice = fuente_df.get_index()
    except Exception as e:
        print(f"[ipsa_historico] fuente_df.get_index() fallo -- "
              f"{type(e).__name__}: {e}")
        indice = None

    if indice and indice.get("value") is not None:
        hoy = datetime.now(_TZ_CHILE).strftime("%Y-%m-%d")
        punto_hoy = {"date": hoy, "close": indice["value"]}
        if base and base[-1]["date"] == hoy:
            base[-1] = punto_hoy
        else:
            base.append(punto_hoy)
    return base
