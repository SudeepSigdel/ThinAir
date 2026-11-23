"""How phones reach this server: public address detection, sslip.io names, HTTPS readiness.

Used by the installer (through `thinair net detect` / `thinair net https-check`), by
`thinair doctor` and by a one-off check at server start. Nothing here polls.

- **sslip.io** answers DNS for `203-0-113-7.sslip.io` with `203.0.113.7`, so a machine with a
  public IPv4 address gets a host name, and Caddy a Let's Encrypt certificate for it, with no
  account and no domain. The name is derived from the IP, so it changes when the IP changes.
- **The public IP** comes from `THINAIR_PUBLIC_IP` / `--public-ip` when given, else from one
  anonymous request to `PUBLIC_IP_URL` (it only ever sees this machine's address).
- **The cloud** is recognised from DMI strings, the same ones cloud-init's `ds-identify` uses.
  It only chooses which "open ports 80 and 443" hint to show.
- **HTTPS readiness** is a TLS handshake that verifies the certificate for the host name. It can
  connect through Caddy's container (`caddy:443`), so it works without hairpin routing, and a
  valid certificate also proves Let's Encrypt reached this machine from the internet.
"""

import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SSLIP_SUFFIX = ".sslip.io"
PUBLIC_IP_URL = "https://ipv4.icanhazip.com"
DMI_DIR = Path("/sys/class/dmi/id")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_SSLIP_RE = re.compile(r"^(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.sslip\.io\.?$", re.IGNORECASE)

Kind = Literal["direct", "cloud", "nat", "none"]
# direct: the public address is on this machine itself (Hetzner, DigitalOcean, most VPS).
# cloud:  a known cloud that maps a public address 1:1 to this machine (Oracle, GCP, Azure).
# nat:    a public address exists but belongs to something else (a home router, AWS: see
#         below); inbound connections work only if someone forwarded/opened the ports.
# none:   no usable public IPv4 (carrier-grade NAT, IPv6-only, offline).


def is_public_ipv4(value: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global


def sslip_host(ip: str) -> str:
    """`203.0.113.7` -> `203-0-113-7.sslip.io` (dashes: one label, so no dotted wildcard)."""
    if not is_public_ipv4(ip):
        raise ValueError(f"not a public IPv4 address: {ip!r}")
    return ip.strip().replace(".", "-") + SSLIP_SUFFIX


def ip_from_sslip(host: str | None) -> str | None:
    """The IPv4 address an sslip.io name stands for, or None if it isn't one."""
    m = _SSLIP_RE.match((host or "").strip())
    if not m:
        return None
    ip = ".".join(str(int(p)) for p in m.groups())
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    return ip


def host_of(url: str | None) -> str:
    """Host part of an http(s) URL ('' if none)."""
    m = re.match(r"^https?://([^/:?#]+)", (url or "").strip())
    return m.group(1).lower() if m else ""


# ---- which cloud --------------------------------------------------------------------------------
# Exact strings from cloud-init's tools/ds-identify (checked 2026-11-14): Oracle and Azure by
# chassis_asset_tag, Google by product_name, the rest by sys_vendor. AWS is identified there by
# product_uuid, which only root can read, so AWS is not recognised here (it gets the generic hint).
_ASSET = {"OracleCloud.com": "oracle", "7783-7084-3265-9085-8269-3286-77": "azure"}
_PRODUCT = {"Google Compute Engine": "gcp"}
_VENDOR = {
    "DigitalOcean": "digitalocean",
    "Hetzner": "hetzner",
    "Vultr": "vultr",
    "Scaleway": "scaleway",
    "Linode": "akamai",
    "Akamai": "akamai",
}
# Clouds whose instances get their public address through 1:1 NAT (the address is not on the
# machine's own interface) and that block ports 80/443 until the owner opens them.
NAT_CLOUDS = {"oracle", "gcp", "azure"}


def identify_cloud(vendor: str = "", product: str = "", asset: str = "") -> str | None:
    vendor, product, asset = vendor.strip(), product.strip(), asset.strip()
    return _ASSET.get(asset) or _PRODUCT.get(product) or _VENDOR.get(vendor)


def read_dmi(name: str) -> str:
    try:
        return (DMI_DIR / name).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def local_cloud() -> str | None:
    """The cloud this process runs on, from DMI (readable inside containers too)."""
    return identify_cloud(
        read_dmi("sys_vendor"), read_dmi("product_name"), read_dmi("chassis_asset_tag")
    )


# ---- public address -----------------------------------------------------------------------------
def fetch_public_ipv4(timeout_s: float = 6, get=None) -> str | None:  # noqa: ANN001
    """This machine's public IPv4 as the internet sees it, or None. One anonymous request."""
    import requests

    get = get or requests.get
    try:
        r = get(PUBLIC_IP_URL, timeout=timeout_s, headers={"User-Agent": "thinair"})
    except requests.RequestException:
        return None
    text = (getattr(r, "text", "") or "").strip()
    return text if getattr(r, "status_code", 0) == 200 and is_public_ipv4(text) else None


def current_public_ipv4(fetch=None) -> str | None:  # noqa: ANN001
    """THINAIR_PUBLIC_IP when set by hand, else one lookup (`fetch`, default PUBLIC_IP_URL)."""
    from thinair.config import get_settings

    given = (get_settings().public_ip or "").strip()
    if given:
        return given if is_public_ipv4(given) else None
    return (fetch or fetch_public_ipv4)()


def classify(local_ip: str | None, public_ip: str | None, cloud: str | None) -> Kind:
    if not is_public_ipv4(public_ip):
        return "none"
    local = (local_ip or "").strip()
    if local == public_ip:
        return "direct"
    try:
        if ipaddress.ip_address(local) in _CGNAT:
            return "none"  # the provider shares one address between customers: no way in
    except ValueError:
        pass
    return "cloud" if cloud in NAT_CLOUDS else "nat"


@dataclass(frozen=True)
class Detection:
    kind: Kind
    public_ip: str | None
    local_ip: str | None
    cloud: str | None

    @property
    def host(self) -> str | None:
        return sslip_host(self.public_ip) if self.kind != "none" and self.public_ip else None

    def as_env(self) -> str:
        """`KEY=value` lines for the installer (values never contain spaces or quotes)."""
        rows = {
            "KIND": self.kind,
            "PUBLIC_IP": self.public_ip or "",
            "LOCAL_IP": self.local_ip or "",
            "CLOUD": self.cloud or "",
            "HOST": self.host or "",
        }
        return "\n".join(f"{k}={v}" for k, v in rows.items())


def detect(
    local_ip: str | None,
    *,
    vendor: str = "",
    product: str = "",
    asset: str = "",
    public_ip: str | None = None,
    get=None,  # noqa: ANN001
) -> Detection:
    """Classify how this machine can be reached. A given public IP is trusted (no request)."""
    public_ip = (public_ip or "").strip() or None
    if public_ip is not None and not is_public_ipv4(public_ip):
        raise ValueError(f"THINAIR_PUBLIC_IP is not a public IPv4 address: {public_ip!r}")
    if public_ip is None:
        public_ip = fetch_public_ipv4(get=get)
    cloud = identify_cloud(vendor, product, asset)
    local = (local_ip or "").strip() or None
    return Detection(classify(local, public_ip, cloud), public_ip, local, cloud)


# ---- HTTPS readiness ----------------------------------------------------------------------------
@dataclass(frozen=True)
class HttpsResult:
    ok: bool
    reason: str  # phone-readable
    # The address could not be reached at all (no route, refused, timed out, no DNS): says nothing
    # about its certificate, and from inside Docker it is expected for the machine's own address.
    unreachable: bool = False


def https_ready(
    host: str,
    *,
    via: str | None = None,
    timeout_s: float = 6,
    cafile: str | None = None,
) -> HttpsResult:
    """Does `https://host/healthz` answer with a certificate valid for `host`?

    `via` = `name:port` to connect to instead of the host's public address (Caddy's container
    inside Compose), keeping the TLS name check for `host`. No redirects, no body is read beyond
    the status line.
    """
    target, _, port = (via or f"{host}:443").rpartition(":")
    ctx = ssl.create_default_context(cafile=cafile)
    try:
        with socket.create_connection((target, int(port)), timeout=timeout_s) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(
                    f"GET /healthz HTTP/1.1\r\nHost: {host}\r\nUser-Agent: thinair\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                status = tls.recv(64).split(b"\r\n", 1)[0].decode("latin-1")
    except ssl.SSLCertVerificationError as e:
        return HttpsResult(False, f"no valid certificate for {host} yet ({e.verify_message})")
    except ssl.SSLError as e:
        # Caddy refuses the handshake while it has no certificate for the name at all.
        return HttpsResult(False, f"no certificate for {host} yet ({e.reason or type(e).__name__})")
    except socket.gaierror:
        return HttpsResult(False, f"{target} does not resolve", unreachable=True)
    except (OSError, ValueError) as e:
        return HttpsResult(
            False, f"could not connect to {target}:{port} ({type(e).__name__})", unreachable=True
        )
    parts = status.split()
    if len(parts) >= 2 and parts[1] == "200":
        return HttpsResult(True, f"https://{host} has a valid certificate and answers")
    return HttpsResult(False, f"https://{host} answered {status or 'nothing'}")


def resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, 443)
    except OSError:
        return False
    return True


# ---- "open ports 80 and 443" hints --------------------------------------------------------------
def firewall_hint(cloud: str | None) -> list[str]:
    """Lines telling the owner where to open ports 80 and 443 (what only they can do)."""
    if cloud == "oracle":
        return [
            "Oracle's cloud firewall is blocking ports 80 and 443. Open them once:",
            "  Oracle console > Networking > Virtual cloud networks > your network >",
            "  Security Lists > Default Security List > Add Ingress Rules:",
            "  Source 0.0.0.0/0, IP protocol TCP, Destination port range 80,443",
        ]
    where = {
        "gcp": "Google Cloud: add a VPC firewall rule allowing TCP 80 and 443 to this VM.",
        "azure": "Azure: add inbound rules for TCP 80 and 443 to this VM's network security group.",
    }.get(cloud or "")
    if where:
        return ["Ports 80 and 443 are not reachable from the internet yet.", where]
    return [
        "Ports 80 and 443 are not reachable from the internet yet. Open them to 0.0.0.0/0:",
        "  on a cloud server, in its firewall (AWS: the instance's security group);",
        "  at home, forward both ports on your router to this machine.",
    ]
