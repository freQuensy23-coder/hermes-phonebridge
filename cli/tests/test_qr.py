import json
import stat
import time

import pytest
import zxingcpp
from PIL import Image

from phonebridge.cli import dispatch, main, parser
from phonebridge.errors import BridgeError
from phonebridge.local import pairing_qr


PAIR_TOKEN = "p" * 43


def pairing(expiry=2000):
    return {"id": "pair", "expires_at": expiry, "qr_payload": {"v": 1, "server": "https://bridge.example", "token": PAIR_TOKEN, "expires_at": expiry}}


def test_real_png_independent_decode(tmp_path):
    path = tmp_path / "pair.png"
    result = pairing_qr(pairing(), str(path), 1000)
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    decoded = zxingcpp.read_barcode(Image.open(path).convert("RGB"))
    assert decoded is not None
    assert json.loads(decoded.text) == pairing()["qr_payload"]
    assert result == {"id": "pair", "expires_at": 2000, "status": "pending", "qr_path": str(path)}
    assert PAIR_TOKEN not in json.dumps(result)


def test_pair_cli_wire_png_and_token_free_stdout(tmp_path, wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    response = pairing(int(time.time()) + 300)
    wire.respond(response)
    path = tmp_path / "scan.png"
    assert main(["pair", "--name", "Pixel", "--ttl", "300", "--qr", str(path)]) == 0
    output = capsys.readouterr()
    assert PAIR_TOKEN not in output.out + output.err
    assert wire.client.settings.token not in output.out + output.err
    assert json.loads(output.out)["id"] == "pair"
    request = wire.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/pairings"
    assert request["body"] == {"name": "Pixel", "ttl_seconds": 300}
    decoded = zxingcpp.read_barcode(Image.open(path).convert("RGB"))
    assert json.loads(decoded.text) == response["qr_payload"]


def test_pair_token_is_redacted_even_when_echoed_as_metadata(tmp_path, wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    response = pairing(int(time.time()) + 300)
    response["id"] = PAIR_TOKEN
    wire.respond(response)
    assert main(["pair", "--name", "Pixel", "--qr", str(tmp_path / "scan.png")]) == 0
    output = capsys.readouterr()
    assert PAIR_TOKEN not in output.out + output.err


@pytest.mark.parametrize("expiry", [999, 1000, True, "2000", float("nan"), float("inf")])
def test_expired_or_invalid_pairing_refused(tmp_path, expiry):
    with pytest.raises(BridgeError) as caught:
        pairing_qr(pairing(expiry), str(tmp_path / "pair.png"), 1000)
    assert caught.value.code == "PAIRING_EXPIRED"
    assert not (tmp_path / "pair.png").exists()


def test_expiry_mismatch_refused(tmp_path):
    result = pairing()
    result["expires_at"] = 2001
    with pytest.raises(BridgeError) as caught:
        pairing_qr(result, str(tmp_path / "pair.png"), 1000)
    assert caught.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize("server", ["http://127.0.0.1:18940", "https://user:secret@bridge.example", "https://bridge.example?token=x"])
def test_qr_requires_safe_public_https(tmp_path, server):
    result = pairing()
    result["qr_payload"]["server"] = server
    with pytest.raises(BridgeError):
        pairing_qr(result, str(tmp_path / "pair.png"), 1000)
    assert not (tmp_path / "pair.png").exists()


def test_invalid_qr_version(tmp_path):
    result = pairing()
    result["qr_payload"]["v"] = True
    with pytest.raises(BridgeError):
        pairing_qr(result, str(tmp_path / "pair.png"), 1000)


def test_existing_file_never_overwritten(tmp_path):
    path = tmp_path / "pair.png"
    path.write_bytes(b"existing")
    with pytest.raises(BridgeError) as caught:
        pairing_qr(pairing(), str(path), 1000)
    assert caught.value.code == "LOCAL_FILE_ERROR"
    assert path.read_bytes() == b"existing"


def test_qr_leaf_symlink_refused(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"original")
    path = tmp_path / "pair.png"
    path.symlink_to(target)
    with pytest.raises(BridgeError):
        pairing_qr(pairing(), str(path), 1000)
    assert target.read_bytes() == b"original"


def test_pair_file_failure_retains_pairing_id(tmp_path, wire):
    wire.client.wall_clock = lambda: 1000
    wire.respond(pairing())
    path = tmp_path / "existing.png"
    path.write_bytes(b"keep")
    with pytest.raises(BridgeError) as caught:
        dispatch(parser().parse_args(["pair", "--name", "Pixel", "--qr", str(path)]), wire.client)
    assert caught.value.details["pairing_id"] == "pair"
    assert caught.value.details["pairing_created"] is True
    assert len(wire.requests) == 1
    assert path.read_bytes() == b"keep"


def test_pair_requires_name_and_png_destination(wire):
    for args in (["pair"], ["pair", "--name", "Pixel"], ["pair", "--qr", "scan.png"], ["pair", "--code"]):
        with pytest.raises(BridgeError):
            dispatch(parser().parse_args(args), wire.client)
    assert not wire.requests



@pytest.mark.parametrize("token", ["p" * 42, "p" * 44, "+" * 43, "é" * 43, " p" * 22])
def test_invalid_credential_format_refused_without_normalization(tmp_path, token):
    response = pairing()
    response["qr_payload"]["token"] = token
    with pytest.raises(BridgeError) as caught:
        pairing_qr(response, str(tmp_path / "pair.png"), 1000)
    assert caught.value.code == "INVALID_RESPONSE"
    assert response["qr_payload"]["token"] == token
    assert not (tmp_path / "pair.png").exists()


def test_oversized_payload_refused_with_pairing_id(tmp_path, wire):
    response = pairing()
    response["qr_payload"]["server"] = "https://bridge.example/" + "a" * 3000
    wire.client.wall_clock = lambda: 1000
    wire.respond(response)
    with pytest.raises(BridgeError) as caught:
        dispatch(parser().parse_args(["pair", "--name", "Pixel", "--qr", str(tmp_path / "pair.png")]), wire.client)
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.details["pairing_id"] == "pair"
    assert not (tmp_path / "pair.png").exists()


def decode_code(code):
    import base64

    assert code.startswith("pb1:")
    body = code[4:]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_pair_code_only_prints_copyable_code(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    response = pairing(int(time.time()) + 300)
    wire.respond(response)
    assert main(["pair", "--name", "Pixel", "--code"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"id", "expires_at", "status", "pairing_code"}
    assert "qr_path" not in output
    assert decode_code(output["pairing_code"]) == response["qr_payload"]
    assert all(char.isalnum() or char in "-_:" for char in output["pairing_code"])


def test_pair_code_and_qr_carry_the_same_pairing(tmp_path, wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    response = pairing(int(time.time()) + 300)
    wire.respond(response)
    path = tmp_path / "scan.png"
    assert main(["pair", "--name", "Pixel", "--qr", str(path), "--code"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["qr_path"] == str(path)
    decoded = zxingcpp.read_barcode(Image.open(path).convert("RGB"))
    assert decode_code(output["pairing_code"]) == json.loads(decoded.text)
    assert len(wire.requests) == 1


def test_expired_pairing_never_yields_a_code(wire):
    wire.client.wall_clock = lambda: 3000
    wire.respond(pairing(2000))
    with pytest.raises(BridgeError) as caught:
        dispatch(parser().parse_args(["pair", "--name", "Pixel", "--code"]), wire.client)
    assert caught.value.code == "PAIRING_EXPIRED"
    assert caught.value.details["pairing_id"] == "pair"
