import base64
import binascii
import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


OPERATIONS = frozenset({
    "device.info", "capabilities", "device.capabilities",
    "screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global",
    "files.receive",
})
FILE_OPERATIONS = frozenset({"files.receive"})
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
SCREEN_OPERATIONS = frozenset({
    "screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global",
})
MAX_RESULT_DATA_BYTES = 384 * 1024
MAX_SCREENSHOT_BYTES = 240 * 1024
MAX_RESULT_STRING_CHARS = 327680


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


def validate_screenshot_data(data):
    if not isinstance(data, dict) or data.get("mime") != "image/jpeg":
        raise ValueError("Screenshot must be JPEG")
    encoded = data.get("base64")
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_RESULT_STRING_CHARS:
        raise ValueError("Screenshot base64 limit exceeded")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Invalid screenshot base64") from error
    if (not 1 <= len(image) <= MAX_SCREENSHOT_BYTES
            or base64.b64encode(image).decode("ascii") != encoded):
        raise ValueError("Invalid screenshot encoding or byte limit")
    if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise ValueError("Invalid JPEG boundaries")
    if any(type(data.get(key)) is not int or not 1 <= data[key] <= 1280 for key in ("width", "height")):
        raise ValueError("Invalid screenshot dimensions")
    return data


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
        if len(set(value)) != len(value) or any(not IDENTIFIER_PATTERN.fullmatch(op) for op in value):
            raise ValueError("Invalid capabilities")
        return [op for op in value if op in OPERATIONS]

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
        return validate_json(value, MAX_RESULT_DATA_BYTES, MAX_RESULT_STRING_CHARS) if value is not None else None


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


IMAGE_SIGNATURES = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda data: data[:6] in (b"GIF87a", b"GIF89a"),
    "image/webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
}
MIME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}\Z")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}\Z")
MAX_FILE_NAME_BYTES = 180


def valid_file_name(name):
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    if any(character in "/\\" or ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF
           for character in name):
        return False
    return len(name.encode("utf-8")) <= MAX_FILE_NAME_BYTES


def upload_metadata(query, headers, max_ttl=900):
    from urllib.parse import parse_qsl

    try:
        pairs = parse_qsl(query.decode("ascii"), keep_blank_values=True, strict_parsing=bool(query),
                          encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError) as error:
        raise BridgeError("VALIDATION_ERROR", "Upload query is not valid UTF-8", 422) from error
    fields = {}
    for key, value in pairs:
        if key not in {"name", "mime", "kind", "ttl_seconds"} or key in fields:
            raise BridgeError("VALIDATION_ERROR", f"Unexpected or repeated query field {key!r}", 422)
        fields[key] = value
    name, mime, kind = fields.get("name"), fields.get("mime"), fields.get("kind")
    if not valid_file_name(name):
        raise BridgeError("VALIDATION_ERROR", "name must be a plain file name of at most 180 UTF-8 bytes", 422)
    if mime is None or not MIME_PATTERN.fullmatch(mime):
        raise BridgeError("VALIDATION_ERROR", "mime must be a type/subtype media type", 422)
    if kind not in ("file", "image"):
        raise BridgeError("VALIDATION_ERROR", "kind must be file or image", 422)
    if kind == "image" and mime not in IMAGE_SIGNATURES:
        raise BridgeError("VALIDATION_ERROR", "Images must be PNG, JPEG, WebP or GIF", 422)
    ttl = fields.get("ttl_seconds", "300")
    if not ttl.isascii() or not ttl.isdigit() or not 1 <= int(ttl) <= max_ttl:
        raise BridgeError("VALIDATION_ERROR", f"ttl_seconds must be an integer 1..{max_ttl}", 422)
    digests = headers.getlist("x-content-sha256")
    if len(digests) != 1 or not SHA256_PATTERN.fullmatch(digests[0]):
        raise BridgeError("VALIDATION_ERROR", "Exactly one lowercase hex X-Content-SHA256 header is required", 422)
    keys = headers.getlist("idempotency-key")
    if len(keys) > 1 or (keys and not IDENTIFIER_PATTERN.fullmatch(keys[0])):
        raise BridgeError("VALIDATION_ERROR", "Idempotency-Key must be 1..128 characters [A-Za-z0-9_.:-]", 422)
    return {"name": name, "mime": mime, "kind": kind, "ttl_seconds": int(ttl), "sha256": digests[0],
            "idempotency_key": keys[0] if keys else None}
