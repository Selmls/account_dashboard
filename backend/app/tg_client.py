import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from .crypto import decrypt_text

API_ID_ENV = "TG_API_ID"
API_HASH_ENV = "TG_API_HASH"

def get_api_creds() -> tuple[int, str]:
    api_id = os.getenv(API_ID_ENV)
    api_hash = os.getenv(API_HASH_ENV)
    if not api_id or not api_hash:
        raise RuntimeError(f"Missing {API_ID_ENV} or {API_HASH_ENV} env vars")
    return int(api_id), api_hash

def client_from_encrypted_session(session_enc: str) -> TelegramClient:
    api_id, api_hash = get_api_creds()
    session_str = decrypt_text(session_enc)
    return TelegramClient(StringSession(session_str), api_id, api_hash)
