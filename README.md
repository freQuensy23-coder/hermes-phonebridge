# Hermes PhoneBridge

Android companion for cloud-hosted agents. The server generates a short-lived QR; the app scans it and opens an authenticated outbound connection. CLI and Hermes skill expose permission-aware native phone actions and explicit screen-dependent automation.

Targets modern Samsung and Pixel. Existing clock applications remain in use. No own clock subsystem or mandatory root/ADB. Android permissions are granted once by the phone owner. A background connection does not bypass screen lock or Android capability restrictions.

Implementation and device verification are in progress. See [current contract](docs/contract-v1.md). CI/CD and release links will be documented after real execution.
