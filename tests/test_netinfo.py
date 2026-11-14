"""Phone reachability: sslip.io names, public address detection, HTTPS readiness, IP changes."""

import shutil
import socket
import ssl
import subprocess
import threading
from types import SimpleNamespace

import pytest
import requests
from typer.testing import CliRunner

from thinair import doctor, netinfo
from thinair.cli.main import app as cli
from thinair.config import get_settings

PUBLIC = "129.146.10.20"  # any globally routable address (documentation ranges are not)


# ---- names ---------------------------------------------------------------------------------------
def test_sslip_names_round_trip() -> None:
    assert netinfo.sslip_host(PUBLIC) == "129-146-10-20.sslip.io"
    assert netinfo.ip_from_sslip("129-146-10-20.sslip.io") == PUBLIC
    assert netinfo.ip_from_sslip("129-146-10-20.SSLIP.IO.") == PUBLIC
    for other in ("thinair.example.com", "1-2-3.sslip.io", "999-1-1-1.sslip.io", "", None):
        assert netinfo.ip_from_sslip(other) is None
    assert netinfo.host_of("https://129-146-10-20.sslip.io/x") == "129-146-10-20.sslip.io"


@pytest.mark.parametrize(
    "ip", ["10.0.0.5", "192.168.1.2", "100.64.1.1", "127.0.0.1", "203.0.113.7", "2001:db8::1", "x"]
)
def test_only_public_ipv4_gets_a_name(ip) -> None:
    assert not netinfo.is_public_ipv4(ip)
    with pytest.raises(ValueError):
        netinfo.sslip_host(ip)


# ---- which cloud and what kind of network --------------------------------------------------------
@pytest.mark.parametrize(
    ("vendor", "product", "asset", "cloud"),
    [
        ("QEMU", "Standard PC", "OracleCloud.com", "oracle"),
        ("Microsoft Corporation", "Virtual Machine", "7783-7084-3265-9085-8269-3286-77", "azure"),
        ("Microsoft Corporation", "Virtual Machine", "", None),  # Hyper-V at home is not Azure
        ("Google", "Google Compute Engine", "", "gcp"),
        ("DigitalOcean", "Droplet", "", "digitalocean"),
        ("Hetzner", "vServer", "", "hetzner"),
        ("Akamai", "", "", "akamai"),
        ("Amazon EC2", "t4g.small", "", None),  # AWS is told apart by a root-only file
        ("", "", "", None),
    ],
)
def test_identify_cloud_uses_cloud_init_strings(vendor, product, asset, cloud) -> None:
    assert netinfo.identify_cloud(vendor, product, asset) == cloud


@pytest.mark.parametrize(
    ("local", "public", "cloud", "kind"),
    [
        (PUBLIC, PUBLIC, None, "direct"),
        ("10.0.0.5", PUBLIC, "oracle", "cloud"),
        ("10.0.0.5", PUBLIC, "gcp", "cloud"),
        ("172.31.4.4", PUBLIC, None, "nat"),  # AWS, or a home router
        ("192.168.1.20", PUBLIC, "hetzner", "nat"),  # not a 1:1-NAT cloud
        ("100.70.1.2", PUBLIC, None, "none"),  # carrier-grade NAT: no way in
        ("10.0.0.5", None, "oracle", "none"),
        ("10.0.0.5", "10.0.0.5", None, "none"),
    ],
)
def test_classify(local, public, cloud, kind) -> None:
    assert netinfo.classify(local, public, cloud) == kind


def fake_get(text="", status=200, exc=None):
    calls = []

    def get(url, timeout, headers):
        calls.append(url)
        if exc:
            raise exc
        return SimpleNamespace(status_code=status, text=text)

    get.calls = calls
    return get


@pytest.mark.parametrize(
    ("get", "expected"),
    [
        (fake_get(f"{PUBLIC}\n"), PUBLIC),
        (fake_get("<html>blocked</html>"), None),
        (fake_get("10.1.2.3"), None),
        (fake_get(PUBLIC, status=503), None),
        (fake_get(exc=requests.ConnectionError("down")), None),
    ],
)
def test_public_ip_lookup_never_trusts_odd_answers(get, expected) -> None:
    assert netinfo.fetch_public_ipv4(get=get) == expected
    assert get.calls == [netinfo.PUBLIC_IP_URL]


def test_detect_uses_a_given_address_without_any_request() -> None:
    get = fake_get(exc=AssertionError("must not be called"))
    d = netinfo.detect("10.0.0.5", asset="OracleCloud.com", public_ip=PUBLIC, get=get)
    assert (d.kind, d.cloud, d.host) == ("cloud", "oracle", "129-146-10-20.sslip.io")
    assert "KIND=cloud\nPUBLIC_IP=129.146.10.20\n" in d.as_env() + "\n"
    with pytest.raises(ValueError, match="THINAIR_PUBLIC_IP"):
        netinfo.detect("10.0.0.5", public_ip="192.168.1.1")
    none = netinfo.detect("10.0.0.5", get=fake_get(exc=requests.Timeout()))
    assert none.kind == "none" and none.host is None and "HOST=\n" in none.as_env() + "\n"


def test_cli_detect_prints_installer_lines() -> None:
    r = CliRunner().invoke(cli, ["net", "detect", "--local-ip", PUBLIC, "--public-ip", PUBLIC])
    assert r.exit_code == 0, r.output
    lines = dict(line.split("=", 1) for line in r.output.strip().splitlines())
    assert lines == {
        "KIND": "direct",
        "PUBLIC_IP": PUBLIC,
        "LOCAL_IP": PUBLIC,
        "CLOUD": "",
        "HOST": "129-146-10-20.sslip.io",
    }
    bad = CliRunner().invoke(cli, ["net", "detect", "--public-ip", "10.0.0.1"])
    assert bad.exit_code == 2


# ---- HTTPS readiness against a real local TLS server ---------------------------------------------
@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "key.pem", d / "crt.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(crt), "-subj", "/CN=thinair.test",
         "-addext", "subjectAltName=DNS:thinair.test"],
        check=True, capture_output=True,
    )  # fmt: skip
    return str(crt), str(key)


@pytest.fixture
def tls_server(cert):
    """A one-thread HTTPS server answering every request with `status`."""
    crt, key = cert
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(crt, key)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    state = {"status": "200 OK"}

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1024)
                    tls.sendall(f"HTTP/1.1 {state['status']}\r\nContent-Length: 0\r\n\r\n".encode())
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    yield f"127.0.0.1:{sock.getsockname()[1]}", crt, state
    sock.close()


def test_https_ready_checks_the_certificate_for_the_name(tls_server) -> None:
    via, ca, state = tls_server
    ok = netinfo.https_ready("thinair.test", via=via, cafile=ca)
    assert ok.ok, ok.reason
    wrong = netinfo.https_ready("other.test", via=via, cafile=ca)
    assert not wrong.ok and "certificate" in wrong.reason
    untrusted = netinfo.https_ready("thinair.test", via=via)  # self-signed, system CAs only
    assert not untrusted.ok and "certificate" in untrusted.reason
    state["status"] = "502 Bad Gateway"
    bad = netinfo.https_ready("thinair.test", via=via, cafile=ca)
    assert not bad.ok and "502" in bad.reason


def test_https_ready_explains_unreachable_targets() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    res = netinfo.https_ready("thinair.test", via=f"127.0.0.1:{port}", timeout_s=2)
    assert not res.ok and "could not connect" in res.reason
    res = netinfo.https_ready("thinair.test", via="no-such-host.invalid:443", timeout_s=2)
    assert not res.ok and "resolve" in res.reason


# ---- IP changed: startup check and doctor ---------------------------------------------------------
@pytest.fixture
def sslip(monkeypatch):
    monkeypatch.setenv("THINAIR_DOMAIN", "129-146-10-20.sslip.io")
    monkeypatch.setenv("THINAIR_PUBLIC_URL", "https://129-146-10-20.sslip.io")
    get_settings.cache_clear()


def test_startup_origin_check(sslip, monkeypatch) -> None:
    from thinair.api.app import check_origin

    assert check_origin(lambda: PUBLIC) is None
    assert check_origin(lambda: None) is None  # lookup failed: say nothing rather than guess
    warning = check_origin(lambda: "129.146.99.1")
    assert "129.146.99.1" in warning and "installer again" in warning and "QR" in warning
    monkeypatch.setenv("THINAIR_DOMAIN", "thinair.example.com")
    get_settings.cache_clear()
    assert check_origin(lambda: pytest.fail("own domains are not derived from the IP")) is None


def test_startup_check_runs_once_and_never_blocks_the_server(sslip, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from thinair.api import app as app_mod

    calls = []
    monkeypatch.setattr(netinfo, "fetch_public_ipv4", lambda: calls.append(1) or "129.146.99.1")
    application = app_mod.create_app({})
    with TestClient(application) as c:
        assert c.get("/healthz").status_code == 200
    assert calls == [1]
    assert "129.146.99.1" in application.state.origin_warning


def test_doctor_public_address(sslip, monkeypatch) -> None:
    monkeypatch.setattr(netinfo, "fetch_public_ipv4", lambda: "129.146.99.1")
    [c] = doctor._public_address(online=True)
    assert c.status == "fail" and "129.146.99.1" in c.detail and "installer again" in c.fix
    monkeypatch.setattr(netinfo, "fetch_public_ipv4", lambda: PUBLIC)
    assert doctor._public_address(online=True)[0].status == "ok"
    monkeypatch.setattr(netinfo, "fetch_public_ipv4", lambda: None)
    assert doctor._public_address(online=True)[0].status == "warn"
    assert "not checked" in doctor._public_address(online=False)[0].detail


def test_doctor_has_no_address_check_without_sslip(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_DOMAIN", "thinair.example.com")
    get_settings.cache_clear()
    monkeypatch.setattr(netinfo, "fetch_public_ipv4", lambda: pytest.fail("no lookup needed"))
    assert doctor._public_address(online=True) == []


def test_doctor_https_check_explains_the_firewall(sslip, monkeypatch) -> None:
    seen = {}

    def fake(host, via=None):
        seen.update(host=host, via=via)
        return netinfo.HttpsResult(False, f"no certificate for {host} yet (x)")

    monkeypatch.setattr(netinfo, "https_ready", fake)
    monkeypatch.setattr(netinfo, "resolves", lambda name: name == "caddy")
    monkeypatch.setattr(netinfo, "local_cloud", lambda: "oracle")
    [c] = doctor._https(online=True)
    assert seen == {"host": "129-146-10-20.sslip.io", "via": "caddy:443"}
    assert c.status == "warn" and "Security Lists" in c.fix and "Caddy retries" in c.fix
    assert doctor._https(online=False) == []
    monkeypatch.setattr(
        netinfo, "https_ready", lambda host, via=None: netinfo.HttpsResult(True, "fine")
    )
    assert doctor._https(online=True)[0].status == "ok"


def test_cli_firewall_hint() -> None:
    oracle = CliRunner().invoke(cli, ["net", "firewall-hint", "--cloud", "oracle"])
    assert oracle.exit_code == 0 and "Security Lists" in oracle.output
    other = CliRunner().invoke(cli, ["net", "firewall-hint"])
    assert "security group" in other.output and "router" in other.output


def test_a_hand_set_public_ip_is_trusted_without_a_lookup(sslip, monkeypatch) -> None:
    """THINAIR_PUBLIC_IP (saved by the installer when given) wins: e.g. outgoing traffic leaves
    through another address than the one phones connect to."""
    from thinair.api.app import check_origin

    monkeypatch.setenv("THINAIR_PUBLIC_IP", PUBLIC)
    get_settings.cache_clear()
    monkeypatch.setattr(netinfo, "fetch_public_ipv4", lambda: pytest.fail("no lookup"))
    assert check_origin() is None
    [c] = doctor._public_address(online=False)  # nothing to look up, so offline checks it too
    assert c.status == "ok" and PUBLIC in c.detail
    monkeypatch.setenv("THINAIR_PUBLIC_IP", "129.146.99.1")
    get_settings.cache_clear()
    assert doctor._public_address(online=True)[0].status == "fail"
