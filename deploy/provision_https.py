import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import uuid

try:
    from rollout import deployment_lock, origin, run
except ModuleNotFoundError as error:
    if error.name != "rollout":
        raise
    from deploy_entry import deployment_lock, origin, run

CONFIG = Path.home() / ".config/phonebridge/server.env"
SITE = Path("/etc/nginx/sites-available/phonebridge")
ENABLED = Path("/etc/nginx/sites-enabled/phonebridge")
SCRATCH = Path.home() / ".hermes/cache/scratch"


def snapshot():
    if SITE.is_symlink() or (SITE.exists() and not stat.S_ISREG(SITE.stat().st_mode)):
        raise RuntimeError("Refusing nonregular PhoneBridge nginx site")
    if ENABLED.is_symlink():
        link = os.readlink(ENABLED)
        if link != str(SITE):
            raise RuntimeError("Enabled site points to another origin configuration")
    elif ENABLED.exists():
        raise RuntimeError("Refusing existing non-symlink enabled site")
    else:
        link = None
    data = SITE.read_bytes() if SITE.exists() else None
    if data is not None and len(data) > 65536:
        raise RuntimeError("Existing nginx site exceeds size limit")
    return {"data": data, "mode": stat.S_IMODE(SITE.stat().st_mode) if data is not None else 0o644, "link": link}


def write_site(data, mode=0o644):
    SCRATCH.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=SCRATCH, delete=False) as handle:
        filename = Path(handle.name)
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
    temporary = SITE.with_name(SITE.name + ".phonebridge-staged-" + uuid.uuid4().hex)
    try:
        run("sudo", "-n", "install", "-m", f"{mode:o}", str(filename), str(temporary))
        run("sudo", "-n", "mv", "-T", str(temporary), str(SITE))
    finally:
        filename.unlink(missing_ok=True)
        run("sudo", "-n", "rm", "-f", str(temporary))


def restore(before):
    if before["data"] is None:
        run("sudo", "-n", "rm", "-f", str(SITE))
    else:
        write_site(before["data"], before["mode"])
    if before["link"] is None:
        run("sudo", "-n", "rm", "-f", str(ENABLED))
    elif not ENABLED.is_symlink():
        run("sudo", "-n", "ln", "-s", before["link"], str(ENABLED))
    run("sudo", "-n", "nginx", "-t")
    run("sudo", "-n", "systemctl", "reload", "nginx")


def promote(content):
    write_site(content.encode())
    if not ENABLED.is_symlink():
        run("sudo", "-n", "ln", "-s", str(SITE), str(ENABLED))
    run("sudo", "-n", "nginx", "-t")
    run("sudo", "-n", "systemctl", "reload", "nginx")


def transaction(operation):
    before = snapshot()
    saved = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
    try:
        operation()
    except BaseException as original:
        try:
            restore(before)
        except BaseException as failure:
            raise RuntimeError(f"HTTPS provisioning failed {type(original).__name__}; config restore failed {type(failure).__name__}") from None
        raise
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


def install_config(content):
    transaction(lambda: promote(content))


def root_file_exists(path):
    return subprocess.run(["sudo", "-n", "test", "-f", str(path)], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0


def provision(domain):
    certificate = Path("/etc/letsencrypt/live") / domain / "fullchain.pem"
    http = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/www/letsencrypt; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
"""
    if not root_file_exists(certificate):
        promote(http)
        run("sudo", "-n", "certbot", "certonly", "--webroot", "-w", "/var/www/letsencrypt", "-d", domain, "--non-interactive", "--agree-tos", "--register-unsafely-without-email", timeout=120)
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
        proxy_set_header X-Forwarded-For $remote_addr;
    }}
    location = /v1/pair/claim {{
        limit_except POST {{ deny all; }}
        proxy_pass http://127.0.0.1:18940;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }}
    location = /v1/device/ws {{
        proxy_pass http://127.0.0.1:18940;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }}
    location ^~ /v1/device/files/ {{
        limit_except GET {{ deny all; }}
        proxy_pass http://127.0.0.1:18940;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }}
    location / {{ return 403; }}
}}
"""
    promote(http + https)


def main():
    os.umask(0o077)
    settings = dict(line.strip().split("=", 1) for line in CONFIG.read_text().splitlines() if "=" in line and not line.lstrip().startswith("#"))
    public = origin(settings["PHONEBRIDGE_PUBLIC_URL"])
    domain = public.removeprefix("https://").removesuffix(":443")
    SCRATCH.mkdir(parents=True, exist_ok=True, mode=0o700)
    with deployment_lock(SCRATCH / "phonebridge-https.lock"):
        transaction(lambda: provision(domain))
    print("Stable PhoneBridge HTTPS origin provisioned")


if __name__ == "__main__":
    main()
