"""部署第 5 步:certbot 签 st.18257.xyz 证书 + nginx 反代 conf + 公网验证。"""
import os
import paramiko

BASE = "/srv/demohub-data/apps/steward"
NGINX_CONF = """server {
    listen 443 ssl http2;
    server_name st.18257.xyz;

    ssl_certificate /etc/letsencrypt/live/st.18257.xyz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/st.18257.xyz/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # 只放行中转服务需要的方法;仅回环 8900,公网不直连
    location / {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 12m;
        proxy_read_timeout 60s;
    }
}
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(os.environ["SSH_HOST"], port=int(os.environ.get("SSH_PORT", "531")),
            username=os.environ["SSH_USER"], password=os.environ["SSH_PASS"],
            timeout=20, look_for_keys=False, allow_agent=False)
sftp = ssh.open_sftp()

STEPS = [
    ("certbot", "certbot certonly --nginx -d st.18257.xyz -n --agree-tos 2>&1 | tail -6"),
]
for name, cmd in STEPS:
    _, out, err = ssh.exec_command(cmd, timeout=300)
    code = out.channel.recv_exit_status()
    print(f"===== {name} (exit={code}) =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:300])
    if code != 0:
        ssh.close()
        raise SystemExit("certbot failed; nginx conf not written")

with sftp.open("/etc/nginx/conf.d/steward.conf", "w") as f:
    f.write(NGINX_CONF)
print("nginx conf written")
STEPS2 = [
    ("nginx-test", "nginx -t 2>&1 && systemctl reload nginx && echo reloaded"),
    ("origin-check", "curl -sk --max-time 5 --resolve st.18257.xyz:443:127.0.0.1 https://st.18257.xyz/health"),
]
for name, cmd in STEPS2:
    _, out, err = ssh.exec_command(cmd, timeout=120)
    code = out.channel.recv_exit_status()
    print(f"===== {name} (exit={code}) =====")
    print(out.read().decode(errors="replace").strip())
    e = err.read().decode(errors="replace").strip()
    if e:
        print("[stderr]", e[:300])
sftp.close()
ssh.close()
