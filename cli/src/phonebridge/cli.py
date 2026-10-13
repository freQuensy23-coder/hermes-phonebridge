import argparse
import json
import sys

from . import __version__
from .client import Client, PHONE_ERRORS, Settings, bounded_number, segment
from .errors import BridgeError
from .jsonio import object_args, sanitize
from .local import install_skill, pairing_qr


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BridgeError("USAGE_ERROR", "Invalid command or arguments; use phonebridge --help.")


def integer(minimum, maximum):
    def parse(value):
        try:
            result = int(value)
            if not minimum <= result <= maximum:
                raise ValueError("Range")
            return result
        except ValueError:
            raise argparse.ArgumentTypeError("Integer is outside the supported range.") from None
    return parse


def number(minimum, maximum):
    def parse(value):
        try:
            return bounded_number(float(value), minimum, maximum, "Value")
        except (ValueError, BridgeError):
            raise argparse.ArgumentTypeError("Finite number is outside the supported range.") from None
    return parse


def days(value):
    try:
        result = [int(part) for part in value.split(",")]
        if not result or any(day < 1 or day > 7 for day in result) or len(set(result)) != len(result):
            raise ValueError("Days")
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("Days are unique numbers 1..7, Monday through Sunday.") from None


def execution_flags(parser):
    parser.add_argument("--args", type=object_args, default={})
    parser.add_argument("--ttl", type=integer(1, 86400), default=60)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout", type=number(0.1, 86400), default=60.0)
    parser.add_argument("--poll-interval", type=number(0.05, 30), default=0.5)
    parser.add_argument("--idempotency-key")


def parser():
    root = Parser(prog="phonebridge", description="Permission-aware PhoneBridge client. Success JSON goes to stdout; error JSON to stderr.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--url", help="Override PHONEBRIDGE_URL; HTTPS or loopback HTTP only")
    root.add_argument("--config", help="Optional private mode-0600 JSON file with url and token")
    root.add_argument("--request-timeout", type=number(0.1, 120), default=10.0)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("install-skill", help="Install only the bundled skill in HERMES_HOME")
    pair = commands.add_parser("pair", help="Create a one-use local QR PNG or inspect/revoke a pairing")
    pair.add_argument("--name")
    pair.add_argument("--ttl", type=integer(1, 900), default=300)
    pair.add_argument("--qr")
    pairing_actions = pair.add_subparsers(dest="pair_action")
    for action in ("status", "revoke"):
        pairing_actions.add_parser(action).add_argument("id")
    devices = commands.add_parser("devices").add_subparsers(dest="device_action", required=True)
    devices.add_parser("list")
    for action in ("show", "revoke"):
        devices.add_parser(action).add_argument("device")
    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("device")
    command = commands.add_parser("command")
    command.add_argument("device")
    command.add_argument("op")
    execution_flags(command)
    status = commands.add_parser("command-status", help="Read an existing command ID without submitting anything")
    status.add_argument("id")
    status.add_argument("--wait", action="store_true")
    status.add_argument("--timeout", type=number(0.1, 86400), default=60.0)
    status.add_argument("--poll-interval", type=number(0.05, 30), default=0.5)
    events = commands.add_parser("events")
    events.add_argument("device")
    events.add_argument("--after", type=integer(0, 9223372036854775807), default=0)
    events.add_argument("--limit", type=integer(1, 100), default=50)
    clock = commands.add_parser("clock").add_subparsers(dest="action", required=True)
    providers = clock.add_parser("providers")
    providers.add_argument("device")
    execution_flags(providers)
    groups = {
        "alarms": ("create", "dismiss", "snooze", "show", "list", "enable", "disable", "delete"),
        "timers": ("create", "dismiss", "show", "list", "cancel", "pause", "resume"),
        "notifications": ("list", "dismiss", "reply", "action"),
    }
    for group, actions in groups.items():
        group_commands = commands.add_parser(group).add_subparsers(dest="action", required=True)
        for action in actions:
            item = group_commands.add_parser(action)
            item.add_argument("device")
            execution_flags(item)
            if group in {"alarms", "timers"}:
                item.add_argument("--package")
                if action not in {"list", "show"}:
                    item.add_argument("--label")
                if action in {"enable", "disable", "delete", "cancel", "pause", "resume"}:
                    item.add_argument("--id", dest="target_id")
                    if group == "alarms":
                        item.add_argument("--time")
            if group == "alarms":
                if action in {"create", "dismiss"}:
                    item.add_argument("--hour", type=integer(0, 23))
                    item.add_argument("--minute", type=integer(0, 59))
                if action == "create":
                    item.add_argument("--days", type=days)
                if action == "snooze":
                    item.add_argument("--minutes", type=integer(1, 1440))
            if group == "timers":
                if action == "create":
                    item.add_argument("--seconds", type=integer(1, 86400))
                if action == "dismiss":
                    item.add_argument("--uri")
            if group == "notifications" and action != "list":
                item.add_argument("--key")
                if action == "reply":
                    item.add_argument("--text")
                    item.add_argument("--result-key")
                if action in {"reply", "action"}:
                    item.add_argument("--action", dest="action_index", type=integer(0, 255))
    return root


def operation_args(options):
    args = dict(options.args)
    mappings = {"package": "package", "label": "label", "hour": "hour", "minute": "minute",
                "days": "days", "minutes": "minutes", "seconds": "seconds", "uri": "uri",
                "target_id": "id", "time": "time", "key": "key", "text": "text", "action_index": "action",
                "result_key": "result_key"}
    for attribute, key in mappings.items():
        value = getattr(options, attribute, None)
        if value is not None:
            if key in args:
                raise BridgeError("VALIDATION_ERROR", "Specify each operation argument only once.")
            args[key] = value
    required = {
        ("alarms", "create"): ("hour", "minute"),
        ("timers", "create"): ("seconds",),
        ("notifications", "dismiss"): ("key",),
        ("notifications", "reply"): ("key", "action", "text"),
        ("notifications", "action"): ("key", "action"),
    }.get((options.command, options.action), ())
    if any(key not in args for key in required):
        raise BridgeError("VALIDATION_ERROR", "Required operation arguments are missing.")
    if options.command == "alarms":
        for key, maximum in (("hour", 23), ("minute", 59)):
            if key in args and (type(args[key]) is not int or not 0 <= args[key] <= maximum):
                raise BridgeError("VALIDATION_ERROR", "Alarm time is outside the supported range.")
        if "days" in args and (not isinstance(args["days"], list) or not args["days"] or any(type(day) is not int or not 1 <= day <= 7 for day in args["days"]) or len(set(args["days"])) != len(args["days"])):
            raise BridgeError("VALIDATION_ERROR", "Alarm days must be unique integers 1..7.")
    if options.command == "timers" and options.action == "create":
        if type(args["seconds"]) is not int or not 1 <= args["seconds"] <= 86400:
            raise BridgeError("VALIDATION_ERROR", "Timer seconds must be an integer in 1..86400.")
    if options.command == "notifications" and options.action != "list":
        if not isinstance(args["key"], str) or not args["key"]:
            raise BridgeError("VALIDATION_ERROR", "An exact notification key is required.")
        if options.action == "reply" and (not isinstance(args["text"], str) or not args["text"] or len(args["text"]) > 4096):
            raise BridgeError("VALIDATION_ERROR", "A bounded nonempty reply is required.")
        if options.action in {"reply", "action"} and (type(args["action"]) is not int or not 0 <= args["action"] <= 255):
            raise BridgeError("VALIDATION_ERROR", "An exact notification action index is required.")
    return args


def dispatch(options, client):
    if options.command == "pair":
        if options.pair_action == "status":
            return client.request("GET", f"/v1/pairings/{segment(options.id)}")
        if options.pair_action == "revoke":
            return revoke(client, f"/v1/pairings/{segment(options.id)}", options.id)
        if not options.name or len(options.name) > 128 or not options.name.strip() or any(ord(char) < 32 for char in options.name) or not options.qr:
            raise BridgeError("VALIDATION_ERROR", "Pair requires a bounded --name and a new --qr PNG path.")
        result = client.request("POST", "/v1/pairings", {"name": options.name, "ttl_seconds": options.ttl})
        try:
            return pairing_qr(result, options.qr, client.wall_clock())
        except BridgeError as error:
            if isinstance(result.get("id"), str):
                error.details["pairing_id"] = result["id"]
                error.details["pairing_created"] = True
            raise
    if options.command == "devices":
        if options.device_action == "list":
            return client.request("GET", "/v1/devices")
        if options.device_action == "show":
            return client.device(options.device)
        return revoke(client, f"/v1/devices/{segment(options.device)}", options.device)
    if options.command == "capabilities":
        device = client.device(options.device)
        if not isinstance(device.get("capabilities"), list) or not isinstance(device.get("permissions"), dict):
            raise BridgeError("INVALID_RESPONSE", "Device has no valid capability and permission report.")
        return {key: device[key] for key in ("id", "name", "online", "version", "capabilities", "permissions") if key in device}
    if options.command == "events":
        return client.request("GET", f"/v1/devices/{segment(options.device)}/events", query={"after": options.after, "limit": options.limit})
    if options.command == "command-status":
        return client.wait(options.id, options.timeout, options.poll_interval) if options.wait else client.get_command(options.id)
    if options.command == "command":
        op, args = options.op, options.args
    else:
        prefix = {"alarms": "clock.alarms", "timers": "clock.timers", "notifications": "notifications", "clock": "clock"}[options.command]
        op, args = f"{prefix}.{options.action}", operation_args(options)
    return client.submit(options.device, op, args, options.ttl, options.idempotency_key, options.wait, options.timeout, options.poll_interval)


def revoke(client, path, identifier):
    result = client.request("DELETE", path)
    try:
        current = client.request("GET", path)
    except BridgeError as error:
        if error.code == "NOT_FOUND":
            return {"id": identifier, "revocation_requested": True, "verified": True, "state": "not_found"}
        error.details.update({"id": identifier, "revocation_requested": True})
        raise
    verified = current.get("status") == "revoked" or current.get("revoked") is True
    return {"id": identifier, "revocation_requested": True, "verified": verified, "state": current, "result": result}


def main(argv=None):
    secrets = set()
    try:
        options = parser().parse_args(argv)
        if options.command == "install-skill":
            result = install_skill()
        else:
            settings = Settings.load(options.config, options.url, options.request_timeout)
            client = Client(settings)
            secrets = client.secrets
            result = dispatch(options, client)
        if result.get("status") == "failed":
            result = dict(result)
            upstream_error = result.get("error")
            code = upstream_error.get("code") if isinstance(upstream_error, dict) else None
            message = "Device authorization was revoked." if code == "DEVICE_REVOKED" else "Phone reported a command failure."
            result["error"] = {"code": code if isinstance(code, str) and code in PHONE_ERRORS else "COMMAND_FAILED", "message": message}
        if isinstance(result.get("data"), dict) and result["data"].get("outcome") == "dispatched":
            result = {**result, "data": {**result["data"], "verified": False}}
        safe = sanitize(result, secrets)
        print(json.dumps(safe, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
        return 1 if result.get("status") in {"failed", "expired"} else 0
    except BridgeError as error:
        print(json.dumps(sanitize(error.as_dict(), secrets), ensure_ascii=True, separators=(",", ":")), file=sys.stderr)
        return 130 if error.code == "INTERRUPTED" else 1
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "INTERRUPTED", "message": "Interrupted. A submitted command may still execute; do not resubmit."}}), file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "CLIENT_ERROR", "message": "Client failed safely; no automatic retry was attempted."}}), file=sys.stderr)
        return 1
