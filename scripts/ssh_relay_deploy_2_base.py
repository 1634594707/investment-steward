"""部署第 2-3 步:swap 2G + 独立低权用户 + 目录;并确认 nginx include 布局。"""
import os
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(os.environ["SSH_HOST"], port=int(os.environ.get("SSH_PORT", "531")),
            username=os.environ["SSH_USER"], password=os.environ["SSH_PASS"],
            timeout=20, look_for_keys=False, allow_agent=False)

STEPS = [
    ("swap-create", "fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && swapon --show && free -h | tail -1"),
    ("swap-fstab", "grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; grep swapfile /etc/fstab"),
    ("user", "useradd --system --home-dir /srv/demohub-data/apps/steward --shell /usr/sbin/nologin steward && id steward"),
    ("dirs", "mkdir -p /srv/demohub-data/apps/steward/{app/src,relay-data,etc} && chown -R steward:steward /srv/demohub-data/apps/steward && ls -la /srv/demohub-data/apps/steward"),
    ("nginx-layout", "grep -n 'include' /etc/nginx/nginx.conf | grep -v '^\\s*#' && ls /etc/nginx/sites-enabled/ 2>/dev/null"),
]
for name, cmd in STEPS:
    _, out, err = ssh.exec_command(cmd, timeout=120)
    code = out.channel.recv_exit_status()
    print(f"===== {name} (exit={code}) =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:400])
ssh.close()
