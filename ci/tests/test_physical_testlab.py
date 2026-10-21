import base64
import copy
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import struct
import tempfile
import unittest
import urllib.error
import urllib.parse
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("physical_testlab_source", Path(__file__).parents[1] / "run_physical_testlab.py")
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


def api_fixtures():
    metric = "testing.googleapis.com/spark_physical_tests"
    billing = {"name": f"projects/{lab.PROJECT}/billingInfo", "projectId": lab.PROJECT, "billingEnabled": False, "billingAccountName": ""}
    settings = {"name": f"projects/{lab.PROJECT}/settings", "defaultBucket": lab.BUCKET}
    quota = {"name": f"projects/{lab.PROJECT_NUMBER}/services/testing.googleapis.com/consumerQuotaMetrics/testing.googleapis.com%2Fspark_physical_tests", "metric": metric, "consumerQuotaLimits": [{"metric": metric, "unit": "1/d/{project}", "quotaBuckets": [{"effectiveLimit": "5", "defaultLimit": "5"}]}]}
    catalog = {"androidDeviceCatalog": {"models": [{"id": "fixture-samsung-physical", "name": "Galaxy S25 Ultra", "manufacturer": "Samsung", "codename": "fixture-samsung", "form": "PHYSICAL", "formFactor": "PHONE", "supportedVersionIds": ["fixture-os35", "fixture-os36"]}, {"id": "fixture-pixel-physical", "name": "Pixel 9", "manufacturer": "Google", "codename": "fixture-pixel", "form": "PHYSICAL", "formFactor": "PHONE", "supportedVersionIds": ["fixture-os35"]}], "versions": [{"id": "fixture-os35", "apiLevel": 35, "versionString": "15"}, {"id": "fixture-os36", "apiLevel": 36, "versionString": "16"}], "runtimeConfiguration": {"locales": [{"id": "en"}], "orientations": [{"id": "portrait"}]}}}
    return billing, settings, quota, catalog


def report_fixture(binding, selected):
    package = "com.sec.android.app.clockpackage" if selected["model"]["manufacturer"] == "Samsung" else "com.google.android.deskclock"
    metadata = {"package": package, "installed": True, "version_name": "fixture-version", "version_code": 1, "current_signer_sha256": ["a" * 64]}
    return {"fixture_only": True, "binding": binding, "physical": True, "device": {"device": selected["model"]["codename"], "sdk": selected["version"]["apiLevel"], "manufacturer": selected["model"]["manufacturer"]}, "packages": [metadata], "packages_after": [copy.deepcopy(metadata)], "frames": [{"phase": "fixture-only", "active_package": package, "nodes": [{"package": package, "id": "fixture-only"}]}], "outcome": "passed", "transport_roundtrip": True, "revocation_verified": True, "clock_crud_verified": True}


def archive_fixture(entries, compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=compression) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return stream.getvalue()


class FixtureApi:
    def __init__(self):
        self.billing, self.settings, self.quota, self.catalog = api_fixtures()
        self.requests = []
        self.objects = {}
        self.matrix = None
        self.create_error = None
        self.case_status = "passed"
        self.report_outcome = "passed"
        self.omit_report = False
        self.result_transform = None

    def add_object(self, name, data):
        metadata = {"bucket": lab.BUCKET, "name": name, "generation": "12345", "size": str(len(data)), "md5Hash": base64.b64encode(hashlib.md5(data).digest()).decode(), "acl": [{"entity": "user-fixture-service-account", "role": "OWNER"}]}
        self.objects[name] = (metadata, data)
        return metadata

    def request(self, method, url, body=None, **options):
        self.requests.append((method, url, copy.deepcopy(body)))
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if options.get("authenticated") is False:
            raise lab.ApiError(401, parsed.hostname, "UNAUTHENTICATED")
        if parsed.hostname == "cloudresourcemanager.googleapis.com":
            return {"name": f"projects/{lab.PROJECT_NUMBER}", "projectId": lab.PROJECT, "state": "ACTIVE"}
        if parsed.hostname == "firebase.googleapis.com":
            return {"projectId": lab.PROJECT, "projectNumber": lab.PROJECT_NUMBER, "state": "ACTIVE"}
        if parsed.hostname == "cloudbilling.googleapis.com":
            return copy.deepcopy(self.billing)
        if parsed.hostname == "serviceusage.googleapis.com":
            return copy.deepcopy(self.quota)
        if parsed.hostname == "toolresults.googleapis.com":
            if parsed.path.endswith("/settings"):
                return copy.deepcopy(self.settings)
            if parsed.path.endswith("/testCases"):
                return {"testCases": [{"testCaseReference": {"className": lab.TEST_CLASS, "name": lab.TEST_METHOD}, "status": self.case_status}]}
            return {"stepId": parsed.path.rsplit("/", 1)[1], "state": "complete", "outcome": {"summary": "success"}}
        if parsed.hostname == "testing.googleapis.com":
            if "/testEnvironmentCatalog/" in parsed.path:
                return copy.deepcopy(self.catalog)
            if method == "POST" and not parsed.path.endswith(":cancel"):
                if self.create_error:
                    raise self.create_error
                self.matrix = copy.deepcopy(body)
                self.matrix.update({"testMatrixId": "fixture-matrix", "state": "FINISHED", "outcomeSummary": "SUCCESS", "testExecutions": []})
                prefix = body["resultStorage"]["googleCloudStorage"]["gcsPath"].split(f"gs://{lab.BUCKET}/", 1)[1]
                binding = {item["key"]: item["value"] for item in body["clientInfo"]["clientInfoDetails"]}
                for index, selected in enumerate(lab.select_devices(self.catalog)):
                    self.matrix["testExecutions"].append({"projectId": lab.PROJECT, "matrixId": "fixture-matrix", "state": "FINISHED", "environment": {"androidDevice": selected["device"]}, "toolResultsStep": {"projectId": lab.PROJECT, "historyId": "bh.1234567890abcdef", "executionId": "1234567890123456789", "stepId": f"bs.1234567890abcde{index}"}})
                    if not (self.omit_report and index == 1):
                        report = report_fixture(binding, selected)
                        report["outcome"] = self.report_outcome
                        self.add_object(f"{prefix}/fixture-device-{index}/physical.json", json.dumps(report).encode())
                if self.result_transform:
                    self.result_transform(self, prefix)
                return copy.deepcopy(self.matrix)
            if parsed.path.endswith(":cancel"):
                return {"testState": "CANCELLED"}
            return copy.deepcopy(self.matrix)
        if parsed.hostname == "storage.googleapis.com":
            if method == "POST":
                boundary = options["content_type"].split("boundary=", 1)[1].encode()
                parts = body.split(b"--" + boundary)
                metadata = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n"))
                data = parts[2].split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")
                return self.add_object(metadata["name"], data)
            if parsed.path.endswith("/o"):
                prefix = query["prefix"][0]
                return {"items": [copy.deepcopy(metadata) for name, (metadata, data) in self.objects.items() if name.startswith(prefix)]}
            name = urllib.parse.unquote(parsed.path.split("/o/", 1)[1])
            metadata, data = self.objects[name]
            return data if options.get("raw") else copy.deepcopy(metadata)
        raise AssertionError(f"Unexpected fixture request {method} {url}")


class PhysicalTestLabSourceTest(unittest.TestCase):
    def setUp(self):
        self.billing, self.settings, self.quota, self.catalog = api_fixtures()
        self.binding = {"repository": lab.REPOSITORY, "revision": "a" * 40, "run_id": "123", "run_attempt": "1", "nonce": "b" * 32}
        self.test_args = {"serverUrl": "https://fixture-ci.trycloudflare.com", "expectedPublicServerUrl": "https://fixture-ci.trycloudflare.com", "adminToken": "z" * 64}

    def test_exact_free_plan_readback(self):
        lab.validate_free(self.billing, self.settings, self.quota)
        for changes in ({"billingEnabled": True}, {"billingAccountName": "billingAccounts/fixture-paid"}, {"billingEnabled": None}, {"projectId": "other-project"}):
            with self.subTest(changes=changes), self.assertRaises(lab.Blocked):
                lab.validate_free({**self.billing, **changes}, self.settings, self.quota)
        del self.billing["billingAccountName"]
        with self.assertRaises(lab.Blocked):
            lab.validate_free(self.billing, self.settings, self.quota)

    def test_quota_and_managed_bucket_are_exact(self):
        for bucket in (None, "fixture-custom-paid-bucket", "gs://" + lab.BUCKET):
            with self.subTest(bucket=bucket), self.assertRaises(lab.Blocked):
                lab.validate_free(self.billing, {**self.settings, "defaultBucket": bucket}, self.quota)
        for limit in ("4", "6", "200", None, 5):
            value = copy.deepcopy(self.quota)
            value["consumerQuotaLimits"][0]["quotaBuckets"][0]["effectiveLimit"] = limit
            with self.subTest(limit=limit), self.assertRaises(lab.Blocked):
                lab.validate_free(self.billing, self.settings, value)
        for field, value in (("metric", "testing.googleapis.com/blaze_physical_tests"), ("name", "projects/other/consumerQuotaMetrics/fixture")):
            with self.subTest(field=field), self.assertRaises(lab.Blocked):
                lab.validate_free(self.billing, self.settings, {**self.quota, field: value})
        self.quota["consumerQuotaLimits"][0]["quotaBuckets"].append({"effectiveLimit": "5"})
        with self.assertRaises(lab.Blocked):
            lab.validate_free(self.billing, self.settings, self.quota)

    def test_catalog_selects_exact_two_physical_devices_and_latest_supported_os(self):
        devices = lab.select_devices(self.catalog)
        self.assertEqual(["fixture-samsung-physical", "fixture-pixel-physical"], [item["device"]["androidModelId"] for item in devices])
        self.assertEqual(["fixture-os36", "fixture-os35"], [item["device"]["androidVersionId"] for item in devices])
        self.catalog["androidDeviceCatalog"]["versions"][1]["tags"] = ["preview"]
        self.assertEqual("fixture-os35", lab.select_devices(self.catalog)[0]["device"]["androidVersionId"])

    def test_catalog_excludes_streaming_only_alias_without_runnable_versions(self):
        alias = copy.deepcopy(self.catalog["androidDeviceCatalog"]["models"][0])
        alias["id"] = "fixture-samsung-streaming-only"
        alias["codename"] = "fixture-samsung-streaming-only"
        for versions in (None, [], ["fixture-unknown-os"]):
            catalog = copy.deepcopy(self.catalog)
            alias["supportedVersionIds"] = versions
            catalog["androidDeviceCatalog"]["models"].append(copy.deepcopy(alias))
            with self.subTest(versions=versions):
                selected = lab.select_devices(catalog)
                self.assertEqual("fixture-samsung-physical", selected[0]["device"]["androidModelId"])

    def test_no_virtual_pro_xl_alias_or_ambiguous_device_fallback(self):
        for field, value in (("form", "VIRTUAL"), ("name", "Pixel 9 Pro"), ("name", "Pixel 9 Pro XL"), ("accessDeniedReasons", ["EULA_NOT_ACCEPTED"])):
            catalog = copy.deepcopy(self.catalog)
            catalog["androidDeviceCatalog"]["models"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(lab.Blocked):
                lab.select_devices(catalog)
        self.catalog["androidDeviceCatalog"]["models"].append(copy.deepcopy(self.catalog["androidDeviceCatalog"]["models"][0]))
        with self.assertRaises(lab.Blocked):
            lab.select_devices(self.catalog)

    def test_public_https_dynamic_test_args_no_emulator_or_secret_defaults(self):
        self.assertEqual(self.test_args, lab.validate_test_args(self.test_args))
        for changes in ({"serverUrl": "http://10.0.2.2:18940"}, {"serverUrl": "https://prod.example.com"}, {"expectedPublicServerUrl": "https://different.trycloudflare.com"}, {"adminToken": "fixture-short"}, {"qrFixture": "true"}):
            with self.subTest(changes=changes), self.assertRaises(lab.Blocked):
                lab.validate_test_args({**self.test_args, **changes})

    def test_matrix_targets_only_real_oem_suite_without_shards_or_retries(self):
        devices = lab.select_devices(self.catalog)
        uploads = {key: {"uri": f"gs://{lab.BUCKET}/fixture/{key}"} for key in ("app", "test", "fixture")}
        payload = lab.matrix_payload(devices, uploads, self.binding, self.test_args, "fixture", 600)
        instrument = payload["testSpecification"]["androidInstrumentationTest"]
        self.assertEqual([f"class {lab.TEST_CLASS}"], instrument["testTargets"])
        self.assertNotIn("shardingOption", instrument)
        self.assertEqual(0, payload["flakyTestAttempts"])
        self.assertIs(True, payload["failFast"])
        self.assertEqual([lab.DEVICE_DIRECTORY], payload["testSpecification"]["testSetup"]["directoriesToPull"])
        self.assertEqual(2, len(payload["environmentMatrix"]["androidDeviceList"]["androidDevices"]))

    def run_fixture(self, api, directory):
        app = directory / "fixture-app.apk"
        test = directory / "fixture-test.apk"
        app.write_bytes(b"TEST_ONLY_APP_APK_FIXTURE")
        test.write_bytes(b"TEST_ONLY_ANDROID_TEST_APK_FIXTURE")
        output = directory / "evidence"
        output.mkdir()
        args = SimpleNamespace(app_apk=app, test_apk=test, test_timeout=600)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = lab.run(args, api, lab.Deadline(1800), self.binding, self.test_args, output)
        self.last_stdout = stdout.getvalue()
        return code, json.loads((output / "summary.json").read_text()), output

    def test_fixture_api_roundtrip_upload_readback_matrix_poll_actual_result_download(self):
        api = FixtureApi()
        with tempfile.TemporaryDirectory() as name:
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(0, code)
            self.assertEqual("PASSED", summary["status"])
            self.assertEqual(2, len(summary["evidence"]))
            self.assertEqual(2, len(summary["device_reports"]))
            self.assertTrue(all((output / entry["local_file"]).is_file() for entry in summary["evidence"]))
            self.assertNotIn(self.test_args["adminToken"], (output / "summary.json").read_text())
        creates = [item for item in api.requests if item[0] == "POST" and "/testMatrices?" in item[1]]
        self.assertEqual(1, len(creates))
        self.assertEqual(3, len([item for item in api.requests if item[0] == "POST" and "/upload/storage/" in item[1]]))
        self.assertEqual(2, len([item for item in api.requests if item[1].endswith("/billingInfo")]))
        self.assertFalse(any(":enable" in item[1] or "setIamPolicy" in item[1] for item in api.requests))

    def test_exhausted_quota_never_retries_or_passes(self):
        api = FixtureApi()
        api.create_error = lab.ApiError(429, "testing.googleapis.com", "RESOURCE_EXHAUSTED")
        with tempfile.TemporaryDirectory() as name:
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertIn("FREE_QUOTA_EXHAUSTED", summary["reason"])
        self.assertEqual(1, len([item for item in api.requests if item[0] == "POST" and "/testMatrices?" in item[1]]))

    def test_billing_or_bucket_guard_blocks_before_upload_or_create(self):
        for kind in ("billing", "bucket", "quota"):
            api = FixtureApi()
            if kind == "billing":
                api.billing["billingEnabled"] = True
            elif kind == "bucket":
                api.settings["defaultBucket"] = "fixture-other"
            else:
                api.quota["consumerQuotaLimits"][0]["quotaBuckets"][0]["effectiveLimit"] = "200"
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                code, summary, output = self.run_fixture(api, Path(name))
                self.assertEqual(2, code)
                self.assertFalse(any(item[0] == "POST" for item in api.requests))

    def test_unsupported_stock_profile_and_skipped_case_never_pass(self):
        for field, value in (("case_status", "skipped"), ("report_outcome", "unsupported_failure"), ("omit_report", True)):
            api = FixtureApi()
            setattr(api, field, value)
            with self.subTest(field=field), tempfile.TemporaryDirectory() as name:
                code, summary, output = self.run_fixture(api, Path(name))
                self.assertNotEqual(0, code)
                self.assertNotEqual("PASSED", summary["status"])
                self.assertTrue(summary["evidence"])

    def test_report_revision_device_and_positive_assertion_bindings(self):
        devices = lab.select_devices(self.catalog)
        reports = [report_fixture(self.binding, item) for item in devices]
        self.assertTrue(lab.validate_reports(reports, self.binding, devices))
        reports[0]["clock_crud_verified"] = False
        self.assertFalse(lab.validate_reports(reports, self.binding, devices))
        for changed in ({"binding": {**self.binding, "revision": "f" * 40}}, {"physical": False}, {"device": {"device": "other-fixture", "sdk": 36}}):
            with self.subTest(changed=changed), self.assertRaises(lab.Blocked):
                lab.validate_reports([{**reports[0], **changed}, reports[1]], self.binding, devices)

    def test_deadline_no_unbounded_poll(self):
        now = [0.0]
        deadline = lab.Deadline(15, clock=lambda: now[0])
        api = FixtureApi()
        payload = lab.matrix_payload(lab.select_devices(self.catalog), {key: {"uri": "gs://fixture-only/" + key} for key in ("app", "test", "fixture")}, self.binding, self.test_args, "fixture-only", 600)
        api.matrix = {**copy.deepcopy(payload), "testMatrixId": "fixture-matrix", "state": "PENDING"}
        def sleep(seconds):
            now[0] += seconds
        with self.assertRaises(lab.DeadlineExceeded):
            lab.poll_matrix(api, "fixture-matrix", payload, deadline, sleep)
        self.assertLessEqual(len(api.requests), 2)

    def test_matrix_and_execution_provenance_rejects_wrong_project_bucket_sha_devices(self):
        api = FixtureApi()
        with tempfile.TemporaryDirectory() as name:
            self.run_fixture(api, Path(name))
        payload = next(body for method, url, body in api.requests if method == "POST" and "/testMatrices?" in url)
        for field, value in (("projectId", "fixture-other-project"), ("testMatrixId", "fixture-other-matrix"), ("flakyTestAttempts", 1), ("failFast", False)):
            with self.subTest(field=field), self.assertRaises(lab.Blocked):
                lab.matrix_binding({**api.matrix, field: value}, "fixture-matrix", payload, executions=True)
        changed = copy.deepcopy(api.matrix)
        changed["testExecutions"][1]["environment"] = changed["testExecutions"][0]["environment"]
        with self.assertRaises(lab.Blocked):
            lab.matrix_binding(changed, "fixture-matrix", payload, executions=True)
        changed = copy.deepcopy(api.matrix)
        next(item for item in changed["testSpecification"]["testSetup"]["environmentVariables"] if item["key"] == "revision")["value"] = "f" * 40
        with self.assertRaises(lab.Blocked):
            lab.matrix_binding(changed, "fixture-matrix", payload)
        changed = copy.deepcopy(api.matrix)
        changed["resultStorage"]["googleCloudStorage"]["gcsPath"] = "gs://fixture-other/results"
        with self.assertRaises(lab.Blocked):
            lab.matrix_binding(changed, "fixture-matrix", payload)

    def test_results_integrity_and_private_upload_fail_closed(self):
        api = FixtureApi()
        original = api.request
        def tampered(method, url, body=None, **options):
            result = original(method, url, body, **options)
            if "/o/" in url and options.get("raw") is not True:
                result["md5Hash"] = "fixture-corrupt"
            return result
        api.request = tampered
        with self.assertRaises(lab.Blocked):
            lab.upload(api, "fixture-only/app.apk", b"TEST_ONLY_APK", "application/octet-stream")

    def test_local_fork_pr_dispatch_and_other_workflow_are_rejected(self):
        with self.assertRaises(lab.Blocked):
            lab.ci_identity({})
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": lab.REPOSITORY, "GITHUB_REPOSITORY_ID": "1417002417", "GITHUB_REPOSITORY_OWNER": "freQuensy23-coder", "GITHUB_REPOSITORY_OWNER_ID": "64750224", "GITHUB_ACTOR": "freQuensy23-coder", "GITHUB_TRIGGERING_ACTOR": "freQuensy23-coder", "GITHUB_ACTOR_ID": "64750224", "GITHUB_REF": "refs/heads/main", "GITHUB_REF_TYPE": "branch", "GITHUB_EVENT_NAME": "push", "GITHUB_WORKFLOW_REF": f"{lab.REPOSITORY}/.github/workflows/ci.yml@refs/heads/main", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
        with tempfile.TemporaryDirectory() as name:
            event = Path(name) / "fixture-event.json"
            event.write_text(json.dumps({"after": "a" * 40, "ref": "refs/heads/main", "deleted": False, "repository": {"id": 1417002417, "full_name": lab.REPOSITORY, "owner": {"id": 64750224}}, "sender": {"id": 64750224}}))
            env["GITHUB_EVENT_PATH"] = str(event)
            self.assertEqual("a" * 40, lab.ci_identity(env)["revision"])
            for changes in ({"GITHUB_REPOSITORY": "fixture-fork/hermes-phonebridge"}, {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_EVENT_NAME": "workflow_dispatch"}, {"GITHUB_WORKFLOW_REF": f"{lab.REPOSITORY}/.github/workflows/other.yml@refs/heads/main"}):
                with self.subTest(changes=changes), self.assertRaises(lab.Blocked):
                    lab.ci_identity({**env, **changes})
            env["GITHUB_SHA"] = "f" * 40
            with self.assertRaises(lab.Blocked):
                lab.ci_identity(env)

    def test_oidc_only_rejects_personal_adc_keys_wrong_provider_and_impersonation(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "fixture-oidc.json"
            value = {"type": "external_account", "audience": f"//iam.googleapis.com/{lab.PROVIDER}", "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "token_url": "https://sts.googleapis.com/v1/token", "service_account_impersonation_url": f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{lab.SERVICE_ACCOUNT}:generateAccessToken", "credential_source": {"url": "https://fixture.actions.githubusercontent.com/idtoken?" + urllib.parse.urlencode({"audience": lab.PROVIDER}), "headers": {"Authorization": "Bearer TEST_ONLY_OIDC_FIXTURE"}, "format": {"type": "json", "subject_token_field_name": "value"}}}
            path.write_text(json.dumps(value))
            env = {"GOOGLE_APPLICATION_CREDENTIALS": str(path), "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": str(path), "RUNNER_TEMP": name}
            lab.oidc_credentials(env, lab.PROVIDER)
            for change in ({"type": "authorized_user"}, {"type": "service_account", "private_key": "TEST_ONLY_NEVER_REAL"}, {"audience": "fixture-other-provider"}, {"service_account_impersonation_url": "https://iamcredentials.googleapis.com/fixture-other"}, {"credential_source": {"file": "fixture-personal-token"}}):
                path.write_text(json.dumps({**value, **change}))
                with self.subTest(change=change), self.assertRaises(lab.Blocked):
                    lab.oidc_credentials(env, lab.PROVIDER)
            with self.assertRaises(lab.Blocked):
                lab.oidc_credentials(env, "fixture-other-provider")

    def test_guard_is_rechecked_immediately_before_matrix_creation(self):
        api = FixtureApi()
        original = api.request
        reads = [0]
        def changed_billing(method, url, body=None, **options):
            result = original(method, url, body, **options)
            if url.endswith("/billingInfo"):
                reads[0] += 1
                if reads[0] == 2:
                    result["billingEnabled"] = True
            return result
        api.request = changed_billing
        with tempfile.TemporaryDirectory() as name:
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertIn("BILLING_ENABLED", summary["reason"])
        self.assertFalse(any(method == "POST" and "/testMatrices?" in url for method, url, body in api.requests))

    def test_controlled_archive_reports_reject_traversal_duplicates_and_symlinks(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("fixture-only/physical.json", json.dumps({"fixture_only": True}))
        inventory, reports = lab.archived_reports(stream.getvalue(), self.test_args["adminToken"])
        self.assertEqual({"fixture_only": True}, reports[0][0])
        self.assertEqual("fixture-only/physical.json", inventory[0]["member"])
        for name in ("../physical.json", "/physical.json", "fixture-only/../physical.json", "fixture-only\\physical.json"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr(name, "{}")
            with self.subTest(name=name), self.assertRaises(lab.Blocked):
                lab.archived_reports(stream.getvalue(), self.test_args["adminToken"])
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            entry = zipfile.ZipInfo("fixture-only/physical.json")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "fixture-target")
        with self.assertRaises(lab.Blocked):
            lab.archived_reports(stream.getvalue(), self.test_args["adminToken"])

    def matrix_fixture(self):
        devices = lab.select_devices(self.catalog)
        uploads = {key: {"uri": f"gs://{lab.BUCKET}/fixture/{key}"} for key in ("app", "test", "fixture")}
        payload = lab.matrix_payload(devices, uploads, self.binding, self.test_args, "fixture", 600)
        matrix = {**copy.deepcopy(payload), "testMatrixId": "fixture-matrix", "state": "FINISHED", "testExecutions": []}
        for index, selected in enumerate(devices):
            matrix["testExecutions"].append({"projectId": lab.PROJECT, "matrixId": "fixture-matrix", "state": "FINISHED", "environment": {"androidDevice": selected["device"]}, "toolResultsStep": {"projectId": lab.PROJECT, "historyId": "bh.1234567890abcdef", "executionId": "execution.1234567890123456789", "stepId": f"bs.1234567890abcde{index}"}})
        return matrix, payload

    def test_documented_dotted_tool_results_ids_and_each_encoded_path_segment(self):
        matrix, payload = self.matrix_fixture()
        for execution_id in ("1234567890123456789", "execution.1234567890123456789", "x" * 128):
            api = FixtureApi()
            for execution in matrix["testExecutions"]:
                execution["toolResultsStep"]["executionId"] = execution_id
            with self.subTest(execution_id=execution_id), patch.object(lab.urllib.parse, "quote", wraps=urllib.parse.quote) as quote:
                results = lab.tool_results(api, matrix, payload, "fixture-matrix")
                self.assertEqual(2, len(results))
                self.assertEqual(8, quote.call_count)
                self.assertTrue(all(call.kwargs == {"safe": ""} for call in quote.call_args_list))
            for index, result in enumerate(results):
                path = f"https://toolresults.googleapis.com/toolresults/v1beta3/projects/{lab.PROJECT}/histories/bh.1234567890abcdef/executions/{execution_id}/steps/bs.1234567890abcde{index}"
                self.assertEqual(path, api.requests[index * 2][1])
                self.assertEqual(path + "/testCases?pageSize=100", api.requests[index * 2 + 1][1])
                self.assertEqual("passed", result["case"]["status"])

    def test_bad_tool_results_ids_rejected_before_any_request(self):
        matrix, payload = self.matrix_fixture()
        invalid = ("", ".", "..", "../step", "history/step", "history\\step", "%2f", "%2F", "%5c", "%2e%2e", "%252f", "%252e%252e", "step?alt=media", "step#fragment", "step:method", "step\n", "step\x00", "é", "x" * 129, None, 12)
        for field in ("historyId", "executionId", "stepId"):
            for value in invalid:
                api = FixtureApi()
                changed = copy.deepcopy(matrix)
                changed["testExecutions"][1]["toolResultsStep"][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(lab.Blocked, "TOOL_RESULTS_PROVENANCE_MISSING"):
                    lab.tool_results(api, changed, payload, "fixture-matrix")
                self.assertEqual([], api.requests)
        changed = copy.deepcopy(matrix)
        changed["testExecutions"][1]["toolResultsStep"] = copy.deepcopy(changed["testExecutions"][0]["toolResultsStep"])
        api = FixtureApi()
        with self.assertRaisesRegex(lab.Blocked, "DUPLICATE_TOOL_RESULTS_STEP"):
            lab.tool_results(api, changed, payload, "fixture-matrix")
        self.assertEqual([], api.requests)

    def assert_emitted_artifacts_sanitized(self, output, summary):
        secret = self.test_args["adminToken"].encode()
        entries = []
        sources = summary.get("evidence")
        if sources is None:
            sources = [json.loads((output / entry["local_file"]).read_bytes()) for entry in summary["inventories"]]
        for source in sources:
            if "local_file" in source:
                entries.append(source)
            entries.extend(member for member in source.get("members", []) if "local_file" in member)
        paths = {entry["local_file"] for entry in entries}
        self.assertEqual(len(entries), len(paths))
        inventory_paths = {entry["local_file"] for entry in summary.get("inventories", [])}
        files = [path for path in output.rglob("*") if path.is_file()]
        self.assertEqual(paths | inventory_paths | {"summary.json"}, {path.relative_to(output).as_posix() for path in files})
        self.assertLessEqual(len(files), lab.MAX_PUBLICATION_FILES)
        self.assertLessEqual(sum(path.stat().st_size for path in files), lab.MAX_PUBLICATION_BYTES)
        self.assertNotIn(secret.decode(), self.last_stdout)
        for entry in summary.get("inventories", []):
            data = (output / entry["local_file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["download_sha256"])
            self.assertEqual(len(data), entry["download_size"])
        if sources is not None:
            self.assertEqual(sources, [json.loads((output / entry["local_file"]).read_bytes()) for entry in summary.get("inventories", [])])
        for path in files:
            with self.subTest(artifact=path.relative_to(output).as_posix()):
                self.assertFalse(path.is_symlink())
                data = path.read_bytes()
                self.assertLessEqual(len(data), lab.MAX_ARTIFACT_BYTES)
                self.assertNotIn(secret, data)
                self.assertNotIn(secret.decode(), data.decode("utf-8"))
                self.assertIn(path.suffix, {".json", ".txt", ".log", ".xml", ".csv"})
                self.assertFalse(zipfile.is_zipfile(io.BytesIO(data)))
                if path.suffix == ".json":
                    self.assertNotIn(secret.decode(), json.dumps(json.loads(data)))
        for entry in entries:
            data = (output / entry["local_file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["download_sha256"])
            self.assertEqual(len(data), entry["download_size"])

    def test_deflated_reports_logs_json_keys_and_omitted_binary_have_safe_provenance(self):
        api = FixtureApi()
        secret = self.test_args["adminToken"]
        original_sources = {}
        raw_members = {}
        def transform(api, prefix):
            members = []
            for index in range(2):
                name = f"{prefix}/fixture-device-{index}/physical.json"
                report = json.loads(api.objects[name][1])
                report["diagnostic"] = {"token-in-key-" + secret: [secret, {"nested-" + secret: secret}]}
                report["environmentVariables"] = [{"key": "adminToken", "value": secret}]
                text = json.dumps(report)
                if index == 1:
                    escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
                    text = text.replace(secret, escaped)
                content = text.encode()
                api.add_object(name, content)
                members.append((f"fixture-device-{index}/physical.json", content))
            escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
            members.extend([("fixture/", b""), (f"fixture/{secret}.log", secret.encode()), ("fixture/logs.txt", ("admin=" + secret + "\n").encode()), ("fixture/details.json", json.dumps({secret: {"nested": [secret]}}).replace(secret, escaped).encode()), ("fixture/screen.png", b"\x89PNG\x00" + secret.encode()), ("fixture/nested.zip", archive_fixture([("secret.log", secret.encode())]))])
            raw_members.update(dict(members))
            api.add_object(f"{prefix}/artifact.zip", archive_fixture(members))
            api.add_object(f"{prefix}/direct-{secret}.json", json.dumps({secret: {secret: [secret]}}).replace(secret, escaped).encode())
            api.add_object(f"{prefix}/direct.log", ("admin=" + secret).encode())
            api.add_object(f"{prefix}/screen.png", b"\x89PNG\x00" + secret.encode())
            api.add_object(f"{prefix}/invalid.log", b"\xff" + secret.encode())
            api.add_object(f"{prefix}/binary.txt", b"\x00" + secret.encode())
            original_sources.update(copy.deepcopy({name: value for name, value in api.objects.items() if name.startswith(prefix + "/")}))
        api.result_transform = transform
        with tempfile.TemporaryDirectory() as name:
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(0, code)
            self.assertEqual("PASSED", summary["status"])
            self.assertEqual(2, len(summary["device_reports"]))
            self.assert_emitted_artifacts_sanitized(output, summary)
            archive = next(entry for entry in summary["evidence"] if entry["name"].endswith("artifact.zip"))
            self.assertEqual("RAW_ARCHIVE_NEVER_PUBLISHED", archive["omission_reason"])
            self.assertNotIn("local_file", archive)
            for member in archive["members"]:
                original_name = member["member"].replace("[synthetic-admin-redacted]", secret)
                self.assertEqual(hashlib.sha256(raw_members[original_name]).hexdigest(), member["source_sha256"])
                self.assertEqual(len(raw_members[original_name]), member["size"])
                if member["member"].endswith("/"):
                    self.assertEqual("ARCHIVE_DIRECTORY_NOT_AN_ARTIFACT", member["omission_reason"])
                    self.assertNotIn("local_file", member)
                if member["member"].endswith((".png", ".zip")):
                    self.assertEqual("TYPE_NOT_ALLOWLISTED_CREDENTIAL_FREEDOM_UNVERIFIED", member["omission_reason"])
                    self.assertNotIn("local_file", member)
            for source in summary["evidence"]:
                original_name = source["name"].replace("[synthetic-admin-redacted]", secret)
                metadata, data = original_sources[original_name]
                self.assertEqual(hashlib.sha256(data).hexdigest(), source["source_sha256"])
                self.assertEqual(len(data), source["size"])
                self.assertEqual(metadata["generation"], source["generation"])
                self.assertEqual(metadata["md5Hash"], source["md5Hash"])
            omitted = {entry["name"].rsplit("/", 1)[1]: entry["omission_reason"] for entry in summary["evidence"] if "omission_reason" in entry}
            self.assertEqual("TYPE_NOT_ALLOWLISTED_CREDENTIAL_FREEDOM_UNVERIFIED", omitted["screen.png"])
            self.assertEqual("ALLOWLISTED_TYPE_NOT_UTF8_TEXT", omitted["invalid.log"])
            self.assertEqual("ALLOWLISTED_TYPE_CONTAINS_BINARY_CONTROL_BYTES", omitted["binary.txt"])
        self.assertEqual(original_sources, {name: api.objects[name] for name in original_sources})
        result_reads = []
        for method, url, body in api.requests:
            parsed = urllib.parse.urlsplit(url)
            if "/storage/v1/" in parsed.path and "/results/" in urllib.parse.unquote(parsed.path) and method == "GET":
                query = urllib.parse.parse_qs(parsed.query)
                if "projection" in query or query.get("alt") == ["media"]:
                    self.assertEqual(["12345"], query["generation"])
                    result_reads.append(url)
        self.assertEqual(2 * len(original_sources), len(result_reads))
        self.assertFalse(any(method != "GET" and "/storage/v1/" in url and "/upload/" not in url for method, url, body in api.requests))

    def test_identical_direct_and_archive_reports_dedupe_but_conflicts_block(self):
        for mutation in (None, "ordering", "outcome", "frame", "redacted-value", "binding", "environmentVariables", "commandLineArguments"):
            api = FixtureApi()
            def transform(api, prefix):
                name = f"{prefix}/fixture-device-0/physical.json"
                report = json.loads(api.objects[name][1])
                report["diagnostic"] = self.test_args["adminToken"]
                api.add_object(name, json.dumps(report).encode())
                duplicate = copy.deepcopy(report)
                if mutation == "outcome":
                    duplicate["outcome"] = "failed"
                elif mutation == "frame":
                    duplicate["frames"][0]["phase"] = "fixture-conflicting"
                elif mutation == "redacted-value":
                    duplicate["diagnostic"] = "[synthetic-admin-redacted]"
                elif mutation == "binding":
                    duplicate["binding"]["run_id"] = "124"
                elif mutation in {"environmentVariables", "commandLineArguments"}:
                    duplicate["diagnostic"] = {mutation: [self.test_args["adminToken"]]}
                    report["diagnostic"] = {mutation: ["fixture-original"]}
                    api.add_object(name, json.dumps(report).encode())
                elif mutation == "ordering":
                    duplicate = dict(reversed(list(duplicate.items())))
                content = json.dumps(duplicate, indent=2 if mutation == "ordering" else None).encode()
                data = archive_fixture([("fixture-device-0/physical.json", content)])
                api.add_object(f"{prefix}/duplicate.zip", data)
            api.result_transform = transform
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                code, summary, output = self.run_fixture(api, Path(name))
                if mutation in (None, "ordering"):
                    self.assertEqual(0, code)
                    self.assertEqual(2, len(summary["device_reports"]))
                    self.assertEqual(3, len(summary["evidence"]))
                    direct = summary["evidence"][0]
                    archived = summary["evidence"][2]["members"][0]
                    self.assertEqual(direct["source_report_sha256"], archived["source_report_sha256"])
                    if mutation == "ordering":
                        self.assertNotEqual(direct["source_sha256"], archived["source_sha256"])
                    self.assert_emitted_artifacts_sanitized(output, summary)
                else:
                    self.assertEqual(2, code)
                    self.assertEqual("BLOCKED", summary["status"])
                    self.assertEqual("OEM_EVIDENCE_RUN_PROVENANCE_MISMATCH" if mutation == "binding" else "CONFLICTING_OEM_EVIDENCE_REPORTS", summary["reason"])
                    self.assertEqual(3, len(summary["evidence"]))
                    self.assert_emitted_artifacts_sanitized(output, summary)
                self.assertTrue(all(self.test_args["adminToken"].encode() not in path.read_bytes() for path in output.rglob("*") if path.is_file()))

    def test_duplicate_reports_require_two_distinct_bound_devices_and_positive_assertions(self):
        devices = lab.select_devices(self.catalog)
        reports = [report_fixture(self.binding, item) for item in devices]
        self.assertTrue(lab.validate_reports([reports[0], copy.deepcopy(reports[0]), reports[1], copy.deepcopy(reports[1])], self.binding, devices))
        with self.assertRaisesRegex(lab.Blocked, "EXACT_TWO_REAL_DEVICE_EVIDENCE_REPORTS_REQUIRED"):
            lab.validate_reports([reports[0], copy.deepcopy(reports[0])], self.binding, devices)
        for field in ("transport_roundtrip", "revocation_verified", "clock_crud_verified"):
            changed = copy.deepcopy(reports)
            changed[0][field] = False
            with self.subTest(field=field):
                self.assertFalse(lab.validate_reports(changed + [copy.deepcopy(changed[0])], self.binding, devices))
        changed = copy.deepcopy(reports[0])
        changed["binding"]["run_attempt"] = "2"
        with self.assertRaisesRegex(lab.Blocked, "OEM_EVIDENCE_RUN_PROVENANCE_MISMATCH"):
            lab.validate_reports(reports + [changed], self.binding, devices)
        changed = copy.deepcopy(reports[0])
        changed["frames"][0]["phase"] = "fixture-conflict"
        with self.assertRaisesRegex(lab.Blocked, "CONFLICTING_OEM_EVIDENCE_REPORTS"):
            lab.validate_reports(reports + [changed], self.binding, devices)

    def test_archive_collisions_count_member_and_expansion_bounds(self):
        cases = [([("same.log", b"first"), ("same.log", b"second")], "UNSAFE_RESULT_ARCHIVE_MEMBER"), ([("node", b"file"), ("node/", b"")], "UNSAFE_RESULT_ARCHIVE_MEMBER"), ([("node", b"file"), ("node/child.log", b"text")], "RESULT_ARCHIVE_PATH_COLLISION"), ([("node/child.log", b"text"), ("node", b"file")], "RESULT_ARCHIVE_PATH_COLLISION"), ([("node//", b"")], "UNSAFE_RESULT_ARCHIVE_MEMBER"), ([(f"entry-{index}.log", b"") for index in range(101)], "RESULT_ARCHIVE_ENTRY_BOUND_EXCEEDED")]
        for entries, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(lab.Blocked, reason):
                lab.archived_reports(archive_fixture(entries), self.test_args["adminToken"])
        data = archive_fixture([("oversized.log", b"text")])
        for size in (32 * 1024 * 1024 + 1, 0xffffffff):
            corrupted = bytearray(data)
            central = corrupted.index(b"PK\x01\x02")
            struct.pack_into("<I", corrupted, central + 24, size)
            with self.subTest(size=size), self.assertRaisesRegex(lab.Blocked, "RESULT_ARCHIVE_EXPANSION_BOUND_EXCEEDED"):
                lab.archived_reports(bytes(corrupted), self.test_args["adminToken"])
        data = bytearray(archive_fixture([(f"fixture-{index}.bin", b"x") for index in range(5)]))
        position = 0
        for index in range(5):
            position = data.index(b"PK\x01\x02", position)
            struct.pack_into("<I", data, position + 24, 32 * 1024 * 1024)
            position += 4
        with self.assertRaisesRegex(lab.Blocked, "RESULT_ARCHIVE_EXPANSION_BOUND_EXCEEDED"):
            lab.archived_reports(bytes(data), self.test_args["adminToken"])
        data = bytearray(archive_fixture([("physical.json", b"{}")]))
        position = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, position + 24, 4 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(lab.Blocked, "OEM_REPORT_ARCHIVE_BOUND_EXCEEDED"):
            lab.archived_reports(bytes(data), self.test_args["adminToken"])

    def test_archive_crc_checks_omitted_binary_and_never_writes_raw_on_failure(self):
        api = FixtureApi()
        data = bytearray(archive_fixture([("credential.png", b"RAW_TEST_ONLY_SECRET")], compression=zipfile.ZIP_STORED))
        data[data.index(b"RAW_TEST_ONLY_SECRET")] ^= 1
        api.add_object("fixture/results/corrupt.zip", bytes(data))
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            with self.assertRaisesRegex(lab.Blocked, "RESULT_ARCHIVE_INVALID_OR_CRC_FAILED"):
                lab.evidence_objects(api, "fixture", output, self.test_args["adminToken"], lab.Deadline(30))
            self.assertEqual([], list(output.iterdir()))

    def test_deflated_archive_crc_and_encryption_flags_fail_closed(self):
        secret = self.test_args["adminToken"]
        source = archive_fixture([("credential.png", secret.encode())])
        for mutation, reason in (("crc", "RESULT_ARCHIVE_INVALID_OR_CRC_FAILED"), ("encrypted", "UNSAFE_RESULT_ARCHIVE_MEMBER")):
            data = bytearray(source)
            position = data.index(b"PK\x01\x02")
            if mutation == "crc":
                crc = struct.unpack_from("<I", data, position + 16)[0]
                struct.pack_into("<I", data, position + 16, crc ^ 1)
            else:
                flags = struct.unpack_from("<H", data, position + 8)[0]
                struct.pack_into("<H", data, position + 8, flags | 1)
            api = FixtureApi()
            api.add_object("fixture/results/blocked.zip", bytes(data))
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                output = Path(name)
                with self.assertRaisesRegex(lab.Blocked, reason):
                    lab.evidence_objects(api, "fixture", output, secret, lab.Deadline(30))
                self.assertEqual([], list(output.iterdir()))

    def test_redacted_key_collision_invalid_json_and_exception_body_are_not_dumped(self):
        secret = self.test_args["adminToken"]
        for archive in (False, True):
            for content, reason in ((json.dumps({secret: 1, "[synthetic-admin-redacted]": 2}).encode(), "EVIDENCE_REDACTED_KEY_COLLISION"), (b"{\"" + secret.encode() + b"\":", "EVIDENCE_JSON_INVALID")):
                api = FixtureApi()
                if archive:
                    api.add_object("fixture/results/report.zip", archive_fixture([("physical.json", content)]))
                else:
                    api.add_object("fixture/results/physical.json", content)
                with self.subTest(archive=archive, reason=reason), tempfile.TemporaryDirectory() as name:
                    output = Path(name)
                    with self.assertRaisesRegex(lab.Blocked, reason) as caught:
                        lab.evidence_objects(api, "fixture", output, secret, lab.Deadline(30))
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertEqual([], list(output.iterdir()))
        api = FixtureApi()
        with tempfile.TemporaryDirectory() as name, patch.object(lab, "free_guard", side_effect=ValueError("ARBITRARY_PRIVATE_BODY " + secret)):
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("RUNNER_INPUT_OR_EVIDENCE_INVALID: ValueError", summary["reason"])
            self.assertNotIn("ARBITRARY_PRIVATE_BODY", (output / "summary.json").read_text())
            self.assertNotIn(secret, (output / "summary.json").read_text())

    def test_archive_artifact_count_is_bounded_across_objects(self):
        api = FixtureApi()
        secret = self.test_args["adminToken"]
        data = archive_fixture([(f"fixture-{index}.log", secret.encode()) for index in range(100)])
        for index in range(11):
            api.add_object(f"fixture/results/archive-{index}.zip", data)
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            with self.assertRaisesRegex(lab.Blocked, "RESULT_EXPANDED_ARTIFACT_BOUND_EXCEEDED"):
                lab.evidence_objects(api, "fixture", output, secret, lab.Deadline(1800))
            self.assertEqual(1000, len(list(output.iterdir())))
            for path in output.iterdir():
                self.assertEqual(".log", path.suffix)
                self.assertNotIn(secret.encode(), path.read_bytes())
                self.assertEqual(b"[synthetic-admin-redacted]", path.read_bytes())

    def test_zip_signature_under_text_name_never_publishes_raw_archive(self):
        api = FixtureApi()
        secret = self.test_args["adminToken"]
        api.add_object("fixture/results/not-an-archive.txt", archive_fixture([("safe.log", secret.encode())]))
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            manifest, reports = lab.evidence_objects(api, "fixture", output, secret, lab.Deadline(30))
            self.assertEqual([], reports)
            self.assertEqual("RAW_ARCHIVE_NEVER_PUBLISHED", manifest[0]["omission_reason"])
            self.assertNotIn("local_file", manifest[0])
            self.assertEqual(["object-000-member-000.log"], [path.name for path in output.iterdir()])
            self.assertEqual(b"[synthetic-admin-redacted]", (output / "object-000-member-000.log").read_bytes())

    def test_existing_local_artifact_and_symlink_are_never_overwritten(self):
        for symlink in (False, True):
            api = FixtureApi()
            api.add_object("fixture/results/test.log", self.test_args["adminToken"].encode())
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                output = root / "evidence"
                output.mkdir()
                target = root / "target.txt"
                target.write_bytes(b"TEST_ONLY_EXISTING_CONTENT")
                path = output / "object-000.log"
                if symlink:
                    path.symlink_to(target)
                else:
                    path.write_bytes(b"TEST_ONLY_EXISTING_CONTENT")
                with self.assertRaisesRegex(lab.Blocked, "LOCAL_EVIDENCE_PATH_COLLISION"):
                    lab.evidence_objects(api, "fixture", output, self.test_args["adminToken"], lab.Deadline(30))
                self.assertEqual(b"TEST_ONLY_EXISTING_CONTENT", path.read_bytes())
                self.assertEqual(b"TEST_ONLY_EXISTING_CONTENT", target.read_bytes())

    def test_api_error_status_is_allowlisted_without_arbitrary_body_dumps(self):
        secret = self.test_args["adminToken"]
        for status in ("ARBITRARY_PRIVATE_BODY " + secret, {"token": secret}, [secret], None):
            api = lab.GoogleApi(lab.Deadline(30))
            body = json.dumps({"error": {"status": status, "message": "ARBITRARY_PRIVATE_BODY " + secret}}).encode()
            error = urllib.error.HTTPError("https://testing.googleapis.com/v1", 403, "Forbidden", {}, io.BytesIO(body))
            with self.subTest(status=status), patch.object(api, "access_token", return_value="TEST_ONLY_ACCESS_TOKEN"), patch.object(api.opener, "open", side_effect=error), self.assertRaises(lab.ApiError) as caught:
                api.request("GET", "https://testing.googleapis.com/v1")
            self.assertIn("UNSPECIFIED", str(caught.exception))
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn("ARBITRARY_PRIVATE_BODY", str(caught.exception))

    def test_evidence_private_acl_generation_and_integrity_guards_before_writes(self):
        for mutation in ("public-acl", "missing-acl", "generation", "integrity", "anonymous-readable"):
            api = FixtureApi()
            api.add_object("fixture/results/physical.json", b"{}")
            original = api.request
            def changed(method, url, body=None, **options):
                if mutation == "anonymous-readable" and options.get("authenticated") is False:
                    return b""
                result = original(method, url, body, **options)
                if "projection=full" in url:
                    if mutation == "public-acl":
                        result["acl"] = [{"entity": "allUsers", "role": "READER"}]
                    elif mutation == "missing-acl":
                        result["acl"] = []
                    elif mutation == "generation":
                        result["generation"] = "999"
                elif mutation == "integrity" and options.get("raw") and options.get("authenticated") is not False:
                    return b"[]"
                return result
            api.request = changed
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                output = Path(name)
                with self.assertRaises(lab.Blocked):
                    lab.evidence_objects(api, "fixture", output, self.test_args["adminToken"], lab.Deadline(30))
                self.assertEqual([], list(output.iterdir()))

    def test_deadline_attempts_only_exact_matrix_cancel(self):
        api = FixtureApi()
        with tempfile.TemporaryDirectory() as name, patch.object(lab, "poll_matrix", side_effect=lab.DeadlineExceeded("MATRIX_DEADLINE_EXCEEDED")), patch.object(lab, "GoogleApi", return_value=api):
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual({"testState": "CANCELLED"}, summary["deadline_cancellation"])
        cancels = [url for method, url, body in api.requests if method == "POST" and url.endswith(":cancel")]
        self.assertEqual([f"https://testing.googleapis.com/v1/projects/{lab.PROJECT}/testMatrices/fixture-matrix:cancel"], cancels)

    def test_stock_signer_and_before_after_version_are_required(self):
        devices = lab.select_devices(self.catalog)
        reports = [report_fixture(self.binding, item) for item in devices]
        for field, value in (("current_signer_sha256", []), ("version_name", ""), ("version_code", 0)):
            changed = copy.deepcopy(reports)
            changed[0]["packages"][0][field] = value
            with self.subTest(field=field), self.assertRaises(lab.Blocked):
                lab.validate_reports(changed, self.binding, devices)
        changed = copy.deepcopy(reports)
        changed[0]["packages_after"][0]["version_name"] = "fixture-changed-version"
        with self.assertRaises(lab.Blocked):
            lab.validate_reports(changed, self.binding, devices)

    def test_actual_run_validates_original_reports_before_lossy_sanitization(self):
        secret = self.test_args["adminToken"]
        cases = (("binding-arguments", "OEM_EVIDENCE_RUN_PROVENANCE_MISMATCH"), ("binding-environment", "OEM_EVIDENCE_RUN_PROVENANCE_MISMATCH"), ("version-redaction", "STOCK_PACKAGE_OR_SOURCE_PROFILE_CHANGED_DURING_TEST"), ("metadata-arguments", "STOCK_PACKAGE_OR_SOURCE_PROFILE_CHANGED_DURING_TEST"), ("metadata-environment", "STOCK_PACKAGE_OR_SOURCE_PROFILE_CHANGED_DURING_TEST"))
        for archived in (False, True):
            for mutation, reason in cases:
                api = FixtureApi()
                def transform(api, prefix):
                    name = f"{prefix}/fixture-device-0/physical.json"
                    original = json.loads(api.objects[name][1])
                    changed = copy.deepcopy(original)
                    if mutation.startswith("binding-"):
                        field = "commandLineArguments" if mutation == "binding-arguments" else "environmentVariables"
                        changed["binding"][field] = [secret]
                    elif mutation == "version-redaction":
                        changed["packages"][0]["version_name"] = secret
                        changed["packages_after"][0]["version_name"] = "[synthetic-admin-redacted]"
                    else:
                        field = "commandLineArguments" if mutation == "metadata-arguments" else "environmentVariables"
                        changed["packages"][0][field] = [secret]
                        changed["packages_after"][0][field] = ["fixture-different-original"]
                    content = json.dumps(changed).encode()
                    if archived:
                        del api.objects[name]
                        api.add_object(f"{prefix}/invalid-original.zip", archive_fixture([("device/physical.json", content)]))
                    else:
                        api.add_object(name, content)
                api.result_transform = transform
                with self.subTest(archived=archived, mutation=mutation), tempfile.TemporaryDirectory() as name:
                    code, summary, output = self.run_fixture(api, Path(name))
                    self.assertEqual(2, code)
                    self.assertEqual("BLOCKED", summary["status"])
                    self.assertEqual(reason, summary["reason"])
                    self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_rejects_duplicate_decoded_json_keys_at_every_depth(self):
        secret = self.test_args["adminToken"]
        for archived in (False, True):
            for field in ("environmentVariables", "commandLineArguments"):
                for nested in (False, True):
                    api = FixtureApi()
                    def transform(api, prefix):
                        name = f"{prefix}/fixture-device-0/physical.json"
                        report = json.loads(api.objects[name][1])
                        escaped_field = "\\u%04x" % ord(field[0]) + field[1:]
                        conflict = '"' + field + '":[' + json.dumps(secret) + '],"' + escaped_field + '":["fixture-different-original"]'
                        extra = '"diagnostic":{"nested":{' + conflict + '}}' if nested else conflict
                        content = (json.dumps(report)[:-1] + "," + extra + "}").encode()
                        if archived:
                            del api.objects[name]
                            api.add_object(f"{prefix}/ambiguous.zip", archive_fixture([("device/physical.json", content)]))
                        else:
                            api.add_object(name, content)
                    api.result_transform = transform
                    with self.subTest(archived=archived, field=field, nested=nested), tempfile.TemporaryDirectory() as name:
                        code, summary, output = self.run_fixture(api, Path(name))
                        self.assertEqual(2, code)
                        self.assertEqual("BLOCKED", summary["status"])
                        self.assertEqual("EVIDENCE_JSON_DUPLICATE_KEY", summary["reason"])
                        self.assert_emitted_artifacts_sanitized(output, summary)

    def test_original_validation_proof_is_immutable_digest_and_context_bound(self):
        devices = lab.select_devices(self.catalog)
        proofs = []
        for selected in devices:
            entry, sanitized = lab.sanitized_evidence(json.dumps(report_fixture(self.binding, selected)).encode(), "physical.json", self.test_args["adminToken"], self.binding, devices)
            proofs.append(entry["original_validation"])
            self.assertIsInstance(sanitized, dict)
        digests = {proof.source_digest for proof in proofs}
        self.assertTrue(lab.validate_original_proofs(proofs, self.binding, devices, digests))
        self.assertTrue(lab.validate_original_proofs(proofs + proofs, self.binding, devices, digests))
        for supplied, retained in ((proofs, digests | {"c" * 64}), (proofs[:-1], digests)):
            with self.subTest(supplied=supplied, retained=retained), self.assertRaisesRegex(lab.Blocked, "OEM_ORIGINAL_VALIDATION_BINDING_MISMATCH"):
                lab.validate_original_proofs(supplied, self.binding, devices, retained)
        with self.assertRaises(AttributeError):
            proofs[0].positive = False
        for binding, selected, sources in (({**self.binding, "run_id": "124"}, devices, digests), (self.binding, list(reversed(devices)), digests), (self.binding, devices, set())):
            with self.subTest(binding=binding, selected=selected, sources=sources), self.assertRaisesRegex(lab.Blocked, "OEM_ORIGINAL_VALIDATION_BINDING_MISMATCH"):
                lab.validate_original_proofs(proofs, binding, selected, sources)

    def test_actual_run_missing_original_proof_cannot_accept_retained_reports(self):
        api = FixtureApi()
        original = lab.evidence_objects
        def missing_proof(api, prefix, output, secret, deadline, binding=None, devices=None, proofs=None, publication=None):
            result = original(api, prefix, output, secret, deadline, binding, devices, proofs, publication)
            proofs.pop()
            return result
        with tempfile.TemporaryDirectory() as name, patch.object(lab, "evidence_objects", side_effect=missing_proof):
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual("OEM_ORIGINAL_VALIDATION_BINDING_MISMATCH", summary["reason"])
            self.assertEqual(2, len(summary["inventories"]))
            self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_publication_count_includes_inventory_and_reserved_summary(self):
        for last_count in (85, 86):
            api = FixtureApi()
            def transform(api, prefix):
                for index in range(10):
                    count = 100 if index < 9 else last_count
                    api.add_object(f"{prefix}/archive-{index}.zip", archive_fixture([(f"fixture-{member}.log", self.test_args["adminToken"].encode()) for member in range(count)]))
            api.result_transform = transform
            with self.subTest(last_count=last_count), tempfile.TemporaryDirectory() as name:
                code, summary, output = self.run_fixture(api, Path(name))
                if last_count == 85:
                    self.assertEqual(0, code)
                    self.assertEqual("PASSED", summary["status"])
                    self.assertEqual(lab.MAX_PUBLICATION_FILES, len([path for path in output.rglob("*") if path.is_file()]))
                else:
                    self.assertEqual(2, code)
                    self.assertEqual("BLOCKED", summary["status"])
                    self.assertEqual("RESULT_EXPANDED_ARTIFACT_BOUND_EXCEEDED", summary["reason"])
                self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_aggregate_bytes_reserve_summary_and_inventory(self):
        self.assertEqual(32 * 1024 * 1024, lab.MAX_ARTIFACT_BYTES)
        self.assertEqual(128 * 1024 * 1024, lab.MAX_PUBLICATION_BYTES)
        for margin, expected_code in ((lab.SUMMARY_RESERVE_BYTES, 0), (1024, 2)):
            api = FixtureApi()
            def transform(api, prefix):
                content = b"x" * (lab.MAX_ARTIFACT_BYTES - margin - len(self.test_args["adminToken"])) + self.test_args["adminToken"].encode()
                data = archive_fixture([("near-boundary.log", content)])
                for index in range(4):
                    api.add_object(f"{prefix}/large-{index}.zip", data)
            api.result_transform = transform
            with self.subTest(margin=margin), tempfile.TemporaryDirectory() as name:
                code, summary, output = self.run_fixture(api, Path(name))
                self.assertEqual(expected_code, code)
                self.assertEqual("PASSED" if expected_code == 0 else "BLOCKED", summary["status"])
                if expected_code:
                    self.assertEqual("RESULT_EXPANDED_ARTIFACT_BOUND_EXCEEDED", summary["reason"])
                else:
                    self.assertGreater(sum(path.stat().st_size for path in output.rglob("*") if path.is_file()), lab.MAX_PUBLICATION_BYTES - 5 * lab.SUMMARY_RESERVE_BYTES)
                self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_oversized_summary_blocks_finally_and_references_retained_inventory(self):
        api = FixtureApi()
        def transform(api, prefix):
            api.matrix["diagnostic"] = "x" * lab.MAX_ARTIFACT_BYTES + self.test_args["adminToken"]
        api.result_transform = transform
        with tempfile.TemporaryDirectory() as name:
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual("SUMMARY_PUBLICATION_BOUND_EXCEEDED", summary["reason"])
            self.assertNotIn("diagnostic", summary)
            self.assertNotIn("device_reports", summary)
            self.assertLessEqual((output / "summary.json").stat().st_size, lab.SUMMARY_RESERVE_BYTES)
            self.assertEqual(2, len(summary["inventories"]))
            self.assert_emitted_artifacts_sanitized(output, summary)
            for reference in summary["inventories"]:
                inventory = json.loads((output / reference["local_file"]).read_bytes())
                metadata, original = api.objects[inventory["name"]]
                self.assertEqual(metadata["generation"], inventory["generation"])
                self.assertEqual(metadata["md5Hash"], inventory["md5Hash"])
                self.assertEqual(hashlib.sha256(original).hexdigest(), inventory["source_sha256"])

    def test_actual_run_failed_evidence_batch_keeps_only_inventoried_publication(self):
        api = FixtureApi()
        def transform(api, prefix):
            api.add_object(f"{prefix}/interrupted.zip", archive_fixture([("first.log", self.test_args["adminToken"].encode()), ("second.log", b"fixture")]))
        api.result_transform = transform
        original = lab.write_evidence
        def interrupted(output, filename, entry, secret, publication=None):
            if filename.endswith("member-001.log"):
                raise lab.Blocked("FIXTURE_PUBLICATION_INTERRUPTED")
            return original(output, filename, entry, secret, publication)
        with tempfile.TemporaryDirectory() as name, patch.object(lab, "write_evidence", side_effect=interrupted):
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual("FIXTURE_PUBLICATION_INTERRUPTED", summary["reason"])
            self.assertEqual(2, len(summary["evidence"]))
            self.assertEqual(2, len(summary["inventories"]))
            self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_failed_second_write_and_unlink_never_publish_uninventoried_files(self):
        api = FixtureApi()
        secret = self.test_args["adminToken"]
        def transform(api, prefix):
            api.add_object(f"{prefix}/interrupted.zip", archive_fixture([("first.log", secret.encode()), ("second.log", ("fixture " + secret).encode())]))
        api.result_transform = transform
        original_open = Path.open
        original_unlink = Path.unlink
        cleanup_failures = []
        partial_files = []
        @contextmanager
        def interrupted_open(path, *args, **kwargs):
            with original_open(path, *args, **kwargs) as stream:
                if args == ("xb",) and path.name == "object-002-member-001.log":
                    def failed_write(content):
                        stream.write(content[:7])
                        partial_files.append(path)
                        raise OSError("FIXTURE_WRITE_FAILED " + secret)
                    yield SimpleNamespace(write=failed_write)
                else:
                    yield stream
        def failed_unlink(path, *args, **kwargs):
            if path.parent.name.startswith(".phonebridge-publication-") and path.name.startswith("object-002-member-"):
                cleanup_failures.append(path)
                raise OSError("FIXTURE_UNLINK_FAILED " + secret)
            return original_unlink(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as name:
            with patch.object(Path, "open", interrupted_open), patch.object(Path, "unlink", failed_unlink):
                code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual("RUNNER_INPUT_OR_EVIDENCE_INVALID: OSError", summary["reason"])
            self.assertEqual(1, len(partial_files))
            self.assertEqual(2, len(cleanup_failures))
            self.assertEqual(b"fixture", partial_files[0].read_bytes())
            self.assertTrue(all(path.is_file() and not path.is_relative_to(output) and path.parent.parent == output.parent for path in cleanup_failures))
            self.assertTrue(all(secret.encode() not in path.read_bytes() for path in cleanup_failures))
            self.assertFalse((output / "batch-002").exists())
            self.assertEqual(2, len(summary["evidence"]))
            self.assertEqual(2, len(summary["inventories"]))
            self.assert_emitted_artifacts_sanitized(output, summary)
            for reference, source in zip(summary["inventories"], summary["evidence"]):
                inventory = json.loads((output / reference["local_file"]).read_bytes())
                self.assertEqual(source, inventory)
                metadata, raw = api.objects[inventory["name"]]
                self.assertEqual(metadata["generation"], inventory["generation"])
                self.assertEqual(metadata["md5Hash"], inventory["md5Hash"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), inventory["source_sha256"])
                self.assertEqual(len(raw), inventory["size"])
                self.assertEqual(Path(reference["local_file"]).parent, Path(inventory["local_file"]).parent)

    def test_actual_run_summary_aggregate_overflow_uses_reserved_bounded_failure(self):
        api = FixtureApi()
        def transform(api, prefix):
            api.matrix["diagnostic"] = "x" * (90 * 1024) + self.test_args["adminToken"]
            for index in range(2):
                api.add_object(f"{prefix}/large-{index}.log", b"x" * (20 * 1024))
        api.result_transform = transform
        with tempfile.TemporaryDirectory() as name, patch.object(lab, "MAX_PUBLICATION_BYTES", 128 * 1024):
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual("SUMMARY_PUBLICATION_BOUND_EXCEEDED", summary["reason"])
            self.assertLessEqual((output / "summary.json").stat().st_size, lab.SUMMARY_RESERVE_BYTES)
            self.assertEqual(4, len(summary["inventories"]))
            self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_original_native_provenance_and_positive_predicates(self):
        mutations = (("physical", False), ("device", "foreign-device"), ("manufacturer", "foreign-manufacturer"), ("sdk", 35), ("signer", []), ("frames", []), ("transport_roundtrip", False), ("revocation_verified", False), ("clock_crud_verified", False))
        for archived in (False, True):
            for field, value in mutations:
                api = FixtureApi()
                def transform(api, prefix):
                    name = f"{prefix}/fixture-device-0/physical.json"
                    report = json.loads(api.objects[name][1])
                    if field in {"device", "manufacturer", "sdk"}:
                        report["device"][field] = value
                    elif field == "signer":
                        report["packages"][0]["current_signer_sha256"] = value
                        report["packages_after"][0]["current_signer_sha256"] = value
                    else:
                        report[field] = value
                    report["diagnostic"] = self.test_args["adminToken"]
                    content = json.dumps(report).encode()
                    if archived:
                        del api.objects[name]
                        api.add_object(f"{prefix}/negative.zip", archive_fixture([("device/physical.json", content)]))
                    else:
                        api.add_object(name, content)
                api.result_transform = transform
                with self.subTest(archived=archived, field=field), tempfile.TemporaryDirectory() as name:
                    code, summary, output = self.run_fixture(api, Path(name))
                    positive_failure = field in {"transport_roundtrip", "revocation_verified", "clock_crud_verified"}
                    self.assertEqual(1 if positive_failure else 2, code)
                    self.assertEqual("FAILED" if positive_failure else "BLOCKED", summary["status"])
                    self.assert_emitted_artifacts_sanitized(output, summary)

    def test_actual_run_final_summary_sanitation_failure_never_returns_pass(self):
        api = FixtureApi()
        secret = self.test_args["adminToken"]
        original = lab.redacted
        def missed_tuple(value, token, depth=0):
            if isinstance(value, dict) and "tool_results" in value:
                return {**original(value, token, depth), "missed-token": secret}
            return original(value, token, depth)
        with tempfile.TemporaryDirectory() as name, patch.object(lab, "redacted", side_effect=missed_tuple):
            code, summary, output = self.run_fixture(api, Path(name))
            self.assertEqual(2, code)
            self.assertEqual("BLOCKED", summary["status"])
            self.assertEqual("SUMMARY_SANITIZATION_OR_SERIALIZATION_FAILED", summary["reason"])
            self.assert_emitted_artifacts_sanitized(output, summary)

    def test_no_bearer_redirect_or_arbitrary_host(self):
        api = lab.GoogleApi(lab.Deadline(30))
        with patch.object(api, "access_token", side_effect=AssertionError("No credentials for refused destination")):
            for url in ("http://testing.googleapis.com/v1", "https://fixture-attacker.example/v1", "https://testing.googleapis.com:8443/v1", "https://user@testing.googleapis.com/v1"):
                with self.subTest(url=url), self.assertRaises(lab.Blocked):
                    api.request("GET", url)
        self.assertIsNone(lab.NoRedirect().redirect_request(None, None, 302, None, None, "https://fixture-attacker.example"))


if __name__ == "__main__":
    unittest.main()
