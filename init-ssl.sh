#!/bin/bash
# First-time SSL certificate setup script
# Run this ONCE on the server after DNS is pointed to the server

set -e

DOMAIN="kluchove.com.ua"
EMAIL="$1"

if [ -z "$EMAIL" ]; then
    echo "Usage: ./init-ssl.sh your-email@example.com"
    exit 1
fi

echo "==> Starting Nginx with HTTP-only config for ACME challenge..."
cp nginx/init.conf nginx/default.conf.bak
cp nginx/init.conf nginx/default.conf

docker compose up -d nginx

echo "==> Requesting SSL certificate for $DOMAIN..."
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    -d "$DOMAIN" \
    -d "www.$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email

echo "==> Restoring full Nginx config with SSL..."
cp nginx/default.conf.bak nginx/default.conf
rm nginx/default.conf.bak

echo "==> Restarting services..."
docker compose down
docker compose up -d

echo "==> Done! SSL certificate installed. Auto-renewal is active."
