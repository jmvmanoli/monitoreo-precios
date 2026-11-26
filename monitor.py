"""Monitoreo de precios (Black Friday 2026).

Lee la hoja de Google (productos + links por tienda), consulta el precio actual
de cada link y envia un correo SOLO si el precio encontrado es menor al de
referencia en mas de UMBRAL pesos. Guarda lo ya avisado en estado.json para no
repetir el mismo correo cada 30 minutos.

Uso:
  python monitor.py            -> corrida normal (solo funciona dentro de la ventana)
  python monitor.py --forzar   -> ignora la ventana (para probar lectura de precios)
  python monitor.py --test     -> envia un correo SIMULACRO con datos inventados
"""
import csv
import html as _html
import io
import json
import os
import re
import smtplib
import sys
import time
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
DISPONIBILIDAD = {
    "InStock": "En stock",
    "OutOfStock": "Agotado",
    "SoldOut": "Agotado",
    "PreOrder": "Preventa",
    "BackOrder": "Sobre pedido",
    "LimitedAvailability": "Stock limitado",
    "InStoreOnly": "Solo en tienda",
    "OnlineOnly": "Solo online",
    "Discontinued": "Descontinuado",
}
AVISO_PAGO = (
    "Medio de pago, IVA y costo de despacho no se verifican automáticamente: "
    "confirmar en el enlace antes de comprar."
)


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


# ------------------------------------------------------------- SoloTodo ----
HOSTS_IGNORADOS = (
    "solotodo.cl", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "google.com", "googleapis.com", "gstatic.com", "apple.com", "tiktok.com", "linkedin.com",
    "whatsapp.com", "wa.me", "cloudflare.com", "w3.org", "schema.org",
)
PRECIO_RE = r"\$\s?(\d{1,3}(?:\.\d{3})+)"


def host_de(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def host_ignorado(host):
    return any(host == h or host.endswith("." + h) for h in HOSTS_IGNORADOS)


def texto_plano(fragmento):
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", fragmento, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(re.sub(r"\s+", " ", t))


def leer_solotodo(html, ref):
    """Lee una pagina de producto de SoloTodo.

    Devuelve (tiendas, diagnostico). Cada tienda es un dict con url (enlace directo
    a la tienda), host, efectivo y normal. Si la pagina no trae lo esperado devuelve
    lista vacia: nunca inventa datos.
    """
    anclas = []
    for m in re.finditer(r'<a\b[^>]*?href=["\'](https?://[^"\'\s>]+)["\']', html, flags=re.I):
        url = _html.unescape(m.group(1))
        host = host_de(url)
        if host and not host_ignorado(host):
            anclas.append((url, host, m.start(), html.find(">", m.end()) + 1))
    hosts = sorted({h for _u, h, _i, _f in anclas})[:8]
    diag = (
        f"bytes={len(html)} json_ld={'si' if 'ld+json' in html.lower() else 'no'} "
        f"next_data={'si' if '__NEXT_DATA__' in html else 'no'} "
        f"enlaces_externos={len(anclas)} hosts={','.join(hosts) or '-'}"
    )
    segmentos = {}
    for i, (url, host, _ini, fin) in enumerate(anclas):
        limite = len(html)
        for _u, h2, ini2, _f in anclas[i + 1:]:
            if h2 != host:
                limite = ini2
                break
        segmentos[url] = segmentos.get(url, "") + " " + texto_plano(html[fin:min(limite, fin + 3000)])

    tiendas, vistos = [], set()
    for url, seg in segmentos.items():
        host = host_de(url)
        if host in vistos:
            continue
        ef = re.search(r"efectivo\D{0,60}?" + PRECIO_RE, seg, flags=re.I)
        no = re.search(r"normal\D{0,60}?" + PRECIO_RE, seg, flags=re.I)
        if ef:
            efectivo = parse_precio(ef.group(1))
            normal = parse_precio(no.group(1)) if no else None
        else:
            nums = [parse_precio(x) for x in re.findall(PRECIO_RE, seg)][:2]
            if not nums:
                continue
            efectivo = min(nums)  # el precio en efectivo nunca supera al normal
            normal = max(nums) if len(nums) == 2 else None
        if not (0.5 * ref <= efectivo <= 1.6 * ref):
            continue  # fuera de rango: probablemente no es el precio de este producto
        vistos.add(host)
        tiendas.append({"url": url, "host": host, "efectivo": efectivo, "normal": normal})
    return tiendas, diag


def registrar_hallazgo(p, tienda, url, precio, disp, estado, hallazgos,
                       clave=None, fuente=None, url_comp=None, normal=None):
    """Compara contra la referencia y, si corresponde, agrega un hallazgo nuevo."""
    clave = clave or url
    dif = p["ref"] - precio
    if dif > UMBRAL and estado.get(clave) != precio:
        nota = ""
        if abs(precio - p["ref"] / 1.19) < p["ref"] * 0.02:
            nota = "el precio parece NETO (sin IVA); la diferencia podría no ser real."
        hallazgos.append({
            "producto": p["producto"], "tienda": tienda, "ref": p["ref"],
            "precio": precio, "dif": dif, "disp": DISPONIBILIDAD.get(disp, disp),
            "url": url, "nota": nota, "fuente": fuente, "url_comp": url_comp, "normal": normal,
        })
    estado[clave] = precio
    return dif


def revisar_solotodo(p, url, directos, estado, hallazgos, no_verificados):
    time.sleep(2)  # cortesia: espaciar las consultas a SoloTodo
    try:
        tiendas, diag = leer_solotodo(descargar(url), p["ref"])
    except Exception as ex:
        print(f"[ERROR] solotodo.cl: {ex}")
        no_verificados.append(f"{p['producto']} en SoloTodo (no se pudo abrir la página)")
        return
    print(f"[SOLOTODO] {p['producto']} | {diag} | tiendas leidas: {len(tiendas)}")
    if not tiendas:
        no_verificados.append(f"{p['producto']} en SoloTodo (no se pudo leer la lista de tiendas)")
        return
    for t in tiendas:
        if t["host"] in directos:
            continue  # esa tienda ya se revisa directo, con datos mas frescos
        dif = registrar_hallazgo(
            p, t["host"], t["url"], t["efectivo"], "No informada por SoloTodo", estado, hallazgos,
            clave="solotodo:" + t["url"], fuente="SoloTodo", url_comp=url, normal=t["normal"],
        )
        print(f"[SOLOTODO OK] {t['host']} | efectivo {t['efectivo']} | normal {t['normal']} "
              f"| ref {p['ref']} | dif {dif}")


# ---------------------------------------------------------------- correo ----
def construir_correo(hallazgos, no_verificados, ahora, simulacro=False):
    """Devuelve (asunto, texto_plano, html). Cada hallazgo es un dict con:
    producto, tienda, ref, precio, dif, disp, url, nota."""
    n = len(hallazgos)
    pref = "[SIMULACRO] " if simulacro else ""
    if n == 1:
        h = hallazgos[0]
        asunto = f"{pref}{h['producto']}: {clp(h['dif'])} más barato en {h['tienda']}"
    else:
        asunto = f"{pref}{n} productos con diferencia mayor a {clp(UMBRAL)}"

    hora = f"{ahora:%d-%m-%Y %H:%M} (hora de Chile)"

    # --- texto plano
    t = []
    if simulacro:
        t.append("*** SIMULACRO: los datos de este correo son inventados. ***\n")
    t.append(f"Se encontraron {n} producto(s) con una diferencia mayor a {clp(UMBRAL)}.")
    t.append(f"Revisión: {hora}\n")
    t.append("RESUMEN")
    for h in hallazgos:
        t.append(f"- {h['producto']}: {clp(h['dif'])} más barato en {h['tienda']} "
                 f"({clp(h['precio'])} vs referencia {clp(h['ref'])})")
    t.append("\nDETALLE")
    for i, h in enumerate(hallazgos, 1):
        t += [
            f"\n{i}) {h['producto']}",
            f"   Tienda: {h['tienda']}",
            f"   Precio de referencia: {clp(h['ref'])}",
            f"   Precio encontrado: {clp(h['precio'])}",
            f"   Diferencia: {clp(h['dif'])}",
            f"   Disponibilidad: {h['disp']}",
            f"   Enlace: {h['url']}",
        ]
        if h.get("normal"):
            t.append(f"   Precio normal (otro medio de pago): {clp(h['normal'])}")
        if h.get("fuente"):
            t.append(f"   Fuente: {h['fuente']} (puede estar desfasado; confirmar en la tienda)")
        if h.get("url_comp"):
            t.append(f"   Comparación en SoloTodo: {h['url_comp']}")
        if h.get("nota"):
            t.append(f"   OJO: {h['nota']}")
    t.append(f"\n{AVISO_PAGO}")
    if no_verificados:
        t.append("\nNo se pudo verificar:")
        t += [f"- {x}" for x in no_verificados]
    texto = "\n".join(t)

    # --- html (diseño corporativo: solo tablas y estilos en línea, lo lee cualquier correo)
    e = _html.escape
    color_disp = {
        "En stock": ("#065f46", "#d1fae5"),
        "Stock limitado": ("#92400e", "#fef3c7"),
        "Agotado": ("#991b1b", "#fee2e2"),
    }

    def tarjeta(h):
        fg, bg = color_disp.get(h["disp"], ("#374151", "#e5e7eb"))
        pct = f"{h['dif'] / h['ref'] * 100:.1f}".replace(".", ",")
        nota = ""
        if h.get("nota"):
            nota = (
                "<div style='margin-top:16px;background:#fffbeb;border-left:4px solid #f59e0b;"
                "padding:10px 12px;font-size:13px;color:#92400e'>"
                f"<b>Atención:</b> {e(h['nota'])}</div>"
            )
        fila = ("<tr><td style='padding:10px 0;border-bottom:1px solid #eef0f3;color:#6b7280'>{k}</td>"
                "<td align='right' style='padding:10px 0;border-bottom:1px solid #eef0f3;color:#111827'>{v}</td></tr>")
        extra = ""
        if h.get("normal"):
            extra += fila.format(k="Precio normal (otro medio de pago)", v=e(clp(h["normal"])))
        if h.get("fuente"):
            extra += fila.format(k="Fuente", v=e(h["fuente"]) + " <span style='color:#9ca3af'>(puede estar desfasado)</span>")
        enlace_comp = ""
        if h.get("url_comp"):
            enlace_comp = ("<div style='margin-top:12px;font-size:13px'>"
                           f"<a href=\"{e(h['url_comp'], quote=True)}\" style='color:#0b6bcb'>"
                           "Ver comparación en SoloTodo</a></div>")
        return f"""
<table role="presentation" width="100%" style="border-collapse:separate;border:1px solid #e5e7eb;border-radius:10px;margin:0 0 20px">
<tr><td style="padding:22px">
  <div style="font-size:11px;letter-spacing:1.6px;color:#6b7280;text-transform:uppercase">{e(h['tienda'])}</div>
  <div style="font-size:18px;font-weight:700;color:#0f2a47;margin:4px 0 16px;line-height:1.3">{e(h['producto'])}</div>
  <table role="presentation" width="100%" style="border-collapse:collapse"><tr>
    <td style="background:#ecfdf5;border-radius:8px;padding:14px 18px">
      <div style="font-size:11px;color:#047857;text-transform:uppercase;letter-spacing:1.2px">Ahorro frente a la referencia</div>
      <div style="font-size:32px;font-weight:700;color:#047857;line-height:1.2">{e(clp(h['dif']))}
        <span style="font-size:15px;font-weight:600">(-{pct} %)</span></div>
    </td></tr></table>
  <table role="presentation" width="100%" style="border-collapse:collapse;margin-top:12px;font-size:14px">
    {fila.format(k='Precio de referencia', v=e(clp(h['ref'])))}
    {fila.format(k='Precio encontrado', v='<b>' + e(clp(h['precio'])) + '</b>')}
    {fila.format(k='Disponibilidad', v=f"<span style='background:{bg};color:{fg};padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600'>{e(h['disp'])}</span>")}
    {extra}
  </table>
  <div style="margin-top:20px">
    <a href="{e(h['url'], quote=True)}" style="display:inline-block;background:#0b6bcb;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 24px;border-radius:6px">Ver producto en {e(h['tienda'])} &rarr;</a>
  </div>
  {enlace_comp}
  {nota}
</td></tr></table>"""

    banner = ""
    if simulacro:
        banner = ("<tr><td style='background:#fef3c7;padding:12px 28px;font-size:13px;color:#92400e;"
                  "border-bottom:1px solid #fcd34d'><b>SIMULACRO:</b> los datos de este correo son "
                  "inventados y sirven solo para ver el formato.</td></tr>")
    nv = ""
    if no_verificados:
        nv = ("<div style='margin-top:6px;font-size:13px;color:#374151'><b>No se pudo verificar:</b><br>"
              + "<br>".join(e(x) for x in no_verificados) + "</div>")
    cuerpo = f"""<div style="background:#eef1f5;padding:24px 12px;font-family:'Segoe UI',Arial,Helvetica,sans-serif;color:#111827">
<table role="presentation" width="100%" style="max-width:600px;margin:0 auto;border-collapse:collapse">
  <tr><td style="background:#0f2a47;border-radius:10px 10px 0 0;padding:28px">
    <div style="color:#8fb4e3;font-size:11px;letter-spacing:2.4px;text-transform:uppercase">Monitoreo de precios &middot; Black Friday 2026</div>
    <div style="color:#ffffff;font-size:26px;font-weight:700;margin-top:8px">Alerta de precios</div>
    <div style="color:#c9d6e8;font-size:14px;margin-top:8px">{n} oportunidad(es) con diferencia mayor a {e(clp(UMBRAL))} &middot; {e(hora)}</div>
  </td></tr>
  {banner}
  <tr><td style="background:#ffffff;padding:26px 28px 8px">{''.join(tarjeta(h) for h in hallazgos)}</td></tr>
  <tr><td style="background:#f8fafc;border-radius:0 0 10px 10px;padding:18px 28px 22px;font-size:12px;color:#6b7280;line-height:1.6">
    <div><b>Referencia:</b> precios de la hoja de cotización (pago en efectivo o transferencia).</div>
    <div>{e(AVISO_PAGO)}</div>
    {nv}
    <div style="margin-top:10px;color:#9ca3af">Mensaje automático del monitoreo de precios. Revisión cada 30 minutos hasta el lunes 30 de noviembre.</div>
  </td></tr>
</table></div>"""
    return asunto, texto, cuerpo


def enviar(asunto, texto, html=None):
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = os.environ["REMITENTE"].strip()
    msg["To"] = os.environ["DESTINATARIO"].strip()
    msg.set_content(texto)
    if html:
        msg.add_alternative(html, subtype="html")
    clave = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(msg["From"], clave)
        s.send_message(msg)


def hallazgo_ejemplo():
    """Datos INVENTADOS para el correo simulacro (--test)."""
    return {
        "producto": "MSI GEFORCE RTX 5070 TI SHADOW 3X OC (EJEMPLO)",
        "tienda": "tienda-ejemplo.cl",
        "ref": 1544928,
        "precio": 1489990,
        "dif": 1544928 - 1489990,
        "disp": "En stock",
        "url": "https://tienda-ejemplo.cl/producto-inventado",
        "nota": "",
        "fuente": "SoloTodo",
        "url_comp": "https://www.solotodo.cl/products/271704",
        "normal": 1565000,
    }


def main():
    args = sys.argv[1:]
    ahora = datetime.now(TZ)
    if "--test" in args:
        asunto, texto, cuerpo = construir_correo(
            [hallazgo_ejemplo()], ["Ejemplo de producto sin verificar en otra-tienda.cl"],
            ahora, simulacro=True,
        )
        enviar(asunto, texto, cuerpo)
        print("Correo SIMULACRO enviado.")
        return
    if "--forzar" not in args and not (INICIO <= ahora <= FIN):
        print(f"Fuera de la ventana ({ahora:%d-%m-%Y %H:%M}). No se hace nada.")
        return

    try:
        estado = json.load(open(ARCHIVO_ESTADO, encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}

    hallazgos, no_verificados = [], []
    for p in cargar_productos():
        directos = {host_de(u) for u in p["links"] if "solotodo.cl" not in u.lower()}
        for url in p["links"]:
            tienda = host_de(url)
            if tienda.endswith("solotodo.cl"):
                revisar_solotodo(p, url, directos, estado, hallazgos, no_verificados)
                continue
            try:
                precio, disp = extraer_precio(descargar(url))
            except Exception as ex:  # sitio caido, bloqueo, etc.
                precio, disp = None, None
                print(f"[ERROR] {tienda}: {ex}")
            if precio is None:
                no_verificados.append(f"{p['producto']} en {tienda}")
                print(f"[SIN PRECIO] {p['producto']} | {tienda}")
                continue
            dif = registrar_hallazgo(p, tienda, url, precio, disp, estado, hallazgos)
            print(f"[OK] {p['producto']} | {tienda} | ref {p['ref']} | hoy {precio} | dif {dif}")

    if hallazgos:
        asunto, texto, cuerpo = construir_correo(hallazgos, no_verificados, ahora)
        enviar(asunto, texto, cuerpo)
        print(f"Correo enviado con {len(hallazgos)} hallazgo(s).")
    else:
        print("Sin diferencias nuevas mayores al umbral.")

    json.dump(estado, open(ARCHIVO_ESTADO, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
