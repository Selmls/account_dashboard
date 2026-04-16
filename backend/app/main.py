import os
import asyncio
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import select, or_, text
from dotenv import load_dotenv
from telethon.errors import SessionPasswordNeededError, PhoneNumberBannedError, FloodWaitError, UserDeactivatedBanError, UserDeactivatedError, PeerIdInvalidError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from fastapi.responses import Response

from .db import Base, engine, get_db
from .models import TelegramAccount
from .crypto import encrypt_text, decrypt_text
from .tg_auth import client_from_session, new_client_for_login, session_string_from_client
from app.models import TelegramAccountStatus
from app.tg_client import client_from_encrypted_session



load_dotenv()
# Create tables on startup (MVP approach)
Base.metadata.create_all(bind=engine)

# Simple migration: add new columns if they don't exist yet
with engine.connect() as _conn:
    for _col in ("first_name VARCHAR(128)", "last_name VARCHAR(128)", "used VARCHAR(32)", "telegram_user_id BIGINT"):
        try:
            _conn.execute(text(f"ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS {_col}"))
            _conn.commit()
        except Exception:
            pass
TELEGRAM_SERVICE_PEER_ID = 777000
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

class ConfirmCodeIn(BaseModel):
    code: str = Field(min_length=3, max_length=10)
    password: Optional[str] = None  # only if account has 2-step verification enabled

class AccountOut(BaseModel):
    id: int
    phone: str
    country: Optional[str]
    agent: Optional[str]
    status: str

    class Config:
        from_attributes = True

class AccountCreateIn(BaseModel):
    phone: str
    country: Optional[str] = None
    agent: Optional[str] = None

class AccountUpdateIn(BaseModel):
    country: Optional[str] = Field(default=None, max_length=128)
    agent: Optional[str] = Field(default=None, max_length=64)
    used: Optional[str] = Field(default=None, max_length=32)

@app.get("/")
def ui_home():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.post("/accounts", response_model=AccountOut)
def create_account(payload: AccountCreateIn, db: Session = Depends(get_db)):
    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone is required")

    exists = db.query(TelegramAccount).filter(TelegramAccount.phone == phone).first()
    if exists:
        raise HTTPException(status_code=400, detail="Account with this phone already exists")

    acc = TelegramAccount(
        phone=phone,
        country=payload.country,
        agent=payload.agent,
        status=TelegramAccountStatus.NEEDS_LOGIN,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)

    return acc

@app.patch("/accounts/{account_id}")
def update_account(account_id: int, payload: AccountUpdateIn, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")
    if "country" in payload.model_fields_set:
        acc.country = payload.country or None
    if "agent" in payload.model_fields_set:
        acc.agent = payload.agent or None
    if "used" in payload.model_fields_set:
        acc.used = payload.used or None
    db.commit()
    return {"ok": True}

@app.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(acc)
    db.commit()
    return {"ok": True}

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
                "first_name": a.first_name,
                "last_name": a.last_name,
                "used": a.used,
                "telegram_user_id": a.telegram_user_id,
                "has_session": bool(a.session_enc),
                "has_login_session": bool(a.login_session_enc),
            }
            for a in rows
        ],
    }

@app.post("/accounts/refresh_all")
async def refresh_all_accounts(db: Session = Depends(get_db)):
    ids = [a.id for a in db.query(TelegramAccount).all()]
    results = await asyncio.gather(*[_refresh_one(aid) for aid in ids], return_exceptions=False)
    return {"ok": True, "results": list(results)}

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
    if acc.status == TelegramAccountStatus.ACTIVE:
        pass
    else:
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
        me = await client.get_me()
        final_session = session_string_from_client(client)
    finally:
        await client.disconnect()

    acc.session_enc = encrypt_text(final_session)
    acc.login_session_enc = None
    acc.phone_code_hash = None
    acc.status = TelegramAccountStatus.ACTIVE
    acc.telegram_user_id = me.id
    acc.first_name = getattr(me, "first_name", None)
    acc.last_name = getattr(me, "last_name", None)
    db.commit()

    return {"ok": True, "status": acc.status}

async def _refresh_one(account_id: int) -> dict:
    from .db import SessionLocal

    # Read — hold the connection only long enough to fetch what we need
    db = SessionLocal()
    try:
        acc = db.get(TelegramAccount, account_id)
        if not acc:
            return {"id": account_id, "ok": False, "error": "not_found"}
        if not acc.session_enc:
            acc.status = TelegramAccountStatus.NEEDS_LOGIN
            db.commit()
            return {"id": account_id, "ok": False, "status": acc.status, "error": "no_session_enc"}
        session_enc = acc.session_enc
    finally:
        db.close()

    # Telegram work — no DB connection held during network I/O
    new_status = TelegramAccountStatus.ERROR
    first_name = last_name = telegram_user_id = None
    result_extra = {}

    client = client_from_encrypted_session(session_enc)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            new_status = TelegramAccountStatus.NEEDS_LOGIN
            result_extra = {"error": "not_authorized"}
        else:
            me = await client.get_me()
            telegram_user_id = me.id
            first_name = getattr(me, "first_name", None)
            last_name = getattr(me, "last_name", None)

            is_frozen = getattr(me, "restricted", False)
            if not is_frozen:
                try:
                    msg = await client.send_message(me, ".")
                    await client.delete_messages(me, [msg.id])
                except (UserDeactivatedBanError, UserDeactivatedError, PeerIdInvalidError):
                    is_frozen = True

            new_status = TelegramAccountStatus.FROZEN if is_frozen else TelegramAccountStatus.ACTIVE
    except PhoneNumberBannedError:
        new_status = TelegramAccountStatus.BANNED
        result_extra = {"error": "banned"}
    except FloodWaitError as e:
        new_status = TelegramAccountStatus.FLOOD_WAIT
        result_extra = {"error": "flood_wait", "wait_seconds": int(e.seconds)}
    except Exception as e:
        new_status = TelegramAccountStatus.ERROR
        result_extra = {"error": str(e)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # Write — open a fresh short-lived session just to persist the result
    db = SessionLocal()
    try:
        acc = db.get(TelegramAccount, account_id)
        if acc:
            acc.status = new_status
            if telegram_user_id is not None and acc.telegram_user_id is None:
                acc.telegram_user_id = telegram_user_id
            if first_name is not None or last_name is not None:
                acc.first_name = first_name
                acc.last_name = last_name
            db.commit()
    finally:
        db.close()

    ok = new_status in (TelegramAccountStatus.ACTIVE, TelegramAccountStatus.FROZEN)
    return {"id": account_id, "ok": ok, "status": new_status, **result_extra}

@app.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")
    return await _refresh_one(account_id)

@app.get("/accounts/{account_id}/telegram/777000/latest")
async def latest_service_message(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(TelegramAccount, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Not found")

    if acc.status != TelegramAccountStatus.ACTIVE or not acc.session_enc:
        raise HTTPException(status_code=400, detail=f"Account not active (status={acc.status})")

    client = client_from_encrypted_session(acc.session_enc)

    try:
        await client.connect()

        msg = await client.get_messages(TELEGRAM_SERVICE_PEER_ID, limit=1)
        if not msg:
            return {"ok": True, "peer_id": TELEGRAM_SERVICE_PEER_ID, "message": None}

        # Telethon returns a list-like; take first element
        m = msg[0]
        text = m.message or ""


        return {
            "ok": True,
            "peer_id": TELEGRAM_SERVICE_PEER_ID,
            "message": {
                "id": m.id,
                "date": m.date.isoformat() if m.date else None,
                "text": text,
            },
        }

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass