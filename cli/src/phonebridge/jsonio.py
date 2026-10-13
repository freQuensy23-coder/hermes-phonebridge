import json
import re

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


def secret_values(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold().replace("-", "_") in SECRET_KEYS and isinstance(item, str) and item:
                result.add(item)
            result.update(secret_values(item))
    elif isinstance(value, list):
        for item in value:
            result.update(secret_values(item))
    return result


def sanitize(value, secrets=()):
    known = sorted(set(secrets) | secret_values(value), key=len, reverse=True)

    def clean(item):
        if isinstance(item, dict):
            return {clean(key): clean(data) for key, data in item.items()
                    if key.casefold().replace("-", "_") not in SECRET_KEYS}
        if isinstance(item, list):
            return [clean(data) for data in item]
        if isinstance(item, str):
            for secret in known:
                if secret:
                    item = item.replace(secret, "[redacted]")
            return re.sub(r"(?i)bearer\s+[^\s\"<>]+", "Bearer [redacted]", item)
        return item

    return clean(value)
