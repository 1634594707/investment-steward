"""部署第 4 步:bootstrap token 写 env(chmod 600) + systemd unit(加固) + 启动 + 回环 /health 验证。"""
import os
import secrets
import paramiko

BASE = "/srv/demohub-data/apps/steward"
UNIT = """[Unit]
Description=Investment Steward Relay (sync relay, stage 4)
After=network.target

[Service]
Type=simple
User=steward
Group=steward
EnvironmentFile=%(base)s/etc/relay.env
ExecStart=%(base)s/venv/bin/python -m uvicorn relay_server.app:app --host 127.0.0.1 --port 8900
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=%(base)s

[Install]
WantedBy=multi-user.target
""" % {"base": BASE}

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(os.environ["SSH_HOST"], port=int(os.environ.get("SSH_PORT", "531")),
            username=os.environ["SSH_USER"], password=os.environ["SSH_PASS"],
            timeout=20, look_for_keys=False, allow_agent=False)

token = secrets.token_hex(32)
sftp = ssh.open_sftp()
with sftp.open(f"{BASE}/etc/relay.env", "w") as f:
    f.write(f"RELAY_DATA_DIR={BASE}/relay-data\nRELAY_BOOTSTRAP_TOKEN={token}\n")
with sftp.open("/etc/systemd/system/steward-relay.service", "w") as f:
    f.write(UNIT)
print("bootstrap_token_saved_locally_below")
with open(r"D:\Administrator\Desktop\quant\investment-steward\scripts\relay_bootstrap_token.txt", "w", encoding="utf-8") as f:
    f.write(token + "\n")
print("token file written (gitignored scripts dir? verify)")

STEPS = [
    ("perm", f"chown steward:steward {BASE}/etc/relay.env && chmod 600 {BASE}/etc/relay.env && ls -l {BASE}/etc/relay.env"),
    ("daemon", "systemctl daemon-reload && systemctl enable --now steward-relay && sleep 2 && systemctl is-active steward-relay"),
    ("local-health", "curl -s --max-time 5 http://127.0.0.1:8900/health"),
    ("journal", "journalctl -u steward-relay -n 5 --no-pager -o cat"),
]
for name, cmd in STEPS:
    _, out, err = ssh.exec_command(cmd, timeout=120)
    code = out.channel.recv_exit_status()
    print(f"===== {name} (exit={code}) =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:400])
sftp.close()
ssh.close()
