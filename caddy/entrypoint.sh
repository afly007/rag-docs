#!/bin/sh
set -e

CADDYFILE=/etc/caddy/Caddyfile
DOMAIN="${TLS_DOMAIN:-distill.local}"
MODE="${TLS_MODE:-internal}"

die()     { echo "ERROR: $*" >&2; exit 1; }
require() {
    eval "val=\$$1"
    [ -n "$val" ] || die "$1 must be set for TLS_MODE=$MODE / TLS_DNS_PROVIDER=${TLS_DNS_PROVIDER}"
}

# write_caddyfile <tls_block>
# Writes a complete Caddyfile.  All write_* functions delegate here so that
# security directives are always present regardless of TLS mode chosen.
write_caddyfile() {
    local tls_block="$1"
    local rate="${CLIP_RATE_LIMIT:-20}"

    # Validate auth vars — must both be set or both be unset
    if [ -n "$ADMIN_USER" ] && [ -z "$ADMIN_PASSWORD_HASH" ]; then
        die "ADMIN_PASSWORD_HASH must be set when ADMIN_USER is set"
    fi
    if [ -z "$ADMIN_USER" ] && [ -n "$ADMIN_PASSWORD_HASH" ]; then
        die "ADMIN_USER must be set when ADMIN_PASSWORD_HASH is set"
    fi

    {
        printf '%s {\n' "$DOMAIN"
        printf '%s\n' "$tls_block"

        # Security response headers
        cat <<CADDY

    header {
        -Server
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
CADDY

        # Optional basic auth on /stats and /files
        if [ -n "$ADMIN_USER" ]; then
            cat <<CADDY

    @admin path /stats* /files*
    basicauth @admin {
        $ADMIN_USER $ADMIN_PASSWORD_HASH
    }
CADDY
        fi

        # Rate limit /clip to protect OpenAI API credits
        cat <<CADDY

    @clip path /clip*
    rate_limit @clip {
        zone clip {
            key    {remote_host}
            events $rate
            window 1m
        }
    }

    reverse_proxy mcp-server:8000
}
CADDY
    } > "$CADDYFILE"
}

write_internal() {
    require TLS_DOMAIN
    write_caddyfile "    tls internal"
}

write_dns_cloudflare() {
    require TLS_DOMAIN
    require CF_API_TOKEN
    write_caddyfile "    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }"
}

write_dns_route53() {
    require TLS_DOMAIN
    # Route53 uses the AWS SDK credential chain:
    # AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY, or an IAM instance role.
    write_caddyfile "    tls {
        dns route53 {
            max_retries 10
            region {env.AWS_REGION}
        }
    }"
}

write_dns_acmedns() {
    require TLS_DOMAIN
    require ACMEDNS_HOST
    # ACME-DNS: self-hosted acme-dns server. See https://github.com/joohoi/acme-dns
    write_caddyfile "    tls {
        dns acmedns {env.ACMEDNS_HOST}
    }"
}

write_dns_digitalocean() {
    require TLS_DOMAIN
    require DO_AUTH_TOKEN
    write_caddyfile "    tls {
        dns digitalocean {env.DO_AUTH_TOKEN}
    }"
}

case "$MODE" in
    internal)
        write_internal
        ;;
    dns)
        case "${TLS_DNS_PROVIDER}" in
            cloudflare)   write_dns_cloudflare ;;
            route53)      write_dns_route53 ;;
            acmedns)      write_dns_acmedns ;;
            digitalocean) write_dns_digitalocean ;;
            "") die "TLS_DNS_PROVIDER must be set when TLS_MODE=dns (cloudflare|route53|acmedns|digitalocean)" ;;
            *)  die "Unknown TLS_DNS_PROVIDER='${TLS_DNS_PROVIDER}' — supported: cloudflare, route53, acmedns, digitalocean" ;;
        esac
        ;;
    *)
        die "Unknown TLS_MODE='$MODE' — supported: internal, dns"
        ;;
esac

echo "==> Caddy starting with TLS_MODE=$MODE, domain=$DOMAIN"
cat "$CADDYFILE"
exec caddy run --config "$CADDYFILE" --adapter caddyfile
