"""
Busca noticias recientes sobre una acción usando el feed RSS público de
Google News (no requiere API key). Se usa para dar contexto cuando se
dispara una alerta: si hay una noticia reciente, puede explicar por qué
cayó el precio; si no hay ninguna, también es información útil (la
caída podría ser solo ruido de mercado, no un evento concreto).

LIMITACIÓN HONESTA: esto es una búsqueda por palabras clave sobre el
nombre de la empresa, no un análisis de si la noticia es relevante o
tiene relación causal con la caída del precio. Trae los titulares más
recientes que mencionan a la empresa; la interpretación de si explican
el movimiento del precio sigue siendo tuya.
"""
import feedparser
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

# Términos de búsqueda por ticker -- el nemotécnico solo (ej. "CCU")
# trae muchos falsos positivos en noticias, así que usamos el nombre
# de la empresa.
NEWS_QUERY = {
    "AGUAS-A": "Aguas Andinas",
    "ANDINA-B": "Embotelladora Andina",
    "BCI": "Banco de Crédito e Inversiones BCI",
    "BSANTANDER": "Banco Santander Chile",
    "CAP": "CAP minería acero",
    "CCU": "CCU Compañía Cervecerías Unidas",
    "CENCOSUD": "Cencosud",
    "CHILE": "Banco de Chile",
    "CMPC": "Empresas CMPC",
    "COLBUN": "Colbún energía",
    "CONCHATORO": "Viña Concha y Toro",
    "COPEC": "Empresas Copec",
    "ECL": "Engie Energía Chile",
    "ENELAM": "Enel Américas",
    "ENELCHILE": "Enel Chile",
    "ENTEL": "Entel Chile",
    "FALABELLA": "Falabella",
    "IAM": "Inversiones Aguas Metropolitanas",
    "LTM": "LATAM Airlines",
    "MALLPLAZA": "Mallplaza",
    "PARAUCO": "Parque Arauco",
    "RIPLEY": "Ripley Corp",
    "SMU": "SMU supermercados Chile",
    "SONDA": "Sonda TI",
    "SQM-B": "SQM Sociedad Química y Minera",
    "VAPORES": "Compañía Sudamericana de Vapores",
    "ANTARCHILE": "AntarChile",
    "QUINENCO": "Quiñenco",
    "HABITAT": "AFP Habitat",
    "CUPRUM": "AFP Cuprum",
    "PROVIDA": "AFP Provida",
    "PLANVITAL": "AFP PlanVital",
    "SK": "Sigdo Koppers",
    "CAMANCHACA": "Pesquera Camanchaca",
    "ALMENDRAL": "Almendral Entel",
    "ENELGXCH": "Enel Generación Chile",
    "WATTS": "Watts alimentos Chile",
    "CRISTALES": "Cristalerías de Chile",
    "BESALCO": "Besalco construcción",
    "PUCOBRE": "Pucobre minería cobre",
    "LIPIGAS": "Lipigas",
    "BLUMAR": "Blumar pesquera",
    "ORO-BLANCO": "Oro Blanco SQM",
    "AAISA": "AAISA ILC",
    "ENJOY": "Enjoy casinos reorganización judicial",
    "INDISA": "Clínica Indisa",
    "SOCOVESA": "Socovesa inmobiliaria",
}


def get_recent_news(ticker, hours=48, max_items=3):
    """
    Devuelve una lista de hasta max_items noticias recientes (dict con
    'title', 'link', 'published') publicadas en las últimas 'hours'
    horas. Lista vacía si no encuentra nada o si falla la búsqueda
    (nunca lanza excepción hacia afuera, para no romper el ciclo de
    alertas por un problema de red en esta parte secundaria).
    """
    query = NEWS_QUERY.get(ticker, ticker)
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=es-419&gl=CL&ceid=CL:es-419"

    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"[news] Error consultando noticias para {ticker}: {e}")
        return []

    if not feed.entries:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    resultados = []

    for entry in feed.entries:
        try:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            continue
        if published < cutoff:
            continue
        resultados.append({
            "title": entry.title,
            "link": entry.link,
            "published": published.isoformat(),
        })
        if len(resultados) >= max_items:
            break

    return resultados


def describe(news_items):
    """Convierte la lista de noticias en una línea de texto para la alerta."""
    if not news_items:
        return "No se encontraron noticias recientes sobre esta acción en las últimas 48 horas."
    lineas = [f"- {n['title']}" for n in news_items]
    return "Noticias recientes:\n" + "\n".join(lineas)
