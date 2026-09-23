import html
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup


FUENTE_PRINCIPAL = "https://rss.elconfidencial.com/empresas/"
FUENTES_ALTERNATIVAS = [
    FUENTE_PRINCIPAL,
    "https://rss.elconfidencial.com/empresas",
    "http://rss.elconfidencial.com/empresas/",
]

PAGINA_EMPRESAS = "https://www.elconfidencial.com/empresas/"
ARCHIVO_RSS = Path("rss.xml")
ZONA_ESPANA = ZoneInfo("Europe/Madrid")
MAXIMO_NOTICIAS = 3000

URL_RSS_GITHUB = (
    "https://raw.githubusercontent.com/"
    "plis2100/rss-elconfidencial-empresas/main/rss.xml"
)

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/rss+xml,application/xml,text/xml,"
        "text/html;q=0.8,*/*;q=0.5"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.5",
    "Cache-Control": "no-cache",
}


def ejecucion_permitida():
    """
    Las ejecuciones manuales funcionan a cualquier hora.

    Las programadas se realizan de lunes a sábado,
    entre las 07:00 y las 22:59 de España.
    """
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Ejecución manual: se ignora el horario.")
        return True

    ahora = datetime.now(ZONA_ESPANA)

    print(
        "Hora de España:",
        ahora.strftime("%d/%m/%Y %H:%M:%S %Z"),
    )

    if ahora.weekday() == 6:
        print("Es domingo. No se actualiza.")
        return False

    if not 7 <= ahora.hour <= 22:
        print("Fuera del horario de 07:00 a 22:59.")
        return False

    return True


def limpiar_texto(texto):
    if not texto:
        return ""

    return re.sub(
        r"\s+",
        " ",
        html.unescape(str(texto)),
    ).strip()


def limpiar_html(contenido):
    if not contenido:
        return ""

    soup = BeautifulSoup(
        contenido,
        "html.parser",
    )

    for elemento in soup.select(
        "script, style, iframe, form, button, noscript"
    ):
        elemento.decompose()

    return str(soup).strip()


def extraer_imagen(entrada):
    """
    Busca la imagen en los campos habituales de RSS.
    """
    for media in entrada.get("media_content", []):
        url = media.get("url", "").strip()

        if url:
            return url

    for miniatura in entrada.get("media_thumbnail", []):
        url = miniatura.get("url", "").strip()

        if url:
            return url

    for enclosure in entrada.get("enclosures", []):
        url = enclosure.get("href", "").strip()
        tipo = enclosure.get("type", "").lower()

        if url and (
            tipo.startswith("image/")
            or re.search(
                r"\.(jpg|jpeg|png|webp)(?:$|\?)",
                url,
                flags=re.IGNORECASE,
            )
        ):
            return url

    contenido = ""

    if entrada.get("content"):
        contenido = entrada.content[0].get(
            "value",
            "",
        )
    elif entrada.get("summary"):
        contenido = entrada.get("summary", "")

    soup = BeautifulSoup(
        contenido,
        "html.parser",
    )
    imagen = soup.find("img", src=True)

    if imagen:
        return imagen.get("src", "").strip()

    return ""


def convertir_fecha(entrada):
    """
    Convierte la fecha de Feedparser a una fecha UTC.
    """
    estructura = (
        entrada.get("published_parsed")
        or entrada.get("updated_parsed")
        or entrada.get("created_parsed")
    )

    if estructura:
        try:
            return datetime(
                estructura.tm_year,
                estructura.tm_mon,
                estructura.tm_mday,
                estructura.tm_hour,
                estructura.tm_min,
                estructura.tm_sec,
                tzinfo=timezone.utc,
            )
        except (ValueError, AttributeError):
            pass

    textos = [
        entrada.get("published", ""),
        entrada.get("updated", ""),
        entrada.get("created", ""),
    ]

    for texto in textos:
        if not texto:
            continue

        try:
            fecha = parsedate_to_datetime(texto)

            if fecha.tzinfo is None:
                fecha = fecha.replace(
                    tzinfo=timezone.utc
                )

            return fecha.astimezone(timezone.utc)

        except (ValueError, TypeError):
            continue

    return datetime.now(timezone.utc)


def obtener_descripcion(entrada):
    if entrada.get("content"):
        contenido = entrada.content[0].get(
            "value",
            "",
        )
    else:
        contenido = (
            entrada.get("summary")
            or entrada.get("description")
            or ""
        )

    contenido = limpiar_html(contenido)

    if not contenido:
        contenido = "<p>Noticia publicada por El Confidencial.</p>"

    return contenido


def descargar_fuente():
    session = requests.Session()
    session.headers.update(CABECERAS)

    errores = []

    for url in FUENTES_ALTERNATIVAS:
        try:
            print(f"Descargando fuente: {url}")

            respuesta = session.get(
                url,
                timeout=45,
                allow_redirects=True,
            )

            print(
                f"Respuesta HTTP: {respuesta.status_code}; "
                f"{len(respuesta.content)} bytes."
            )

            respuesta.raise_for_status()

            if len(respuesta.content) < 200:
                raise RuntimeError(
                    "La respuesta recibida es demasiado corta."
                )

            fuente = feedparser.parse(
                respuesta.content
            )

            if fuente.bozo and not fuente.entries:
                raise RuntimeError(
                    f"RSS no válido: {fuente.bozo_exception}"
                )

            if not fuente.entries:
                raise RuntimeError(
                    "La fuente no contiene entradas."
                )

            print(
                f"Entradas recibidas de la fuente: "
                f"{len(fuente.entries)}"
            )

            return fuente

        except Exception as error:
            errores.append(f"{url}: {error}")

            print(
                f"AVISO: falló {url}: {error}",
                file=sys.stderr,
            )

    raise RuntimeError(
        "No se pudo descargar ninguna dirección de la "
        "RSS de Empresas: " + " | ".join(errores)
    )


def procesar_fuente(fuente):
    noticias = {}

    for entrada in fuente.entries:
        titulo = limpiar_texto(
            entrada.get("title", "")
        )
        enlace = (
            entrada.get("link")
            or entrada.get("id")
            or ""
        ).strip()

        if not titulo or not enlace:
            continue

        guid = (
            entrada.get("id")
            or entrada.get("guid")
            or enlace
        ).strip()

        fecha = convertir_fecha(entrada)
        descripcion = obtener_descripcion(entrada)
        imagen = extraer_imagen(entrada)

        autor = limpiar_texto(
            entrada.get("author", "")
        )

        categorias = []

        for etiqueta in entrada.get("tags", []):
            categoria = limpiar_texto(
                etiqueta.get("term", "")
            )

            if categoria and categoria not in categorias:
                categorias.append(categoria)

        noticias[guid] = {
            "titulo": titulo,
            "enlace": enlace,
            "guid": guid,
            "fecha": fecha,
            "descripcion": descripcion,
            "imagen": imagen,
            "autor": autor,
            "categorias": categorias,
        }

    return noticias


def leer_rss_anterior():
    anteriores = {}

    if not ARCHIVO_RSS.exists():
        return anteriores

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return anteriores

        for item in canal.findall("item"):
            titulo = limpiar_texto(
                item.findtext("title", "")
            )
            enlace = item.findtext(
                "link",
                "",
            ).strip()
            guid = item.findtext(
                "guid",
                enlace,
            ).strip()
            fecha_texto = item.findtext(
                "pubDate",
                "",
            )
            descripcion = item.findtext(
                "description",
                "",
            )
            autor = item.findtext(
                "author",
                "",
            )

            if not titulo or not enlace:
                continue

            try:
                fecha = parsedate_to_datetime(
                    fecha_texto
                )

                if fecha.tzinfo is None:
                    fecha = fecha.replace(
                        tzinfo=timezone.utc
                    )

                fecha = fecha.astimezone(timezone.utc)

            except (ValueError, TypeError):
                fecha = datetime(
                    1970,
                    1,
                    1,
                    tzinfo=timezone.utc,
                )

            enclosure = item.find("enclosure")
            imagen = ""

            if enclosure is not None:
                imagen = enclosure.get("url", "")

            categorias = [
                limpiar_texto(categoria.text)
                for categoria in item.findall("category")
                if categoria.text
            ]

            anteriores[guid] = {
                "titulo": titulo,
                "enlace": enlace,
                "guid": guid,
                "fecha": fecha,
                "descripcion": descripcion,
                "imagen": imagen,
                "autor": autor,
                "categorias": categorias,
            }

    except Exception as error:
        print(
            f"AVISO: no se pudo leer el RSS anterior: {error}",
            file=sys.stderr,
        )

    return anteriores


def escribir_rss(noticias):
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "El Confidencial — Empresas"
    )
    ET.SubElement(canal, "link").text = (
        PAGINA_EMPRESAS
    )
    ET.SubElement(canal, "description").text = (
        "Noticias de empresas publicadas por "
        "El Confidencial."
    )
    ET.SubElement(canal, "language").text = "es-ES"
    ET.SubElement(canal, "ttl").text = "60"
    ET.SubElement(canal, "lastBuildDate").text = (
        format_datetime(datetime.now(timezone.utc))
    )

    atom = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    atom.set("href", URL_RSS_GITHUB)
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    for noticia in noticias[:MAXIMO_NOTICIAS]:
        item = ET.SubElement(canal, "item")

        ET.SubElement(item, "title").text = (
            noticia["titulo"]
        )
        ET.SubElement(item, "link").text = (
            noticia["enlace"]
        )
        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        ).text = noticia["guid"]
        ET.SubElement(item, "pubDate").text = (
            format_datetime(
                noticia["fecha"].astimezone(
                    timezone.utc
                )
            )
        )
        ET.SubElement(item, "description").text = (
            noticia["descripcion"]
        )

        if noticia.get("autor"):
            ET.SubElement(item, "author").text = (
                noticia["autor"]
            )

        for categoria in noticia.get(
            "categorias",
            [],
        ):
            ET.SubElement(
                item,
                "category",
            ).text = categoria

        if noticia.get("imagen"):
            ET.SubElement(
                item,
                "enclosure",
                {
                    "url": noticia["imagen"],
                    "type": "image/jpeg",
                },
            )

    ET.indent(rss, space="  ")

    ET.ElementTree(rss).write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    if not ejecucion_permitida():
        return

    anteriores = leer_rss_anterior()

    try:
        fuente = descargar_fuente()
        nuevas = procesar_fuente(fuente)

    except Exception as error:
        print(
            f"ERROR DE DESCARGA: {error}",
            file=sys.stderr,
        )

        if anteriores:
            print(
                "Se conserva el RSS anterior para evitar "
                "publicar un archivo vacío."
            )
            return

        raise

    todas = dict(anteriores)
    todas.update(nuevas)

    ordenadas = sorted(
        todas.values(),
        key=lambda noticia: noticia["fecha"],
        reverse=True,
    )

    print(f"Noticias recuperadas ahora: {len(nuevas)}")
    print(
        f"Noticias conservadas anteriormente: "
        f"{len(anteriores)}"
    )
    print(
        f"Total de noticias en el RSS: "
        f"{len(ordenadas)}"
    )

    if not ordenadas:
        raise RuntimeError(
            "No se encontró ninguna noticia de Empresas."
        )

    escribir_rss(ordenadas)

    print(
        "RSS de El Confidencial Empresas "
        "generado correctamente."
    )


if __name__ == "__main__":
    main()
