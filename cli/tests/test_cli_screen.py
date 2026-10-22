import base64
import io
import json
import stat
import time
from importlib.resources import files
from pathlib import Path

import pytest
from PIL import Image

from phonebridge import screen
from phonebridge.cli import dispatch, main, parser
from phonebridge.errors import BridgeError


@pytest.fixture
def cli_wire(wire, environment, monkeypatch):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    return wire


def queue_result(wire, data, status="succeeded", error=None, op="screen.tap"):
    expiry = time.time() + 60
    wire.respond({"id": "screen-command", "status": "queued", "expires_at": expiry})
    resource = {"id": "screen-command", "op": op, "status": status, "expires_at": expiry,
                "data": data, "error": error}
    wire.respond(resource)
    return resource


def screenshot_data():
    image = Image.new("RGB", (20, 40), "white")
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    return {"mime": "image/jpeg", "base64": base64.b64encode(stream.getvalue()).decode("ascii"),
            "width": 20, "height": 40, "package": "exact.package",
            "screen_bounds": [0, 0, 1000, 2000], "window_bounds": [0, 0, 1000, 2000]}


@pytest.mark.parametrize("action, flags, args", [
    ("tree", [], {}),
    ("screenshot", [], {}),
    ("tap", ["--x", "30", "--y", "50"], {"x": 30, "y": 50}),
    ("swipe", ["--x1", "1", "--y1", "2", "--x2", "3", "--y2", "4", "--duration-ms", "5000"],
     {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 5000}),
    ("swipe", ["--x1", "1", "--y1", "2", "--x2", "3", "--y2", "4"], {"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
    ("input", ["--resource-id", "exact.package:id/input", "--text", ""], {"resource_id": "exact.package:id/input", "text": ""}),
    ("input", ["--text", "123"], {"text": "123"}),
    ("global", ["--action", "back"], {"action": "back"}),
    ("tap", ["--args", '{"x":30,"y":50}'], {"x": 30, "y": 50}),
])
def test_screen_helpers_submit_exact_bounded_schema_once(cli_wire, capsys, action, flags, args):
    resource = queue_result(cli_wire, {"outcome": "dispatched", "verified": False}, op=f"screen.{action}")
    arguments = ["screen", action, "device", *flags,
                 "--wait", "--ttl", "15", "--timeout", "2", "--poll-interval", "0.05", "--idempotency-key", "key-EXACT"]
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == resource
    assert not output.err
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]
    assert cli_wire.requests[0]["body"] == {"op": f"screen.{action}",
        "args": args, "ttl_seconds": 15, "idempotency_key": "key-EXACT"}


@pytest.mark.parametrize("arguments", [
    ["debug", "status", "device"],
    ["screen", "tree", "device", "--debug-session", "s"],
    ["screen", "tap", "device", "--package", "p", "--x", "1", "--y", "2"],
    ["screen", "input", "device", "--expected-text", "a", "--text", "b"],
])
def test_removed_commands_and_flags_are_usage_errors(cli_wire, capsys, arguments):
    assert main(arguments) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "USAGE_ERROR"
    assert not cli_wire.requests


@pytest.mark.parametrize("action, flags", [
    ("tree", ["--args", '{"unknown":1}']),
    ("tree", ["--args", '{"debug_session":"s"}']),
    ("tap", ["--args", '{"x":true,"y":0}']),
    ("tap", ["--args", '{"x":1.5,"y":0}']),
    ("tap", ["--args", '{"x":2147483648,"y":0}']),
    ("tap", ["--args", '{"x":-1,"y":0}']),
    ("tap", ["--args", '{"x":100001,"y":0}']),
    ("tap", ["--x", "1", "--y", "2", "--args", '{"x":1}']),
    ("swipe", ["--args", '{"x1":0,"y1":0,"x2":1,"y2":1,"duration_ms":49}']),
    ("swipe", ["--args", '{"x1":0,"y1":0,"x2":1,"y2":1,"duration_ms":5001}']),
    ("input", ["--resource-id", "id", "--args", json.dumps({"text": "x" * 4097})]),
    ("input", ["--resource-id", "id", "--args", json.dumps({"text": "😀" * 2049})]),
    ("input", ["--resource-id", "id", "--args", '{"text":null}']),
    ("global", ["--args", '{"action":"permission_grant"}']),
    ("tree", ["--args", '{"expected_package":"p"}']),
    ("tap", ["--args", '{"x":1}']),
    ("input", []),
    ("input", ["--resource-id", "x" * 257, "--text", "a"]),
])
def test_invalid_screen_json_is_rejected_before_http(cli_wire, capsys, action, flags):
    assert main(["screen", action, "device", *flags]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "VALIDATION_ERROR"
    assert not cli_wire.requests


@pytest.mark.parametrize("flags", [
    ["--x", "nan", "--y", "0"], ["--x", "1.5", "--y", "0"],
    ["--x", "2147483648", "--y", "0"], ["--x", "-1", "--y", "0"], ["--x", "100001", "--y", "0"],
    ["--x", "0", "--y", "0", "--timeout", "nan"],
    ["--x", "0", "--y", "0", "--ttl", "0"],
])
def test_invalid_numeric_flags_are_usage_errors(cli_wire, capsys, flags):
    assert main(["screen", "tap", "device", *flags]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "USAGE_ERROR"
    assert not cli_wire.requests


@pytest.mark.parametrize("code", ["PERMISSION_REQUIRED", "AMBIGUOUS_STATE"])
def test_gesture_failure_preserves_code_without_retry(cli_wire, capsys, code):
    queue_result(cli_wire, None, status="failed", error={"code": code, "message": "private screen text"})
    assert main(["screen", "tap", "device", "--x", "1", "--y", "2", "--wait"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == {"code": code, "message": "private screen text"}
    assert not output.err
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


@pytest.mark.parametrize("code", ["PERMISSION_REQUIRED", "AMBIGUOUS_STATE"])
def test_http_failure_preserves_code_without_retry(cli_wire, capsys, code):
    cli_wire.respond({"error": {"code": code}}, status=403)
    assert main(["screen", "global", "device", "--action", "back", "--wait"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == code
    assert [request["method"] for request in cli_wire.requests] == ["POST"]


def test_ambiguous_submit_never_retries(cli_wire, capsys):
    cli_wire.respond({}, status="disconnect")
    assert main(["screen", "global", "device", "--action", "home", "--wait", "--idempotency-key", "unchanged"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "TRANSPORT_ERROR"
    assert error["may_have_been_submitted"] is True
    assert error["idempotency_key"] == "unchanged"
    assert [request["method"] for request in cli_wire.requests] == ["POST"]


def test_expired_screen_command_is_not_resubmitted(cli_wire, capsys):
    resource = queue_result(cli_wire, None, status="expired")
    assert main(["screen", "global", "device", "--action", "back", "--wait"]) == 1
    assert json.loads(capsys.readouterr().out) == resource
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


def test_screenshot_default_preserves_base64_json(cli_wire, capsys):
    resource = queue_result(cli_wire, screenshot_data(), op="screen.screenshot")
    assert main(["screen", "screenshot", "device", "--wait"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == resource
    assert not output.err


def test_screenshot_output_waits_and_writes_new_private_viewable_image(cli_wire, tmp_path, capsys):
    data = screenshot_data()
    queue_result(cli_wire, data, op="screen.screenshot")
    path = tmp_path / "capture.jpg"
    assert main(["screen", "screenshot", "device", "--output", str(path)]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["data"] == {**{key: value for key, value in data.items() if key != "base64"}, "output_path": str(path)}
    assert path.read_bytes() == base64.b64decode(data["base64"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with Image.open(path) as image:
        assert image.size == (20, 40)
        image.verify()
    assert not output.err
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


@pytest.mark.parametrize("existing", ["regular", "symlink"])
def test_export_never_overwrites_existing_path(cli_wire, tmp_path, capsys, existing):
    target = tmp_path / "existing.jpg"
    target.write_bytes(b"preserve")
    path = target
    if existing == "symlink":
        path = tmp_path / "link.jpg"
        path.symlink_to(target)
    queue_result(cli_wire, screenshot_data(), op="screen.screenshot")
    assert main(["screen", "screenshot", "device", "--output", str(path)]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "LOCAL_FILE_ERROR"
    assert error["command_id"] == "screen-command"
    assert target.read_bytes() == b"preserve"
    if existing == "symlink":
        assert path.is_symlink()
        assert path.readlink() == target
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


@pytest.mark.parametrize("changes", [
    {"base64": "not base64!"}, {"base64": base64.b64encode(b"not an image").decode()},
    {"base64": "A" * 327684}, {"mime": "image/png"}, {"width": 21}, {"height": True},
    {"width": 1281}, {"window_bounds": [1, 2, 1, 3]}, {"screen_bounds": [0, 0, 1]},
    {"window_bounds": [0, 0, False, 2]}, {"window_bounds": None},
])
def test_invalid_capture_cannot_write_or_resubmit(cli_wire, tmp_path, capsys, changes):
    data = {**screenshot_data(), **changes}
    queue_result(cli_wire, data, op="screen.screenshot")
    path = tmp_path / "capture.jpg"
    assert main(["screen", "screenshot", "device", "--output", str(path)]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "INVALID_RESPONSE"
    assert error["command_id"] == "screen-command"
    assert not path.exists()
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


def test_existing_screenshot_can_be_exported_with_only_get(cli_wire, tmp_path, capsys):
    data = screenshot_data()
    cli_wire.respond({"id": "original", "op": "screen.screenshot", "status": "succeeded", "data": data})
    path = tmp_path / "saved.jpg"
    assert main(["command-status", "original", "--output", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["output_path"] == str(path)
    assert path.read_bytes() == base64.b64decode(data["base64"])
    assert [request["method"] for request in cli_wire.requests] == ["GET"]


def test_failed_screenshot_preserves_permission_failure_without_export(cli_wire, tmp_path, capsys):
    queue_result(cli_wire, None, status="failed", error={"code": "PERMISSION_REQUIRED"}, op="screen.screenshot")
    path = tmp_path / "capture.jpg"
    assert main(["screen", "screenshot", "device", "--output", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "PERMISSION_REQUIRED"
    assert not path.exists()
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


def test_skill_contract_is_bundled_exactly():
    canonical = Path(__file__).resolve().parents[2] / "skills" / "phonebridge" / "SKILL.md"
    content = canonical.read_bytes()
    assert files("phonebridge").joinpath("resources", "SKILL.md").read_bytes() == content
    for phrase in (b"screen tree", b"screen input", b"--output", b"screen_bounds", b"PERMISSION_REQUIRED", b"AMBIGUOUS_STATE"):
        assert phrase in content
    for removed in (b"debug status", b"--debug-session", b"--package"):
        assert removed not in content


def test_screen_parser_retains_submit_once_options():
    options = parser().parse_args(["screen", "tap", "device",
                                  "--args", '{"x":0,"y":100000}', "--ttl", "1", "--timeout", "0.1",
                                  "--poll-interval", "0.05", "--idempotency-key", "Case-Key"])
    assert options.args == {"x": 0, "y": 100000}
    assert options.ttl == 1
    assert options.timeout == 0.1
    assert options.poll_interval == 0.05
    assert options.idempotency_key == "Case-Key"
    assert options.wait is False


def test_exact_identities_and_coordinates_are_not_rewritten(cli_wire, capsys):
    cli_wire.respond({"id": "cmd", "status": "queued", "expires_at": time.time() + 60})
    assert main(["screen", "tap", "Device-Case",
                 "--x", "10", "--y", "20", "--idempotency-key", "Key-Case"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert cli_wire.requests[0]["path"] == "/v1/devices/Device-Case/commands"
    assert cli_wire.requests[0]["body"]["args"] == {"x": 10, "y": 20}
    assert [request["method"] for request in cli_wire.requests] == ["POST"]


def test_read_failure_does_not_retry_or_resubmit(cli_wire, capsys):
    cli_wire.respond({"id": "cmd", "status": "queued", "expires_at": time.time() + 60})
    cli_wire.respond({}, status="disconnect")
    assert main(["screen", "global", "device", "--action", "recents", "--wait"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "TRANSPORT_ERROR"
    assert error["command_id"] == "cmd"
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


def test_wait_timeout_leaves_original_command_for_readback(wire, fake_time):
    wire.client.clock = fake_time.monotonic
    wire.client.wall_clock = fake_time.wall
    wire.client.sleep = fake_time.sleep
    wire.respond({"id": "cmd", "status": "queued", "expires_at": 2000})
    wire.respond({"id": "cmd", "status": "sent", "expires_at": 2000})
    options = parser().parse_args(["screen", "global", "device", "--action", "back", "--wait", "--timeout", "0.1"])
    with pytest.raises(BridgeError) as caught:
        dispatch(options, wire.client)
    assert caught.value.code == "WAIT_TIMEOUT"
    assert caught.value.details["command_id"] == "cmd"
    assert [request["method"] for request in wire.requests] == ["POST", "GET"]


def test_parent_symlink_cannot_receive_export(cli_wire, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    queue_result(cli_wire, screenshot_data(), op="screen.screenshot")
    assert main(["screen", "screenshot", "device", "--output", str(link / "capture.jpg")]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "LOCAL_FILE_ERROR"
    assert not (outside / "capture.jpg").exists()


def test_existing_non_screenshot_cannot_be_exported(cli_wire, tmp_path, capsys):
    cli_wire.respond({"id": "original", "op": "clipboard.get", "status": "succeeded", "data": screenshot_data()})
    path = tmp_path / "capture.jpg"
    assert main(["command-status", "original", "--output", str(path)]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_RESPONSE"
    assert not path.exists()
    assert [request["method"] for request in cli_wire.requests] == ["GET"]


def test_missing_native_bounds_never_exports(cli_wire, tmp_path, capsys):
    data = screenshot_data()
    del data["screen_bounds"]
    queue_result(cli_wire, data, op="screen.screenshot")
    path = tmp_path / "capture.jpg"
    assert main(["screen", "screenshot", "device", "--output", str(path)]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_RESPONSE"
    assert not path.exists()


@pytest.mark.parametrize("mode", ["submit", "recover"])
@pytest.mark.parametrize("stage", ["decode", "image_load", "expanduser", "open", "fchmod", "fdopen", "write", "flush", "fsync"])
@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError, RuntimeError])
def test_export_local_failure_retains_id_cleans_partial_file_and_never_replays(
        cli_wire, tmp_path, capsys, monkeypatch, mode, stage, failure):
    data = screenshot_data()
    path = tmp_path / "private-capture.jpg"
    private_detail = "private local diagnostic: " + str(path)
    if mode == "submit":
        queue_result(cli_wire, data, op="screen.screenshot")
        command_id = "screen-command"
        arguments = ["screen", "screenshot", "device"]
        methods = ["POST", "GET"]
    else:
        command_id = "original"
        cli_wire.respond({"id": command_id, "op": "screen.screenshot", "status": "succeeded", "data": data})
        arguments = ["command-status", command_id]
        methods = ["GET"]
    reached = []
    partial = []
    real_decode = screen.base64.b64decode
    real_expanduser = Path.expanduser
    real_open = screen.os.open
    real_fdopen = screen.os.fdopen
    real_fchmod = screen.os.fchmod
    real_image_open = Image.open
    real_fsync = screen.os.fsync

    def fail():
        reached.append(stage)
        raise failure(private_detail)

    def decode(encoded, *args, **kwargs):
        if encoded == data["base64"]:
            fail()
        return real_decode(encoded, *args, **kwargs)

    def image_open(source, *args, **kwargs):
        image = real_image_open(source, *args, **kwargs)
        if isinstance(source, io.BytesIO) and source.getvalue() == real_decode(data["base64"]):
            monkeypatch.setattr(image, "load", fail)
        return image

    def expanduser(candidate):
        if candidate == path:
            fail()
        return real_expanduser(candidate)

    def open_file(name, flags, *args, **kwargs):
        if name == path.name and flags & screen.os.O_CREAT and kwargs.get("dir_fd") is not None:
            fail()
        return real_open(name, flags, *args, **kwargs)

    def is_export_descriptor(descriptor):
        if not path.exists():
            return False
        opened = screen.os.fstat(descriptor)
        current = path.stat()
        return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def close(self):
            return self.stream.close()

        def write(self, content):
            if stage == "write":
                self.stream.write(content[:11])
                self.stream.flush()
                partial.append(path.read_bytes())
                fail()
            return self.stream.write(content)

        def flush(self):
            self.stream.flush()
            if stage == "flush":
                partial.append(path.read_bytes())
                fail()

        def fileno(self):
            return self.stream.fileno()

    def fchmod(descriptor, mode):
        if is_export_descriptor(descriptor):
            fail()
        return real_fchmod(descriptor, mode)

    def fdopen(descriptor, *args, **kwargs):
        selected = is_export_descriptor(descriptor)
        if selected and stage == "fdopen":
            fail()
        stream = real_fdopen(descriptor, *args, **kwargs)
        return Stream(stream) if selected else stream

    def fsync(descriptor):
        if is_export_descriptor(descriptor):
            real_fsync(descriptor)
            partial.append(path.read_bytes())
            fail()
        return real_fsync(descriptor)

    with monkeypatch.context() as patch:
        if stage == "decode":
            patch.setattr(screen.base64, "b64decode", decode)
        elif stage == "image_load":
            patch.setattr(Image, "open", image_open)
        elif stage == "expanduser":
            patch.setattr(Path, "expanduser", expanduser)
        elif stage == "open":
            patch.setattr(screen.os, "open", open_file)
        elif stage == "fchmod":
            patch.setattr(screen.os, "fchmod", fchmod)
        elif stage in {"fdopen", "write", "flush"}:
            patch.setattr(screen.os, "fdopen", fdopen)
        else:
            patch.setattr(screen.os, "fsync", fsync)
        assert main([*arguments, "--output", str(path)]) == (130 if failure is KeyboardInterrupt else 1)
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["command_id"] == command_id
    if failure is KeyboardInterrupt:
        assert error["code"] == "INTERRUPTED"
    else:
        assert error["code"] == ("INVALID_RESPONSE" if stage in {"decode", "image_load"} and failure is OSError else "LOCAL_FILE_ERROR")
    assert str(path) not in output.err
    assert private_detail not in output.err
    assert len(output.err) < 512
    assert reached == [stage]
    if stage in {"write", "flush", "fsync"}:
        expected = real_decode(data["base64"])
        assert partial == [expected[:11] if stage == "write" else expected]
    assert not path.exists()
    assert not path.is_symlink()
    assert [(request["method"], request["path"]) for request in cli_wire.requests] == (
        [("POST", "/v1/devices/device/commands")] if mode == "submit" else []) + [
            ("GET", f"/v1/commands/{command_id}")]
    assert [request["method"] for request in cli_wire.requests] == methods


@pytest.mark.parametrize("mode", ["submit", "recover"])
@pytest.mark.parametrize("kind", ["unknown_user", "missing_parent", "nul"])
def test_invalid_local_path_has_safe_correlated_error_without_replay(cli_wire, tmp_path, capsys, mode, kind):
    if kind == "unknown_user":
        path = "~phonebridge:missing-user/private-capture.jpg"
    elif kind == "missing_parent":
        path = str(tmp_path / "missing-private-directory" / "capture.jpg")
    else:
        path = str(tmp_path / "private\x00capture.jpg")
    if mode == "submit":
        queue_result(cli_wire, screenshot_data(), op="screen.screenshot")
        command_id = "screen-command"
        arguments = ["screen", "screenshot", "device"]
        methods = ["POST", "GET"]
    else:
        command_id = "original"
        cli_wire.respond({"id": command_id, "op": "screen.screenshot", "status": "succeeded", "data": screenshot_data()})
        arguments = ["command-status", command_id]
        methods = ["GET"]
    assert main([*arguments, "--output", path]) == 1
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["code"] == "LOCAL_FILE_ERROR"
    assert error["command_id"] == command_id
    assert path not in output.err
    assert path not in error["message"]
    assert str(tmp_path) not in output.err
    assert len(output.err) < 512
    assert not list(tmp_path.iterdir())
    assert [request["method"] for request in cli_wire.requests] == methods


@pytest.mark.parametrize("existing", ["regular", "symlink"])
def test_interrupt_before_exclusive_create_preserves_existing_path(cli_wire, tmp_path, capsys, monkeypatch, existing):
    target = tmp_path / "existing.jpg"
    target.write_bytes(b"preserve")
    path = target
    if existing == "symlink":
        path = tmp_path / "link.jpg"
        path.symlink_to(target)
    queue_result(cli_wire, screenshot_data(), op="screen.screenshot")
    real_open = screen.os.open
    reached = []

    def open_file(name, flags, *args, **kwargs):
        if name == path.name and flags & screen.os.O_CREAT and kwargs.get("dir_fd") is not None:
            reached.append(name)
            raise KeyboardInterrupt(str(path))
        return real_open(name, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(screen.os, "open", open_file)
        assert main(["screen", "screenshot", "device",
                     "--output", str(path)]) == 130
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["code"] == "INTERRUPTED"
    assert error["command_id"] == "screen-command"
    assert str(path) not in output.err
    assert reached == [path.name]
    assert target.read_bytes() == b"preserve"
    if existing == "symlink":
        assert path.is_symlink()
        assert path.readlink() == target
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


@pytest.mark.parametrize("replacement", ["regular", "symlink"])
def test_export_cleanup_does_not_remove_a_replaced_file_or_symlink(
        cli_wire, tmp_path, capsys, monkeypatch, replacement):
    path = tmp_path / "capture.jpg"
    target = tmp_path / "existing.jpg"
    target.write_bytes(b"preserve")
    queue_result(cli_wire, screenshot_data(), op="screen.screenshot")
    real_fsync = screen.os.fsync
    reached = []

    def fsync(descriptor):
        opened = screen.os.fstat(descriptor)
        current = path.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            return real_fsync(descriptor)
        real_fsync(descriptor)
        path.unlink()
        if replacement == "symlink":
            path.symlink_to(target)
        else:
            path.write_bytes(b"replacement")
        reached.append(replacement)
        raise KeyboardInterrupt(str(path))

    with monkeypatch.context() as patch:
        patch.setattr(screen.os, "fsync", fsync)
        assert main(["screen", "screenshot", "device",
                     "--output", str(path)]) == 130
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["code"] == "INTERRUPTED"
    assert error["command_id"] == "screen-command"
    assert str(path) not in output.err
    assert reached == [replacement]
    assert target.read_bytes() == b"preserve"
    if replacement == "symlink":
        assert path.is_symlink()
        assert path.readlink() == target
    else:
        assert path.read_bytes() == b"replacement"
    assert [request["method"] for request in cli_wire.requests] == ["POST", "GET"]


def test_interrupted_export_can_recover_original_command_with_get_only(cli_wire, tmp_path, capsys, monkeypatch):
    data = screenshot_data()
    resource = queue_result(cli_wire, data, op="screen.screenshot")
    path = tmp_path / "capture.jpg"
    real_fsync = screen.os.fsync
    captured = []

    def fsync(descriptor):
        opened = screen.os.fstat(descriptor)
        current = path.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            return real_fsync(descriptor)
        real_fsync(descriptor)
        captured.append(path.read_bytes())
        raise KeyboardInterrupt(str(path))

    with monkeypatch.context() as patch:
        patch.setattr(screen.os, "fsync", fsync)
        assert main(["screen", "screenshot", "device",
                     "--output", str(path)]) == 130
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["code"] == "INTERRUPTED"
    assert error["command_id"] == resource["id"]
    assert captured == [base64.b64decode(data["base64"])]
    assert not path.exists()
    cli_wire.respond(resource)
    assert main(["command-status", error["command_id"], "--output", str(path)]) == 0
    recovered = capsys.readouterr()
    assert not recovered.err
    result = json.loads(recovered.out)
    assert result["id"] == resource["id"]
    assert result["data"] == {**{key: value for key, value in data.items() if key != "base64"},
                              "output_path": str(path)}
    assert path.read_bytes() == captured[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [(request["method"], request["path"]) for request in cli_wire.requests] == [
        ("POST", "/v1/devices/device/commands"), ("GET", "/v1/commands/screen-command"),
        ("GET", "/v1/commands/screen-command")]


def queue_export_attempt(wire, path, mode):
    data = screenshot_data()
    if mode == "submit":
        queue_result(wire, data, op="screen.screenshot")
        command_id = "screen-command"
        arguments = ["screen", "screenshot", "device"]
        requests = [("POST", "/v1/devices/device/commands")]
    else:
        command_id = "original"
        wire.respond({"id": command_id, "op": "screen.screenshot", "status": "succeeded", "data": data})
        arguments = ["command-status", command_id]
        requests = []
    requests.append(("GET", f"/v1/commands/{command_id}"))
    return [*arguments, "--output", str(path)], command_id, requests, base64.b64decode(data["base64"])


def assert_export_cleanup_error(output, command_id, code, path, wire, requests):
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["command_id"] == command_id
    assert error["code"] == code
    assert str(path) not in output.err
    assert "private cleanup diagnostic" not in output.err
    assert len(output.err) < 512
    assert [(request["method"], request["path"]) for request in wire.requests] == requests


class ExportHandles:
    def __init__(self, path, sentinel):
        self.path = path
        self.sentinel = sentinel
        self.real_open = screen.os.open
        self.real_close = screen.os.close
        self.real_fstat = screen.os.fstat
        self.real_dup2 = screen.os.dup2
        self.active = {}
        self.acquired = []
        self.events = []
        self.reused = []
        self.close_errors = []
        self.close_failure = None
        self.traversal_failure = None
        self.traversal_secondary = False
        self.traversal_child = None
        self.stream_failure = False
        self.stream_closes = 0
        self.body_failure = None
        self.body_reached = []
        self.partial = []
        self.primary = None

    def open(self, name, flags, *args, **kwargs):
        descriptor = self.real_open(name, flags, *args, **kwargs)
        parent = self.active.get(kwargs.get("dir_fd"))
        if (flags & screen.os.O_DIRECTORY and (str(name) == self.path.anchor or parent is not None)) or (
                flags & screen.os.O_CREAT and name == self.path.name and parent is not None):
            kind = "directory" if flags & screen.os.O_DIRECTORY else "file"
            record = {"fd": descriptor, "kind": kind, "name": str(name), "closes": 0}
            self.active[descriptor] = record
            self.acquired.append(record)
            if parent is not None and name == self.path.parent.name and kind == "directory":
                self.traversal_child = record
                if self.traversal_failure is not None:
                    self.close_failure = parent
        return descriptor

    def reuse(self, descriptor):
        replacement = self.real_open(self.sentinel, screen.os.O_RDONLY)
        if replacement != descriptor:
            self.real_dup2(replacement, descriptor)
            self.real_close(replacement)
        self.reused.append(descriptor)

    def close(self, descriptor):
        if descriptor in self.reused:
            self.events.append("reclosed-unrelated")
            return self.real_close(descriptor)
        record = self.active.pop(descriptor, None)
        if record is None:
            return self.real_close(descriptor)
        record["closes"] += 1
        self.events.append("close-" + record["kind"])
        self.real_close(descriptor)
        selected = (self.close_failure is record or self.close_failure == record["kind"]
                    or isinstance(self.close_failure, set) and record["kind"] in self.close_failure)
        if selected:
            if isinstance(self.close_failure, set):
                self.close_failure.remove(record["kind"])
            else:
                self.close_failure = None
            self.reuse(descriptor)
            self.close_errors.append(record["kind"])
            if self.traversal_failure is not None:
                failure, self.traversal_failure = self.traversal_failure, None
                if self.traversal_secondary:
                    self.close_failure = self.traversal_child
                self.primary = failure("private cleanup diagnostic")
                raise self.primary
            raise OSError("private cleanup diagnostic")

    def fail_body(self, stage):
        self.body_reached.append(stage)
        self.primary = self.body_failure("private cleanup diagnostic")
        raise self.primary

    def finish(self):
        try:
            assert self.acquired
            assert not self.active
            assert all(record["closes"] == 1 for record in self.acquired)
            assert "reclosed-unrelated" not in self.events
            expected = self.sentinel.stat()
            for descriptor in self.reused:
                current = self.real_fstat(descriptor)
                assert (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
        finally:
            for descriptor in list(self.active):
                self.real_close(descriptor)
            for descriptor in self.reused:
                try:
                    self.real_close(descriptor)
                except OSError:
                    pass


@pytest.mark.parametrize("mode", ["submit", "recover"])
@pytest.mark.parametrize("stage, secondary", [
    ("write", "stream"), ("flush", "stream"), ("fsync", "stream"),
    ("write", "file"), ("flush", "directory"), ("fsync", "directory"),
    ("fchmod", "file"), ("fchmod", "directory"),
    ("write", "all"), ("flush", "all"), ("fsync", "all"), ("fchmod", "all"),
])
def test_export_body_interrupt_survives_independent_close_errors(
        cli_wire, tmp_path, capsys, monkeypatch, mode, stage, secondary):
    path = tmp_path / "capture.jpg"
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"preserve")
    arguments, command_id, requests, raw = queue_export_attempt(cli_wire, path, mode)
    handles = ExportHandles(path, sentinel)
    handles.body_failure = KeyboardInterrupt
    handles.stream_failure = secondary in {"stream", "all"}
    real_fdopen = screen.os.fdopen
    real_fchmod = screen.os.fchmod
    real_fsync = screen.os.fsync

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            self.close()

        def write(self, content):
            if stage == "write":
                self.stream.write(content[:11])
                self.stream.flush()
                handles.partial.append(path.read_bytes())
                handles.fail_body(stage)
            return self.stream.write(content)

        def flush(self):
            self.stream.flush()
            if stage == "flush":
                handles.partial.append(path.read_bytes())
                handles.fail_body(stage)

        def fileno(self):
            return self.stream.fileno()

        def close(self):
            handles.stream_closes += 1
            descriptor = self.stream.fileno()
            self.stream.close()
            handles.events.append("close-stream")
            if descriptor in handles.active and not handles.active[descriptor]["closes"]:
                try:
                    handles.real_fstat(descriptor)
                except OSError:
                    handles.active.pop(descriptor)["closes"] += 1
                    handles.reuse(descriptor)
            if handles.stream_failure:
                handles.close_errors.append("stream")
                raise OSError("private cleanup diagnostic")

    def fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        return Stream(stream) if descriptor in handles.active else stream

    def fchmod(descriptor, mode):
        real_fchmod(descriptor, mode)
        if stage == "fchmod" and descriptor in handles.active:
            handles.fail_body(stage)

    def fsync(descriptor):
        real_fsync(descriptor)
        if stage == "fsync" and descriptor in handles.active:
            handles.partial.append(path.read_bytes())
            handles.fail_body(stage)

    def fail_body(stage_name):
        if secondary in {"file", "directory", "all"}:
            handles.close_failure = {"file", "directory"} if secondary == "all" else secondary
        original_fail_body(stage_name)

    original_fail_body = handles.fail_body
    handles.fail_body = fail_body
    real_writer = screen.write_screenshot
    observed = []

    def writer(*args, **kwargs):
        try:
            return real_writer(*args, **kwargs)
        except BaseException as error:
            observed.append(error)
            raise

    try:
        with monkeypatch.context() as patch:
            patch.setattr(screen.os, "open", handles.open)
            patch.setattr(screen.os, "close", handles.close)
            patch.setattr(screen.os, "fdopen", fdopen)
            patch.setattr(screen.os, "fchmod", fchmod)
            patch.setattr(screen.os, "fsync", fsync)
            patch.setattr(screen, "write_screenshot", writer)
            assert main(arguments) == 130
        assert_export_cleanup_error(capsys.readouterr(), command_id, "INTERRUPTED", path, cli_wire, requests)
        assert handles.body_reached == [stage]
        assert len(observed) == 1
        assert observed[0] is handles.primary
        expected_errors = ["file", "directory"] if secondary == "all" else [secondary]
        if secondary == "all" and stage != "fchmod":
            expected_errors.insert(0, "stream")
        assert handles.close_errors == expected_errors
        assert len(handles.reused) == (2 if secondary == "all" else 0 if secondary == "stream" else 1)
        assert handles.stream_closes == (0 if stage == "fchmod" else 1)
        if stage != "fchmod":
            assert handles.partial == [raw[:11] if stage == "write" else raw]
        assert not path.exists()
        assert handles.acquired[-1]["kind"] == "file"
        assert handles.events[-1] == "close-directory"
        assert sentinel.read_bytes() == b"preserve"
    finally:
        handles.finish()


@pytest.mark.parametrize("mode", ["submit", "recover"])
@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError, RuntimeError])
@pytest.mark.parametrize("secondary", [False, True])
def test_export_transfers_real_child_before_failing_parent_close(
        cli_wire, tmp_path, capsys, monkeypatch, mode, failure, secondary):
    parent = tmp_path / "child"
    parent.mkdir()
    path = parent / "capture.jpg"
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"preserve")
    arguments, command_id, requests, raw = queue_export_attempt(cli_wire, path, mode)
    handles = ExportHandles(path, sentinel)
    handles.traversal_failure = failure
    handles.traversal_secondary = secondary
    real_writer = screen.write_screenshot
    observed = []

    def writer(*args, **kwargs):
        try:
            return real_writer(*args, **kwargs)
        except BaseException as error:
            observed.append(error)
            raise

    try:
        with monkeypatch.context() as patch:
            patch.setattr(screen.os, "open", handles.open)
            patch.setattr(screen.os, "close", handles.close)
            patch.setattr(screen, "write_screenshot", writer)
            assert main(arguments) == (130 if failure is KeyboardInterrupt else 1)
        assert_export_cleanup_error(capsys.readouterr(), command_id,
                                    "INTERRUPTED" if failure is KeyboardInterrupt else "LOCAL_FILE_ERROR",
                                    path, cli_wire, requests)
        assert len(observed) == 1
        assert observed[0] is handles.primary
        assert handles.traversal_child is not None
        assert handles.traversal_child["closes"] == 1
        assert not any(record["kind"] == "file" for record in handles.acquired)
        assert not path.exists()
        assert not list(parent.iterdir())
        assert len(handles.reused) == (2 if secondary else 1)
        assert handles.close_errors == (["directory", "directory"] if secondary else ["directory"])
    finally:
        handles.finish()


@pytest.mark.parametrize("mode", ["submit", "recover"])
@pytest.mark.parametrize("primary", ["bridge", "runtime", "os", "none"])
@pytest.mark.parametrize("secondary", ["stream", "file", "directory", "all"])
def test_export_close_failures_preserve_primary_and_cleanup_created_entry(
        cli_wire, tmp_path, capsys, monkeypatch, mode, primary, secondary):
    path = tmp_path / "capture.jpg"
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"preserve")
    arguments, command_id, requests, raw = queue_export_attempt(cli_wire, path, mode)
    handles = ExportHandles(path, sentinel)
    real_fdopen = screen.os.fdopen
    real_fsync = screen.os.fsync
    captured = []
    failure = (BridgeError("AMBIGUOUS_STATE", "Primary export failure.") if primary == "bridge" else
               {"runtime": RuntimeError, "os": OSError, "none": lambda message: None}[primary]("private cleanup diagnostic"))

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            self.close()

        def write(self, content):
            return self.stream.write(content)

        def flush(self):
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

        def close(self):
            handles.stream_closes += 1
            self.stream.close()
            handles.events.append("close-stream")
            if secondary in {"stream", "all"}:
                handles.close_errors.append("stream")
                raise OSError("private cleanup diagnostic")

    def fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        return Stream(stream) if descriptor in handles.active else stream

    def fsync(descriptor):
        real_fsync(descriptor)
        if descriptor in handles.active:
            captured.append(path.read_bytes())
            handles.close_failure = ({"file", "directory"} if secondary == "all" else
                                     secondary if secondary != "stream" else None)
            if failure is not None:
                raise failure

    real_writer = screen.write_screenshot
    observed = []

    def writer(*args, **kwargs):
        try:
            return real_writer(*args, **kwargs)
        except BaseException as error:
            observed.append(error)
            raise

    try:
        with monkeypatch.context() as patch:
            patch.setattr(screen.os, "open", handles.open)
            patch.setattr(screen.os, "close", handles.close)
            patch.setattr(screen.os, "fdopen", fdopen)
            patch.setattr(screen.os, "fsync", fsync)
            patch.setattr(screen, "write_screenshot", writer)
            assert main(arguments) == 1
        assert_export_cleanup_error(capsys.readouterr(), command_id,
                                    "AMBIGUOUS_STATE" if primary == "bridge" else "LOCAL_FILE_ERROR",
                                    path, cli_wire, requests)
        assert captured == [raw]
        assert len(observed) == 1
        if failure is not None:
            assert observed[0] is failure
        else:
            assert isinstance(observed[0], OSError)
        assert handles.stream_closes == 1
        assert handles.close_errors == (["stream", "file", "directory"] if secondary == "all" else [secondary])
        assert len(handles.reused) == (2 if secondary == "all" else 0 if secondary == "stream" else 1)
        assert handles.events[-1] == "close-directory"
        assert not path.exists()
        assert not path.is_symlink()
    finally:
        handles.finish()


@pytest.mark.parametrize("mode", ["submit", "recover"])
@pytest.mark.parametrize("replacement", ["regular", "symlink"])
@pytest.mark.parametrize("secondary", ["stream", "file", "directory"])
def test_cleanup_only_failure_keeps_replacement_identity(
        cli_wire, tmp_path, capsys, monkeypatch, mode, replacement, secondary):
    path = tmp_path / "capture.jpg"
    target = tmp_path / "existing.jpg"
    target.write_bytes(b"preserve")
    arguments, command_id, requests, raw = queue_export_attempt(cli_wire, path, mode)
    handles = ExportHandles(path, target)
    real_fdopen = screen.os.fdopen
    real_fsync = screen.os.fsync
    replaced = []

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def write(self, content):
            return self.stream.write(content)

        def flush(self):
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

        def close(self):
            handles.stream_closes += 1
            self.stream.close()
            handles.events.append("close-stream")
            if secondary == "stream":
                handles.close_errors.append("stream")
                raise OSError("private cleanup diagnostic")

    def fdopen(descriptor, *args, **kwargs):
        stream = real_fdopen(descriptor, *args, **kwargs)
        return Stream(stream) if descriptor in handles.active else stream

    def fsync(descriptor):
        real_fsync(descriptor)
        if descriptor not in handles.active:
            return
        opened = handles.real_fstat(descriptor)
        assert path.read_bytes() == raw
        path.unlink()
        if replacement == "regular":
            path.write_bytes(b"replacement")
        else:
            path.symlink_to(target)
        current = path.lstat()
        assert (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        replaced.append(replacement)
        handles.close_failure = secondary if secondary != "stream" else None

    try:
        with monkeypatch.context() as patch:
            patch.setattr(screen.os, "open", handles.open)
            patch.setattr(screen.os, "close", handles.close)
            patch.setattr(screen.os, "fdopen", fdopen)
            patch.setattr(screen.os, "fsync", fsync)
            assert main(arguments) == 1
        assert_export_cleanup_error(capsys.readouterr(), command_id, "LOCAL_FILE_ERROR", path, cli_wire, requests)
        assert replaced == [replacement]
        assert handles.stream_closes == 1
        assert handles.close_errors == [secondary]
        assert len(handles.reused) == (0 if secondary == "stream" else 1)
        assert target.read_bytes() == b"preserve"
        if replacement == "regular":
            assert path.read_bytes() == b"replacement"
        else:
            assert path.is_symlink()
            assert path.readlink() == target
    finally:
        handles.finish()
