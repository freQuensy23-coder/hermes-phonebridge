import argparse
import json
import sys

from . import __version__
from . import files
from .client import Client, Settings, bounded_number, error_code, safe_message, segment
from .errors import BridgeError
from .jsonio import object_args, sanitize
from .local import install_skill, pairing_qr
from .screen import COORDINATE_MAX, COORDINATE_MIN, GLOBAL_ACTIONS, export_screenshot, screen_args


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BridgeError("USAGE_ERROR", f"{message}; use phonebridge --help.")


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
    install = commands.add_parser("install-skill", help="Install the bundled skill in HERMES_HOME")
    install.add_argument("--replace", action="store_true", help="Replace an older installed PhoneBridge skill")
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
    status.add_argument("--output", help="Export a completed screenshot to a new private JPEG; implies --wait")
    status.add_argument("--timeout", type=number(0.1, 86400), default=60.0)
    status.add_argument("--poll-interval", type=number(0.05, 30), default=0.5)
    debug = commands.add_parser("debug").add_subparsers(dest="action", required=True)
    debug_status = debug.add_parser("status", help="Read local experimental debug consent; cannot enable or renew it")
    debug_status.add_argument("device")
    execution_flags(debug_status)
    debug_status.set_defaults(wait=True)
    screen = commands.add_parser("screen", help="Experimental high-risk UI control; requires local owner consent").add_subparsers(dest="action", required=True)
    for action in ("tree", "screenshot", "tap", "swipe", "input", "global"):
        item = screen.add_parser(action)
        item.add_argument("device")
        execution_flags(item)
        item.add_argument("--debug-session", required=True)
        if action != "global":
            item.add_argument("--package", required=action != "tree", help="Exact current foreground package, sent as expected_package")
        if action == "screenshot":
            item.add_argument("--output", help="Export to a new mode-0600 .jpg/.jpeg file; implies --wait")
        if action in {"tap", "swipe"}:
            for coordinate in (("x", "y") if action == "tap" else ("x1", "y1", "x2", "y2")):
                item.add_argument(f"--{coordinate}", type=integer(COORDINATE_MIN, COORDINATE_MAX))
        if action == "swipe":
            item.add_argument("--duration-ms", type=integer(100, 2000))
        if action == "input":
            item.add_argument("--resource-id")
            item.add_argument("--expected-text")
            item.add_argument("--text")
        if action == "global":
            item.add_argument("--action", dest="global_action", choices=GLOBAL_ACTIONS)
    events = commands.add_parser("events")
    events.add_argument("device")
    events.add_argument("--after", type=integer(0, 9223372036854775807), default=0)
    events.add_argument("--limit", type=integer(1, 100), default=50)
    for group, image in (("files", False), ("images", True)):
        send = commands.add_parser(group, help="Send a local file to the phone").add_subparsers(dest="action", required=True).add_parser("send")
        send.add_argument("device")
        send.add_argument("path")
        if not image:
            send.add_argument("--image", action="store_true", help="Save to the gallery; PNG, JPEG, WebP or GIF only")
        send.set_defaults(image_group=image)
        send.add_argument("--name", help="File name on the phone; defaults to the local file name")
        send.add_argument("--mime", help="Explicit media type")
        send.add_argument("--ttl", type=integer(1, 900), default=300)
        send.add_argument("--wait", action="store_true")
        send.add_argument("--timeout", type=number(0.1, 86400), default=120.0)
        send.add_argument("--poll-interval", type=number(0.05, 30), default=0.5)
        send.add_argument("--idempotency-key")
    return root


def operation_args(options):
    if options.command == "screen":
        return screen_args(options)
    if options.args:
        raise BridgeError("VALIDATION_ERROR", "debug.status accepts only empty args and cannot change local consent.")
    return {}


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
        result = client.wait(options.id, options.timeout, options.poll_interval) if options.wait or options.output else client.get_command(options.id)
        return export_screenshot(result, options.output) if options.output else result
    if options.command in {"files", "images"}:
        upload = files.prepare(options.path, image=options.image_group or getattr(options, "image", False), name=options.name,
                               mime=options.mime, ttl=options.ttl, idempotency_key=options.idempotency_key)
        return files.send(client, options.device, upload, options.wait, options.timeout, options.poll_interval)
    if options.command == "command":
        op, args = options.op, options.args
    else:
        op, args = f"{options.command}.{options.action}", operation_args(options)
    output = getattr(options, "output", None)
    result = client.submit(options.device, op, args, options.ttl, options.idempotency_key, options.wait or bool(output), options.timeout, options.poll_interval)
    return export_screenshot(result, output, args.get("expected_package")) if output else result


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
            result = install_skill(options.replace)
        else:
            settings = Settings.load(options.config, options.url, options.request_timeout)
            client = Client(settings)
            secrets = client.secrets
            result = dispatch(options, client)
        if result.get("status") in {"failed", "expired"} and isinstance(result.get("error"), dict):
            upstream = result["error"]
            result = {**result, "error": {"code": error_code(upstream.get("code"), "COMMAND_FAILED"),
                                          "message": safe_message(upstream.get("message"), "Phone reported a command failure.")}}
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
