# CI

`.github/workflows/ci.yml` runs on pushes to `main`, `feat/**`, `fix/**`, `ci/**` and on pull requests:

- `security`: pip-audit of runtime dependencies and zizmor workflow audit (`classify_workflow_audit.py`).
- `python`: ruff, all Python tests, wheel builds.
- `android-build`: unit tests, debug/release/test APKs.
- `android-e2e (31|35)` and `android-e2e (31|35) part 2`: each API level runs the instrumentation classes split in two parts on separate emulators. Each job starts the real server behind a Cloudflare quick tunnel (`prepare_emulator_server.py`), boots an emulator and runs every instrumentation class through `run_android_instrumentation.sh`: QR pairing from a CLI-generated PNG, the WebSocket lifecycle and revocation, durable result replay, screen reads, gestures, text input and screenshots against the `debugfixture` app, and file delivery into MediaStore.
- On `main` only: `release-input`, `deployment-artifact` and `offline-runtime` package the exact CI outputs for `release.yml` and `deploy.yml`.

Run the same Python tests locally with `python -m pytest server/tests cli/tests deploy/tests ci/tests release/tests` and the Android unit tests with `cd android && ./gradlew testDebugUnitTest`.
