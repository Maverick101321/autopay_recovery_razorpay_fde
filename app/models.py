from enum import StrEnum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class Language(StrEnum):
    EN = "en"
    HI = "hi"


class FailureReason(StrEnum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    MANDATE_EXPIRED = "mandate_expired"
    BANK_DECLINED = "bank_declined"
    OTP_NOT_COMPLETED = "otp_not_completed"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    RECOVERED = "recovered"
    PROMISED = "promised"
    ESCALATED = "escalated"
    NO_ANSWER = "no_answer"
    DO_NOT_CALL = "do_not_call"
    FAILED = "failed"


class Customer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    phone: str
    language: Language
    consent_to_call: int = Field(default=1, ge=0, le=1)
    call_window_start: str
    call_window_end: str
    timezone: str = "Asia/Kolkata"


class FailedPayment(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    merchant_name: str
    plan_name: str
    amount_inr: int = Field(gt=0)
    failure_reason: FailureReason
    failed_at: str
    retry_count: int = Field(default=0, ge=0)
    status: PaymentStatus = PaymentStatus.PENDING
    next_retry_at: Optional[str] = None


class CallAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    failed_payment_id: str
    retell_call_id: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None
    outcome: Optional[str] = None
    transcript: Optional[str] = None
    notes: Optional[str] = None


class PaymentLink(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    failed_payment_id: str
    url: str
    created_at: str
    status: str = "created"


class OutboundMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    failed_payment_id: str
    channel: str = "sms"
    body: str
    created_at: str
