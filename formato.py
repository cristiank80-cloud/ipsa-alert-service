# -*- coding: utf-8 -*-
"""
Formato chileno de numeros para todo lo que lee una persona.

Miles con PUNTO y decimales con COMA: 1.234,56. Es como se escriben los
numeros en Chile y es el mismo formato que usa la app en pantalla. Sin esto,
el correo y el push que manda este servidor escribian "1,234.56" y el mismo
dato se veia de dos maneras distintas segun donde lo mirara Cristian.

POR QUE NO SE USA EL `locale` DEL SISTEMA
=========================================
`locale.setlocale(locale.LC_NUMERIC, "es_CL.UTF-8")` es la forma "correcta" en
otra maquina, pero depende de que esa configuracion regional este INSTALADA, y
en el contenedor de Render no lo esta. Ahi la llamada falla -- o peor, no falla
y sigue formateando en ingles sin avisar, que es el tipo de error que nadie ve
hasta que llega un correo con el numero mal escrito. Esto es aritmetica de
texto: funciona igual en cualquier maquina y no hay nada que instalar.

DONDE **NO** USARLO
===================
En JSON, en logs para maquinas y en cualquier numero que el frontend vaya a
volver a parsear. Esto produce TEXTO para leer, no un numero.
"""


def num(valor, dec=0, signo=False):
    """1234.5 -> '1.234,5'. `signo=True` antepone '+' a los positivos."""
    if valor is None:
        return "s/d"
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return "s/d"
    txt = "{:,.{}f}".format(v, dec)   # formato de EE.UU.: '1,234.50'
    # El intercambio va por un caracter puente. Reemplazar uno y despues el
    # otro convertiria '1,234.50' en '1.234.50'.
    txt = txt.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if signo and v >= 0:
        txt = "+" + txt
    return txt
