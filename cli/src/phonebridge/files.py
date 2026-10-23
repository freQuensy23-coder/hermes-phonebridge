import hashlib
import math
import mimetypes
import os
import re
import stat
import uuid
from pathlib import PurePath

from .client import STATUSES, segment
from .errors import BridgeError

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_NAME_BYTES = 180
IMAGE_TYPES = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
    "image/gif": (".gif",),
}
MIME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}")
KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
TYPES = mimetypes.MimeTypes(filenames=())


def safe_name(name):
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    if any(char in "/\\" or ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in name):
        return False
    return len(name.encode("utf-8")) <= MAX_NAME_BYTES


def image_signature(mime, content):
    if mime == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return content[:6] in (b"GIF87a", b"GIF89a")
    if mime == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def infer_mime(name):
    suffix = PurePath(name).suffix.lower()
    for mime, suffixes in IMAGE_TYPES.items():
        if suffix in suffixes:
            return mime
    return TYPES.guess_type(name, strict=True)[0] or "application/octet-stream"


def read_source(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise BridgeError("VALIDATION_ERROR", "Source must be an existing regular file, not a symlink.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BridgeError("VALIDATION_ERROR", "Source must be a regular file.")
        if info.st_size > MAX_FILE_BYTES:
            raise BridgeError("VALIDATION_ERROR", "Files larger than 32 MiB cannot be sent.")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, min(1048576, MAX_FILE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise BridgeError("VALIDATION_ERROR", "Files larger than 32 MiB cannot be sent.")
        return b"".join(chunks)
    except OSError:
        raise BridgeError("VALIDATION_ERROR", "Source file could not be read.") from None
    finally:
        os.close(descriptor)


def prepare(path, image=False, name=None, mime=None, ttl=300, idempotency_key=None):
    remote_name = name if name is not None else os.path.basename(os.fsdecode(path))
    if not safe_name(remote_name):
        raise BridgeError("VALIDATION_ERROR", "File name must be 1..180 UTF-8 bytes without path separators or control characters.")
    if mime is not None and not MIME_PATTERN.fullmatch(mime):
        raise BridgeError("VALIDATION_ERROR", "MIME type must look like type/subtype.")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 900:
        raise BridgeError("VALIDATION_ERROR", "File TTL must be an integer from 1 to 900 seconds.")
    key = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
    if not KEY_PATTERN.fullmatch(key):
        raise BridgeError("VALIDATION_ERROR", "Idempotency key must be 1..128 characters [A-Za-z0-9_.:-].")
    content = read_source(path)
    selected = mime or infer_mime(remote_name)
    if image:
        if selected not in IMAGE_TYPES:
            raise BridgeError("VALIDATION_ERROR", "Images must be PNG, JPEG, WebP or GIF; pass --mime for files without an image extension.")
        if not image_signature(selected, content):
            raise BridgeError("VALIDATION_ERROR", "File contents do not match the image type.")
    return {"name": remote_name, "mime": selected, "kind": "image" if image else "file", "ttl": ttl,
            "key": key, "content": content}


def send(client, device, upload, wait=False, timeout=60.0, poll_interval=0.5):
    if wait and (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
        raise BridgeError("VALIDATION_ERROR", "Wait timeout must be a positive finite number.")
    content = upload["content"]
    headers = {"Content-Type": "application/octet-stream",
               "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
               "Idempotency-Key": upload["key"]}
    query = {"name": upload["name"], "mime": upload["mime"], "kind": upload["kind"], "ttl_seconds": upload["ttl"]}
    try:
        result = client.request("POST", f"/v1/devices/{segment(device)}/files", query=query,
                                raw_body=content, extra_headers=headers)
    except BridgeError as error:
        error.details["idempotency_key"] = upload["key"]
        raise
    except KeyboardInterrupt:
        raise BridgeError("INTERRUPTED", "Upload interrupted; delivery is uncertain. Do not resend.",
                          idempotency_key=upload["key"], may_have_been_submitted=True) from None
    command_id = result.get("id")
    expires = result.get("expires_at")
    if (not isinstance(command_id, str) or not command_id or result.get("status") not in STATUSES
            or isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)):
        raise BridgeError("INVALID_RESPONSE", "Upload returned invalid command metadata; do not resend.",
                          may_have_been_submitted=True, idempotency_key=upload["key"])
    if not wait:
        return result
    return client.wait(command_id, timeout, poll_interval, expires)
