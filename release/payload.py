import hashlib
import io
import json
import re
import stat
import zipfile
from email.parser import BytesParser
from pathlib import Path

WHEELS = ("phonebridge_cli-0.1.0-py3-none-any.whl", "phonebridge_server-0.1.0-py3-none-any.whl")
UNSIGNED = "phonebridge-android-unsigned.apk"
SIGNED = "phonebridge-android.apk"
INPUT_NAMES = {"manifest.json", UNSIGNED, *WHEELS}
PUBLIC_NAMES = {"provenance.json", "SHA256SUMS", SIGNED, *WHEELS}
MAX_FILE = 96 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024


def payload_require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        payload_require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def read_json(data):
    payload_require(len(data) <= 64 * 1024, "Oversized manifest")
    return json.loads(data, object_pairs_hook=unique_object)


def canonical(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def extract_contract(data, destination, expected):
    destination = Path(destination)
    payload_require(destination.is_absolute() and not destination.exists(), "Extraction requires fresh absolute destination")
    payload_require(len(data) <= MAX_TOTAL, "Oversized archive")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        payload_require(len(entries) == len(expected) and len(names) == len(set(names)) and set(names) == expected, "Unexpected or duplicate archive members")
        total = 0
        for entry in entries:
            payload_require(entry.filename == Path(entry.filename).name and not entry.is_dir(), "Paths/directories forbidden")
            mode = entry.external_attr >> 16
            payload_require(stat.S_IFMT(mode) in (0, stat.S_IFREG), "Links or nonregular archive members forbidden")
            payload_require(not entry.flag_bits & 1 and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "Unsupported ZIP encoding")
            payload_require(0 < entry.file_size <= MAX_FILE and entry.file_size <= max(1, entry.compress_size) * 1000, "Expansion/file bound exceeded")
            if entry.filename.endswith(".json") or entry.filename == "SHA256SUMS":
                payload_require(entry.file_size <= 64 * 1024, "Metadata bound exceeded")
            if entry.filename.endswith(".whl"):
                payload_require(entry.file_size <= 16 * 1024 * 1024, "Wheel bound exceeded")
            total += entry.file_size
        payload_require(total <= MAX_TOTAL, "Archive expansion bound exceeded")
        destination.mkdir(mode=0o700)
        for entry in entries:
            target = destination / entry.filename
            payload_require(target.resolve().parent == destination.resolve(), "Extraction escaped destination")
            with archive.open(entry) as source, target.open("xb") as output:
                written = 0
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    payload_require(written <= entry.file_size, "Unexpected ZIP expansion")
                    output.write(chunk)
            payload_require(written == entry.file_size, "ZIP size mismatch")
            target.chmod(0o600)
    return destination


def file_record(path):
    path = Path(path)
    payload_require(not path.is_symlink() and path.is_file(), "Nonregular payload")
    size = path.stat().st_size
    payload_require(0 < size <= MAX_FILE, "Payload size bound exceeded")
    return {"size": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_records(directory, records, names):
    payload_require(isinstance(records, dict) and set(records) == set(names), "Manifest file contract mismatch")
    for name in names:
        record = records[name]
        payload_require(set(record) == {"size", "sha256"} and type(record["size"]) is int, "Invalid file record")
        payload_require(re.fullmatch("[0-9a-f]{64}", record["sha256"]) is not None, "Invalid payload hash")
        payload_require(record == file_record(Path(directory) / name), "Payload hash/size mismatch")


def verify_input(directory, binding):
    manifest = read_json((Path(directory) / "manifest.json").read_bytes())
    payload_require(set(manifest) == {"schema", "version", "repository", "source_sha", "run_id", "run_attempt", "files", "dependency_inputs", "toolchain"}, "Input manifest fields mismatch")
    payload_require(type(manifest["schema"]) is int and manifest["schema"] == 1 and manifest["version"] == "0.1.0", "Input manifest version mismatch")
    for field, expected in (("repository", binding["repository"]), ("source_sha", binding["source_sha"]), ("run_id", binding["ci_run_id"]), ("run_attempt", binding["ci_run_attempt"])):
        payload_require(manifest[field] == expected and type(manifest[field]) is type(expected), "Input manifest provenance mismatch: " + field)
    verify_records(directory, manifest["files"], {UNSIGNED, *WHEELS})
    locks = manifest["dependency_inputs"]
    payload_require(isinstance(locks, dict) and 1 <= len(locks) <= 20, "Missing/bounded dependency inputs")
    for name, digest in locks.items():
        payload_require(isinstance(name, str) and re.fullmatch("[a-zA-Z0-9_./-]{1,200}", name) is not None and not name.startswith("/") and ".." not in name.split("/"), "Invalid dependency input name")
        payload_require(re.fullmatch("[0-9a-f]{64}", digest) is not None, "Invalid dependency input digest")
    payload_require(isinstance(manifest["toolchain"], dict) and set(manifest["toolchain"]) == {"python", "runner_os", "runner_arch", "java_home_17", "android_home"}, "Toolchain fields mismatch")
    payload_require(all(isinstance(value, str) and 0 < len(value) <= 500 for value in manifest["toolchain"].values()), "Toolchain metadata invalid")
    for name in WHEELS:
        verify_wheel(Path(directory) / name)
    return manifest


def verify_wheel(path):
    project = Path(path).name.split("-", 1)[0]
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        payload_require(len(entries) <= 2000 and len(names) == len(set(names)), "Wheel duplicate/entry bound")
        payload_require(sum(entry.file_size for entry in entries) <= 64 * 1024 * 1024, "Wheel expansion bound")
        metadata_name = project + "-0.1.0.dist-info/METADATA"
        metadata = [entry for entry in entries if entry.filename.endswith(".dist-info/METADATA")]
        payload_require(len(metadata) == 1 and metadata[0].filename == metadata_name and metadata[0].file_size <= 64 * 1024, "Wheel metadata identity/bound")
        message = BytesParser().parsebytes(archive.read(metadata[0]))
        payload_require(message.get_all("Version") == ["0.1.0"] and message.get_all("Name") == [project.replace("_", "-")], "Wheel package/version does not match tag")


def certificate_fingerprint(output):
    signers = re.findall(r"^Signer #([0-9]+) certificate SHA-256 digest: ([0-9a-fA-F]{64})$", output, re.MULTILINE)
    payload_require(len(signers) == 1 and signers[0][0] == "1", "Require exactly one APK signer")
    return signers[0][1].lower()


def expected_certificate(value):
    payload_require(re.fullmatch("[0-9a-f]{64}", value or "") is not None, "PHONEBRIDGE_SIGNER_SHA256 must be 64 lowercase hex digits")
    return value


def verify_public(directory, binding, manifest, fingerprint, release_identity):
    directory = Path(directory)
    provenance = read_json((directory / "provenance.json").read_bytes())
    payload_require(set(provenance) == {"schema", "version", "binding", "ci_input", "signer_sha256", "release", "files", "wheel_signatures"}, "Public provenance fields mismatch")
    payload_require(type(provenance["schema"]) is int and provenance["schema"] == 1 and provenance["version"] == "0.1.0", "Public provenance version mismatch")
    payload_require(provenance["binding"] == binding and provenance["ci_input"] == manifest, "Published CI/source provenance mismatch")
    payload_require(provenance["signer_sha256"] == fingerprint and provenance["release"] == release_identity, "Published signer/release identity mismatch")
    payload_require(provenance["wheel_signatures"] == "not-implemented", "Unsubstantiated wheel signature claim")
    verify_records(directory, provenance["files"], {SIGNED, *WHEELS})
    for name in WHEELS:
        payload_require(provenance["files"][name] == manifest["files"][name], "Wheel differs from accepted CI output")
    names = sorted({SIGNED, *WHEELS, "provenance.json"})
    sums = "".join(file_record(directory / name)["sha256"] + "  " + name + "\n" for name in names).encode()
    payload_require((directory / "SHA256SUMS").read_bytes() == sums, "Checksum list mismatch")
    return provenance
