import os
from cryptography.fernet import Fernet

def get_fernet() -> Fernet:
    key = os.getenv("TG_DASH_FERNET_KEY")
    if not key:
        raise RuntimeError("Missing TG_DASH_FERNET_KEY env var")
    return Fernet(key.encode("utf-8"))

def encrypt_text(plain: str) -> str:
    f = get_fernet()
    return f.encrypt(plain.encode("utf-8")).decode("utf-8")

def decrypt_text(cipher: str) -> str:
    f = get_fernet()
    return f.decrypt(cipher.encode("utf-8")).decode("utf-8")
