import os
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import select, or_
from dotenv import load_dotenv
from telethon.errors import SessionPasswordNeededError, PhoneNumberBannedError, FloodWaitError


from .db import Base, engine, get_db
from .models import TelegramAccount
from .crypto import encrypt_text, decrypt_text
from .tg_auth import client_from_session, new_client_for_login, session_string_from_client
from app.models import TelegramAccountStatus
from app.tg_client import client_from_encrypted_session



load_dotenv()
# Create tables on startup (MVP approach)
Base.metadata.create_all(bind=engine)

app = FastAPI()

class ConfirmCodeIn(BaseModel):
    code: str = Field(min_length=3, max_length=10)
    password: Optional[str] = None  # only if account has 2-step verification enabled

class AccountCreate(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    country: Optional[str] = Field(default=None, max_length=128)
    agent: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(default=TelegramAccountStatus.NEEDS_LOGIN, max_length=32)

class AccountOut(BaseModel):
    id: int
    phone: str
    country: Optional[str]
    agent: Optional[str]
    status: str

    class Config:
        from_attributes = True

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/accounts", response_model=AccountOut)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    # ensure unique phone
    existing = db.execute(
        select(TelegramAccount).where(TelegramAccount.phone == payload.phone.strip())
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Phone already exists")

    acc = TelegramAccount(
        phone=payload.phone.strip(),
        country=payload.country,
        agent=payload.agent,
        status=payload.status,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc

@app.get("/debug/version")
def debug_version():
    return {"send_code_has_await": "YES"}


@app.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    rows = db.query(TelegramAccount).order_by(TelegramAccount.id.desc()).all()
    return {
        "ok": True,
        "accounts": [
            {
                "id": a.id,
                "phone": a.phone,
                "country": a.country,
                "agent": a.agent,
                "status": a.status,
                "has_session": bool(a.session_enc),
                "has_login_session": bool(a.login_session_enc),
            }
            for a in rows
        ],
    }

@app.post("/accounts/{account_id}/send_code")
async def send_code(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")
    phone = acc.phone.replace(" ", "")
    if not phone:
        raise HTTPException(status_code=400, detail="Account has no phone number")

    client = new_client_for_login()
    await client.connect()
    try:
        sent = await client.send_code_request(acc.phone)
        temp_session = session_string_from_client(client)
    finally:
        await client.disconnect()

    acc.login_session_enc = encrypt_text(temp_session)
    acc.phone_code_hash = sent.phone_code_hash
    acc.status = TelegramAccountStatus.CODE_SENT
    db.commit()

    return {"ok": True, "phone": acc.phone, "debug_hash_prefix": sent.phone_code_hash[:10]}



@app.post("/accounts/{account_id}/confirm_code")
async def confirm_code(account_id: int, payload: ConfirmCodeIn, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")
    if not acc.phone_code_hash:
        raise HTTPException(status_code=400, detail="No code sent to this account. Call /send_code first.")

    print("CONFIRM using hash prefix:", acc.phone_code_hash[:10] if acc.phone_code_hash else None)

    temp_session = decrypt_text(acc.login_session_enc)
    client = client_from_session(temp_session)

    await client.connect()
    try:
        try: 
            await client.sign_in(phone=acc.phone, code=payload.code, phone_code_hash=acc.phone_code_hash)
        except SessionPasswordNeededError:
            if not payload.password:
                acc.status = TelegramAccountStatus.TWO_FA_REQUIRED
                db.commit()
                raise HTTPException(status_code=400, detail="2-step password required. Send password in request.")
            await client.sign_in(password=payload.password)
        except Exception as e:
            acc.status = TelegramAccountStatus.ERROR
            db.commit()
            raise HTTPException(status_code=400, detail=f"Failed to sign in: {str(e)}")
        final_session = session_string_from_client(client)
    finally:
        await client.disconnect()

    acc.session_enc = encrypt_text(final_session)
    acc.login_session_enc = None
    acc.phone_code_hash = None
    acc.status = TelegramAccountStatus.ACTIVE
    db.commit()

    return {"ok": True, "status": acc.status}

@app.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")

    if not acc.session_enc:
        acc.status = TelegramAccountStatus.NEEDS_LOGIN
        db.commit()
        return {"ok": False, "status": acc.status, "error": "no_session_enc"}

    client = client_from_encrypted_session(acc.session_enc)

    try:
        await client.connect()

        is_auth = await client.is_user_authorized()
        if not is_auth:
            acc.status = TelegramAccountStatus.NEEDS_LOGIN
            db.commit()
            return {"ok": False, "status": acc.status, "error": "not_authorized"}

        # Optional: this is a nice sanity check
        me = await client.get_me()

        acc.status = TelegramAccountStatus.ACTIVE
        db.commit()

        return {
            "ok": True,
            "status": acc.status,
            "me": {
                "id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
                "last_name": getattr(me, "last_name", None),
                "is_bot": bool(getattr(me, "bot", False)),
            },
        }

    except PhoneNumberBannedError:
        acc.status = TelegramAccountStatus.BANNED
        db.commit()
        return {"ok": False, "status": acc.status, "error": "banned"}

    except SessionPasswordNeededError:
        # Usually not expected here if session_enc is valid, but good to reflect reality
        acc.status = TelegramAccountStatus.TWO_FA_REQUIRED
        db.commit()
        return {"ok": False, "status": acc.status, "error": "2fa_required"}

    except FloodWaitError as e:
        acc.status = TelegramAccountStatus.FLOOD_WAIT
        db.commit()
        return {"ok": False, "status": acc.status, "error": "flood_wait", "wait_seconds": int(e.seconds)}

    except Exception as e:
        acc.status = TelegramAccountStatus.ERROR
        db.commit()
        return {"ok": False, "status": acc.status, "error": str(e)}

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass