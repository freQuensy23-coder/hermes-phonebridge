# Hermes PhoneBridge

Lets a Hermes agent on your server operate your Android phone:

- **Screen control** through Android Accessibility: read the UI tree, take screenshots, tap, swipe, type, press back/home. Off by default; you switch it on in the app for 10 minutes, 15 minutes, 1 hour or until stopped. See [docs/screen-control.md](docs/screen-control.md).
- **File delivery**: `phonebridge files send DEVICE ./report.pdf` puts the file into `Download/PhoneBridge`, `phonebridge images send` into the gallery. See [docs/file-delivery.md](docs/file-delivery.md).

The phone connects out to the server over an authenticated WebSocket; no inbound port, root or ADB is needed. Protocol and API: [docs/contract.md](docs/contract.md). Platform limits: [docs/support-matrix.md](docs/support-matrix.md).

## Setup

1. Server: `deploy/bootstrap.py` provisions the systemd user service and `deploy/provision_https.py` the nginx site (see [deploy/README.md](deploy/README.md)).
2. CLI and skill: `pip install ./cli`, then `phonebridge install-skill` (add `--replace` to update an older skill). Set `PHONEBRIDGE_TOKEN` to the admin token.
3. Phone: install the APK, run `phonebridge pair --name 'My phone' --qr /private/path/pair.png` and scan the QR in the app, or run `phonebridge pair --name 'My phone' --code` and paste the printed code in the app. Confirm the hostname, allow notifications and grant Accessibility.

## Development

    python -m pip install -e './server[test]' -e './cli[test]'
    python -m pytest server/tests cli/tests deploy/tests ci/tests release/tests
    cd android && ./gradlew testDebugUnitTest assembleDebug

Emulator instrumentation runs in GitHub CI (`.github/workflows/ci.yml`, `debug-control.yml`).
