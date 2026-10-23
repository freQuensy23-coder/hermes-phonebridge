# Android owner interface

These are unedited Android 12 (API 31) emulator screenshots of the production native `OwnerUi` views, captured by `OwnerUiTourTest` from code commit `8fd5cd25bf7c833ca2e456381d5c0ed25362937d`.

Capture: [successful CI run 39806147398](https://github.com/freQuensy23-coder/hermes-phonebridge/actions/runs/39806147398), artifact `emulator-api31-8fd5cd25bf7c833ca2e456381d5c0ed25362937d` → `_temp/screenshots/phonebridge-redesign/`.

The server name and connection states are synthetic fixtures. No personal server or valid token is shown. This isolated test clears the production window's screenshot protection only for these fixtures. A separate end-to-end tour in the same CI run exercises real pairing, HTTPS/WebSocket transport, screen actions and file delivery on API 31 and 35.

| File | State |
| --- | --- |
| `01-setup.png` | First connection; code or QR |
| `02-connected.png` | Connected; Accessibility ready; screen access off |
| `03-screen-active.png` | Timed screen access with a stop action |
| `04-paused.png` | Connection paused; remote controls disabled |
