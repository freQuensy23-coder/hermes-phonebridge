import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


OPERATIONS = frozenset({
    "device.info", "capabilities", "device.capabilities",
    "notifications.list", "notifications.dismiss", "notifications.reply", "notifications.action",
    "apps.list", "apps.open", "audio.get", "audio.set", "contacts.list",
    "calendar.list", "calendar.create", "calendar.delete", "clipboard.get", "clipboard.set",
    "location.get", "torch.set", "calls.dial", "sms.list", "sms.send",
    "screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global",
    "clock.providers", "clock.alarms.create", "clock.alarms.dismiss", "clock.alarms.snooze",
    "clock.alarms.show", "clock.alarms.list", "clock.alarms.enable", "clock.alarms.disable",
    "clock.alarms.delete", "clock.timers.create", "clock.timers.dismiss", "clock.timers.show",
    "clock.timers.list", "clock.timers.cancel", "clock.timers.pause", "clock.timers.resume",
})
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")


class BridgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)

    def payload(self):
        return {"error": {"code": self.code, "message": self.message}}


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(value):
    return digest(encode_json(value))


def validate_json(value: Any, max_bytes: int = 32768, string_limit: int = 16384):
    remaining = 2048

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if depth > 12 or remaining < 0:
            raise ValueError("JSON nesting or item limit exceeded")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if len(item) > string_limit:
                raise ValueError("JSON string limit exceeded")
            return
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            if isinstance(item, int) and abs(item) > 2**63 - 1:
                raise ValueError("JSON integer limit exceeded")
            return
        if isinstance(item, list):
            if len(item) > 512:
                raise ValueError("JSON array limit exceeded")
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 128:
                raise ValueError("JSON object limit exceeded")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ValueError("Invalid JSON object key")
                visit(child, depth + 1)
            return
        raise ValueError("Unsupported JSON value")

    visit(value, 0)
    if len(encode_json(value).encode()) > max_bytes:
        raise ValueError("JSON byte limit exceeded")
    return value


def decode_json(raw, max_bytes=65536, string_limit=16384):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Invalid JSON constant")

    value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    validate_json(value, max_bytes, string_limit)
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PairingRequest(Model):
    name: StrictStr = Field(min_length=1, max_length=128)
    ttl_seconds: StrictInt = Field(default=300, ge=1, le=900)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("Invalid name")
        return value


class ClaimRequest(Model):
    token: StrictStr = Field(min_length=43, max_length=43, pattern=TOKEN_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=128)
    platform: Literal["android"]

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        return PairingRequest.valid_name(value)


class CommandRequest(Model):
    op: StrictStr = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: StrictInt = Field(default=60, ge=1, le=86400)
    idempotency_key: StrictStr | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("args")
    @classmethod
    def valid_args(cls, value):
        return validate_json(value)

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None and not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("Invalid idempotency key")
        return value


class HelloFrame(Model):
    type: Literal["hello"]
    version: StrictStr = Field(min_length=1, max_length=32, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    capabilities: list[StrictStr] = Field(max_length=100)
    permissions: dict[StrictStr, StrictBool]

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, value):
        if len(set(value)) != len(value) or any(op not in OPERATIONS for op in value):
            raise ValueError("Invalid capabilities")
        return value

    @field_validator("permissions")
    @classmethod
    def valid_permissions(cls, value):
        if len(value) > 64 or any(not IDENTIFIER_PATTERN.fullmatch(key) for key in value):
            raise ValueError("Invalid permissions")
        return value


class ResultError(Model):
    code: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: StrictStr = Field(min_length=1, max_length=1024)


class ResultFrame(Model):
    type: Literal["result"]
    id: StrictStr = Field(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    ok: StrictBool
    data: dict[str, Any] | None = None
    error: ResultError | None = None

    @field_validator("data")
    @classmethod
    def valid_data(cls, value):
        return validate_json(value, 524288, 524288) if value is not None else None


class EventFrame(Model):
    type: Literal["event"]
    id: StrictStr = Field(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    data: dict[str, Any]
    time: StrictInt = Field(ge=0, le=2**63 - 1)

    @field_validator("data")
    @classmethod
    def valid_data(cls, value):
        return validate_json(value, 16384)
