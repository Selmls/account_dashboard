from sqlalchemy import BigInteger, Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from .db import Base


class TelegramAccountStatus:
    NEEDS_LOGIN = "needs_login"
    CODE_SENT = "code_sent"
    ACTIVE = "active"
    TWO_FA_REQUIRED = "2fa_required"
    BANNED = "banned"
    FROZEN = "frozen"
    ERROR = "error"
    FLOOD_WAIT = "flood_wait"

    ALL = {
        NEEDS_LOGIN,
        CODE_SENT,
        ACTIVE,
        TWO_FA_REQUIRED,
        BANNED,
        FROZEN,
        ERROR,
        FLOOD_WAIT,
    }

class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id = Column(Integer, primary_key=True, index=True)

    phone = Column(String(32), unique=True, index=True, nullable=False)
    country = Column(String(128), nullable=True)
    agent = Column(String(64), nullable=True)

    status = Column(String(32), nullable=False, default=TelegramAccountStatus.NEEDS_LOGIN)

    login_session_enc = Column(Text, nullable=True)
    session_enc = Column(Text, nullable=True)

    phone_code_hash = Column(String(128), nullable=True)

    telegram_user_id = Column(BigInteger, nullable=True)

    first_name = Column(String(128), nullable=True)
    last_name = Column(String(128), nullable=True)
    used = Column(String(32), nullable=True)

