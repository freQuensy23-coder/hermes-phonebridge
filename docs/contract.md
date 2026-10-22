# PhoneBridge contract

PhoneBridge lets an agent on a Linux host operate the owner's Android phone. It has three parts:

- `server/`: FastAPI service on `127.0.0.1:18940` behind an HTTPS reverse proxy, SQLite state.
- `android/`: app that pairs by QR, keeps an outbound authenticated WebSocket from a foreground service, executes commands.
- `cli/` and `skills/phonebridge/SKILL.md`: the `phonebridge` command and the Hermes skill that drives it.

The MVP exposes exactly two capabilities: screen control through Android Accessibility (see [screen-control.md](screen-control.md)) and one-way file delivery to the phone (see [file-delivery.md](file-delivery.md)).

## Configuration

`PHONEBRIDGE_ADMIN_TOKEN` (high entropy, at least 32 bytes), `PHONEBRIDGE_PUBLIC_URL` (https origin), `PHONEBRIDGE_DB_PATH`, `PHONEBRIDGE_ALLOW_INSECURE` (false; only for emulator CI). Uploaded files are staged next to the database in a private `transfers/` directory.

The reverse proxy exposes only `GET /healthz`, `POST /v1/pair/claim`, `GET /v1/device/ws` and `GET /v1/device/files/{id}`. It overwrites `X-Forwarded-For` with the real client address so rate limits are per client. Admin routes are reachable only on the host.

## HTTP API

Admin routes need `Authorization: Bearer <admin token>`; credentials in query strings are refused.

- `GET /healthz` → `{status, version, revision}` (anonymous).
- `POST /v1/pairings {name, ttl_seconds≤900}` → `{id, expires_at, qr_payload:{v:1, server, token, expires_at}}`. `GET /v1/pairings/{id}` → status. `DELETE` revokes the pairing; if it was already claimed the paired device is revoked too.
- The CLI renders `qr_payload` as a QR PNG and/or as a connection code: `pb1:` followed by the unpadded base64url encoding of the same compact JSON. The app accepts either and applies the same checks (version, HTTPS origin, expiry, hostname confirmation).
- `POST /v1/pair/claim {token, name, platform:"android"}` (public, one use, rate limited per client) → `{device_id, device_token, ws_url}`.
- `GET /v1/devices`, `GET /v1/devices/{id}` → `{id, name, online, version, capabilities, permissions}`. `DELETE` revokes the device and tells a connected phone immediately.
- `POST /v1/devices/{id}/commands {op, args, ttl_seconds≤86400, idempotency_key?}` → `{id, status:"queued", expires_at}`. The op must be advertised by the device. `files.receive` cannot be created here.
- `GET /v1/commands/{id}` → `{id, device_id, op, status, data, error, expires_at}`; status is `queued`, `sent`, `succeeded`, `failed` or `expired`.
- `POST /v1/devices/{id}/files` and `GET /v1/device/files/{transfer}`: see file delivery.
- `GET /v1/devices/{id}/events?after=&limit=` → retained device events.

Errors are `{"error":{"code","message"}}`. The CLI shows both; messages are written for the agent.

## Operations

`device.info`, `capabilities`, `screen.tree`, `screen.screenshot`, `screen.tap`, `screen.swipe`, `screen.input`, `screen.global`, `files.receive`. The phone advertises only what it can do right now (screen operations only while Accessibility is granted and the owner has screen control on in the app). The server ignores unknown operations in a hello, so app and server can be upgraded independently.

## Device WebSocket

`GET /v1/device/ws` with `Authorization: Bearer <device token>`. The server accepts the socket and then authorizes it:

- unknown or revoked token: frame `{"type":"revoked"}`, close code 4401. This is the only signal on which the phone erases its pairing.
- too many reconnects from one device: error frame, close 1013. Other server problems: error frame and close 1008/1009/1011. The phone reconnects with jittered exponential backoff (up to 5 minutes) in every non-revoked case, including HTTP errors from a proxy.

Frames: phone `hello {version, capabilities, permissions}` first; server `command {id, op, args, expires_at}`; phone `result {id, ok, data, error}`; phone `event {id, name, data, time}`, server `ack {id}`. Commands that were sent but not answered are delivered again after a reconnect; the phone answers from its durable result cache and never executes a command id twice. A result that arrives after the command TTL is still recorded. Command history is pruned oldest first; only undelivered commands count against the queue limit.
