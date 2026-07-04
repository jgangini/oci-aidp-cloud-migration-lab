#!/bin/bash
set -euo pipefail

APP_NAME="aidp-lab"
SOURCE_REPO_URL="${source_repo_url}"
SOURCE_COMMIT_SHA="${source_commit_sha}"
SOURCE_DIR="/opt/aidp-lab/source"
STATE_DIR="/opt/aidp-lab/state"
TLS_DIR="/opt/aidp-lab/tls"
LOCAL_IMAGE="aidp-lab:${source_commit_sha}"

retry() {
  local attempts="$1"
  shift
  local delay=10
  local attempt
  for attempt in $(seq 1 "$attempts"); do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -eq "$attempts" ]; then
      return 1
    fi
    echo "Command failed on attempt $attempt/$attempts: $*"
    echo "Retrying in $delay seconds..."
    sleep "$delay"
    if [ "$delay" -lt 60 ]; then
      delay=$((delay * 2))
    fi
  done
}

use_reachable_base_images() {
  sed -i \
    -e 's#^FROM node:#FROM public.ecr.aws/docker/library/node:#' \
    -e 's#^FROM python:#FROM public.ecr.aws/docker/library/python:#' \
    "$SOURCE_DIR/Dockerfile"
}

dnf -y makecache
dnf -y install dnf-plugins-core firewalld curl git openssl python3
systemctl stop firewalld >/dev/null 2>&1 || true
firewall-offline-cmd --zone=public --add-service=http
firewall-offline-cmd --zone=public --add-service=https
systemctl enable --now firewalld

dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker
docker info >/dev/null

install -d -m 0700 "$TLS_DIR" "$STATE_DIR"
PUBLIC_IP=$(curl -fsS -H "Authorization: Bearer Oracle" http://169.254.169.254/opc/v2/vnics/ | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["publicIp"])')
test -n "$PUBLIC_IP"
FQDN=$(hostname -f 2>/dev/null || hostname)
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
  -subj "/CN=$PUBLIC_IP" \
  -addext "subjectAltName=IP:$PUBLIC_IP,DNS:$FQDN" \
  -keyout "$TLS_DIR/tls.key" \
  -out "$TLS_DIR/tls.crt"
chmod 0600 "$TLS_DIR/tls.key"

rm -rf "$SOURCE_DIR"
mkdir -p "$(dirname "$SOURCE_DIR")"
retry 5 git clone --filter=blob:none "$SOURCE_REPO_URL" "$SOURCE_DIR"
git -C "$SOURCE_DIR" checkout --detach "$SOURCE_COMMIT_SHA"
test "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$SOURCE_COMMIT_SHA"
use_reachable_base_images

cat > /opt/aidp-lab/app.env <<'EOF'
ADMIN_USERNAME=${admin_username}
ADMIN_PASSWORD_HASH=${admin_password_hash}
REGISTRATION_CODE_HASH=${registration_code_hash}
IDENTITY_DOMAIN_URL=${identity_domain_url}
IDENTITY_OAUTH_CLIENT_ID=${identity_oauth_client_id}
OAUTH_SECRET_OCID=${oauth_secret_ocid}
IDENTITY_DEVELOPER_GROUP_ID=${developer_group_id}
IDENTITY_PENDING_GROUP_ID=${pending_group_id}
LAB_MARKER=${lab_marker}
SESSION_SECRET_FILE=/var/lib/aidp-lab/session.key
COOKIE_SECURE=true
EOF
chmod 0600 /opt/aidp-lab/app.env

retry 5 docker build -t "$LOCAL_IMAGE" "$SOURCE_DIR"
docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$APP_NAME" \
  --restart unless-stopped \
  --env-file /opt/aidp-lab/app.env \
  -p 80:80 \
  -p 443:443 \
  -v "$TLS_DIR:/etc/aidp-lab/tls:ro,Z" \
  -v "$STATE_DIR:/var/lib/aidp-lab:Z" \
  "$LOCAL_IMAGE"

for attempt in $(seq 1 120); do
  if curl --fail --silent --insecure https://127.0.0.1/api/health >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 120 ]; then
    docker logs "$APP_NAME" >/home/opc/aidp-lab-container.log 2>&1 || true
    exit 1
  fi
  sleep 5
done

cat >/home/opc/startup_info.txt <<'EOF'
OCI AIDP Cloud Migration Lab is ready.

Application URL: https://[PUBLIC-IP]
Admin URL: https://[PUBLIC-IP]/admin/users
Container: aidp-lab
Source: https://github.com/jgangini/oci-aidp-cloud-migration-lab

Useful commands:
  sudo docker ps
  sudo docker logs aidp-lab
  sudo journalctl -u docker --no-pager
EOF
sed -i "s/\[PUBLIC-IP\]/$PUBLIC_IP/g" /home/opc/startup_info.txt
chown opc:opc /home/opc/startup_info.txt
mkdir -p /var/local
touch /var/local/userdata.done
cat /home/opc/startup_info.txt
