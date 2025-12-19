import os
from telethon import TelegramClient
from telethon.sessions import StringSession

def _creds():
    api_id = int(os.getenv("TG_API_ID"))
    api_hash = os.getenv("TG_API_HASH")
    return api_id, api_hash

def client_from_session(session_str: str) -> TelegramClient:
    api_id, api_hash = _creds()
    return TelegramClient(StringSession(session_str), api_id, api_hash)

def new_client_for_login() -> TelegramClient:
    api_id, api_hash = _creds()
    return TelegramClient(StringSession(), api_id, api_hash)

def session_string_from_client(client: TelegramClient) -> str:
    return client.session.save()
