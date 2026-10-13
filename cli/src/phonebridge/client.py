import http.client
import ipaddress
import math
import os
import socket
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from .errors import BridgeError
from .jsonio import loads, secret_values

MAX_RESPONSE = 1048576
TERMINAL = {"succeeded", "failed", "expired"}
STATUSES = TERMINAL | {"queued", "sent"}
PHONE_ERRORS = {"PERMISSION_REQUIRED", "PHONE_LOCKED", "FOREGROUND_REQUIRED", "UNSUPPORTED",
                "VALIDATION_ERROR", "NOT_FOUND", "AMBIGUOUS_STATE", "EXPIRED",
                "DEVICE_OFFLINE", "REVOKED", "QUEUE_FULL"}


def bounded_number(value, minimum, maximum, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BridgeError("VALIDATION_ERROR", f"{name} must be a finite number.")
    if value < minimum or value > maximum:
        raise BridgeError("VALIDATION_ERROR", f"{name} is outside the supported range.")
    return value


def segment(value):
    if not isinstance(value, str) or not value or len(value) > 256 or value in {".", ".."}:
        raise BridgeError("VALIDATION_ERROR", "A nonempty bounded identifier is required.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BridgeError("VALIDATION_ERROR", "Identifiers cannot contain control characters.")
    return quote(value, safe="")


def validate_url(value):
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
            raise ValueError("Invalid URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("Invalid URL")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("URL credentials or parameters")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid port")
        if parsed.scheme == "http":
            host = parsed.hostname
            if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
                raise ValueError("Remote HTTP")
        if chr(92) in parsed.path or any(part in {".", ".."} for part in parsed.path.split("/")):
            raise ValueError("Invalid path")
        return parsed
    except (ValueError, TypeError):
        raise BridgeError("CONFIG_ERROR", "URL must use HTTPS, or loopback HTTP, without credentials or query parameters.") from None


@dataclass(frozen=True)
class Settings:
    url: str
    token: str = field(repr=False)
    request_timeout: float = 10.0

    @classmethod
    def load(cls, config=None, url=None, request_timeout=10.0):
        values = {}
        if config:
            descriptor = None
            try:
                descriptor = os.open(Path(config).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise BridgeError("CONFIG_ERROR", "Config must be a regular file with mode 0600.")
                if info.st_uid != os.getuid() or info.st_size > 65536:
                    raise BridgeError("CONFIG_ERROR", "Config must be owned by the current user and bounded in size.")
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = None
                    values = loads(stream.read(65537))
                if not isinstance(values, dict) or set(values) - {"url", "token"}:
                    raise BridgeError("CONFIG_ERROR", "Config accepts only url and token fields.")
            except (OSError, ValueError, TypeError, RecursionError):
                raise BridgeError("CONFIG_ERROR", "Cannot read a valid private config file.") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        selected_url = url if url is not None else os.environ.get("PHONEBRIDGE_URL", values.get("url", "http://127.0.0.1:18940"))
        token = os.environ.get("PHONEBRIDGE_TOKEN", values.get("token"))
        validate_url(selected_url)
        if not isinstance(token, str) or not token or len(token) > 8192 or not token.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in token):
            raise BridgeError("CONFIG_ERROR", "Set PHONEBRIDGE_TOKEN or a token in the private config file.")
        bounded_number(request_timeout, 0.1, 120, "Request timeout")
        return cls(selected_url.rstrip("/"), token, request_timeout)


class Client:
    def __init__(self, settings, clock=time.monotonic, wall_clock=time.time, sleep=time.sleep):
        self.settings = settings
        self.clock = clock
        self.wall_clock = wall_clock
        self.sleep = sleep
        self.parsed = validate_url(settings.url)
        self.secrets = {settings.token}

    def request(self, method, path, payload=None, query=None, timeout=None):
        import json

        connection = None
        watchdog = None
        ambiguous = method in {"POST", "DELETE"}
        try:
            body = None if payload is None else json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if body is not None and len(body) > 65536:
                raise BridgeError("VALIDATION_ERROR", "Request JSON exceeds the size limit.")
            connection_type = http.client.HTTPSConnection if self.parsed.scheme == "https" else http.client.HTTPConnection
            budget = timeout if timeout is not None else self.settings.request_timeout
            deadline = self.clock() + budget
            connection = connection_type(self.parsed.hostname, self.parsed.port, timeout=budget)
            target = self.parsed.path.rstrip("/") + path
            if query:
                target += "?" + urlencode(query)
            headers = {"Authorization": f"Bearer {self.settings.token}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.connect()
            active_socket = connection.sock
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Connection timeout")
            active_socket.settimeout(remaining)

            def abort_socket():
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            watchdog = threading.Timer(remaining, abort_socket)
            watchdog.daemon = True
            watchdog.start()
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if 300 <= status < 400:
                raise BridgeError("REDIRECT_REFUSED", "Redirects are refused; verify the configured server URL.", http_status=status, may_have_been_submitted=ambiguous)
            chunks = []
            size = 0
            while size <= MAX_RESPONSE:
                if response.isclosed():
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("Response timeout")
                if active_socket is not None:
                    active_socket.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                chunks.append(chunk)
            raw = b"".join(chunks)
            if status >= 400:
                code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED"}.get(status, "SERVER_ERROR" if status >= 500 else "HTTP_ERROR")
                safe_codes = PHONE_ERRORS | {"UNAUTHORIZED", "FORBIDDEN", "CONFLICT", "RATE_LIMITED", "IDEMPOTENCY_CONFLICT", "UNAVAILABLE"}
                try:
                    error_body = loads(raw) if len(raw) <= MAX_RESPONSE else None
                    server_error = error_body.get("error") if isinstance(error_body, dict) else None
                    server_code = server_error.get("code") if isinstance(server_error, dict) else None
                    if isinstance(server_code, str) and server_code in safe_codes:
                        code = server_code
                except (ValueError, TypeError, RecursionError):
                    pass
                raise BridgeError(code, "Server rejected the request; response content is withheld.", http_status=status, may_have_been_submitted=ambiguous and status >= 500)
            if response.length not in {None, 0}:
                raise BridgeError("INVALID_RESPONSE", "Server response was truncated.", may_have_been_submitted=ambiguous)
            if status < 200 or status >= 300 or len(raw) > MAX_RESPONSE:
                raise BridgeError("INVALID_RESPONSE", "Server returned an invalid or oversized response.", may_have_been_submitted=ambiguous)
            if status == 204 and not raw:
                return {}
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise BridgeError("INVALID_RESPONSE", "Server response must be JSON.", may_have_been_submitted=ambiguous)
            try:
                data = loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Object required")
            except (ValueError, TypeError, RecursionError):
                raise BridgeError("INVALID_RESPONSE", "Server returned invalid JSON.", may_have_been_submitted=ambiguous) from None
            self.secrets.update(secret_values(data))
            return data
        except BridgeError:
            raise
        except (OSError, http.client.HTTPException):
            raise BridgeError("TRANSPORT_ERROR", "Request failed; no automatic retry was attempted.", may_have_been_submitted=ambiguous) from None
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise BridgeError("VALIDATION_ERROR", "Cannot encode a valid bounded request.") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if connection is not None:
                connection.close()

    def device(self, device_id):
        return self.request("GET", f"/v1/devices/{segment(device_id)}")

    def get_command(self, command_id, timeout=None):
        result = self.request("GET", f"/v1/commands/{segment(command_id)}", timeout=timeout)
        if result.get("id") != command_id or not isinstance(result.get("status"), str) or result["status"] not in STATUSES:
            raise BridgeError("INVALID_RESPONSE", "Command response has an invalid identity or status.", command_id=command_id)
        return result

    def submit(self, device_id, op, args, ttl=60, idempotency_key=None, wait=False, timeout=60.0, poll_interval=0.5):
        bounded_number(ttl, 1, 86400, "Command TTL")
        bounded_number(timeout, 0.1, 86400, "Wait timeout")
        bounded_number(poll_interval, 0.05, 30, "Poll interval")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not isinstance(args, dict):
            raise BridgeError("VALIDATION_ERROR", "Command TTL must be an integer and args an object.")
        segment(op)
        key = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        segment(key)
        try:
            result = self.request("POST", f"/v1/devices/{segment(device_id)}/commands", {"op": op, "args": args, "ttl_seconds": ttl, "idempotency_key": key})
        except BridgeError as error:
            error.details["idempotency_key"] = key
            raise
        except KeyboardInterrupt:
            raise BridgeError("INTERRUPTED", "Submission interrupted; its effect is uncertain. Do not resubmit.", idempotency_key=key, may_have_been_submitted=True) from None
        command_id = result.get("id")
        expires = result.get("expires_at")
        if not isinstance(command_id, str) or not command_id or not isinstance(result.get("status"), str) or result["status"] not in STATUSES or isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise BridgeError("INVALID_RESPONSE", "Command submission returned invalid metadata; do not resubmit.", may_have_been_submitted=True, idempotency_key=key)
        if not wait:
            return result
        return self.wait(command_id, timeout, poll_interval, expires)

    def wait(self, command_id, timeout=60.0, poll_interval=0.5, expires_at=None):
        bounded_number(timeout, 0.1, 86400, "Wait timeout")
        bounded_number(poll_interval, 0.05, 30, "Poll interval")
        deadline = self.clock() + timeout
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise BridgeError("WAIT_TIMEOUT", "Wait timed out; the command may still execute. Query its ID, do not resubmit.", command_id=command_id)
            try:
                result = self.get_command(command_id, timeout=min(self.settings.request_timeout, remaining))
            except BridgeError as error:
                error.details["command_id"] = command_id
                raise
            except KeyboardInterrupt:
                raise BridgeError("INTERRUPTED", "Wait interrupted; the command may still execute. Query its ID, do not resubmit.", command_id=command_id) from None
            if result["status"] in TERMINAL:
                return result
            expiry = result.get("expires_at", expires_at)
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise BridgeError("INVALID_RESPONSE", "Command response has no valid expiry.", command_id=command_id)
            until_expiry = expiry - self.wall_clock()
            if until_expiry <= 0:
                raise BridgeError("COMMAND_EXPIRY_REACHED", "Command TTL elapsed without a terminal server result. Query its ID; do not resubmit.", command_id=command_id, last_status=result["status"])
            try:
                self.sleep(max(0, min(poll_interval, deadline - self.clock(), until_expiry)))
            except KeyboardInterrupt:
                raise BridgeError("INTERRUPTED", "Wait interrupted; the command may still execute. Query its ID, do not resubmit.", command_id=command_id) from None
