"""Monitoreo de precios (Black Friday 2026).

Lee la hoja de Google (productos + links por tienda), consulta el precio actual
de cada link y envia un correo SOLO si el precio encontrado es menor al de
referencia en mas de UMBRAL pesos. Guarda lo ya avisado en estado.json para no
repetir el mismo correo cada 30 minutos.

Uso:
  python monitor.py            -> corrida normal (solo funciona dentro de la ventana)
  python monitor.py --forzar   -> ignora la ventana (para probar lectura de precios)
  python monitor.py --test     -> envia un correo de prueba y termina
"""
import csv
import io
import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SHEET_ID = "1R6GX1hd8jc8f4T4w-nfSzjJyX4xIHAltbaF0iuqoFWo"
UMBRAL = 25000
TZ = ZoneInfo("America/Santiago")
INICIO = datetime(2026, 11, 27, 0, 0, 0, tzinfo=TZ)
FIN = datetime(2026, 11, 30, 23, 59, 59, tzinfo=TZ)
ARCHIVO_ESTADO = "estado.json"
UA = {
    "User-Agent": "monitoreo-precios/1.0 (uso personal, 1 consulta cada 30 min)",
    "Accept-Language": "es-CL,es;q=0.9",
}


def descargar(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_precio(valor):
    """'485.792', '$ 1.544.928', 485792, '485792.00' -> int. Si no se puede, None."""
    s = str(valor).strip()
    if re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return int(float(s))
    digitos = re.sub(r"\D", "", s)
    return int(digitos) if digitos else None


def recorrer(nodo):
    if isinstance(nodo, dict):
        yield nodo
        for v in nodo.values():
            yield from recorrer(v)
    elif isinstance(nodo, list):
        for v in nodo:
            yield from recorrer(v)


def extraer_precio(html):
    """Devuelve (precio, disponibilidad). Usa datos estructurados de la pagina."""
    for bloque in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.S | re.I,
    ):
        try:
            datos = json.loads(bloque)
        except ValueError:
            continue
        for nodo in recorrer(datos):
            ofertas = nodo.get("offers")
            if not ofertas:
                continue
            for oferta in ofertas if isinstance(ofertas, list) else [ofertas]:
                if not isinstance(oferta, dict):
                    continue
                bruto = oferta.get("price") or oferta.get("lowPrice")
                precio = parse_precio(bruto) if bruto is not None else None
                if precio:
                    disp = str(oferta.get("availability", "no informada"))
                    return precio, disp.rsplit("/", 1)[-1]
    m = re.search(
        r'<meta[^>]+(?:product:price:amount|og:price:amount)["\'][^>]+content=["\']([\d.,]+)',
        html, flags=re.I,
    )
    if m:
        return parse_precio(m.group(1)), "no informada"
    return None, None


def cargar_productos():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    filas = list(csv.reader(io.StringIO(descargar(url))))
    cab = next(i for i, f in enumerate(filas) if "Componente / Modelo" in f)
    nombres = [c.strip() for c in filas[cab]]
    i_prod = nombres.index("Componente / Modelo")
    i_precio = nombres.index("Precio Unitario")
    i_links = [i for i, n in enumerate(nombres) if n.lower().startswith("link")]
    productos = []
    for f in filas[cab + 1:]:
        if len(f) <= i_precio or not f[i_prod].strip():
            continue
        ref = parse_precio(f[i_precio])
        links = [f[i].strip() for i in i_links if i < len(f) and f[i].startswith("http")]
        if ref and links:
            productos.append({"producto": f[i_prod].strip(), "ref": ref, "links": links})
    return productos


def clp(n):
    return "$ " + f"{n:,}".replace(",", ".")


def enviar(asunto, cuerpo):
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = os.environ["REMITENTE"].strip()
    msg["To"] = os.environ["DESTINATARIO"].strip()
    msg.set_content(cuerpo)
    clave = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(msg["From"], clave)
        s.send_message(msg)


def main():
    args = sys.argv[1:]
    if "--test" in args:
        enviar("Prueba del monitoreo de precios", "Si lees esto, el envio de correos funciona.")
        print("Correo de prueba enviado.")
        return
    ahora = datetime.now(TZ)
    if "--forzar" not in args and not (INICIO <= ahora <= FIN):
        print(f"Fuera de la ventana ({ahora:%d-%m-%Y %H:%M}). No se hace nada.")
        return

    try:
        estado = json.load(open(ARCHIVO_ESTADO, encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}

    hallazgos, no_verificados = [], []
    for p in cargar_productos():
        for url in p["links"]:
            tienda = urlparse(url).netloc.replace("www.", "")
            try:
                precio, disp = extraer_precio(descargar(url))
            except Exception as e:  # sitio caido, bloqueo, etc.
                precio, disp = None, None
                print(f"[ERROR] {tienda}: {e}")
            if precio is None:
                no_verificados.append(f"{p['producto']} en {tienda}")
                print(f"[SIN PRECIO] {p['producto']} | {tienda}")
                continue
            dif = p["ref"] - precio
            print(f"[OK] {p['producto']} | {tienda} | ref {p['ref']} | hoy {precio} | dif {dif}")
            if dif > UMBRAL and estado.get(url) != precio:
                nota = ""
                if abs(precio - p["ref"] / 1.19) < p["ref"] * 0.02:
                    nota = "  OJO: el precio parece NETO (sin IVA); la diferencia podria no ser real.\n"
                hallazgos.append(
                    f"Producto: {p['producto']}\n"
                    f"Tienda: {tienda}\n"
                    f"Precio de referencia: {clp(p['ref'])}\n"
                    f"Precio encontrado: {clp(precio)}\n"
                    f"Diferencia: {clp(dif)}\n"
                    f"Disponibilidad: {disp}\n"
                    f"Enlace: {url}\n"
                    f"{nota}"
                    "Medio de pago / IVA / costo de despacho: NO verificado automaticamente; "
                    "confirmar en el enlace.\n"
                )
            if precio is not None:
                estado[url] = precio

    if hallazgos:
        cuerpo = (
            f"Diferencias mayores a {clp(UMBRAL)} (referencia: precios de la hoja, con IVA).\n"
            f"Revision: {ahora:%d-%m-%Y %H:%M} (hora de Chile)\n\n" + "\n".join(hallazgos)
        )
        if no_verificados:
            cuerpo += "\nNo se pudo verificar:\n- " + "\n- ".join(no_verificados) + "\n"
        enviar(f"Alerta de precios: {len(hallazgos)} diferencia(s) mayor(es) a {clp(UMBRAL)}", cuerpo)
        print(f"Correo enviado con {len(hallazgos)} hallazgo(s).")
    else:
        print("Sin diferencias nuevas mayores al umbral.")

    json.dump(estado, open(ARCHIVO_ESTADO, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
