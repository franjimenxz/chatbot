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
    # AJUSTE: Usamos "webhook_token" del nuevo config.json
    if hub_verify_token == config["webhook_token"]:
        logging.info("webhook verificado!")
        return PlainTextResponse(content=hub_challenge, status_code=200)
    else:
        logging.error("La verificacion ha fallado, porque los tokens no coinciden")
        raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    logging.info("request: " + json.dumps(body))
    
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

@app.get("/send")
async def send_endpoint(number: str = Query(...), message: str = Query(...), platform: str = Query("instagram")):
    logging.info(f"{datetime.now()}: SEND - Numero: {number}. Mensaje: {message} a plataforma {platform}")
    # AJUSTE: Ahora se puede especificar la plataforma para pruebas
    await sendMessage(number, message, platform)
    return {"status": "OK", "message": "Sent"}

# Modelo para la petición de envío de mensaje
class SendMessageRequest(BaseModel):
    to: str
    body: str
    # AJUSTE: Opcional para especificar la plataforma desde el cuerpo de la petición
    platform: str = "instagram"

@app.post("/sendMessage")
async def send_message_endpoint(req: SendMessageRequest):
    if req:
        # AJUSTE: Pasamos la plataforma a la función de envío
        await sendMessage(req.to, req.body, req.platform)
        return {"status": "OK", "message": "Sent"}
    else:
        return {"status": "ERROR", "message": "Bad request: body faltante"}

# ---------------------------------------------------
# Funciones de negocio
# ---------------------------------------------------

async def process_event(event: dict, platform: str):
    """
    Procesa el evento recibido desde el webhook para Facebook o Instagram.
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
            data = await getDataFromMessage(message, platform)
            logging.info(f"{datetime.now()}: RECEIVE ({platform}) - senderID: {message['from']}. Mensaje: {message['body']}")
            await sendToNotification(data)

async def getDataFromMessage(message: dict, platform: str) -> dict:
    """
    Arma la data que se enviará a notificación.
    """
    # AJUSTE: Pasamos la plataforma a getContactInfo
    contacto = await getContactInfo(message["from"], platform)
    data = {
        "idAplicacion": config["idAplicacion"],
        "numeroWhatsappCliente": message["from"],
        "nombreAutor": contacto.get("name") if contacto else "Usuario",
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
    # (Esta función no necesita cambios, está bien como estaba)
    ...

async def sendWebhookChangeState(idWhatsapp, idEstado):
    # (Esta función no necesita cambios, está bien como estaba)
    ...


async def sendMessage(senderID: str, mensaje: str, platform: str):
    """
    Envía un mensaje usando la SendAPI de Facebook.
    Usa el único Page Access Token para todas las plataformas.
    """
    request_body = {
        "recipient": {"id": senderID},
        "message": {"text": mensaje},
        "messaging_type": "RESPONSE" # Recomendado para respuestas a mensajes de usuario
    }
    
    # CORRECCIÓN: La API de Mensajería para Instagram y Facebook usa el mismo dominio.
    # CORRECCIÓN: Usamos una versión de la API más reciente. v15.0 está obsoleta.
    url = "https://graph.facebook.com/v19.0/me/messages"
    
    # AJUSTE: Simplificado. Usamos un único token para todo.
    token = config.get("page_access_token")
    if not token:
        logging.error("¡ERROR! 'page_access_token' no está configurado en config.json")
        return
        
    params = {"access_token": token}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, params=params, json=request_body)
            response.raise_for_status() # Lanza un error si la respuesta no es 2xx
            logging.info(f"Mensaje enviado a {senderID}. Respuesta: {response.json()}")
        except httpx.HTTPStatusError as e:
            logging.error(f"Error enviando mensaje a {senderID}: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logging.error(f"Error inesperado enviando mensaje: {str(e)}")

async def getContactInfo(senderID: str, platform: str):
    """
    Obtiene la información de contacto del remitente usando la API de Facebook.
    """
    # CORRECCIÓN: Usamos el dominio graph.facebook.com y una versión de API reciente.
    url = f"https://graph.facebook.com/v19.0/{senderID}"
    
    # AJUSTE: Simplificado. Usamos un único token para todo.
    token = config.get("page_access_token")
    if not token:
        logging.error("¡ERROR! 'page_access_token' no está configurado en config.json para getContactInfo")
        return None
        
    params = {"fields": "name,profile_pic", "access_token": token}
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, params=params)
            res.raise_for_status()
            return res.json()
        except httpx.HTTPStatusError as e:
            logging.error(f"Error obteniendo info de contacto para {senderID}: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"Error inesperado obteniendo info de contacto: {str(e)}")
            return None

# ---------------------------------------------------
# Ejecución del servidor
# ---------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", config.get("port", 6001)))
    logging.info(f"{config['name']} Facebook Messenger Server is listening on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

