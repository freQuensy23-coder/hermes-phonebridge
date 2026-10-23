import json

from .errors import BridgeError

SECRET_KEYS = {"token", "device_token", "admin_token", "access_token", "refresh_token",
               "authorization", "password", "secret", "qr_payload", "cookie", "cookies"}


def reject_constant(value):
    raise ValueError("Non-finite number")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def loads(value):
    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_object)


def object_args(value):
    try:
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("Too large")
        result = loads(value)
        if not isinstance(result, dict):
            raise ValueError("Object required")
        return result
    except (ValueError, TypeError, RecursionError):
        raise BridgeError("VALIDATION_ERROR", "Arguments must be a bounded, valid JSON object.") from None


def bridge_credentials(value, method, path):
    candidates = []
    if method == "POST" and path == "/v1/pairings":
        payload = value.get("qr_payload")
        if isinstance(payload, dict):
            candidates.append(payload.get("token"))
    if method == "POST" and path == "/v1/pair/claim":
        candidates.append(value.get("device_token"))
    return {item for item in candidates if isinstance(item, str) and len(item) == 43
            and all(char.isascii() and (char.isalnum() or char in "_-") for char in item)}


def sanitize(value, secrets=()):
    known = sorted({secret for secret in secrets if secret}, key=len, reverse=True)

    def clean(item, opaque=False):
        if isinstance(item, dict):
            return {(clean(key, True) if opaque else key): clean(data, opaque or key in {"data", "args", "permissions"})
                    for key, data in item.items()
                    if opaque or key.casefold().replace("-", "_") not in SECRET_KEYS}
        if isinstance(item, list):
            return [clean(data, opaque) for data in item]
        if isinstance(item, str):
            for secret in known:
                item = item.replace(secret, "[redacted]")
        return item

    return clean(value)
