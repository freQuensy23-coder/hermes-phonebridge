import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PROJECT = "hermes-phonebridge-ci-6147c4"
PROJECT_NUMBER = "606598667955"
SERVICE_ACCOUNT = f"phonebridge-ci@{PROJECT}.iam.gserviceaccount.com"
PROVIDER = f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/phonebridge-github/providers/github-main"
BUCKET = "test-lab-c28yqjkayy2rs-w8f8dai8b9a6s"
REPOSITORY = "freQuensy23-coder/hermes-phonebridge"
TEST_CLASS = "org.nous.phonebridge.PhoneOemInstrumentationTest"
TEST_METHOD = "physicalRoundTripNativeClockCrudAndRevocation"
DEVICE_DIRECTORY = "/sdcard/Android/data/org.nous.phonebridge/files/phone-oem"
FIXTURE_PATH = "/sdcard/Android/data/org.nous.phonebridge/files/phone-oem-fixture.json"
TERMINAL = {"FINISHED", "ERROR", "INVALID", "CANCELLED", "UNSUPPORTED_ENVIRONMENT", "INCOMPATIBLE_ENVIRONMENT", "INCOMPATIBLE_ARCHITECTURE"}
HOSTS = {"cloudbilling.googleapis.com", "cloudresourcemanager.googleapis.com", "firebase.googleapis.com", "serviceusage.googleapis.com", "testing.googleapis.com", "toolresults.googleapis.com", "storage.googleapis.com"}


class Blocked(RuntimeError):
    pass


class DeadlineExceeded(Blocked):
    pass


class ApiError(Blocked):
    def __init__(self, status, service, code):
        self.status = status
        super().__init__(f"{'FREE_QUOTA_EXHAUSTED_OR_RATE_LIMITED' if status == 429 or code == 'RESOURCE_EXHAUSTED' else 'API_BLOCKED'}: {service} HTTP {status} {code}; retries=0, no billing or role fallback")


def require(condition, message):
    if not condition:
        raise Blocked(message)


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.end = clock() + seconds

    def remaining(self):
        value = self.end - self.clock()
        if value <= 0:
            raise DeadlineExceeded("MATRIX_DEADLINE_EXCEEDED")
        return value

    def timeout(self):
        return min(30, self.remaining())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GoogleApi:
    def __init__(self, deadline):
        self.deadline = deadline
        self.opener = urllib.request.build_opener(NoRedirect())
        self.token = None
        self.token_time = 0

    def access_token(self):
        if self.token is None or time.monotonic() - self.token_time > 180:
            process = subprocess.run(["gcloud", "auth", "print-access-token", f"--account={SERVICE_ACCOUNT}", f"--project={PROJECT}", "--quiet"], capture_output=True, text=True, timeout=self.deadline.timeout(), check=False)
            require(process.returncode == 0, "OIDC_SDK_TOKEN_FAILED; no personal ADC fallback")
            self.token = process.stdout.strip()
            require(bool(self.token) and not any(c.isspace() for c in self.token), "OIDC_SDK_TOKEN_INVALID")
            self.token_time = time.monotonic()
        return self.token

    def request(self, method, url, body=None, raw=False, limit=8 * 1024 * 1024, content_type="application/json", authenticated=True):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == "https" and parsed.hostname in HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password, "API_DESTINATION_REFUSED")
        payload = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"User-Agent": "PhoneBridge-GitHub-CI/1.0", "Content-Type": content_type}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token()}"
            if parsed.hostname != "storage.googleapis.com":
                headers["X-Goog-User-Project"] = PROJECT
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.deadline.timeout()) as response:
                chunks = []
                length = 0
                while length <= limit:
                    if response.isclosed():
                        break
                    response.fp.raw._sock.settimeout(self.deadline.timeout())
                    chunk = response.read1(min(65536, limit + 1 - length))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    length += len(chunk)
                data = b"".join(chunks)
        except urllib.error.HTTPError as error:
            data = error.read(16384)
            try:
                code = json.loads(data).get("error", {}).get("status", "UNSPECIFIED")
            except (ValueError, AttributeError):
                code = "UNSPECIFIED"
            raise ApiError(error.code, parsed.hostname, code) from None
        require(len(data) <= limit, "API_RESPONSE_BOUND_EXCEEDED")
        self.deadline.remaining()
        return data if raw else json.loads(data) if data else {}


def ci_identity(env):
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REPOSITORY_ID": "1417002417", "GITHUB_REPOSITORY_OWNER": "freQuensy23-coder", "GITHUB_REPOSITORY_OWNER_ID": "64750224", "GITHUB_ACTOR": "freQuensy23-coder", "GITHUB_TRIGGERING_ACTOR": "freQuensy23-coder", "GITHUB_ACTOR_ID": "64750224", "GITHUB_REF": "refs/heads/main", "GITHUB_REF_TYPE": "branch", "GITHUB_EVENT_NAME": "push", "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/main"}
    require(all(env.get(key) == value for key, value in expected.items()), "OWNER_MAIN_PUSH_CI_ONLY")
    revision = env.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "INVALID_REVISION")
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        require(re.fullmatch(r"[1-9][0-9]{0,19}", env.get(key, "")) is not None, "INVALID_RUN_BINDING")
    event_path = Path(env.get("GITHUB_EVENT_PATH", ""))
    require(event_path.is_file() and event_path.stat().st_size <= 1024 * 1024, "PUSH_EVENT_REQUIRED")
    event = json.loads(event_path.read_text())
    repository = event.get("repository", {})
    require(event.get("after") == revision and event.get("ref") == "refs/heads/main" and event.get("deleted") is False and repository.get("full_name") == REPOSITORY and str(repository.get("id")) == "1417002417" and str(repository.get("owner", {}).get("id")) == "64750224" and str(event.get("sender", {}).get("id")) == "64750224", "PUSH_EVENT_PROVENANCE_MISMATCH")
    return {"repository": REPOSITORY, "revision": revision, "run_id": env["GITHUB_RUN_ID"], "run_attempt": env["GITHUB_RUN_ATTEMPT"], "nonce": secrets.token_hex(16)}


def oidc_credentials(env, provider):
    require(provider == PROVIDER, "PROVIDER_MISMATCH")
    credential_name = env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    require(credential_name and env.get("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE") == credential_name, "ACTION_OIDC_CREDENTIAL_FILE_REQUIRED")
    path = Path(credential_name)
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 65536, "INVALID_OIDC_CREDENTIAL_FILE")
    roots = [Path(env[key]).resolve() for key in ("RUNNER_TEMP", "GITHUB_WORKSPACE") if env.get(key)]
    require(any(path.resolve().is_relative_to(root) for root in roots), "PERSONAL_CREDENTIAL_PATH_REFUSED")
    value = json.loads(path.read_text())
    source = value.get("credential_source", {})
    parsed = urllib.parse.urlsplit(source.get("url", ""))
    impersonation = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{SERVICE_ACCOUNT}:generateAccessToken"
    require(value.get("type") == "external_account" and value.get("audience") == f"//iam.googleapis.com/{provider}" and value.get("subject_token_type") == "urn:ietf:params:oauth:token-type:jwt" and value.get("token_url") == "https://sts.googleapis.com/v1/token" and urllib.parse.unquote(value.get("service_account_impersonation_url", "")) == impersonation, "KEYLESS_SERVICE_ACCOUNT_BINDING_MISMATCH")
    require(parsed.scheme == "https" and parsed.hostname is not None and (parsed.hostname == "actions.githubusercontent.com" or parsed.hostname.endswith(".actions.githubusercontent.com")) and parsed.port in (None, 443) and not parsed.username and not parsed.password and set(source) <= {"url", "headers", "format"} and source.get("format") == {"type": "json", "subject_token_field_name": "value"}, "NON_GITHUB_OIDC_SOURCE_REFUSED")
    audience = urllib.parse.parse_qs(parsed.query).get("audience")
    require(audience == [provider], "GITHUB_OIDC_AUDIENCE_MISMATCH")


def validate_free(billing, settings, quota):
    require(billing.get("name") == f"projects/{PROJECT}/billingInfo" and billing.get("projectId") == PROJECT and billing.get("billingEnabled") is False and billing.get("billingAccountName") == "", "FREE_PLAN_UNVERIFIED_OR_BILLING_ENABLED")
    require(settings.get("name") == f"projects/{PROJECT}/settings" and settings.get("defaultBucket") == BUCKET, "MANAGED_TOOLRESULTS_BUCKET_MISMATCH")
    metric = "testing.googleapis.com/spark_physical_tests"
    require(quota.get("name") == f"projects/{PROJECT_NUMBER}/services/testing.googleapis.com/consumerQuotaMetrics/testing.googleapis.com%2Fspark_physical_tests" and quota.get("metric") == metric, "PHYSICAL_SPARK_QUOTA_PROVENANCE_MISMATCH")
    limits = quota.get("consumerQuotaLimits", [])
    require(len(limits) == 1 and limits[0].get("metric") == metric and limits[0].get("unit") == "1/d/{project}", "PHYSICAL_SPARK_DAILY_QUOTA_UNVERIFIED")
    buckets = limits[0].get("quotaBuckets", [])
    require(len(buckets) == 1 and buckets[0].get("effectiveLimit") == "5" and not buckets[0].get("dimensions"), "PHYSICAL_SPARK_LIMIT_MUST_BE_EXACTLY_5")


def free_guard(api):
    project = api.request("GET", f"https://cloudresourcemanager.googleapis.com/v3/projects/{PROJECT_NUMBER}")
    firebase = api.request("GET", f"https://firebase.googleapis.com/v1beta1/projects/{PROJECT}")
    require(project.get("name") == f"projects/{PROJECT_NUMBER}" and project.get("projectId") == PROJECT and project.get("state") == "ACTIVE" and firebase.get("projectId") == PROJECT and str(firebase.get("projectNumber")) == PROJECT_NUMBER and firebase.get("state") == "ACTIVE", "ISOLATED_PROJECT_PROVENANCE_MISMATCH")
    billing = api.request("GET", f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT}/billingInfo")
    settings = api.request("GET", f"https://toolresults.googleapis.com/toolresults/v1beta3/projects/{PROJECT}/settings")
    quota = api.request("GET", f"https://serviceusage.googleapis.com/v1beta1/projects/{PROJECT_NUMBER}/services/testing.googleapis.com/consumerQuotaMetrics/testing.googleapis.com%2Fspark_physical_tests?view=FULL")
    validate_free(billing, settings, quota)
    return {"billing": billing, "settings": settings, "quota": quota}


def select_devices(response):
    catalog = response.get("androidDeviceCatalog", {})
    versions = {item["id"]: item for item in catalog.get("versions", [])}
    require(len(versions) == len(catalog.get("versions", [])), "DUPLICATE_CATALOG_VERSIONS")
    runtime = catalog.get("runtimeConfiguration", {})
    require("en" in {item.get("id") for item in runtime.get("locales", [])} and "portrait" in {item.get("id") for item in runtime.get("orientations", [])}, "REQUIRED_RUNTIME_DIMENSIONS_UNAVAILABLE")
    def supported_versions(model):
        return [versions[version] for version in (model.get("supportedVersionIds") or []) if version in versions and isinstance(versions[version].get("apiLevel"), int) and versions[version]["apiLevel"] >= 31 and not ({"preview", "deprecated"} & set(versions[version].get("tags") or []))]

    selected = []
    for manufacturer, names in (("samsung", {"galaxy s25 ultra", "samsung galaxy s25 ultra"}), ("google", {"pixel 9", "google pixel 9"})):
        candidates = [model for model in catalog.get("models", []) if model.get("manufacturer", "").casefold() == manufacturer and model.get("name", "").casefold() in names and model.get("form") == "PHYSICAL" and model.get("formFactor") == "PHONE" and not model.get("accessDeniedReasons") and not ({"deprecated", "preview"} & set(model.get("tags") or [])) and supported_versions(model)]
        require(len(candidates) == 1, f"EXACT_PHYSICAL_MODEL_UNAVAILABLE_OR_AMBIGUOUS: {manufacturer} {sorted(names)[0]}")
        model = candidates[0]
        supported = supported_versions(model)
        require(bool(supported), "SUPPORTED_STABLE_ANDROID_VERSION_UNAVAILABLE")
        highest = max(item["apiLevel"] for item in supported)
        best = [item for item in supported if item["apiLevel"] == highest]
        require(len(best) == 1, "LATEST_SUPPORTED_OS_AMBIGUOUS")
        version = best[0]
        require(all(isinstance(model.get(key), str) and model[key] for key in ("id", "codename", "name", "manufacturer")), "CATALOG_IDENTITY_INCOMPLETE")
        selected.append({"device": {"androidModelId": model["id"], "androidVersionId": version["id"], "locale": "en", "orientation": "portrait"}, "model": model, "version": version})
    require(len({item["device"]["androidModelId"] for item in selected}) == 2, "TWO_DISTINCT_PHYSICAL_MODELS_REQUIRED")
    return selected


def validate_test_args(value):
    require(set(value) == {"serverUrl", "expectedPublicServerUrl", "adminToken"}, "EXACT_PUBLIC_SERVER_TEST_ARGS_REQUIRED")
    origin = value["serverUrl"]
    parsed = urllib.parse.urlsplit(origin)
    require(re.fullmatch(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com", origin) is not None and parsed.port is None and value["expectedPublicServerUrl"] == origin, "ISOLATED_PUBLIC_HTTPS_ORIGIN_REQUIRED")
    require(isinstance(value["adminToken"], str) and re.fullmatch(r"[A-Za-z0-9_-]{64}", value["adminToken"]) is not None, "RANDOM_SYNTHETIC_ADMIN_REQUIRED")
    return dict(value)


def object_url(name):
    return f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{urllib.parse.quote(name, safe='')}"


def private_object(api, name):
    try:
        api.request("GET", object_url(name) + "?alt=media", raw=True, limit=1, authenticated=False)
    except ApiError as error:
        require(error.status in (401, 403), "OBJECT_PRIVACY_UNVERIFIED")
        return
    raise Blocked("PUBLIC_TEST_OBJECT_REFUSED")


def upload(api, name, payload, content_type):
    require(len(payload) <= 64 * 1024 * 1024, "UPLOAD_SIZE_EXCEEDED")
    boundary = "phonebridge" + secrets.token_hex(16)
    metadata = json.dumps({"name": name, "cacheControl": "private,no-store", "contentType": content_type}).encode()
    body = b"--" + boundary.encode() + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + metadata + b"\r\n--" + boundary.encode() + b"\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + payload + b"\r\n--" + boundary.encode() + b"--\r\n"
    api.request("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?uploadType=multipart&ifGenerationMatch=0&predefinedAcl=private", body, content_type=f"multipart/related; boundary={boundary}")
    readback = api.request("GET", object_url(name) + "?projection=full")
    expected_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode()
    require(readback.get("bucket") == BUCKET and readback.get("name") == name and int(readback.get("size", -1)) == len(payload) and readback.get("md5Hash") == expected_md5 and bool(readback.get("generation")), "UPLOAD_READBACK_MISMATCH")
    acl = readback.get("acl", [])
    require(bool(acl) and all(item.get("entity") not in ("allUsers", "allAuthenticatedUsers") for item in acl), "UPLOAD_PRIVATE_ACL_UNVERIFIED")
    private_object(api, name)
    return {"uri": f"gs://{BUCKET}/{name}", "generation": readback["generation"], "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def matrix_payload(devices, uploads, binding, test_args, prefix, test_timeout):
    arguments = {**test_args, "physicalTestLab": "true", "revision": binding["revision"], "runId": binding["run_id"], "runAttempt": binding["run_attempt"], "fixtureNonce": binding["nonce"]}
    return {"projectId": PROJECT, "clientInfo": {"name": "phonebridge-github-ci", "clientInfoDetails": [{"key": key, "value": value} for key, value in binding.items()]}, "flakyTestAttempts": 0, "failFast": True, "environmentMatrix": {"androidDeviceList": {"androidDevices": [item["device"] for item in devices]}}, "testSpecification": {"testTimeout": f"{test_timeout}s", "disableVideoRecording": True, "disablePerformanceMetrics": True, "androidInstrumentationTest": {"appApk": {"gcsPath": uploads["app"]["uri"]}, "testApk": {"gcsPath": uploads["test"]["uri"]}, "appPackageId": "org.nous.phonebridge", "testPackageId": "org.nous.phonebridge.test", "testRunnerClass": "androidx.test.runner.AndroidJUnitRunner", "testTargets": [f"class {TEST_CLASS}"], "orchestratorOption": "DO_NOT_USE_ORCHESTRATOR"}, "testSetup": {"dontAutograntPermissions": True, "environmentVariables": [{"key": key, "value": value} for key, value in arguments.items()], "filesToPush": [{"regularFile": {"content": {"gcsPath": uploads["fixture"]["uri"]}, "devicePath": FIXTURE_PATH}}], "directoriesToPull": [DEVICE_DIRECTORY]}}, "resultStorage": {"googleCloudStorage": {"gcsPath": f"gs://{BUCKET}/{prefix}/results"}}}


def matrix_binding(matrix, matrix_id, payload, executions=False):
    require(matrix.get("projectId") == PROJECT and matrix.get("testMatrixId") == matrix_id, "MATRIX_PROVENANCE_MISMATCH")
    require(matrix.get("environmentMatrix") == payload["environmentMatrix"] and matrix.get("resultStorage", {}).get("googleCloudStorage") == payload["resultStorage"]["googleCloudStorage"], "MATRIX_DEVICES_OR_BUCKET_MISMATCH")
    specification = matrix.get("testSpecification", {})
    require(specification == payload["testSpecification"] and matrix.get("flakyTestAttempts", 0) == 0 and matrix.get("failFast") is True and matrix.get("clientInfo") == payload["clientInfo"], "MATRIX_INPUTS_OR_RETRIES_MISMATCH")
    if executions:
        values = matrix.get("testExecutions", [])
        expected = payload["environmentMatrix"]["androidDeviceList"]["androidDevices"]
        require(len(values) == 2, "EXACTLY_TWO_EXECUTIONS_REQUIRED")
        actual = []
        for execution in values:
            require(execution.get("projectId") == PROJECT and execution.get("matrixId") == matrix_id, "EXECUTION_PROVENANCE_MISMATCH")
            actual.append(execution.get("environment", {}).get("androidDevice"))
        require(sorted(json.dumps(item, sort_keys=True) for item in actual) == sorted(json.dumps(item, sort_keys=True) for item in expected), "EXECUTION_DEVICE_IDENTITIES_MISMATCH")


def poll_matrix(api, matrix_id, payload, deadline, sleep=time.sleep):
    while True:
        deadline.remaining()
        matrix = api.request("GET", f"https://testing.googleapis.com/v1/projects/{PROJECT}/testMatrices/{matrix_id}")
        matrix_binding(matrix, matrix_id, payload)
        if matrix.get("state") in TERMINAL:
            return matrix
        require(matrix.get("state") in {"VALIDATING", "PENDING", "RUNNING"}, "UNKNOWN_MATRIX_STATE")
        sleep(min(10, deadline.remaining()))


def redacted(value, secret):
    if isinstance(value, dict):
        return {key: redacted(item, secret) for key, item in value.items() if key not in {"environmentVariables", "commandLineArguments"}}
    if isinstance(value, list):
        return [redacted(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[synthetic-admin-redacted]")
    return value


def tool_results(api, matrix, payload, matrix_id):
    matrix_binding(matrix, matrix_id, payload, executions=True)
    results = []
    seen = set()
    for execution in matrix["testExecutions"]:
        reference = execution.get("toolResultsStep", {})
        require(reference.get("projectId") == PROJECT and all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reference.get(key, "")) for key in ("historyId", "executionId", "stepId")), "TOOL_RESULTS_PROVENANCE_MISSING")
        key = tuple(reference[key] for key in ("historyId", "executionId", "stepId"))
        require(key not in seen, "DUPLICATE_TOOL_RESULTS_STEP")
        seen.add(key)
        path = f"https://toolresults.googleapis.com/toolresults/v1beta3/projects/{PROJECT}/histories/{key[0]}/executions/{key[1]}/steps/{key[2]}"
        step = api.request("GET", path)
        require(step.get("stepId") == key[2] and step.get("state") == "complete", "TOOL_RESULTS_STEP_INCOMPLETE")
        cases = api.request("GET", path + "/testCases?pageSize=100")
        require(not cases.get("nextPageToken") and len(cases.get("testCases", [])) == 1, "EXACT_ONE_PHYSICAL_TEST_CASE_REQUIRED")
        case = cases["testCases"][0]
        identity = case.get("testCaseReference", {})
        require(identity.get("className") == TEST_CLASS and identity.get("name") == TEST_METHOD, "PHYSICAL_TEST_CASE_NOT_EXECUTED")
        results.append({"reference": reference, "device": execution["environment"]["androidDevice"], "execution_state": execution.get("state"), "step": step, "case": case})
    return results


def archived_reports(data):
    reports = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        require(len(members) <= 100, "RESULT_ARCHIVE_ENTRY_BOUND_EXCEEDED")
        seen = set()
        expanded = 0
        for member in members:
            name = member.filename
            mode = member.external_attr >> 16
            require(not name.startswith("/") and "\\" not in name and all(part not in ("", ".", "..") for part in name.rstrip("/").split("/")) and name not in seen and not (member.flag_bits & 1) and stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR), "UNSAFE_RESULT_ARCHIVE_MEMBER")
            seen.add(name)
            expanded += member.file_size
            require(0 <= member.file_size <= 32 * 1024 * 1024 and expanded <= 128 * 1024 * 1024, "RESULT_ARCHIVE_EXPANSION_BOUND_EXCEEDED")
            if name.endswith("/physical.json") or name == "physical.json":
                require(not member.is_dir() and member.file_size <= 4 * 1024 * 1024, "OEM_REPORT_ARCHIVE_BOUND_EXCEEDED")
                with archive.open(member) as stream:
                    content = stream.read(4 * 1024 * 1024 + 1)
                require(len(content) == member.file_size, "RESULT_ARCHIVE_MEMBER_SIZE_MISMATCH")
                reports.append({"member": name, "sha256": hashlib.sha256(content).hexdigest(), "report": json.loads(content)})
    return reports


def evidence_objects(api, prefix, output, secret, deadline):
    objects = []
    page_token = None
    for _ in range(10):
        query = {"prefix": prefix + "/results/", "maxResults": "100"}
        if page_token:
            query["pageToken"] = page_token
        page = api.request("GET", f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o?" + urllib.parse.urlencode(query))
        objects.extend(page.get("items", []))
        require(len(objects) <= 100, "RESULT_OBJECT_COUNT_EXCEEDED")
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    require(not page_token and bool(objects), "RESULT_LISTING_INCOMPLETE_OR_EMPTY")
    total = 0
    manifest = []
    reports = []
    seen = set()
    for index, item in enumerate(objects):
        deadline.remaining()
        name = item.get("name", "")
        require(item.get("bucket") == BUCKET and name.startswith(prefix + "/results/") and name not in seen and all(part not in ("", ".", "..") for part in name.split("/")) and "\\" not in name and re.fullmatch(r"[0-9]+", item.get("generation", "")) is not None, "RESULT_OBJECT_PROVENANCE_MISMATCH")
        seen.add(name)
        size = int(item.get("size", -1))
        total += size
        require(0 <= size <= 32 * 1024 * 1024 and total <= 128 * 1024 * 1024, "RESULT_SIZE_BOUND_EXCEEDED")
        readback = api.request("GET", object_url(name) + "?" + urllib.parse.urlencode({"projection": "full", "generation": item["generation"]}))
        require(all(readback.get(key) == item.get(key) for key in ("bucket", "name", "generation", "size", "md5Hash")), "RESULT_METADATA_READBACK_MISMATCH")
        require(bool(readback.get("acl")) and all(entry.get("entity") not in {"allUsers", "allAuthenticatedUsers"} for entry in readback["acl"]), "RESULT_PRIVATE_ACL_UNVERIFIED")
        private_object(api, name)
        data = api.request("GET", object_url(name) + "?" + urllib.parse.urlencode({"alt": "media", "generation": item["generation"]}), raw=True, limit=size)
        require(len(data) == size and base64.b64encode(hashlib.md5(data).digest()).decode() == item.get("md5Hash"), "RESULT_INTEGRITY_MISMATCH")
        cleaned = data.replace(secret.encode(), b"[synthetic-admin-redacted]")
        filename = f"object-{index:03d}" + (".json" if name.endswith("physical.json") else ".bin")
        (output / filename).write_bytes(cleaned)
        manifest.append({"bucket": BUCKET, "name": name, "generation": item["generation"], "size": size, "source_sha256": hashlib.sha256(data).hexdigest(), "download_sha256": hashlib.sha256(cleaned).hexdigest(), "local_file": filename})
        if name.endswith("/physical.json"):
            require(size <= 4 * 1024 * 1024, "OEM_REPORT_BOUND_EXCEEDED")
            reports.append(json.loads(cleaned))
        elif name.endswith(".zip"):
            archived = archived_reports(cleaned)
            manifest[-1]["archived_reports"] = [{"member": item["member"], "sha256": item["sha256"]} for item in archived]
            reports.extend(item["report"] for item in archived)
    return manifest, reports


def validate_reports(reports, binding, devices):
    require(len(reports) == 2, "EXACT_TWO_REAL_DEVICE_EVIDENCE_REPORTS_REQUIRED")
    actual = set()
    expected = {(item["model"]["codename"], item["version"]["apiLevel"]) for item in devices}
    for report in reports:
        require(report.get("binding") == binding and report.get("physical") is True, "OEM_EVIDENCE_RUN_PROVENANCE_MISMATCH")
        identity = report.get("device", {})
        pair = (identity.get("device"), identity.get("sdk"))
        require(pair in expected and pair not in actual, "OEM_EVIDENCE_DEVICE_MISMATCH")
        selected = next(item for item in devices if (item["model"]["codename"], item["version"]["apiLevel"]) == pair)
        require(identity.get("manufacturer", "").casefold() == selected["model"]["manufacturer"].casefold(), "OEM_EVIDENCE_MANUFACTURER_MISMATCH")
        stock_package = "com.sec.android.app.clockpackage" if selected["model"]["manufacturer"].casefold() == "samsung" else "com.google.android.deskclock"
        for key in ("packages", "packages_after"):
            metadata = [item for item in report.get(key, []) if item.get("package") == stock_package and item.get("installed") is True]
            require(len(metadata) == 1 and isinstance(metadata[0].get("version_name"), str) and bool(metadata[0]["version_name"]) and type(metadata[0].get("version_code")) is int and metadata[0]["version_code"] > 0 and bool(metadata[0].get("current_signer_sha256")) and all(re.fullmatch(r"[0-9a-f]{64}", signer) for signer in metadata[0]["current_signer_sha256"]), "EXACT_INSTALLED_STOCK_METADATA_MISSING")
        require(report.get("packages") == report.get("packages_after"), "STOCK_PACKAGE_OR_SOURCE_PROFILE_CHANGED_DURING_TEST")
        frames = report.get("frames", [])
        require(1 <= len(frames) <= 32 and any(frame.get("active_package") == stock_package and any(node.get("package") == stock_package for node in frame.get("nodes", [])) for frame in frames), "BOUNDED_NATIVE_STOCK_UI_EVIDENCE_MISSING")
        actual.add(pair)
    return all(report.get("outcome") == "passed" and report.get("transport_roundtrip") is True and report.get("revocation_verified") is True and report.get("clock_crud_verified") is True for report in reports)


def run(args, api, deadline, binding, test_args, output):
    report = {"binding": binding, "project": PROJECT, "provider": PROVIDER, "bucket": BUCKET, "status": "BLOCKED", "retries": 0}
    matrix_id = None
    payload = None
    try:
        report["preflight"] = free_guard(api)
        catalog = api.request("GET", f"https://testing.googleapis.com/v1/testEnvironmentCatalog/ANDROID?projectId={PROJECT}")
        devices = select_devices(catalog)
        report["selected_devices"] = devices
        prefix = f"phonebridge/{binding['revision']}/{binding['run_id']}-{binding['run_attempt']}/{binding['nonce']}"
        report["prefix"] = prefix
        fixture = {"schema": 1, "synthetic": True, "binding": binding, "devices": devices}
        uploads = {}
        for key, path in (("app", args.app_apk), ("test", args.test_apk)):
            require(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= 64 * 1024 * 1024 and path.suffix == ".apk", "BOUNDED_CI_APK_REQUIRED")
            uploads[key] = upload(api, f"{prefix}/inputs/{key}.apk", path.read_bytes(), "application/vnd.android.package-archive")
        uploads["fixture"] = upload(api, f"{prefix}/inputs/fixture.json", json.dumps(fixture, separators=(",", ":")).encode(), "application/json")
        report["uploads"] = uploads
        report["immediate_precreate_guard"] = free_guard(api)
        payload = matrix_payload(devices, uploads, binding, test_args, prefix, args.test_timeout)
        request_id = secrets.token_hex(16)
        report["create_request_id"] = request_id
        created = api.request("POST", f"https://testing.googleapis.com/v1/projects/{PROJECT}/testMatrices?requestId={request_id}", payload)
        matrix_id = created.get("testMatrixId")
        require(isinstance(matrix_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", matrix_id) is not None, "MATRIX_CREATE_ID_MISSING; no create retry")
        report["matrix_id"] = matrix_id
        matrix = poll_matrix(api, matrix_id, payload, deadline)
        report["matrix"] = redacted(matrix, test_args["adminToken"])
        if matrix.get("invalidMatrixDetails") == "TEST_QUOTA_EXCEEDED":
            raise Blocked("FREE_PHYSICAL_QUOTA_EXHAUSTED; retries=0, no charged fallback")
        if matrix.get("state") != "FINISHED":
            try:
                manifest, reports = evidence_objects(api, prefix, output, test_args["adminToken"], deadline)
                report["evidence"] = manifest
                report["device_reports"] = reports
            except Blocked as error:
                report["partial_evidence_reason"] = str(error)
            raise Blocked(f"MATRIX_{matrix.get('state')}: {matrix.get('invalidMatrixDetails', 'no terminal outcome')}; no retry")
        manifest, reports = evidence_objects(api, prefix, output, test_args["adminToken"], deadline)
        report["evidence"] = manifest
        report["device_reports"] = reports
        results = tool_results(api, matrix, payload, matrix_id)
        report["tool_results"] = redacted(results, test_args["adminToken"])
        evidence_passed = validate_reports(reports, binding, devices)
        passed = matrix.get("outcomeSummary") == "SUCCESS" and all(item["execution_state"] == "FINISHED" and item["step"].get("outcome", {}).get("summary") == "success" and item["case"].get("status") == "passed" for item in results) and evidence_passed
        report["status"] = "PASSED" if passed else "FAILED"
        if not passed:
            report["reason"] = "OEM_PROFILE_UNSUPPORTED; real stock metadata and UI evidence retained" if any(item.get("outcome") == "unsupported_failure" for item in reports) else "REAL_OEM_TEST_OR_POSITIVE_ASSERTIONS_FAILED; no retry"
        return 0 if passed else 1
    except DeadlineExceeded as error:
        report["reason"] = str(error)
        if matrix_id:
            try:
                cancellation = GoogleApi(Deadline(10)).request("POST", f"https://testing.googleapis.com/v1/projects/{PROJECT}/testMatrices/{matrix_id}:cancel", {})
                report["deadline_cancellation"] = cancellation
            except Exception:
                report["deadline_cancellation"] = "NOT_CONFIRMED; server test timeout remains bounded"
        return 2
    except (Blocked, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        report["reason"] = str(error).replace(test_args["adminToken"], "[synthetic-admin-redacted]")[:2000]
        return 2
    finally:
        (output / "summary.json").write_text(json.dumps(redacted(report, test_args["adminToken"]), indent=2) + "\n")
        print(json.dumps({"status": report["status"], "matrix_id": matrix_id, "summary": str(output / "summary.json"), "reason": report.get("reason")}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--app-apk", required=True, type=Path)
    parser.add_argument("--test-apk", required=True, type=Path)
    parser.add_argument("--test-args", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--matrix-deadline", type=int, default=1800)
    parser.add_argument("--test-timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        require(args.project == PROJECT and args.provider == PROVIDER, "EXACT_ISOLATED_PROJECT_AND_PROVIDER_REQUIRED")
        require(600 <= args.matrix_deadline <= 2700 and 120 <= args.test_timeout <= 900 and args.test_timeout < args.matrix_deadline - 120, "INVALID_BOUNDED_DEADLINE")
        binding = ci_identity(os.environ)
        oidc_credentials(os.environ, args.provider)
        require(args.test_args.is_file() and not args.test_args.is_symlink() and args.test_args.stat().st_size <= 8192 and args.test_args.stat().st_mode & 0o077 == 0, "PRIVATE_SYNTHETIC_TEST_ARGS_FILE_REQUIRED")
        test_args = validate_test_args(json.loads(args.test_args.read_text()))
        require(not args.output.exists(), "NEW_ISOLATED_EVIDENCE_DIRECTORY_REQUIRED")
        args.output.mkdir(parents=True, mode=0o700)
        deadline = Deadline(args.matrix_deadline)
        return run(args, GoogleApi(deadline), deadline, binding, test_args, args.output)
    except (Blocked, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)[:1000]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
