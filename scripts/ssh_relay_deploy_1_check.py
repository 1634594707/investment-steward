"""只读复检:确认部署清单前提仍然成立,不改任何东西。"""
import os
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(os.environ["SSH_HOST"], port=int(os.environ.get("SSH_PORT", "531")),
            username=os.environ["SSH_USER"], password=os.environ["SSH_PASS"],
            timeout=20, look_for_keys=False, allow_agent=False)
CHECKS = [
    ("identity", "whoami && hostname && uname -sr"),
    ("resources", "free -h | head -3 && swapon --show && df -h / /srv/demohub-data 2>/dev/null | tail -3"),
    ("ports", "ss -tlnp | awk '{print $4}' | grep -E ':(80|443|8787|3000|8900)$' | sort"),
    ("python", "python3 --version && python3 -m venv --help >/dev/null 2>&1 && echo venv-ok || echo venv-missing"),
    ("nginx", "ls /etc/nginx/conf.d/ && nginx -v 2>&1"),
    ("certbot", "certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry' | head -12"),
    ("steward-user", "id steward 2>/dev/null || echo no-steward-user"),
    ("target-dir", "ls -la /srv/demohub-data/apps/ 2>/dev/null | head -8"),
]
for name, cmd in CHECKS:
    _, out, err = ssh.exec_command(cmd, timeout=60)
    print(f"===== {name} =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:300])
ssh.close()
