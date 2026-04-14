#!/usr/bin/env bash
# One-time VPS bootstrap for SaasMint dev environment.
# Run as root: bash bootstrap-vps.sh
set -euo pipefail

SAASMINT_DIR="/opt/saasmint"
DEPLOY_USER="deploy"
GITHUB_ORG="SergiCoder"

echo "==> [1/7] Installing Docker..."
if ! command -v docker &>/dev/null; then
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
else
    echo "  Docker already installed, skipping."
fi

echo "==> [2/7] Creating deploy user..."
if ! id "$DEPLOY_USER" &>/dev/null; then
    adduser --system --group --shell /bin/bash --home "/home/$DEPLOY_USER" "$DEPLOY_USER"
    usermod -aG docker "$DEPLOY_USER"
else
    echo "  User '$DEPLOY_USER' already exists, ensuring docker group."
    usermod -aG docker "$DEPLOY_USER"
fi

echo "==> [3/7] Setting up SSH key for deploy user..."
DEPLOY_SSH_DIR="/home/$DEPLOY_USER/.ssh"
mkdir -p "$DEPLOY_SSH_DIR"
if [ ! -f "$DEPLOY_SSH_DIR/id_ed25519" ]; then
    ssh-keygen -t ed25519 -f "$DEPLOY_SSH_DIR/id_ed25519" -N "" -C "deploy@$(hostname)"
    cat "$DEPLOY_SSH_DIR/id_ed25519.pub" >> "$DEPLOY_SSH_DIR/authorized_keys"
    chmod 700 "$DEPLOY_SSH_DIR"
    chmod 600 "$DEPLOY_SSH_DIR/authorized_keys" "$DEPLOY_SSH_DIR/id_ed25519"
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_SSH_DIR"
    echo ""
    echo "  ===== PRIVATE KEY (add to GitHub secrets as VPS_SSH_KEY) ====="
    cat "$DEPLOY_SSH_DIR/id_ed25519"
    echo "  ==============================================================="
    echo ""
else
    echo "  SSH key already exists, skipping."
fi

echo "==> [4/7] Creating $SAASMINT_DIR and cloning repos..."
mkdir -p "$SAASMINT_DIR"
chown "$DEPLOY_USER:$DEPLOY_USER" "$SAASMINT_DIR"

for repo in saasmint-core saasmint-app; do
    if [ ! -d "$SAASMINT_DIR/$repo" ]; then
        sudo -u "$DEPLOY_USER" git clone "https://github.com/$GITHUB_ORG/$repo.git" "$SAASMINT_DIR/$repo"
    else
        echo "  $repo already cloned, skipping."
    fi
done

echo "==> [5/7] Creating .env.dev template..."
if [ ! -f "$SAASMINT_DIR/.env.dev" ]; then
    cat > "$SAASMINT_DIR/.env.dev" <<'ENVEOF'
# SaasMint dev VPS — fill in your secrets
ENVIRONMENT=dev
DJANGO_SETTINGS_MODULE=config.settings.dev
DJANGO_SECRET_KEY=CHANGE_ME_generate_with_python_c_import_secrets_secrets_token_urlsafe_64
DEBUG=true
DJANGO_PORT=8001
DJANGO_STATIC_ROOT=/app/staticfiles
ALLOWED_HOSTS=["api.saasmint.net"]
CSRF_TRUSTED_ORIGINS=["https://api.saasmint.net","https://app.saasmint.net"]
CORS_ALLOWED_ORIGINS=["https://app.saasmint.net"]
CORS_ALLOW_ALL_ORIGINS=false
ENABLE_SESSION_AUTH=true

POSTGRES_DB=saasmint
POSTGRES_USER=saasmint
POSTGRES_PASSWORD=CHANGE_ME
DATABASE_URL=postgresql://saasmint:CHANGE_ME@postgres:5432/saasmint
REDIS_URL=redis://redis:6379/0

STRIPE_SECRET_KEY=sk_test_CHANGE_ME
STRIPE_WEBHOOK_SECRET=whsec_CHANGE_ME

RESEND_API_KEY=re_CHANGE_ME
EMAIL_FROM_ADDRESS=noreply@saasmint.net
FRONTEND_URL=https://app.saasmint.net

# SaasMint App (frontend)
NEXT_PUBLIC_API_URL=https://api.saasmint.net
NEXT_PUBLIC_APP_URL=https://app.saasmint.net
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=pk_test_CHANGE_ME
ENVEOF
    chown "$DEPLOY_USER:$DEPLOY_USER" "$SAASMINT_DIR/.env.dev"
    echo "  Created $SAASMINT_DIR/.env.dev — fill in real values before first deploy."
else
    echo "  .env.dev already exists, skipping."
fi

echo "==> [6/7] Configuring nginx vhosts..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NGINX_SRC="$SCRIPT_DIR/../nginx"

for conf in api.saasmint.net.conf app.saasmint.net.conf; do
    cp "$NGINX_SRC/$conf" "/etc/nginx/sites-available/$conf"
    ln -sf "/etc/nginx/sites-available/$conf" "/etc/nginx/sites-enabled/$conf"
    echo "  Installed $conf"
done

nginx -t && systemctl reload nginx
echo "  nginx reloaded."

echo "==> [7/7] Installing certbot..."
if ! command -v certbot &>/dev/null; then
    apt-get install -y certbot python3-certbot-nginx
else
    echo "  certbot already installed, skipping."
fi

echo ""
echo "===== Bootstrap complete ====="
echo ""
echo "Next steps:"
echo "  1. Fill in /opt/saasmint/.env.dev with real credentials"
echo "  2. Run: certbot --nginx -d api.saasmint.net -d app.saasmint.net"
echo "  3. Verify SSH as deploy user works, then disable password auth:"
echo "     Edit /etc/ssh/sshd_config -> PasswordAuthentication no"
echo "     systemctl restart sshd"
echo "  4. Add GitHub secrets to both repos:"
echo "     VPS_HOST=<your-vps-ip>"
echo "     VPS_PORT=<your-ssh-port>"
echo "     VPS_SSH_KEY=(private key printed above)"
echo "     STRIPE_PUBLISHABLE_KEY=(in saasmint-app only)"
echo "  5. Push a dev-v0.1.0 tag to trigger the first deploy"
