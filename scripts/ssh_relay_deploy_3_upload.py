"""部署第 3 步:打包上送 relay 源码,服务器建 venv 安装(用 steward 身份跑 pip)。"""
import os
import sys
import tarfile
import paramiko

LOCAL_APP = r"D:\Administrator\Desktop\quant\investment-steward\apps\relay"
TAR_LOCAL = r"D:\Administrator\Desktop\quant\investment-steward\scripts\relay-app.tar.gz"
BASE = "/srv/demohub-data/apps/steward"

# 1) 本地打包(只带源码与 pyproject,不含 egg-info/测试)
with tarfile.open(TAR_LOCAL, "w:gz") as tar:
    tar.add(os.path.join(LOCAL_APP, "pyproject.toml"), arcname="pyproject.toml")
    for name in ("_version.py", "__init__.py", "app.py", "settings.py"):
        tar.add(os.path.join(LOCAL_APP, "src", "relay_server", name), arcname=f"src/relay_server/{name}")
print("tar ok:", os.path.getsize(TAR_LOCAL), "bytes")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(os.environ["SSH_HOST"], port=int(os.environ.get("SSH_PORT", "531")),
            username=os.environ["SSH_USER"], password=os.environ["SSH_PASS"],
            timeout=20, look_for_keys=False, allow_agent=False)

sftp = ssh.open_sftp()
sftp.put(TAR_LOCAL, "/tmp/relay-app.tar.gz")
print("upload ok")

STEPS = [
    ("unpack", f"rm -rf {BASE}/app/* && tar -xzf /tmp/relay-app.tar.gz -C {BASE}/app && rm /tmp/relay-app.tar.gz && find {BASE}/app -type f"),
    ("venv", f"python3 -m venv {BASE}/venv && {BASE}/venv/bin/pip install -q --upgrade pip 2>&1 | tail -1; {BASE}/venv/bin/python --version"),
    ("install", f"cd {BASE}/app && {BASE}/venv/bin/pip install -q . 2>&1 | tail -2; {BASE}/venv/bin/python -c 'import relay_server; print(\"relay_server\", relay_server.__version__)'"),
    ("own", f"chown -R steward:steward {BASE}/app {BASE}/venv"),
]
for name, cmd in STEPS:
    _, out, err = ssh.exec_command(cmd, timeout=300)
    code = out.channel.recv_exit_status()
    print(f"===== {name} (exit={code}) =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:500])
        if code != 0:
            ssh.close(); sys.exit(1)
sftp.close()
ssh.close()
