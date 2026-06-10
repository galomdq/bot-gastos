import os
import re
import json
import requests
import tempfile
from datetime import datetime, timezone, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Clientes ──────────────────────────────────────────────────────────────────
def get_openai():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_twilio():
    return Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_hoja():
    gc = conectar_sheets()
    spreadsheet = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    return spreadsheet.worksheet(os.getenv("HOJA_NOMBRE", "Hoja 1"))

def conectar_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(creds)

def get_diccionario():
    """Lee la hoja Categorias y devuelve un dict {subcategoria: categoria}."""
    gc = conectar_sheets()
    spreadsheet = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    hoja = spreadsheet.worksheet("Categorias")
    filas = hoja.get_all_values()
    diccionario = {}
    for fila in filas[1:]:  # saltar encabezado
        if len(fila) >= 2 and fila[0].strip():
            sub = fila[0].strip().lower()
            cat = fila[1].strip().capitalize()
            diccionario[sub] = cat
    return diccionario

def buscar_categoria(subcategoria, diccionario):
    """Busca la categoría de una subcategoría. Si no encuentra, devuelve Varios."""
    return diccionario.get(subcategoria.strip().lower(), "Varios")

def cargar_gasto(origen, categoria, subcategoria, monto):
    hoja = get_hoja()
    ahora = datetime.now(timezone(timedelta(hours=-3)))
    fecha = ahora.strftime("%d/%m/%Y")
    hora = ahora.strftime("%H:%M")
    hoja.append_row([fecha, hora, origen, categoria, subcategoria, monto])


# ── Normalizar origen ─────────────────────────────────────────────────────────
def normalizar_origen(texto):
    aliases_particular = ["casa", "particular", "personales", "personal", "hogar"]
    aliases_negocio = ["negocio", "gimnasio", "trabajo", "gym", "urban"]
    t = texto.strip().lower()
    if t in aliases_particular:
        return "Particular"
    elif t in aliases_negocio:
        return "Negocio"
    else:
        return texto.strip().capitalize()


# ── Parseo del mensaje ────────────────────────────────────────────────────────
def limpiar_monto(texto):
    solo_numeros = re.sub(r"[^\d]", "", texto)
    return int(solo_numeros) if solo_numeros else None


def parsear_gasto(texto, diccionario):
    """
    Formato principal (3 campos):  origen , subcategoria , monto
    Formato extendido (4 campos):  origen , subcategoria , descripcion_extra , monto
    """
    partes = [p.strip() for p in texto.split(",")]

    if len(partes) == 3:
        origen_raw, subcategoria_raw, monto_raw = partes
        descripcion_extra = ""
    elif len(partes) == 4:
        origen_raw, subcategoria_raw, descripcion_extra, monto_raw = partes
    else:
        return None

    monto = limpiar_monto(monto_raw)
    if not monto:
        return None

    origen = normalizar_origen(origen_raw)
    subcategoria = subcategoria_raw.strip().capitalize()
    categoria = buscar_categoria(subcategoria_raw, diccionario)

    # Si hay descripción extra la combinamos con subcategoría
    if descripcion_extra.strip():
        descripcion_final = f"{subcategoria} - {descripcion_extra.strip().capitalize()}"
    else:
        descripcion_final = subcategoria

    return {
        "origen": origen,
        "categoria": categoria,
        "subcategoria": descripcion_final,
        "monto": monto,
    }


# ── Transcripción de audio ────────────────────────────────────────────────────
def transcribir_audio(media_url):
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    respuesta = requests.get(media_url, auth=(sid, token))

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(respuesta.content)
        ruta = f.name

    with open(ruta, "rb") as audio:
        resultado = get_openai().audio.transcriptions.create(
            model="whisper-1",
            file=audio,
            language="es",
        )
    os.unlink(ruta)
    return resultado.text


# ── Mensaje de confirmación ───────────────────────────────────────────────────
def mensaje_ok(datos):
    monto_fmt = f"${datos['monto']:,.0f}".replace(",", ".")
    ahora = datetime.now(timezone(timedelta(hours=-3)))
    return (
        f"✅ *Gasto registrado*\n"
        f"🏠 {datos['origen']}\n"
        f"📂 {datos['categoria']} › {datos['subcategoria']}\n"
        f"💰 {monto_fmt}\n"
        f"🕐 {ahora.strftime('%d/%m/%Y %H:%M')}"
    )


AYUDA = (
    "📋 *Formato para cargar un gasto:*\n\n"
    "*3 campos:*\n"
    "`origen , subcategoría , monto`\n"
    "• `particular , luz , 35000`\n"
    "• `gimnasio , ferreteria , 4000`\n"
    "• `casa , mercado , 15000`\n\n"
    "*4 campos (con detalle extra):*\n"
    "`origen , subcategoría , detalle , monto`\n"
    "• `negocio , repuestos , bulones , 4000`\n"
    "• `particular , servicios , agua , 49000`\n\n"
    "*Orígenes válidos:*\n"
    "• Particular: casa, particular, personal, hogar\n"
    "• Negocio: negocio, gimnasio, gym, urban\n\n"
    "💡 Las categorías se buscan automáticamente.\n"
    "   Si no se encuentra → *Varios*\n\n"
    "Comandos: `ayuda` | `ultimo`"
)


def ultimo_gasto():
    try:
        hoja = get_hoja()
        filas = hoja.get_all_values()
        if len(filas) <= 1:
            return "No hay gastos cargados todavía."
        ultima = filas[-1]
        monto_fmt = f"${int(ultima[5]):,.0f}".replace(",", ".")
        return (
            f"📌 *Último gasto:*\n"
            f"🏠 {ultima[2]}\n"
            f"📂 {ultima[3]} › {ultima[4]}\n"
            f"💰 {monto_fmt}\n"
            f"🕐 {ultima[0]} {ultima[1]}"
        )
    except Exception as e:
        return f"Error al leer la planilla: {e}"


# ── Webhook principal ─────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    numero_origen = request.form.get("From", "")
    mi_numero = os.getenv("TU_NUMERO_WHATSAPP", "")

    if mi_numero and numero_origen != mi_numero:
        return "", 403

    tipo = request.form.get("MediaContentType0", "")
    texto_raw = request.form.get("Body", "").strip()

    if tipo.startswith("audio"):
        media_url = request.form.get("MediaUrl0")
        try:
            texto_raw = transcribir_audio(media_url)
        except Exception as e:
            return responder(f"❌ No pude transcribir el audio: {e}")

    texto = texto_raw.lower().strip()

    if texto in ("ayuda", "help", "hola", "?"):
        return responder(AYUDA)

    if texto in ("ultimo", "último", "last"):
        return responder(ultimo_gasto())

    # Cargar diccionario desde Google Sheets
    try:
        diccionario = get_diccionario()
    except Exception as e:
        return responder(f"❌ Error al leer el diccionario de categorías:\n{e}")

    datos = parsear_gasto(texto_raw, diccionario)
    if not datos:
        return responder(
            f"❓ No entendí el formato.\n\n"
            f"Enviá *ayuda* para ver los ejemplos.\n\n"
            f"_Recibí:_ `{texto_raw}`"
        )

    try:
        cargar_gasto(
            datos["origen"],
            datos["categoria"],
            datos["subcategoria"],
            datos["monto"],
        )
        return responder(mensaje_ok(datos))
    except Exception as e:
        return responder(f"❌ Error al guardar en la planilla:\n{e}")


def responder(texto):
    resp = MessagingResponse()
    resp.message(texto)
    return str(resp)


@app.route("/ping")
def ping():
    return "pong", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
