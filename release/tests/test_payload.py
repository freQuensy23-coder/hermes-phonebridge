import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from release.payload import INPUT_NAMES, PUBLIC_NAMES, SIGNED, UNSIGNED, WHEELS, canonical, certificate_fingerprint, expected_certificate, extract_contract, file_record, read_json, verify_input, verify_public, verify_wheel
from release.prepare import prepare


@pytest.fixture(autouse=True)
def preserve_process_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


def wheel_bytes(project, version="0.1.0"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(project + "-0.1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: " + project.replace("_", "-") + "\nVersion: " + version + "\n")
        archive.writestr(project + "/__init__.py", b"raise RuntimeError('artifact code must never run')")
    return buffer.getvalue()


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "input"
    directory.mkdir()
    binding = {"repository": "freQuensy23-coder/hermes-phonebridge", "source_sha": "a" * 40, "ci_run_id": 123, "ci_run_attempt": 2}
    for name in WHEELS:
        (directory / name).write_bytes(wheel_bytes(name.split("-", 1)[0]))
    (directory / UNSIGNED).write_bytes(b"unsigned APK fixture")
    manifest = {"schema": 1, "version": "0.1.0", "repository": binding["repository"], "source_sha": binding["source_sha"], "run_id": 123, "run_attempt": 2, "files": {name: file_record(directory / name) for name in (*WHEELS, UNSIGNED)}, "dependency_inputs": {"server/pyproject.toml": "c" * 64}, "toolchain": {"python": "3.11.13", "runner_os": "Linux", "runner_arch": "X64", "java_home_17": "/hosted/jdk17", "android_home": "/hosted/android"}}
    (directory / "manifest.json").write_bytes(canonical(manifest))
    return directory, binding, manifest


def test_exact_input_manifest_and_hashes(bundle):
    directory, binding, manifest = bundle
    assert verify_input(directory, binding) == manifest


@pytest.mark.parametrize("field,value", [("run_attempt", 1), ("run_id", 999), ("source_sha", "b" * 40), ("repository", "other/repo"), ("version", "0.2.0"), ("schema", True)])
def test_input_provenance_mismatch(bundle, field, value):
    directory, binding, manifest = bundle
    manifest[field] = value
    (directory / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(ValueError):
        verify_input(directory, binding)


def test_tampered_binary_hash(bundle):
    directory, binding, _ = bundle
    (directory / WHEELS[0]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        verify_input(directory, binding)


def test_duplicate_manifest_json_keys():
    with pytest.raises(ValueError, match="Duplicate"):
        read_json(b'{"schema":1,"schema":2}')


def test_manifest_size_bound():
    with pytest.raises(ValueError, match="Oversized"):
        read_json(b" " * 65537)


def test_wheel_version_matches_tag(tmp_path):
    path = tmp_path / WHEELS[0]
    path.write_bytes(wheel_bytes("phonebridge_cli", version="0.2.0"))
    with pytest.raises(ValueError, match="version"):
        verify_wheel(path)


def test_extract_regular_contract_in_fresh_directory(bundle, tmp_path):
    source, binding, manifest = bundle
    data = zip_bytes([(name, (source / name).read_bytes()) for name in sorted(INPUT_NAMES)])
    destination = extract_contract(data, tmp_path / "unpacked", INPUT_NAMES)
    assert verify_input(destination, binding) == manifest
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())


@pytest.mark.parametrize("bad_name", ["../manifest.json", "/manifest.json", "nested/manifest.json", "nested\\manifest.json", "manifest.json/", "C:manifest.json"])
def test_archive_path_injection_rejected(bundle, tmp_path, bad_name):
    source, _, _ = bundle
    files = [(bad_name if name == "manifest.json" else name, (source / name).read_bytes()) for name in sorted(INPUT_NAMES)]
    with pytest.raises(ValueError):
        extract_contract(zip_bytes(files), tmp_path / "unpacked", INPUT_NAMES)
    assert not (tmp_path / "unpacked").exists()


def test_archive_duplicate_members_rejected(bundle, tmp_path):
    source, _, _ = bundle
    files = [(name, (source / name).read_bytes()) for name in sorted(INPUT_NAMES)]
    files.append(("manifest.json", b"{}"))
    with pytest.warns(UserWarning), pytest.raises(ValueError):
        extract_contract(zip_bytes(files), tmp_path / "unpacked", INPUT_NAMES)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO, stat.S_IFDIR])
def test_archive_links_and_special_members_rejected(bundle, tmp_path, mode):
    source, _, _ = bundle
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name in sorted(INPUT_NAMES):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (mode | 0o600) << 16 if name == "manifest.json" else (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, (source / name).read_bytes())
    with pytest.raises(ValueError, match="nonregular"):
        extract_contract(data.getvalue(), tmp_path / "unpacked", INPUT_NAMES)


def test_archive_expansion_bomb_rejected(tmp_path):
    data = zip_bytes([("bomb", b"A" * 4 * 1024 * 1024)])
    with pytest.raises(ValueError, match="Expansion"):
        extract_contract(data, tmp_path / "unpacked", {"bomb"})


def test_archive_metadata_bound_rejected(tmp_path):
    data = zip_bytes([("manifest.json", bytes(range(256)) * 300)])
    with pytest.raises(ValueError, match="Metadata"):
        extract_contract(data, tmp_path / "unpacked", {"manifest.json"})


def test_existing_or_symlink_destination_rejected(tmp_path):
    directory = tmp_path / "existing"
    directory.mkdir()
    with pytest.raises(ValueError, match="fresh"):
        extract_contract(zip_bytes([("one", b"x")]), directory, {"one"})
    link = tmp_path / "link"
    link.symlink_to(directory)
    with pytest.raises(ValueError, match="fresh"):
        extract_contract(zip_bytes([("one", b"x")]), link, {"one"})


def test_payload_symlink_rejected(tmp_path):
    original = tmp_path / "real"
    original.write_bytes(b"data")
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(ValueError, match="Nonregular"):
        file_record(link)


@pytest.mark.parametrize("value", ["", "a" * 63, "A" * 64, "aa:" * 32, "a" * 64 + "\n"])
def test_expected_certificate_literal_format(value):
    with pytest.raises(ValueError):
        expected_certificate(value)


def test_single_stable_signer_certificate():
    digest = "a" * 64
    assert expected_certificate(digest) == digest
    assert certificate_fingerprint("Signer #1 certificate SHA-256 digest: " + digest + "\n") == digest


def test_multiple_or_missing_signers_rejected():
    with pytest.raises(ValueError):
        certificate_fingerprint("certificate digest absent")
    with pytest.raises(ValueError):
        certificate_fingerprint("Signer #1 certificate SHA-256 digest: " + "a" * 64 + "\nSigner #2 certificate SHA-256 digest: " + "b" * 64)


@pytest.fixture
def public_bundle(bundle, tmp_path):
    input_directory, binding, manifest = bundle
    public = tmp_path / "public"
    public.mkdir()
    for name in WHEELS:
        (public / name).write_bytes((input_directory / name).read_bytes())
    (public / SIGNED).write_bytes(b"signed APK fixture")
    release = {"workflow_path": ".github/workflows/release.yml", "workflow_sha": "d" * 40, "run_id": 777, "run_attempt": 1}
    provenance = {"schema": 1, "version": "0.1.0", "binding": binding, "ci_input": manifest, "signer_sha256": "a" * 64, "release": release, "files": {name: file_record(public / name) for name in (*WHEELS, SIGNED)}, "wheel_signatures": "not-implemented"}
    (public / "provenance.json").write_bytes(canonical(provenance))
    (public / "SHA256SUMS").write_text("".join(file_record(public / name)["sha256"] + "  " + name + "\n" for name in sorted({*WHEELS, SIGNED, "provenance.json"})))
    return public, binding, manifest, release, provenance


def test_published_manifest_binds_wheels_signer_and_checksums(public_bundle):
    directory, binding, manifest, release, provenance = public_bundle
    assert {path.name for path in directory.iterdir()} == PUBLIC_NAMES
    assert verify_public(directory, binding, manifest, "a" * 64, release) == provenance


@pytest.mark.parametrize("field,value", [("signer_sha256", "b" * 64), ("wheel_signatures", "signed"), ("binding", {}), ("release", {})])
def test_published_identity_tampering(public_bundle, field, value):
    directory, binding, manifest, release, provenance = public_bundle
    provenance[field] = value
    (directory / "provenance.json").write_bytes(canonical(provenance))
    with pytest.raises(ValueError):
        verify_public(directory, binding, manifest, "a" * 64, release)


def test_wheel_replacement_even_with_new_checksum_rejected(public_bundle):
    directory, binding, manifest, release, provenance = public_bundle
    (directory / WHEELS[0]).write_bytes(b"different wheel")
    provenance["files"][WHEELS[0]] = file_record(directory / WHEELS[0])
    (directory / "provenance.json").write_bytes(canonical(provenance))
    with pytest.raises(ValueError, match="accepted CI"):
        verify_public(directory, binding, manifest, "a" * 64, release)


def test_checksum_manifest_tampering(public_bundle):
    directory, binding, manifest, release, _ = public_bundle
    (directory / "SHA256SUMS").write_text("wrong checksums")
    with pytest.raises(ValueError, match="Checksum"):
        verify_public(directory, binding, manifest, "a" * 64, release)


def test_prepare_exact_ci_outputs_without_building(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in WHEELS:
        (dist / name).write_bytes(wheel_bytes(name.split("-", 1)[0]))
    for project in ("phonebridge_cli", "phonebridge_server"):
        (dist / (project + "-0.1.0.tar.gz")).write_bytes(b"sdist fixture")
    apk = tmp_path / "app-release-unsigned.apk"
    apk.write_bytes(b"unsigned APK fixture")
    lock = Path("pyproject.toml")
    lock.write_text("dependency input")
    context = {"GITHUB_REPOSITORY": "freQuensy23-coder/hermes-phonebridge", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64", "JAVA_HOME_17_X64": "/jdk17", "ANDROID_HOME": "/android"}
    output = tmp_path / "output"
    manifest = prepare(dist, apk, output, ["pyproject.toml"], context)
    assert {path.name for path in output.iterdir()} == INPUT_NAMES
    assert manifest["files"][WHEELS[0]]["sha256"] == hashlib.sha256((dist / WHEELS[0]).read_bytes()).hexdigest()
    assert json.loads((output / "manifest.json").read_bytes()) == manifest
    assert verify_input(output, {"repository": context["GITHUB_REPOSITORY"], "source_sha": context["GITHUB_SHA"], "ci_run_id": 123, "ci_run_attempt": 2}) == manifest


@pytest.mark.parametrize("failure", ["certificate", "signing-tool"])
def test_signing_failure_cleans_key_and_cannot_upload_bundle(bundle, tmp_path, monkeypatch, failure):
    import base64
    from types import SimpleNamespace

    from release import runtime

    directory, binding, manifest = bundle
    aligned = tmp_path / "aligned.apk"
    aligned.write_bytes(b"aligned APK fixture")
    state = {"binding": binding, "directory": str(directory), "aligned": file_record(aligned), "manifest": manifest}
    (tmp_path / "sign-state.json").write_bytes(canonical(state))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("EXPECTED_CERTIFICATE", "a" * 64)
    monkeypatch.setenv("SIGNING_KEY", base64.b64encode(b"fake nonsecret keystore").decode())
    monkeypatch.setenv("SIGNING_PASSWORD", "fake-nonsecret-password")
    monkeypatch.setattr(runtime, "api_client", lambda: object())
    monkeypatch.setattr(runtime, "current_binding", lambda api: binding)
    monkeypatch.setattr(runtime, "android_tools", lambda: (tmp_path / "tools", {"JAVA_HOME": "/jdk17", "PATH": "/jdk17/bin:/usr/bin:/bin"}))

    def fake_tool(arguments, environment, allowed_failure=False):
        assert "GH_TOKEN" not in environment
        assert "SIGNING_KEY" not in environment
        if "sign" in arguments:
            key = Path(arguments[arguments.index("--ks") + 1])
            assert key.read_bytes() == b"fake nonsecret keystore"
            assert key.stat().st_mode & 0o777 == 0o600
            assert environment["SIGNING_PASSWORD"] == "fake-nonsecret-password"
            if failure == "signing-tool":
                raise RuntimeError("simulated signer failure")
            Path(arguments[arguments.index("--out") + 1]).write_bytes(b"signed APK fixture")
        output = "Signer #1 certificate SHA-256 digest: " + "b" * 64 + "\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(runtime, "tool", fake_tool)
    with pytest.raises((ValueError, RuntimeError)):
        runtime.sign()
    assert not list(tmp_path.glob("keystore-*.jks"))
    assert not (tmp_path / "signed/provenance.json").exists()


def test_signing_gate_rejects_before_materializing_credentials(tmp_path, monkeypatch):
    from release import runtime

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(runtime, "api_client", lambda: object())

    def reject(api):
        raise ValueError("tag moved before signing")

    monkeypatch.setattr(runtime, "current_binding", reject)
    with pytest.raises(ValueError, match="tag moved"):
        runtime.sign()
    assert not list(tmp_path.iterdir())


def test_existing_draft_release_is_never_overwritten():
    from release.runtime import release_absent

    class ExistingDraft:
        def pages(self, path):
            assert path == "releases"
            return [{"id": 1, "tag_name": "v0.1.0", "draft": True}]

    with pytest.raises(ValueError, match="draft"):
        release_absent(ExistingDraft(), "v0.1.0")


def test_publisher_gate_rejects_before_any_write(monkeypatch):
    from release import runtime

    class NoWrites:
        def request(self, *args, **kwargs):
            raise AssertionError("No write is allowed before authorization")

    monkeypatch.setattr(runtime, "api_client", NoWrites)

    def reject(api):
        raise ValueError("source not accepted")

    monkeypatch.setattr(runtime, "current_binding", reject)
    with pytest.raises(ValueError, match="not accepted"):
        runtime.publish()


def test_remote_release_assets_are_read_back_and_hashed(public_bundle):
    from release.runtime import verify_remote_release

    directory, binding, _, _, _ = public_bundle
    binding.update(tag="v0.1.0")
    assets = [{"id": index + 1, "name": name, "state": "uploaded", "size": (directory / name).stat().st_size} for index, name in enumerate(sorted(PUBLIC_NAMES))]

    class Remote:
        def get(self, path):
            assert path == "releases/44"
            return {"id": 44, "tag_name": "v0.1.0", "target_commitish": binding["source_sha"], "draft": False, "prerelease": False, "author": {"login": "github-actions[bot]"}, "html_url": "https://example.invalid/fixture"}

        def pages(self, path):
            assert path == "releases/44/assets"
            return assets

        def download(self, path, limit):
            asset = next(value for value in assets if path == "releases/assets/" + str(value["id"]))
            return (directory / asset["name"]).read_bytes()

    assert verify_remote_release(Remote(), 44, directory, binding, False)["id"] == 44

    class Corrupt(Remote):
        def download(self, path, limit):
            return b"wrong asset bytes"

    with pytest.raises(ValueError, match="hash"):
        verify_remote_release(Corrupt(), 44, directory, binding, False)
