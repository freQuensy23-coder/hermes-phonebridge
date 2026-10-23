import base64
import binascii
import io
import json
import os
import re
from pathlib import Path

from .errors import BridgeError

COORDINATE_MIN = -2147483648
COORDINATE_MAX = 2147483647
GLOBAL_ACTIONS = ("back", "home", "recents", "notifications", "quick_settings")
SCREEN_FIELDS = {
    "tree": {"expected_package"},
    "screenshot": {"expected_package"},
    "tap": {"expected_package", "x", "y"},
    "swipe": {"expected_package", "x1", "y1", "x2", "y2", "duration_ms"},
    "input": {"expected_package", "resource_id", "expected_text", "text"},
    "global": {"action"},
}


def bounded_text(value, maximum, empty=False):
    if not isinstance(value, str) or (not empty and not value) or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-16-le")) // 2 <= maximum
    except UnicodeEncodeError:
        return False


def screen_args(options):
    args = dict(options.args)
    mappings = {"debug_session": "debug_session", "package": "expected_package", "x": "x", "y": "y",
                "x1": "x1", "y1": "y1", "x2": "x2", "y2": "y2", "duration_ms": "duration_ms",
                "resource_id": "resource_id", "expected_text": "expected_text", "text": "text",
                "global_action": "action"}
    for attribute, key in mappings.items():
        value = getattr(options, attribute, None)
        if value is not None:
            if key in args:
                raise BridgeError("VALIDATION_ERROR", "Specify each screen argument only once.")
            args[key] = value
    allowed = SCREEN_FIELDS[options.action] | {"debug_session"}
    required = allowed - {"duration_ms", "expected_text"}
    if options.action == "tree":
        required = {"debug_session"}
    if set(args) - allowed or required - set(args):
        raise BridgeError("VALIDATION_ERROR", "Screen arguments are missing or unsupported.")
    if not isinstance(args["debug_session"], str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", args["debug_session"]) is None:
        raise BridgeError("VALIDATION_ERROR", "Debug session must be an exact ASCII identifier matching [A-Za-z0-9_.:-]{1,128}.")
    for key in ("expected_package", "resource_id"):
        if key in args and (not bounded_text(args[key], 256) or any(ord(char) < 32 or ord(char) == 127 for char in args[key])):
            raise BridgeError("VALIDATION_ERROR", "Screen identities must be exact bounded nonempty strings without control characters.")
    for key in ("text", "expected_text"):
        if key in args and not bounded_text(args[key], 4096, empty=True):
            raise BridgeError("VALIDATION_ERROR", "Screen text must be a string of at most 4096 UTF-16 code units without NUL.")
    for key in ("x", "y", "x1", "y1", "x2", "y2"):
        if key in args and (type(args[key]) is not int or not COORDINATE_MIN <= args[key] <= COORDINATE_MAX):
            raise BridgeError("VALIDATION_ERROR", "Screen coordinates must be signed Android 32-bit integers; the phone checks current native bounds.")
    if "duration_ms" in args and (type(args["duration_ms"]) is not int or not 100 <= args["duration_ms"] <= 2000):
        raise BridgeError("VALIDATION_ERROR", "Swipe duration must be an integer in 100..2000 milliseconds.")
    if "action" in args and args["action"] not in GLOBAL_ACTIONS:
        raise BridgeError("VALIDATION_ERROR", "Global action is not allowlisted.")
    if len(json.dumps(args, ensure_ascii=True, allow_nan=False).encode("ascii")) > 32768:
        raise BridgeError("VALIDATION_ERROR", "Screen arguments exceed the bounded command payload.")
    return args


def valid_bounds(value):
    return (isinstance(value, list) and len(value) == 4
            and all(type(item) is int and COORDINATE_MIN <= item <= COORDINATE_MAX for item in value)
            and value[0] < value[2] and value[1] < value[3])


def decode_screenshot(data, expected_package=None):
    from PIL import Image, UnidentifiedImageError

    if not isinstance(data, dict) or data.get("mime") != "image/jpeg":
        raise BridgeError("INVALID_RESPONSE", "Screenshot must be a bounded JPEG result.")
    width, height = data.get("width"), data.get("height")
    if any(type(value) is not int or not 1 <= value <= 1280 for value in (width, height)):
        raise BridgeError("INVALID_RESPONSE", "Screenshot dimensions are invalid or exceed 1280 pixels.")
    if not all(valid_bounds(data.get(key)) for key in ("screen_bounds", "window_bounds")):
        raise BridgeError("INVALID_RESPONSE", "Screenshot requires ordered native screen_bounds and window_bounds metadata.")
    if not bounded_text(data.get("package"), 256) or (expected_package is not None and data["package"] != expected_package):
        raise BridgeError("INVALID_RESPONSE", "Screenshot package does not match the exact requested package.")
    if data.get("secure_window_guard") != "platform_window_capture":
        raise BridgeError("INVALID_RESPONSE", "Screenshot has no supported platform secure-window guard.")
    encoded = data.get("base64")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 327680:
        raise BridgeError("INVALID_RESPONSE", "Screenshot base64 exceeds the bounded JPEG payload.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > 245760:
            raise ValueError("Size")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "JPEG" or image.size != (width, height):
                raise ValueError("Dimensions or format")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
        return raw
    except (ValueError, OSError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError):
        raise BridgeError("INVALID_RESPONSE", "Screenshot base64 or encoded JPEG dimensions are invalid.") from None


def write_screenshot(path, content):
    directories = []
    directory = None
    descriptor = None
    stream = None
    created = False
    identity = None
    primary = None

    def remove_created(parent):
        if not created or identity is None:
            return
        try:
            name = output.name if parent is not None else output
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(name, dir_fd=parent)
        except BaseException:
            pass

    try:
        output = Path(path).expanduser().absolute()
        if ".." in output.parts or output.suffix.lower() not in {".jpg", ".jpeg"}:
            raise BridgeError("VALIDATION_ERROR", "Screenshot output must be a new .jpg or .jpeg path without parent traversal.")
        directory = os.open(output.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directories.append(directory)
        for part in output.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            directories.append(child)
            directory = child
            os.close(directories.pop(0))
        descriptor = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        identity = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb", closefd=False)
        if stream.write(content) != len(content):
            raise OSError("Incomplete screenshot write")
        stream.flush()
        os.fsync(stream.fileno())
    except BaseException as error:
        primary = error

    if created and identity is None and descriptor is not None:
        try:
            identity = os.fstat(descriptor)
        except BaseException as error:
            if primary is None:
                primary = error
    if stream is not None:
        released_stream, stream = stream, None
        try:
            released_stream.close()
        except BaseException as error:
            if primary is None:
                primary = error
    if descriptor is not None:
        released_descriptor, descriptor = descriptor, None
        try:
            os.close(released_descriptor)
        except BaseException as error:
            if primary is None:
                primary = error
    if primary is not None:
        remove_created(directory)
    while directories:
        released_directory = directories.pop()
        try:
            os.close(released_directory)
        except BaseException as error:
            if primary is None:
                primary = error
                remove_created(None)
    if primary is not None:
        raise primary
    return str(output)


def export_screenshot(result, path, expected_package=None):
    command_id = result.get("id")
    if result.get("status") != "succeeded":
        return result
    try:
        if result.get("op") != "screen.screenshot":
            raise BridgeError("INVALID_RESPONSE", "Only a screen.screenshot command result can be exported.")
        raw = decode_screenshot(result.get("data"), expected_package)
        output = write_screenshot(path, raw)
        data = {key: value for key, value in result["data"].items() if key != "base64"}
        return {**result, "data": {**data, "output_path": output}}
    except KeyboardInterrupt:
        raise BridgeError("INTERRUPTED", "Screenshot export interrupted. Query the original command ID; do not resubmit.", command_id=command_id) from None
    except BridgeError as error:
        error.details["command_id"] = command_id
        raise
    except (OSError, RuntimeError, ValueError):
        raise BridgeError("LOCAL_FILE_ERROR", "Cannot export the private screenshot; use the original command ID without resubmitting.", command_id=command_id) from None
