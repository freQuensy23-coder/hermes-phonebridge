import hashlib
import ipaddress
import re
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class Settings:
    admin_token: str = field(repr=False)
    public_url: str = "https://localhost"
    db_path: Path = Path("phonebridge.sqlite3")
    revision: str = "unknown"
    allow_insecure: bool = False
    max_body_bytes: int = 65536
    max_frame_bytes: int = 1048576
    max_json_bytes: int = 524288
    max_database_bytes: int = 2147483648
    max_connections: int = 128
    max_http_requests: int = 128
    max_devices: int = 1000
    max_pairings: int = 10000
    max_pending_commands: int = 256
    max_command_records: int = 10000
    max_events: int = 10000
    max_event_receipts: int = 50000
    max_file_bytes: int = 33554432
    max_transfer_bytes: int = 134217728
    max_device_transfers: int = 16
    outbound_queue_size: int = 32
    command_batch_size: int = 16
    http_rate: int = 240
    claim_rate: int = 10
    websocket_rate: int = 240
    max_rate_keys: int = 4096
    retention_seconds: int = 604800
    receipt_retention_seconds: int = 2592000
    hello_timeout: float = 10.0
    idle_timeout: float = 90.0
    send_timeout: float = 10.0
    request_timeout: float = 10.0
    poll_interval: float = 0.25
    maintenance_interval: float = 2.0
    online_timeout: float = 10.0

    def __post_init__(self):
        token = self.admin_token
        if len(token.encode()) < 32 or len(token) > 512 or len(set(token)) < 8:
            raise ValueError("PHONEBRIDGE_ADMIN_TOKEN must be a high-entropy token of at least 32 bytes")
        if any(character.isspace() for character in token):
            raise ValueError("PHONEBRIDGE_ADMIN_TOKEN must not contain whitespace")
        parsed = urlsplit(self.public_url)
        if parsed.scheme not in ({"https", "http"} if self.allow_insecure else {"https"}):
            raise ValueError("PHONEBRIDGE_PUBLIC_URL requires HTTPS")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("PHONEBRIDGE_PUBLIC_URL must be an absolute URL without credentials or query")
        try:
            port = parsed.port
            if port is not None and port == 0:
                raise ValueError
        except ValueError as error:
            raise ValueError("PHONEBRIDGE_PUBLIC_URL has an invalid port") from error
        if "\\" in self.public_url or any(ord(character) < 33 for character in self.public_url):
            raise ValueError("PHONEBRIDGE_PUBLIC_URL contains invalid characters")
        hostname = parsed.hostname
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            try:
                hostname = hostname.rstrip(".").encode("idna").decode("ascii")
            except UnicodeError as error:
                raise ValueError("PHONEBRIDGE_PUBLIC_URL has an invalid hostname") from error
            if len(hostname) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in hostname.split(".")
            ):
                raise ValueError("PHONEBRIDGE_PUBLIC_URL has an invalid hostname")
        if parsed.path not in ("", "/"):
            raise ValueError("PHONEBRIDGE_PUBLIC_URL must use the origin root")
        if any(character.isspace() for character in self.public_url):
            raise ValueError("PHONEBRIDGE_PUBLIC_URL must not contain whitespace")
        object.__setattr__(self, "public_url", self.public_url.rstrip("/"))
        object.__setattr__(self, "db_path", Path(self.db_path))
        for name, value in vars(self).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.command_batch_size > self.max_pending_commands:
            raise ValueError("command_batch_size exceeds the pending command limit")
        if self.max_pending_commands > self.max_command_records:
            raise ValueError("pending commands exceed the command record limit")
        if self.max_events > self.max_event_receipts:
            raise ValueError("event retention exceeds the receipt limit")

    @property
    def admin_hash(self):
        return hashlib.sha256(self.admin_token.encode()).digest()

    @property
    def ws_url(self):
        parsed = urlsplit(self.public_url)
        return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                           "/v1/device/ws", "", ""))

    @classmethod
    def from_env(cls):
        token = os.environ.get("PHONEBRIDGE_ADMIN_TOKEN", "")
        public_url = os.environ.get("PHONEBRIDGE_PUBLIC_URL", "")
        insecure = os.environ.get("PHONEBRIDGE_ALLOW_INSECURE", "false").lower()
        if insecure not in {"true", "false"}:
            raise ValueError("PHONEBRIDGE_ALLOW_INSECURE must be true or false")
        return cls(
            admin_token=token,
            public_url=public_url,
            db_path=Path(os.environ.get("PHONEBRIDGE_DB_PATH", "phonebridge.sqlite3")),
            revision=os.environ.get("PHONEBRIDGE_REVISION", "unknown"),
            allow_insecure=insecure == "true",
        )
