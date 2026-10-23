import base64
from email.message import Message
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

SHA = "a" * 40


def wheel(directory, name, version="0.1.0", tag="py3-none-any", description=""):
    filename = directory / f"{name}-{version}-{tag}.whl"
    info = f"{name}-{version}.dist-info"
    files = {f"{name}/__init__.py": f"VERSION = '{version}'\n",
             f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name.replace('_', '-')}\nVersion: {version}\nRequires-Python: >=3.11\n\n{description}",
             f"{info}/WHEEL": f"Wheel-Version: 1.0\nGenerator: phonebridge-ci-tests\nRoot-Is-Purelib: true\nTag: {tag}\n\n"}
    files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*files, f"{info}/RECORD"])
    with zipfile.ZipFile(filename, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return filename


@pytest.fixture
def bundle(modules, tmp_path, monkeypatch):
    packager = modules["package_artifact"]
    rollout = modules["rollout"]
    wheelhouse = tmp_path / "input-wheels"
    wheelhouse.mkdir()
    wheel(wheelhouse, "phonebridge_server")
    wheel(wheelhouse, "phonebridge_cli")
    environment = {"GITHUB_SHA": SHA, "GITHUB_REPOSITORY": packager.REPOSITORY,
                   "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push",
                   "GITHUB_WORKFLOW_REF": f"{packager.REPOSITORY}/{packager.WORKFLOW}@refs/heads/main",
                   "GITHUB_REPOSITORY_ID": "3", "PHONEBRIDGE_CI_WORKFLOW_ID": "4",
                   "GITHUB_RUN_ID": "10", "GITHUB_RUN_ATTEMPT": "2"}
    source = Path(__file__).parents[1]
    output = tmp_path / "artifact"
    manifest = packager.package(wheelhouse, output, source, environment)
    archive = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                zipped.write(path, str(path.relative_to(output)))
    provenance = {"source_sha": SHA, "repository_id": 3, "workflow_id": 4, "run_id": 10,
                  "run_attempt": 2, "artifact_id": 20, "artifact_name": "phonebridge-deployment-" + SHA,
                  "artifact_digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()}

    def github(path):
        name = path.split("/deploy/", 1)[1].split("?", 1)[0]
        data = (source / name).read_bytes()
        return {"type": "file", "encoding": "base64", "size": len(data), "content": base64.b64encode(data).decode()}

    monkeypatch.setattr(rollout, "github", github)
    return rollout, packager, output, archive, provenance, manifest, environment, wheelhouse


def test_real_zip_and_manifest_bind_wheels_source_and_producer(bundle, tmp_path):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    rollout.extract_archive(archive, extracted)
    assert rollout.validate_manifest(extracted, provenance) == manifest
    assert manifest["producer"]["run_attempt"] == 2
    assert all("-py3-none-any.whl" in name for name in manifest["wheels"])
    assert packager.wheel_identity(next(wheelhouse.iterdir()))[1] == "0.1.0"


@pytest.mark.parametrize("field,value", [("GITHUB_REF", "refs/tags/v0.1.0"), ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_WORKFLOW_REF", "wrong/path@refs/heads/main")])
def test_packager_cannot_produce_tag_or_other_workflow_artifacts(bundle, tmp_path, field, value):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    environment[field] = value
    with pytest.raises(RuntimeError, match="exact push/main"):
        packager.package(wheelhouse, tmp_path / "rejected", Path(__file__).parents[1], environment)


def test_wheel_with_long_description_is_accepted_but_unbounded_metadata_is_not(modules, tmp_path):
    packager = modules["package_artifact"]
    long_description = wheel(tmp_path, "pydantic", "2.14.1", description="changelog\n" * 13000)
    assert packager.wheel_identity(long_description) == ("pydantic", "2.14.1")
    oversized = wheel(tmp_path, "huge", "1.0", description="x" * (packager.MAX_WHEEL_METADATA + 1))
    with pytest.raises(RuntimeError, match="Invalid wheel metadata"):
        packager.wheel_identity(oversized)


def test_no_sdist_or_git_install_inputs(bundle, tmp_path):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    (wheelhouse / "package.tar.gz").write_bytes(b"source")
    with pytest.raises(RuntimeError, match="regular wheel"):
        packager.package(wheelhouse, tmp_path / "rejected", Path(__file__).parents[1], environment)


@pytest.mark.parametrize("violation", ["sha", "version", "attempt", "run", "repository", "wheelhash", "sourcehash", "extra"])
def test_tampering_fails_before_install(bundle, violation):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    if violation == "sha":
        manifest["source_sha"] = "b" * 40
    elif violation == "version":
        manifest["version"] = "0.2.0"
    elif violation in ("attempt", "run", "repository"):
        key = {"attempt": "run_attempt", "run": "run_id", "repository": "repository_id"}[violation]
        manifest["producer"][key] += 1
    elif violation == "wheelhash":
        (output / next(iter(manifest["wheels"]))).write_bytes(b"corrupted wheel")
    elif violation == "sourcehash":
        manifest["trust"]["rollout.py"] = "0" * 64
    elif violation == "extra":
        (output / "unlisted.whl").write_bytes(b"extra")
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):
        rollout.validate_manifest(output, provenance)


@pytest.mark.parametrize("name,mode", [("../escape", stat.S_IFREG), ("/absolute", stat.S_IFREG), ("wheelhouse/../escape.whl", stat.S_IFREG), ("wheelhouse/link.whl", stat.S_IFLNK), ("wheelhouse/fifo.whl", stat.S_IFIFO), ("wheelhouse/", stat.S_IFDIR), ("server/setup.py", stat.S_IFREG), ("wheelhouse\\escape.whl", stat.S_IFREG)])
def test_controlled_zip_rejects_unsafe_entries(modules, tmp_path, name, mode):
    rollout = modules["rollout"]
    archive = tmp_path / "malicious.zip"
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = (mode | 0o600) << 16
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(entry, b"unsafe")
    destination = tmp_path / "root"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="ZIP"):
        rollout.extract_archive(archive, destination)
    assert not list(destination.iterdir())


def test_duplicate_zip_paths_fail_before_extraction(modules, tmp_path):
    rollout = modules["rollout"]
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("manifest.json", b"first")
        with pytest.warns(UserWarning):
            zipped.writestr("manifest.json", b"second")
    destination = tmp_path / "root"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="ZIP"):
        rollout.extract_archive(archive, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("limit", ["MAX_FILE", "MAX_EXPANDED", "MAX_ARCHIVE", "MAX_ENTRIES"])
def test_archive_bounds_enforced(modules, tmp_path, monkeypatch, limit):
    rollout = modules["rollout"]
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("manifest.json", b"x" * 20)
        zipped.writestr("wheelhouse/package-1-py3-none-any.whl", b"x" * 20)
    destination = tmp_path / "root"
    destination.mkdir()
    monkeypatch.setattr(rollout, limit, 1 if limit == "MAX_ENTRIES" else 10)
    with pytest.raises(RuntimeError, match="limit|bounded"):
        rollout.extract_archive(archive, destination)
    assert not list(destination.iterdir())


def test_expansion_bomb_rejected(modules, tmp_path):
    rollout = modules["rollout"]
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("manifest.json", b"x" * 1000000)
    destination = tmp_path / "root"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="expansion"):
        rollout.extract_archive(archive, destination)


def test_real_offline_pip_prepares_unique_same_sha_candidates(bundle, tmp_path, monkeypatch):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    base = tmp_path / "runtime"
    base.mkdir()
    monkeypatch.setattr(rollout, "BASE", base)
    monkeypatch.setattr(rollout, "DEADLINE", None)
    first = rollout.prepare_release(output, manifest, provenance)
    rollout.switch(first)
    before = (first / "release.json").read_bytes()
    second = rollout.prepare_release(output, manifest, provenance)
    assert first != second
    assert (base / "current").resolve() == first
    assert (first / "release.json").read_bytes() == before
    result = subprocess.run([str(second / ".venv/bin/python"), "-I", "-c", "import phonebridge_server, phonebridge_cli; assert phonebridge_server.VERSION == phonebridge_cli.VERSION == '0.1.0'"], check=True, timeout=10, capture_output=True)
    assert result.returncode == 0
    assert "--hash=sha256:" in (second / "requirements.txt").read_text()
    assert rollout.release_metadata(first)["source_sha"] == SHA


def test_failed_real_preparation_removes_only_candidate(bundle, tmp_path, monkeypatch):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    base = tmp_path / "runtime"
    base.mkdir()
    monkeypatch.setattr(rollout, "BASE", base)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("venv failed")

    monkeypatch.setattr(rollout, "run", run)
    with pytest.raises(RuntimeError, match="venv failed"):
        rollout.prepare_release(output, manifest, provenance)
    assert not list((base / "releases").iterdir())
    assert len(calls) == 1


def test_outdated_installed_trust_fails_clearly(bundle, tmp_path, monkeypatch):
    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    monkeypatch.setattr(rollout, "BASE", tmp_path)
    (tmp_path / "trust.json").write_text(json.dumps({"format": 1, "trust": {}}))
    with pytest.raises(RuntimeError, match="Outdated installed trust"):
        rollout.validate_installed_trust(manifest)


def test_health_exact_version_body_size_and_response_identity(modules, monkeypatch):
    rollout = modules["rollout"]
    headers = Message()
    headers["Content-Type"] = "application/json"

    class Response:
        status = 200

        def __init__(self, body, url="http://127.0.0.1:18940/healthz"):
            self.body = body
            self.url = url
            self.headers = headers

        def read(self, limit):
            return self.body[:limit]

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Client:
        response = None

        def open(self, request, timeout):
            return self.response

    client = Client()
    monkeypatch.setattr(rollout, "opener", lambda: client)
    client.response = Response(json.dumps({"status": "ok", "version": "0.1.0", "revision": SHA}).encode())
    assert rollout.healthy(SHA, timeout=0.01)["version"] == "0.1.0"
    for body, url in [(b"x" * 4097, "http://127.0.0.1:18940/healthz"), (json.dumps({"status": "ok", "version": "wrong", "revision": SHA}).encode(), "http://127.0.0.1:18940/healthz"), (client.response.body, "https://other.example/healthz")]:
        client.response = Response(body, url)
        with pytest.raises(RuntimeError, match="health"):
            rollout.healthy(SHA, timeout=0.001)



def test_download_binds_api_artifact_id_and_does_not_forward_auth_to_storage(bundle, tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    import urllib.error

    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle
    requests = []
    payload = archive.read_bytes()

    class Response(io.BytesIO):
        status = 200

    class Client:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                headers = Message()
                headers["Location"] = "https://storage.example/signed-artifact"
                raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, io.BytesIO())
            return Response(payload)

    monkeypatch.setattr(rollout, "run", lambda *args, **kwargs: SimpleNamespace(stdout="fixture-token"))
    monkeypatch.setattr(rollout, "opener", lambda: Client())
    target = tmp_path / "download.zip"
    rollout.download_artifact(provenance, target)
    assert requests[0].full_url.endswith("/actions/artifacts/20/zip")
    assert requests[0].get_header("Authorization") == "Bearer fixture-token"
    assert requests[1] == "https://storage.example/signed-artifact"
    assert target.read_bytes() == payload


def test_download_digest_and_byte_limit_fail(bundle, tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    rollout, packager, output, archive, provenance, manifest, environment, wheelhouse = bundle

    class Response(io.BytesIO):
        status = 200

    class Client:
        def open(self, request, timeout):
            return Response(b"wrong artifact content")

    monkeypatch.setattr(rollout, "run", lambda *args, **kwargs: SimpleNamespace(stdout="fixture-token"))
    monkeypatch.setattr(rollout, "opener", lambda: Client())
    with pytest.raises(RuntimeError, match="digest"):
        rollout.download_artifact(provenance, tmp_path / "digest.zip")
    monkeypatch.setattr(rollout, "MAX_ARCHIVE", 1)
    with pytest.raises(RuntimeError, match="limit"):
        rollout.download_artifact(provenance, tmp_path / "oversized.zip")
