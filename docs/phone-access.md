# Phone access: what was checked, and where

How the installer makes Thinair reachable from a phone is described in the README ("Using it
from your phone"). This file records the external facts that design relies on, how each was
checked, and what is still unverified. Same idea as [agents.md](agents.md) for agent CLIs.

## Checked (2026-11-14)

| Fact | How it was checked | Source |
|---|---|---|
| `caddy reverse-proxy --from <host>` serves HTTPS with an automatic certificate for a host name, and redirects HTTP to HTTPS | Caddy docs; redirect seen in the installer test (`308` to `https://...`) | <https://caddyserver.com/docs/command-line>, <https://caddyserver.com/docs/automatic-https> |
| Caddy tries Let's Encrypt, then ZeroSSL, then backs off exponentially (max 1 day between attempts, for up to 30 days); HTTP-01 needs port 80, TLS-ALPN-01 port 443, chosen at random | Caddy docs | <https://caddyserver.com/docs/automatic-https> |
| Caddy sets X-Forwarded-For/-Proto/-Host and ignores client-sent values unless `trusted_proxies` is set | Caddy docs | <https://caddyserver.com/docs/caddyfile/directives/reverse_proxy> |
| uvicorn 0.54 accepts CIDRs in `forwarded_allow_ips` and takes the right-most untrusted X-Forwarded-For entry | Read the installed source (`uvicorn/middleware/proxy_headers.py`); live-server test `test_throttle_counts_real_clients_behind_a_trusted_proxy` | uvicorn 0.54.0 |
| Ports published by Docker bypass ufw | Docker docs: packets are "diverted before it reaches the `INPUT` and `OUTPUT` chains that ufw uses" | <https://docs.docker.com/engine/network/packet-filtering-firewalls/> |
| `tailscale serve` / `funnel` syntax: `--bg`, `--https=<port>`, target a port or `http://127.0.0.1:<port>`; Funnel ports 443/8443/10000; both open a consent page to enable unmet requirements (HTTPS certificates, funnel attribute) | Tailscale docs | <https://tailscale.com/kb/1242/tailscale-serve>, <https://tailscale.com/kb/1311/tailscale-funnel>, <https://tailscale.com/kb/1312/serve>, <https://tailscale.com/kb/1223/funnel> |
| Cloudflare quick tunnels don't support SSE and have no uptime guarantee (so they are not offered) | Cloudflare docs | <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/> |
| Cloud DMI strings: Oracle `chassis_asset_tag` = `OracleCloud.com`; Azure `chassis_asset_tag` = `7783-7084-3265-9085-8269-3286-77`; Google `product_name` = `Google Compute Engine`; `sys_vendor` = `DigitalOcean`, `Hetzner`, `Vultr`, `Scaleway`, `Linode`, `Akamai`. AWS is identified by `product_uuid` (root-only), so Thinair doesn't recognise it | cloud-init source | <https://raw.githubusercontent.com/canonical/cloud-init/main/tools/ds-identify> |
| `https://ipv4.icanhazip.com` answers with the caller's IPv4 as plain text | Called it | - |
| sslip.io has exhausted its shared Let's Encrypt quota before, and Let's Encrypt raised it on request | sslip.io issue tracker | <https://github.com/cunnie/sslip.io/issues/108> |

## Tested without the internet

The installer ran end to end in `docker:cli` against a locally built image, with a copy of the
compose file in which Caddy uses its internal CA on high ports (so no Let's Encrypt traffic was
sent for an address that isn't ours):

- NAT path: detection, firewall hint after 45 s, give-up after 90 s, clean revert of `.env` and
  Caddy, the no-terminal message, token shown.
- Success path (the server trusted Caddy's internal CA for the test): `HTTPS is ready`, QR code,
  "public on the internet" line, doctor silent.
- IP change on re-run: moved to the new sslip.io name, new certificate, QR shown again.
- Plain re-run: nothing changed, no QR.
- Through Caddy: pairing sets a `Secure` cookie, HSTS present, `/docs` and `/openapi.json`
  answer 401, HTTP redirects to HTTPS.

## Not verified yet

- A real Let's Encrypt certificate for an sslip.io name on a real public VM (owner's manual test).
- Oracle: whether Oracle's iptables rules block Docker-published ports at all (Docker uses the
  FORWARD path, Oracle's REJECT rule that matters is in INPUT). The installer keeps offering the
  INPUT rules, as before.
- Tailscale: that `serve`/`funnel --bg` show the consent link and then succeed on a tailnet with
  HTTPS certificates off.
- Let's Encrypt IP-address certificates (generally available since 2026-01-15) through Caddy:
  not used; a Caddy issue reports them being refused (<https://github.com/caddyserver/caddy/issues/7399>).
