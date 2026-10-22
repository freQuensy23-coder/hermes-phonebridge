import io
import os
import re
import stat
from importlib.resources import files
from pathlib import Path

from .client import validate_url
from .errors import BridgeError


def write_private_new(path, content):
    descriptor = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise BridgeError("LOCAL_FILE_ERROR", "Cannot create the private file; existing files and symlinks are never overwritten.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def pairing_qr(result, path, now):
    import json
    import math

    import qrcode

    payload = result.get("qr_payload")
    if not isinstance(payload, dict) or set(payload) != {"v", "server", "token", "expires_at"}:
        raise BridgeError("INVALID_RESPONSE", "Pairing response has an invalid QR payload.")
    if type(payload["v"]) is not int or payload["v"] != 1:
        raise BridgeError("INVALID_RESPONSE", "Pairing QR version is unsupported.")
    parsed = validate_url(payload["server"])
    if parsed.scheme != "https":
        raise BridgeError("INVALID_RESPONSE", "Pairing QR requires a public HTTPS server.")
    expiry = payload["expires_at"]
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= now:
        raise BridgeError("PAIRING_EXPIRED", "Pairing QR has already expired or has an invalid expiry.")
    if result.get("expires_at") != expiry or not isinstance(result.get("id"), str) or not result["id"]:
        raise BridgeError("INVALID_RESPONSE", "Pairing metadata and QR expiry disagree.")
    token = payload["token"]
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise BridgeError("INVALID_RESPONSE", "Pairing response has an invalid credential.")
    output = Path(path).expanduser().absolute()
    if output.suffix.lower() != ".png":
        raise BridgeError("VALIDATION_ERROR", "QR output must be a PNG path.")
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) > 2048:
        raise BridgeError("INVALID_RESPONSE", "Pairing payload exceeds the local QR size limit.")
    image = qrcode.make(encoded, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    write_private_new(output, buffer.getvalue())
    return {"id": result["id"], "expires_at": expiry, "status": "pending", "qr_path": str(output)}


def install_skill(replace=False):
    home = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")).expanduser().absolute()
    if ".." in home.parts:
        raise BridgeError("LOCAL_FILE_ERROR", "Hermes home cannot contain parent traversal.")
    content = files("phonebridge").joinpath("resources", "SKILL.md").read_bytes()
    directory = None
    temporary = None
    try:
        directory = os.open(home.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (*home.parts[1:], "skills", "phonebridge"):
            try:
                os.mkdir(part, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        try:
            existing = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with os.fdopen(existing, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise BridgeError("SKILL_EXISTS", "Installed skill path is not a regular file; it was not replaced.")
                if stream.read(len(content) + 1) == content:
                    return {"installed": True, "changed": False, "path": str(home / "skills" / "phonebridge" / "SKILL.md")}
            if not replace:
                raise BridgeError("SKILL_EXISTS", "A different skill already exists; rerun with --replace to update it.")
        import uuid

        temporary = ".phonebridge-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if existing is not None:
            os.replace(temporary, "SKILL.md", src_dir_fd=directory, dst_dir_fd=directory)
        else:
            os.link(temporary, "SKILL.md", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
        temporary = None
        os.fsync(directory)
        return {"installed": True, "changed": True, "path": str(home / "skills" / "phonebridge" / "SKILL.md")}
    except OSError:
        raise BridgeError("LOCAL_FILE_ERROR", "Cannot install skill safely; symlinks and existing conflicting files are refused.") from None
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)
