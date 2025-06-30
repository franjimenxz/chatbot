import os
import json
import asyncio
import logging
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, HTTPException, Query, Response, BackgroundTasks
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Configuración del log
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Cargar configuración desde config.json
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# Constantes de estado
ESTADOSSDN = {
    "ELIMINADO": 0,
    "PENDIENTE": 1,
    "ENVIADO": 2,
    "ERROR": 3,
    "LEIDO": 5
}

app = FastAPI()

# ---------------------------------------------------
# Endpoints
# ---------------------------------------------------

@app.get("/")
async def index():
    return PlainTextResponse("Se ha desplegado de manera exitosa el CMaquera ChatBot :D!!!")

@app.get("/webhook")
async def verify_webhook(
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge")
):
    if hub_verify_token == config["webhookToken"]:
        logging.info("webhook verificado!")
        return PlainTextResponse(content=hub_challenge, status_code=200)
    else:
        logging.error("La verificacion ha fallado, porque los tokens no coinciden")
        raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    logging.info("request: " + json.dumps(body))
    # Determinar la plataforma: "page" para Messenger, "instagram" para Instagram Direct
    platform = body.get("object")
    if platform in ["page", "instagram"]:
        entries = body.get("entry", [])
        for entry in entries:
            for event in entry.get("messaging", []):
                if "message" in event:
                    if event["message"].get("is_deleted", False):
                        await sendWebhookChangeState(event["message"].get("mid"), ESTADOSSDN["ELIMINADO"])
                    elif not event["message"].get("is_echo", False):
                        await process_event(event, platform)
                elif "reaction" in event or "postback" in event:
                    await process_event(event, platform)
                elif "read" in event:
                    await sendWebhookChangeState(event["read"].get("mid"), ESTADOSSDN["LEIDO"])
        return Response(status_code=200)
    return Response(status_code=200)

@app.get("/send")
async def send_endpoint(number: str = Query(...), message: str = Query(...)):
    logging.info(f"{datetime.now()}: SEND - Numero: {number}. Mensaje: {message}")
    # Para pruebas manuales, usamos por defecto el token de Facebook ("page")
    await sendMessage(number, message, "page")
    return {"status": "OK", "message": "Sent"}

# Modelo para la petición de envío de mensaje
class SendMessageRequest(BaseModel):
    to: str
    body: str

@app.post("/sendMessage")
async def send_message_endpoint(req: SendMessageRequest):
    if req:
        await sendPlainMessage(req)
        return {"status": "OK", "message": "Sent"}
    else:
        return {"status": "ERROR", "message": "Bad request: body faltante"}

# ---------------------------------------------------
# Funciones de negocio
# ---------------------------------------------------

async def process_event(event: dict, platform: str):
    """
    Procesa el evento recibido desde el webhook para Facebook o Instagram.
    Extrae el mensaje y, si es "!ping", responde con "pong".
    """
    message = {}
    if "message" in event:
        message["mid"] = event["message"].get("mid")
        message["body"] = event["message"].get("text")
    elif "reaction" in event:
        message["mid"] = event["reaction"].get("mid")
        message["body"] = event["reaction"].get("emoji")
    elif "postback" in event:
        message["mid"] = event["postback"].get("mid")
        message["body"] = event["postback"].get("title")
    message["from"] = event["sender"]["id"]

    if message.get("body"):
        if message["body"] == "!ping":
            await sendMessage(message["from"], "pong", platform)
        else:
            data = await getDataFromMessage(message)
            logging.info(f"{datetime.now()}: RECEIVE ({platform}) - senderID: {message['from']}. Mensaje: {message['body']}")
            await sendToNotification(data)

async def getDataFromMessage(message: dict) -> dict:
    """
    Arma la data que se enviará a notificación,
    obteniendo información de contacto y detalles del mensaje.
    """
    contacto = await getContactInfo(message["from"])
    data = {
        "idAplicacion": config["idAplicacion"],
        "numeroWhatsappCliente": message["from"],
        "nombreAutor": contacto.get("name") if contacto else "test",
        "infoMensaje": await getMessageInfo(message)
    }
    return data

async def getMessageInfo(message: dict) -> dict:
    """
    Retorna la información del mensaje.
    """
    messageInfo = {
        "idWhatsapp": message.get("mid"),
        "body": message.get("body"),
        "hasMedia": False
    }
    return messageInfo

async def sendToNotification(data: dict):
    """
    Envía la data del mensaje a la URL de notificación.
    Si 'notificationServerUrl' no está configurado, solo se loguea la data.
    """
    data["idCanal"] = 2
    if not config.get("notificationServerUrl"):
        logging.info(f"No se envía notificación ya que 'notificationServerUrl' no está configurado. Data: {json.dumps(data)}")
        return
    notification_url = f"{config['notificationServerUrl']}/whatsapp/service/mensaje"
    logging.info(f"{datetime.now()}: Enviamos a {notification_url}. Data: {json.dumps(data)}")
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(notification_url, json=data)
            logging.info(f"{datetime.now()}: Respuesta OK - Response: {res.text}")
        except Exception as e:
            logging.error(f"{datetime.now()}: Respuesta ERROR - Response: {str(e)}")

async def sendWebhookChangeState(idWhatsapp, idEstado):
    """
    Envía un cambio de estado (por ejemplo, mensaje eliminado o leído) a la URL de notificación.
    Si 'notificationServerUrl' no está configurado, solo se loguea la acción.
    Si la respuesta no es 200, reintenta la petición luego de 10 segundos.
    """
    if not config.get("notificationServerUrl"):
        logging.info(f"No se envía cambio de estado ya que 'notificationServerUrl' no está configurado. idWhatsapp: {idWhatsapp}, idEstado: {idEstado}")
        return
    url = f"{config['notificationServerUrl']}/mensaje/cambiarEstadoWebhook"
    logging.info(f"Webhook: {url} id: {idWhatsapp} ack: {idEstado}")
    jsonRequest = {
        "idWhatsapp": idWhatsapp,
        "idEstado": idEstado
    }
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=jsonRequest)
            if res.status_code != 200:
                await asyncio.sleep(10)
                await sendWebhookChangeState(idWhatsapp, idEstado)
        except Exception as e:
            logging.error(f"Error sending webhook change state: {str(e)}")
            await asyncio.sleep(10)
            await sendWebhookChangeState(idWhatsapp, idEstado)

async def sendPlainMessage(requestBody: SendMessageRequest):
    """
    Envía un mensaje de texto plano.
    """
    number = requestBody.to
    mensaje = requestBody.body
    if mensaje:
        logging.info(f"{datetime.now()}: SEND - Numero: {number}. Mensaje: {mensaje}")
        # Para envíos manuales, usamos por defecto el token de Facebook ("page")
        await sendMessage(number, mensaje, "page")

async def sendMessage(senderID: str, mensaje: str, platform: str):
    """
    Envía un mensaje usando la SendAPI de Facebook.
    Selecciona el token adecuado según la plataform23 a.
    """
    request_body = {
        "recipient": {"id": senderID},
        "message": {"text": mensaje}
    }
    url = "https://graph.instagram.com/v15.0/me/messages"
    if platform == "page":
        token = config.get("access_token_facebook")
    elif platform == "instagram":
        token = config.get("access_token_instagram")
    else:
        token = config.get("access_token")
    params = {"access_token": token}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, params=params, json=request_body)
        except Exception as e:
            logging.error(f"Error sending message: {str(e)}")

async def getContactInfo(senderID: str):
    """
    Obtiene la información de contacto del remitente usando la API de Facebook.
    Para obtener información, se usa por defecto el token de Facebook.
    """
    url = f"https://graph.instagram.com/v15.0/{senderID}"
    token = config.get("access_token_instagram")  # Puedes ajustar esto si es necesario
    params = {"fields": "name", "access_token": token}
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, params=params)
            return res.json()
        except Exception as e:
            logging.error(f"Error getting contact info: {str(e)}")
            return None



# ---------------------------------------------------
# Ejecución del servidor
# ---------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", config.get("port", 6001)))
    logging.info(f"{config['name']} Facebook Messenger Server is listening on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
