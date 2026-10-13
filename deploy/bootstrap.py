import os
from pathlib import Path
import shutil
import subprocess


SOURCE = Path(__file__).resolve().parent
HOME = Path.home()


def main():
    os.umask(0o077)
    base = HOME / ".local/share/phonebridge"
    binary = base / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    (base / "state").mkdir(exist_ok=True)
    unit_dir = HOME / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    entry = binary / "deploy_entry.py"
    shutil.copyfile(SOURCE / "rollout.py", entry)
    entry.chmod(0o700)
    shutil.copyfile(SOURCE / "phonebridge.service", unit_dir / "phonebridge.service")
    public_key = (HOME / ".hermes/credentials/phonebridge/deploy_ed25519.pub").read_text().strip()
    fields = public_key.split()
    if len(fields) != 3 or fields[0] != "ssh-ed25519" or fields[2] != "phonebridge-github-deploy":
        raise RuntimeError("Unexpected deployment key")
    ssh = HOME / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    authorized = ssh / "authorized_keys"
    existing = authorized.read_text() if authorized.exists() else ""
    matching = [line for line in existing.splitlines() if fields[1] in line.split()]
    expected = f'restrict,command="/usr/bin/python3 {entry}" {public_key}'
    if matching and matching != [expected]:
        raise RuntimeError("Deployment key exists with different restrictions")
    if not matching:
        with authorized.open("a") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(expected + "\n")
    authorized.chmod(0o600)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("PhoneBridge restricted deployment bootstrap installed")


if __name__ == "__main__":
    main()
