"""修复:更新 app.py(build_app 工厂) + 改 unit 为 --factory + 重启验证。"""
import os
import tarfile
import paramiko

LOCAL_APP = r"D:\Administrator\Desktop\quant\investment-steward\apps\relay"
TAR_LOCAL = r"D:\Administrator\Desktop\quant\investment-steward\scripts\relay-app.tar.gz"
BASE = "/srv/demohub-data/apps/steward"

with tarfile.open(TAR_LOCAL, "w:gz") as tar:
    tar.add(os.path.join(LOCAL_APP, "pyproject.toml"), arcname="pyproject.toml")
    for name in ("_version.py", "__init__.py", "app.py", "settings.py"):
        tar.add(os.path.join(LOCAL_APP, "src", "relay_server", name), arcname=f"src/relay_server/{name}")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(os.environ["SSH_HOST"], port=int(os.environ.get("SSH_PORT", "531")),
            username=os.environ["SSH_USER"], password=os.environ["SSH_PASS"],
            timeout=20, look_for_keys=False, allow_agent=False)
sftp = ssh.open_sftp()
sftp.put(TAR_LOCAL, "/tmp/relay-app.tar.gz")

STEPS = [
    ("unpack", f"tar -xzf /tmp/relay-app.tar.gz -C {BASE}/app && rm /tmp/relay-app.tar.gz && chown -R steward:steward {BASE}/app"),
    ("unit-fix", f"sed -i 's|relay_server.app:app|relay_server.app:build_app --factory|' /etc/systemd/system/steward-relay.service && grep ExecStart /etc/systemd/system/steward-relay.service"),
    ("daemon", "systemctl daemon-reload && systemctl restart steward-relay && sleep 2 && systemctl is-active steward-relay"),
    ("local-health", "curl -s --max-time 5 http://127.0.0.1:8900/health"),
    ("journal", "journalctl -u steward-relay -n 4 --no-pager -o cat"),
]
for name, cmd in STEPS:
    _, out, err = ssh.exec_command(cmd, timeout=180)
    code = out.channel.recv_exit_status()
    print(f"===== {name} (exit={code}) =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:400])
sftp.close()
ssh.close()
