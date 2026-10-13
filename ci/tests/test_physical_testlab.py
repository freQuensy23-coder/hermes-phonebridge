import base64
import copy
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.parse
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
                    self.matrix["testExecutions"].append({"projectId": lab.PROJECT, "matrixId": "fixture-matrix", "state": "FINISHED", "environment": {"androidDevice": selected["device"]}, "toolResultsStep": {"projectId": lab.PROJECT, "historyId": "fixture-history", "executionId": "fixture-execution", "stepId": f"fixture-step-{index}"}})
                    if not (self.omit_report and index == 1):
                        report = report_fixture(binding, selected)
                        report["outcome"] = self.report_outcome
                        self.add_object(f"{prefix}/fixture-device-{index}/physical.json", json.dumps(report).encode())
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
        code = lab.run(args, api, lab.Deadline(1800), self.binding, self.test_args, output)
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
        self.assertEqual({"fixture_only": True}, lab.archived_reports(stream.getvalue())[0]["report"])
        for name in ("../physical.json", "/physical.json", "fixture-only/../physical.json", "fixture-only\\physical.json"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr(name, "{}")
            with self.subTest(name=name), self.assertRaises(lab.Blocked):
                lab.archived_reports(stream.getvalue())
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            entry = zipfile.ZipInfo("fixture-only/physical.json")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "fixture-target")
        with self.assertRaises(lab.Blocked):
            lab.archived_reports(stream.getvalue())

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

    def test_no_bearer_redirect_or_arbitrary_host(self):
        api = lab.GoogleApi(lab.Deadline(30))
        with patch.object(api, "access_token", side_effect=AssertionError("No credentials for refused destination")):
            for url in ("http://testing.googleapis.com/v1", "https://fixture-attacker.example/v1", "https://testing.googleapis.com:8443/v1", "https://user@testing.googleapis.com/v1"):
                with self.subTest(url=url), self.assertRaises(lab.Blocked):
                    api.request("GET", url)
        self.assertIsNone(lab.NoRedirect().redirect_request(None, None, 302, None, None, "https://fixture-attacker.example"))


if __name__ == "__main__":
    unittest.main()
