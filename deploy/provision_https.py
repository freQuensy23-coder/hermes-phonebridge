import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse


CONFIG = Path.home() / ".config/phonebridge/server.env"
SITE = Path("/etc/nginx/sites-available/phonebridge")
ENABLED = Path("/etc/nginx/sites-enabled/phonebridge")


def run(*args):
    return subprocess.run(args, check=True)


def install_config(content):
    scratch = Path.home() / ".hermes/cache/scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=scratch, delete=False) as handle:
        handle.write(content)
        filename = handle.name
    try:
        run("sudo", "-n", "install", "-m", "644", filename, str(SITE))
        if not ENABLED.exists():
            run("sudo", "-n", "ln", "-s", str(SITE), str(ENABLED))
        run("sudo", "-n", "nginx", "-t")
        run("sudo", "-n", "systemctl", "reload", "nginx")
    finally:
        os.unlink(filename)


def main():
    settings = dict(line.strip().split("=", 1) for line in CONFIG.read_text().splitlines() if "=" in line)
    address = urlparse(settings["PHONEBRIDGE_PUBLIC_URL"])
    domain = address.hostname
    if address.scheme != "https" or not domain or not re.fullmatch(r"[a-z0-9.-]+", domain) or address.path not in ("", "/") or address.port not in (None, 443):
        raise RuntimeError("Invalid public HTTPS origin")
    certificate = Path("/etc/letsencrypt/live") / domain / "fullchain.pem"
    http = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/www/letsencrypt; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
"""
    if not certificate.exists():
        install_config(http)
        run("sudo", "-n", "certbot", "certonly", "--webroot", "-w", "/var/www/letsencrypt", "-d", domain, "--non-interactive", "--agree-tos", "--register-unsafely-without-email")
    https = f"""server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    client_max_body_size 256k;
    location = /healthz {{
        limit_except GET {{ deny all; }}
        proxy_pass http://127.0.0.1:18940;
    }}
    location = /v1/pair/claim {{
        limit_except POST {{ deny all; }}
        proxy_pass http://127.0.0.1:18940;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }}
    location = /v1/device/ws {{
        proxy_pass http://127.0.0.1:18940;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }}
    location / {{ return 403; }}
}}
"""
    install_config(http + https)


if __name__ == "__main__":
    main()
