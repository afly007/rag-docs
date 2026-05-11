#!/bin/sh
set -e

CADDYFILE=/etc/caddy/Caddyfile
DOMAIN="${TLS_DOMAIN:-distill.local}"
MODE="${TLS_MODE:-internal}"

die() { echo "ERROR: $*" >&2; exit 1; }

require() {
    eval "val=\$$1"
    [ -n "$val" ] || die "$1 must be set for TLS_MODE=$MODE / TLS_DNS_PROVIDER=${TLS_DNS_PROVIDER}"
}

write_internal() {
    require TLS_DOMAIN
    cat > "$CADDYFILE" <<EOF
$DOMAIN {
    tls internal
    reverse_proxy mcp-server:8000
}
EOF
}

write_dns_cloudflare() {
    require TLS_DOMAIN
    require CF_API_TOKEN
    cat > "$CADDYFILE" <<EOF
$DOMAIN {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy mcp-server:8000
}
EOF
}

write_dns_route53() {
    require TLS_DOMAIN
    # Route53 uses the AWS SDK credential chain:
    # AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY, or an IAM instance role.
    cat > "$CADDYFILE" <<EOF
$DOMAIN {
    tls {
        dns route53 {
            max_retries 10
            region {env.AWS_REGION}
        }
    }
    reverse_proxy mcp-server:8000
}
EOF
}

write_dns_acmedns() {
    require TLS_DOMAIN
    require ACMEDNS_HOST
    # ACME-DNS: self-hosted acme-dns server. See https://github.com/joohoi/acme-dns
    cat > "$CADDYFILE" <<EOF
$DOMAIN {
    tls {
        dns acmedns {env.ACMEDNS_HOST}
    }
    reverse_proxy mcp-server:8000
}
EOF
}

write_dns_digitalocean() {
    require TLS_DOMAIN
    require DO_AUTH_TOKEN
    cat > "$CADDYFILE" <<EOF
$DOMAIN {
    tls {
        dns digitalocean {env.DO_AUTH_TOKEN}
    }
    reverse_proxy mcp-server:8000
}
EOF
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
