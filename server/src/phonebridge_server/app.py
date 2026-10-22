import asyncio
import hashlib
import hmac
import os
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from urllib.parse import urlsplit

from anyio import CancelScope
from fastapi import Depends, FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from . import __version__
from .config import Settings
from .protocol import (
    IMAGE_SIGNATURES,
    TOKEN_PATTERN,
    BridgeError,
    ClaimRequest,
    CommandRequest,
    EventFrame,
    HelloFrame,
    PairingRequest,
    ResultFrame,
    decode_json,
    upload_metadata,
)
from .store import Store
from .transport import Hub, Peer, RateLimiter, RequestBoundary


def bearer(headers):
    values = headers.getlist("authorization")
    if len(values) != 1:
        raise BridgeError("UNAUTHORIZED", "Bearer authorization required", 401)
    parts = values[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or len(parts[1]) > 512:
        raise BridgeError("UNAUTHORIZED", "Bearer authorization required", 401)
    return parts[1]


def create_app(settings: Settings | None = None, clock=time.time):
    settings = settings or Settings.from_env()
    limiter = RateLimiter(settings.max_rate_keys)
    hub = Hub(settings)

    async def maintenance(store):
        while True:
            await asyncio.sleep(settings.maintenance_interval)
            try:
                await asyncio.to_thread(store.maintain)
            except sqlite3.OperationalError:
                continue

    @asynccontextmanager
    async def lifespan(application):
        store = await asyncio.to_thread(Store, settings, clock)
        application.state.store = store
        try:
            await asyncio.to_thread(store.maintain)
            maintenance_task = asyncio.create_task(maintenance(store))
            try:
                yield
            finally:
                await hub.close()
                maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance_task
        finally:
            await asyncio.to_thread(store.close)

    application = FastAPI(title="PhoneBridge", version=__version__, lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.state.settings = settings
    application.state.hub = hub
    application.add_middleware(RequestBoundary, settings=settings, limiter=limiter)

    async def admin(request: Request):
        token = bearer(request.headers)
        if any(key.lower() in {"token", "access_token", "authorization", "admin_token", "device_token"}
               for key in request.query_params):
            raise BridgeError("UNAUTHORIZED", "Query-string authorization is not accepted", 401)
        if not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), settings.admin_hash):
            raise BridgeError("UNAUTHORIZED", "Bearer authorization required", 401)

    async def execute(method, *args):
        store = application.state.store
        return await asyncio.to_thread(getattr(store, method), *args)

    @application.exception_handler(BridgeError)
    async def bridge_error(request, error):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse(error.payload(), status_code=error.status, headers=headers)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"error": {"code": "VALIDATION_ERROR", "message": "Request does not match the contract"}},
                            status_code=422)

    @application.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": {"code": "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR",
                                       "message": "Request could not be served"}}, status_code=error.status_code)

    @application.exception_handler(sqlite3.OperationalError)
    async def database_busy(request, error):
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
            return JSONResponse({"error": {"code": "QUEUE_FULL", "message": "Database storage quota reached"}},
                                status_code=429, headers={"Retry-After": "60"})
        return JSONResponse({"error": {"code": "UNAVAILABLE", "message": "Database temporarily unavailable"}},
                            status_code=503, headers={"Retry-After": "1"})

    @application.get("/healthz")
    async def health():
        await execute("health")
        return {"status": "ok", "version": __version__, "revision": settings.revision}

    @application.post("/v1/pairings", dependencies=[Depends(admin)])
    async def create_pairing(body: PairingRequest):
        return await execute("create_pairing", body)

    @application.get("/v1/pairings/{pairing_id}", dependencies=[Depends(admin)])
    async def get_pairing(pairing_id: str):
        return await execute("pairing", pairing_id)

    @application.delete("/v1/pairings/{pairing_id}", dependencies=[Depends(admin)], status_code=204)
    async def revoke_pairing(pairing_id: str):
        device_id = await execute("revoke_pending_pairing", pairing_id)
        if device_id:
            await hub.revoke(device_id, lambda: execute("revoke_pairing", pairing_id))
        return Response(status_code=204)

    @application.post("/v1/pair/claim")
    async def claim_pairing(body: ClaimRequest):
        return await execute("claim", body)

    @application.get("/v1/devices", dependencies=[Depends(admin)])
    async def list_devices():
        return await execute("devices")

    @application.get("/v1/devices/{device_id}", dependencies=[Depends(admin)])
    async def get_device(device_id: str):
        return await execute("device", device_id)

    @application.delete("/v1/devices/{device_id}", dependencies=[Depends(admin)], status_code=204)
    async def revoke_device(device_id: str):
        await hub.revoke(device_id, lambda: execute("revoke_device", device_id))
        return Response(status_code=204)

    @application.post("/v1/devices/{device_id}/commands", dependencies=[Depends(admin)])
    async def create_command(device_id: str, body: CommandRequest):
        return await execute("enqueue", device_id, body)

    @application.get("/v1/commands/{command_id}", dependencies=[Depends(admin)])
    async def get_command(command_id: str):
        return await execute("command", command_id)

    @application.post("/v1/devices/{device_id}/files", dependencies=[Depends(admin)])
    async def upload_file(device_id: str, request: Request):
        metadata = upload_metadata(request.scope.get("query_string", b""), request.headers)
        body = await request.body()
        if hashlib.sha256(body).hexdigest() != metadata["sha256"]:
            raise BridgeError("VALIDATION_ERROR", "X-Content-SHA256 does not match the uploaded bytes", 422)
        metadata["size"] = len(body)
        metadata["signature_ok"] = metadata["kind"] != "image" or IMAGE_SIGNATURES[metadata["mime"]](body)
        store = application.state.store

        def stage():
            descriptor, path = tempfile.mkstemp(prefix="upload-", dir=store.transfers)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                os.unlink(path)
                raise
            return path

        staged = await asyncio.to_thread(stage)
        return await execute("create_transfer", device_id, metadata, staged)

    @application.get("/v1/device/files/{transfer_id}")
    async def download_file(transfer_id: str, request: Request):
        if request.url.query:
            raise BridgeError("UNAUTHORIZED", "Query-string authorization is not accepted", 401)
        token = bearer(request.headers)
        if not TOKEN_PATTERN.fullmatch(token):
            raise BridgeError("UNAUTHORIZED", "Device bearer authorization required", 401)
        handle = await execute("open_transfer", token, transfer_id)
        size = os.fstat(handle.fileno()).st_size

        async def chunks():
            try:
                while chunk := await asyncio.to_thread(handle.read, 65536):
                    yield chunk
            finally:
                handle.close()

        return StreamingResponse(chunks(), media_type="application/octet-stream",
                                 headers={"Content-Length": str(size)})

    @application.get("/v1/devices/{device_id}/events", dependencies=[Depends(admin)])
    async def get_events(device_id: str, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
                         limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return await execute("events", device_id, after, limit)

    async def read_frame(websocket, timeout):
        message = await asyncio.wait_for(websocket.receive(), timeout)
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        raw = message.get("text")
        if raw is None:
            raise BridgeError("INVALID_FRAME", "Only JSON text frames are supported", 422)
        if len(raw.encode()) > settings.max_frame_bytes:
            raise BridgeError("TOO_LARGE", "Incoming frame limit exceeded", 413)
        try:
            value = decode_json(raw, settings.max_frame_bytes, settings.max_json_bytes)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise BridgeError("INVALID_FRAME", "Invalid or unbounded JSON frame", 422) from error
        if not isinstance(value, dict):
            raise BridgeError("INVALID_FRAME", "Frame must be an object", 422)
        return value

    async def receiver(peer, device_id, session_id):
        while not peer.closed:
            try:
                value = await read_frame(peer.websocket, settings.idle_timeout)
            except TimeoutError:
                await execute("touch", device_id, session_id)
                continue
            if not limiter.take(("frames", device_id), settings.websocket_rate):
                raise BridgeError("RATE_LIMITED", "Frame rate limit exceeded", 429)
            try:
                kind = value.get("type")
                if kind == "hello":
                    await execute("hello", device_id, session_id, HelloFrame.model_validate(value))
                elif kind == "result":
                    await execute("result", device_id, session_id, ResultFrame.model_validate(value))
                elif kind == "event":
                    ack = await execute("event", device_id, session_id, EventFrame.model_validate(value))
                    peer.enqueue(ack)
                else:
                    raise BridgeError("INVALID_FRAME", "Unsupported frame type", 422)
            except ValidationError as error:
                raise BridgeError("INVALID_FRAME", "Frame does not match the contract", 422) from error

    async def sender(peer, device_id, session_id):
        sent = set()
        while not peer.closed:
            await execute("touch", device_id, session_id)
            for _ in range(settings.outbound_queue_size):
                try:
                    value = peer.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await peer.send(value)
                finally:
                    peer.queue.task_done()
            pending = await execute("pending", device_id, session_id)
            sent.intersection_update(pending)
            batch = [command_id for command_id in pending if command_id not in sent][:settings.command_batch_size]
            for command_id in batch:
                frame = await execute("dispatch", device_id, session_id, command_id)
                if frame is not None:
                    await execute("touch", device_id, session_id)
                    await peer.send(frame)
                    sent.add(command_id)
            await asyncio.sleep(settings.poll_interval)

    @application.websocket("/v1/device/ws")
    async def device_socket(websocket: WebSocket):
        device_id = session_id = peer = None
        tasks = []
        address = websocket.client.host if websocket.client else "unknown"
        try:
            if websocket.url.query:
                raise BridgeError("UNAUTHORIZED", "Query-string authorization is not accepted", 401)
            token = bearer(websocket.headers)
            origins = websocket.headers.getlist("origin")
            public = urlsplit(settings.public_url)
            if (not TOKEN_PATTERN.fullmatch(token) or len(origins) > 1
                    or (origins and origins[0] != f"{public.scheme}://{public.netloc}")):
                raise BridgeError("UNAUTHORIZED", "Device bearer authorization required", 401)
        except BridgeError:
            limiter.take(("denied", address), settings.claim_rate)
            with suppress(RuntimeError, OSError):
                await websocket.close(code=1008)
            return
        peer = Peer.create(websocket, settings)
        try:
            await asyncio.wait_for(websocket.accept(), settings.send_timeout)
            try:
                device_id = await execute("authorize", token)
            except BridgeError as error:
                if error.code != "UNAUTHORIZED":
                    raise
                if limiter.take(("denied", address), settings.claim_rate):
                    await peer.revoke()
                else:
                    await peer.close(1013)
                return
            if not limiter.take(("connect", device_id), settings.claim_rate):
                raise BridgeError("RATE_LIMITED", "Reconnecting too often; retry later", 429)
            device_id, session_id = await hub.connect(token, peer, execute)
            if peer.closed:
                raise BridgeError("AUTH_REVOKED", "Connection is closed", 401)
            await hub.add(device_id, session_id, peer, execute)
            await hub.check_current(device_id, session_id, peer, execute)
            first = await read_frame(websocket, settings.hello_timeout)
            try:
                hello = HelloFrame.model_validate(first)
            except ValidationError as error:
                raise BridgeError("INVALID_FRAME", "A valid hello must be the first frame", 422) from error
            await execute("hello", device_id, session_id, hello)
            tasks = [asyncio.create_task(sender(peer, device_id, session_id)),
                     asyncio.create_task(receiver(peer, device_id, session_id))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except BridgeError as error:
            if error.code == "UNAUTHORIZED":
                await peer.revoke()
            else:
                if not peer.closed:
                    with suppress(RuntimeError, WebSocketDisconnect, TimeoutError, OSError, BridgeError):
                        await peer.send({"type": "error", "code": error.code, "message": error.message})
                code = 1013 if error.code in {"QUEUE_FULL", "RATE_LIMITED"} else 1009 if error.code == "TOO_LARGE" else 1008
                await peer.close(code)
        except TimeoutError:
            await peer.close(1013)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        except sqlite3.Error:
            if not peer.closed:
                with suppress(RuntimeError, WebSocketDisconnect, TimeoutError, OSError, BridgeError):
                    await peer.send({"type": "error", "code": "UNAVAILABLE", "message": "Server storage temporarily unavailable"})
            await peer.close(1011)
        finally:
            with CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if peer.device_id is not None:
                    device_id, session_id = peer.device_id, peer.session_id
                if device_id is not None and session_id is not None:
                    await hub.remove(device_id, session_id, peer)
                await peer.close(1000)
                if device_id is not None and session_id is not None:
                    await execute("disconnect", device_id, session_id)

    return application
