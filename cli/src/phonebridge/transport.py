import http.client
import ipaddress
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from socket import socket as create_socket


MAX_ADDRESSES = 16


class Deadline:
    def __init__(self, budget, clock, expires_at=None):
        self.clock = clock
        now = clock()
        self.expires_at = min(now + budget, expires_at) if expires_at is not None else now + budget
        self.lock = threading.Lock()
        self.active_socket = None
        self.expired = False
        self.timer = threading.Timer(max(0, self.expires_at - now), self.abort)
        self.timer.daemon = True
        self.timer.start()

    def remaining(self):
        with self.lock:
            remaining = self.expires_at - self.clock()
            if self.expired or remaining <= 0:
                raise TimeoutError("Request deadline elapsed")
            return remaining

    def track(self, active_socket):
        with self.lock:
            self.active_socket = active_socket
            expired = self.expired or self.expires_at <= self.clock()
        if expired:
            self.abort()
            raise TimeoutError("Request deadline elapsed")

    def abort(self):
        with self.lock:
            self.expired = True
            active_socket = self.active_socket
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active_socket.close()

    def close(self):
        self.timer.cancel()
        self.timer.join()


def resolve_addresses(host, port, deadline):
    deadline.remaining()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        target = (str(address), port, 0, 0) if address.version == 6 else (str(address), port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, target)]
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).with_name("_resolve.py")), host, str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        try:
            output, _ = process.communicate(timeout=deadline.remaining())
        except subprocess.TimeoutExpired:
            raise TimeoutError("Name resolution deadline elapsed") from None
        deadline.remaining()
        if process.returncode != 0 or len(output) > 16384:
            raise OSError("Name resolution failed")
        addresses = json.loads(output)
        if not addresses or len(addresses) > MAX_ADDRESSES:
            raise OSError("Name resolution returned no bounded addresses")
        return [(family, kind, protocol, tuple(target)) for family, kind, protocol, target in addresses]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def open_connection(parsed, deadline):
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=deadline.remaining())
    connection.auto_open = 0
    try:
        addresses = resolve_addresses(connection.host, connection.port, deadline)
        for family, kind, protocol, address in addresses[:MAX_ADDRESSES]:
            deadline.remaining()
            active_socket = create_socket(family, kind, protocol)
            deadline.track(active_socket)
            try:
                active_socket.settimeout(deadline.remaining())
                active_socket.connect(address)
                if parsed.scheme == "https":
                    deadline.remaining()
                    active_socket = connection._context.wrap_socket(
                        active_socket, server_hostname=connection.host, do_handshake_on_connect=False,
                    )
                    deadline.track(active_socket)
                    active_socket.settimeout(deadline.remaining())
                    active_socket.do_handshake()
                active_socket.settimeout(deadline.remaining())
                connection.sock = active_socket
                return connection
            except BaseException as error:
                active_socket.close()
                if not isinstance(error, OSError):
                    raise
                deadline.remaining()
        raise OSError("Connection attempts failed")
    except BaseException:
        connection.close()
        raise
