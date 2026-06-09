import os
import re
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
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    import json
    creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    return spreadsheet.worksheet(os.getenv("HOJA_NOMBRE", "Hoja1"))


def cargar_gasto(descripcion, categoria, subcategoria, monto):
    hoja = get_hoja()
    ahora = datetime.now(timezone(timedelta(hours=-3)))
    fecha = ahora.strftime("%d/%m/%Y")
    hora = ahora.strftime("%H:%M")
    hoja.append_row([fecha, hora, descripcion, categoria, subcategoria, monto])


# ── Parseo del mensaje ────────────────────────────────────────────────────────
# Formato esperado: descripcion , categoria , subcategoria , monto
# El monto puede venir como "115000", "115000 pesos", "$115.000", etc.

def limpiar_monto(texto):
    """Extrae solo los dígitos del monto."""
    solo_numeros = re.sub(r"[^\d]", "", texto)
    return int(solo_numeros) if solo_numeros else None


def parsear_gasto(texto):
    """
    Intenta parsear el texto en (descripcion, categoria, subcategoria, monto).
    Devuelve None si el formato no es válido.
    """
    partes = [p.strip() for p in texto.split(",")]

    if len(partes) == 4:
        descripcion, categoria, subcategoria, monto_raw = partes
    elif len(partes) == 3:
        # Sin subcategoría: descripcion , categoria , monto
        descripcion, categoria, monto_raw = partes
        subcategoria = "-"
    else:
        return None

    monto = limpiar_monto(monto_raw)
    if not monto:
        return None

 # Normalizar categoría
    aliases_particular = ["casa", "particular", "personales", "personal"]
    aliases_negocio = ["negocio", "gimnasio", "trabajo", "gym"]
    cat = categoria.strip().lower()
    if cat in aliases_particular:
        categoria = "Particular"
    elif cat in aliases_negocio:
        categoria = "Negocio"
    else:
        categoria = categoria.strip().capitalize()

    return {
        "descripcion": descripcion.capitalize(),
        "categoria": categoria.strip().capitalize(),
        "subcategoria": subcategoria.strip().capitalize(),
        "monto": monto,
    }


# ── Transcripción de audio ────────────────────────────────────────────────────
def transcribir_audio(media_url):
    """Descarga el audio de Twilio y lo transcribe con Whisper."""
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
    return (
        f"✅ *Gasto registrado*\n"
        f"📝 {datos['descripcion']}\n"
        f"🏷️ {datos['categoria']} › {datos['subcategoria']}\n"
        f"💰 {monto_fmt}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )


AYUDA = (
    "📋 *Formato para cargar un gasto:*\n"
    "`descripción , categoría , subcategoría , monto`\n\n"
    "*Ejemplos:*\n"
    "• `combustible , particular , auto , 115000`\n"
    "• `factura de luz , negocio , calefaccion , 535000`\n"
    "• `sueldo , negocio , pedro , 2500000`\n\n"
    "También podés enviar un *audio* con el mismo formato hablado.\n\n"
    "Comandos:\n"
    "• `ayuda` — muestra este mensaje\n"
    "• `ultimo` — muestra el último gasto cargado"
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
            f"📝 {ultima[2]}\n"
            f"🏷️ {ultima[3]} › {ultima[4]}\n"
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

    # Seguridad: solo responder a tu propio número
    if mi_numero and numero_origen != mi_numero:
        return "", 403

    tipo = request.form.get("MediaContentType0", "")
    texto_raw = request.form.get("Body", "").strip()

    # ── Audio ──
    if tipo.startswith("audio"):
        media_url = request.form.get("MediaUrl0")
        try:
            texto_raw = transcribir_audio(media_url)
        except Exception as e:
            return responder(f"❌ No pude transcribir el audio: {e}")

    texto = texto_raw.lower().strip()

    # ── Comandos especiales ──
    if texto in ("ayuda", "help", "hola", "?"):
        return responder(AYUDA)

    if texto in ("ultimo", "último", "last"):
        return responder(ultimo_gasto())

    # ── Parsear gasto ──
    datos = parsear_gasto(texto_raw)
    if not datos:
        return responder(
            f"❓ No entendí el formato.\n\n"
            f"Enviá *ayuda* para ver los ejemplos.\n\n"
            f"_Recibí:_ `{texto_raw}`"
        )

    try:
        cargar_gasto(
            datos["descripcion"],
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


# ── Keep-alive (evita que Railway duerma el servidor) ─────────────────────────
@app.route("/ping")
def ping():
    return "pong", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
