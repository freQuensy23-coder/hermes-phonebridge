
import hashlib
import hmac

from .protocol import BridgeError


def bearer(headers):
    values = headers.getlist("authorization")
    if len(values) != 1:
        raise BridgeError("UNAUTHORIZED", "Bearer authorization required", 401)
    parts = values[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or len(parts[1]) > 512:
        raise BridgeError("UNAUTHORIZED", "Bearer authorization required", 401)
    return parts[1]


def require_admin(headers, settings):
    token = bearer(headers)
    if not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), settings.admin_hash):
        raise BridgeError("UNAUTHORIZED", "Bearer authorization required", 401)
