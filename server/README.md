# PhoneBridge server

ASGI factory `phonebridge_server.app:create_app` (Python 3.11+). Implements [../docs/contract.md](../docs/contract.md): QR pairing, the device WebSocket, the command queue, events and file delivery. It never executes anything locally; it only relays named operations the phone advertises.

Environment: `PHONEBRIDGE_ADMIN_TOKEN` (random, at least 32 bytes), `PHONEBRIDGE_PUBLIC_URL` (https origin), `PHONEBRIDGE_DB_PATH` (put it in a private writable directory; uploaded files are staged in `transfers/` next to it), `PHONEBRIDGE_REVISION` (echoed by `/healthz`), `PHONEBRIDGE_ALLOW_INSECURE=true` only for emulator CI.

Run one worker on loopback behind an HTTPS proxy that overwrites `X-Forwarded-For` with the client address:

    uvicorn phonebridge_server.app:create_app --factory --host 127.0.0.1 --port 18940 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log

Rate limits apply only to the public endpoints (claim and file download per client address; WebSocket reconnects per device and failed device authentication per client address). The device WebSocket is accepted before authorization so the phone always gets an explicit answer: `{"type":"revoked"}` with close code 4401 for an unknown or revoked token, an error frame and a non-revocation close code for everything else.

Limits: 64 KiB JSON bodies, 32 MiB file uploads, 128 MiB of pending files overall and 16 per device, 32 KiB command args, 384 KiB results, 256 undelivered commands per device. Command history (10,000 per device) and event receipts (50,000 per device) are pruned oldest first instead of blocking new work; completed commands are kept for 30 days, events for 7.

Tests: `python -m pip install -e './server[test]' && python -m pytest server/tests`.
