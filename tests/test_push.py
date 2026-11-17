"""Web Push: RFC 8291 encryption (against the RFC's own test vector), VAPID, the endpoint allowlist,
subscriptions, per-user routing, failure handling, the attention debounce. No network, no Docker."""

import asyncio
import json
import os
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from thinair import doctor, push
from thinair.api import auth as api_auth
from thinair.api import push_api
from thinair.api.app import create_app
from thinair.auth import create_token, revoke_token
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import (
    Job,
    JobKind,
    JobResult,
    JobStatus,
    PushSubscription,
    Repo,
    User,
)
from thinair.db.session import get_engine, session_scope
from thinair.jobs.service import ExecResult
from thinair.notify import WebPushNotifier, job_message, push_payload
from thinair.push import b64u, unb64u
from thinair.users import disable_user

ADMIN = "t" * 32
H = {"Authorization": f"Bearer {ADMIN}"}
FCM = "https://fcm.googleapis.com/fcm/send/abc123"


# ---- RFC 8291 Appendix A: the published test vector -----------------------------------------------
RFC = {
    "plaintext": "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "auth_secret": "BTBZMqHH6r4Tts7J_aSIgg",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    # The body is the 86-octet header, then the record (ciphertext + tag), as printed in the RFC.
    "header": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
    "record": "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ",
}


def test_encryption_matches_the_rfc_8291_test_vector() -> None:
    as_private = ec.derive_private_key(
        int.from_bytes(unb64u(RFC["as_private"]), "big"), ec.SECP256R1()
    )
    body = push.encrypt(
        unb64u(RFC["plaintext"]),
        unb64u(RFC["ua_public"]),
        unb64u(RFC["auth_secret"]),
        as_private=as_private,
        salt=unb64u(RFC["salt"]),
    )
    assert unb64u(RFC["plaintext"]) == b"When I grow up, I want to be a watermelon"
    assert body == unb64u(RFC["header"]) + unb64u(RFC["record"])


def new_browser():
    """A fake browser: its key pair, auth secret and the subscription keys it would send."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = os.urandom(16)
    return private, public, auth, {"p256dh": b64u(public), "auth": b64u(auth)}


def decrypt(body: bytes, ua_private, ua_public: bytes, auth: bytes) -> bytes:
    """What a browser does (RFC 8291 section 3.4), to prove our output is readable by one."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public, record = body[21 : 21 + idlen], body[21 + idlen :]
    assert rs == 4096 and idlen == 65
    ecdh = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    )
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + ua_public + as_public).derive(ecdh)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, record, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_a_fresh_message_round_trips_and_uses_new_keys_each_time() -> None:
    private, public, auth, _ = new_browser()
    one = push.encrypt(b'{"hello":"phone"}', public, auth)
    two = push.encrypt(b'{"hello":"phone"}', public, auth)
    assert decrypt(one, private, public, auth) == b'{"hello":"phone"}'
    assert one != two and one[:16] != two[:16]  # a fresh salt and ephemeral key per message


def test_oversized_messages_are_refused() -> None:
    _, public, auth, _ = new_browser()
    with pytest.raises(push.PushError):
        push.encrypt(b"x" * 5000, public, auth)


def test_subscription_keys_are_validated() -> None:
    _, _, _, keys = new_browser()
    assert push.check_subscription_keys(keys["p256dh"], keys["auth"])
    for p256dh, auth in (
        ("###", keys["auth"]),
        (keys["p256dh"], "AAAA"),
        (b64u(b"\x04" + b"\x01" * 64), keys["auth"]),  # not a point on the curve
        (b64u(b"\x02" + b"\x00" * 64), keys["auth"]),  # not an uncompressed point
        (b64u(b"\x04" * 10), keys["auth"]),
    ):
        with pytest.raises(ValueError):
            push.check_subscription_keys(p256dh, auth)


# ---- VAPID ----------------------------------------------------------------------------------------
def test_vapid_key_is_generated_once_stored_0600_and_never_printed() -> None:
    key = push.load_vapid()
    assert key is not None and "BEGIN" not in repr(key)
    assert push.load_vapid().public_b64u == key.public_b64u  # stable across loads
    assert len(unb64u(key.public_b64u)) == 65
    path = get_settings().data_dir / "secrets" / "vapid.json"
    assert json.loads(path.read_bytes())["pem"].startswith("-----BEGIN PRIVATE KEY")
    if os.name == "posix":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_vapid_jwt_verifies_with_the_public_key() -> None:
    key = push.load_vapid()
    now = 1_800_000_000
    token = push.vapid_jwt(key, "https://fcm.googleapis.com", "https://x.example", now=now)
    head, body, sig = token.split(".")
    assert json.loads(unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(unb64u(body))
    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": now + 43200,
        "sub": "https://x.example",
    }
    assert claims["exp"] - now <= 86400  # RFC 8292: at most 24 hours
    raw = unb64u(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(key.public_b64u))
    pub.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if invalid


def test_web_push_off_means_no_key_is_ever_generated(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_WEB_PUSH", "false")
    get_settings.cache_clear()
    assert push.load_vapid() is None
    assert not (get_settings().data_dir / "secrets" / "vapid.json").exists()


# ---- the endpoint allowlist (the server will POST to these) -----------------------------------------
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/QAbc",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
    ],
)
def test_known_push_services_are_allowed(endpoint) -> None:
    assert push.endpoint_allowed(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",  # not https
        "https://evil.example/fcm.googleapis.com",
        "https://fcm.googleapis.com.evil.example/x",  # suffix trick
        "https://evilfcm.googleapis.com/x",  # not the exact host
        "https://notpush.apple.com.evil.io/x",
        "https://user:pw@fcm.googleapis.com/x",  # credentials
        "https://fcm.googleapis.com:8443/x",  # a port
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://localhost/x",
        "https://[::1]/x",
        "https://push.apple.com/x",  # the bare suffix is not a subdomain
        "https://fcm.googleapis.com/" + "a" * 2100,
        "file:///etc/passwd",
        "",
        "not a url",
    ],
)
def test_other_endpoints_are_refused(endpoint) -> None:
    assert not push.endpoint_allowed(endpoint)


# ---- sending --------------------------------------------------------------------------------------
class Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class FakePushService:
    def __init__(self, status: int = 201) -> None:
        self.status, self.calls = status, []
        self.statuses: dict[str, int] = {}

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "redirects": allow_redirects}
        )
        return Resp(self.statuses.get(url, self.status))


def test_send_posts_an_encrypted_vapid_signed_message_and_never_follows_redirects() -> None:
    private, public, auth, keys = new_browser()
    svc = FakePushService()
    push.send(FCM, keys["p256dh"], keys["auth"], {"a": 1}, session=svc)
    call = svc.calls[0]
    h = call["headers"]
    assert call["url"] == FCM and call["redirects"] is False
    assert h["Content-Encoding"] == "aes128gcm" and h["TTL"] == "86400"
    assert h["Authorization"].startswith("vapid t=") and ", k=" in h["Authorization"]
    assert json.loads(decrypt(call["data"], private, public, auth)) == {"a": 1}
    assert b'"a"' not in call["data"]  # the plaintext is not in the body


def test_a_disallowed_endpoint_is_refused_before_any_request() -> None:
    _, _, _, keys = new_browser()
    svc = FakePushService()
    with pytest.raises(push.PushError, match="not allowed"):
        push.send("https://169.254.169.254/x", keys["p256dh"], keys["auth"], {}, session=svc)
    assert svc.calls == []


@pytest.mark.parametrize(("status", "gone"), [(404, True), (410, True), (429, False), (500, False)])
def test_push_service_answers(status, gone) -> None:
    _, _, _, keys = new_browser()
    with pytest.raises(push.PushError) as e:
        push.send(FCM, keys["p256dh"], keys["auth"], {}, session=FakePushService(status))
    assert e.value.gone is gone and e.value.status == status


def test_network_errors_do_not_leak_the_endpoint() -> None:
    import requests

    class Boom:
        def post(self, url, **kw):
            raise requests.ConnectionError(f"failed to connect to {url}")

    _, _, _, keys = new_browser()
    with pytest.raises(push.PushError) as e:
        push.send(FCM, keys["p256dh"], keys["auth"], {}, session=Boom())
    assert "abc123" not in str(e.value) and "ConnectionError" in str(e.value)


# ---- subscriptions through the API ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_PUBLIC_URL", "")
    get_settings.cache_clear()
    api_auth._failures.clear()
    push_api._last_test.clear()


async def noop(ctx, _mgr):
    return ExecResult()


@pytest.fixture
def client():
    with TestClient(create_app({JobKind.PROFILE: noop, JobKind.AGENT: noop})) as c:
        yield c


def sub_body(endpoint=FCM, keys=None) -> dict:
    return {"endpoint": endpoint, "keys": keys or new_browser()[3]}


def member(name="sam") -> tuple[str, dict]:
    with session_scope() as s:
        u = User(name=name)
        s.add(u)
        s.commit()
        tok = create_token(s, "phone", user_id=u.id)
        return u.id, {"Authorization": f"Bearer {tok}"}


def test_push_routes_need_a_login(client) -> None:
    assert client.get("/api/push").status_code == 401
    assert client.post("/api/push/subscriptions", json=sub_body()).status_code == 401
    assert (
        client.request("DELETE", "/api/push/subscriptions", json={"endpoint": FCM}).status_code
        == 401
    )
    assert client.post("/api/push/test").status_code == 401


def test_status_gives_the_public_key_and_whether_this_login_subscribed(client) -> None:
    st = client.get("/api/push", headers=H).json()
    assert st["enabled"] and len(unb64u(st["public_key"])) == 65 and st["subscribed"] is False
    assert client.post("/api/push/subscriptions", json=sub_body(), headers=H).status_code == 201
    assert client.get("/api/push", headers=H).json()["subscribed"] is True


def test_subscribe_validates_endpoint_and_keys(client) -> None:
    bad = client.post(
        "/api/push/subscriptions", json=sub_body("https://169.254.169.254/x"), headers=H
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_endpoint"
    keys = sub_body()["keys"]
    r = client.post("/api/push/subscriptions", json=sub_body(keys={**keys, "auth": "x"}), headers=H)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_keys"
    assert (
        client.post("/api/push/subscriptions", json={"endpoint": FCM}, headers=H).status_code == 422
    )
    with session_scope() as s:
        assert s.exec(__import__("sqlmodel").select(PushSubscription)).first() is None


def test_one_subscription_per_login_and_the_same_browser_moves_between_logins(client) -> None:
    uid, sam = member()
    client.post("/api/push/subscriptions", json=sub_body(FCM + "1"), headers=H)
    client.post("/api/push/subscriptions", json=sub_body(FCM + "2"), headers=H)  # replaces
    from sqlmodel import select

    with session_scope() as s:
        assert [x.endpoint for x in s.exec(select(PushSubscription))] == [FCM + "2"]
    # the same browser signs in as sam: the subscription now belongs to sam
    client.post("/api/push/subscriptions", json=sub_body(FCM + "2"), headers=sam)
    with session_scope() as s:
        rows = s.exec(select(PushSubscription)).all()
        assert len(rows) == 1 and rows[0].user_id == uid


def test_unsubscribe_only_removes_your_own(client) -> None:
    _, sam = member()
    client.post("/api/push/subscriptions", json=sub_body(FCM + "own"), headers=H)
    assert (
        client.request(
            "DELETE", "/api/push/subscriptions", json={"endpoint": FCM + "own"}, headers=sam
        ).status_code
        == 204
    )
    from sqlmodel import select

    with session_scope() as s:
        assert len(s.exec(select(PushSubscription)).all()) == 1  # sam could not delete the owner's
    client.request("DELETE", "/api/push/subscriptions", json={"endpoint": FCM + "own"}, headers=H)
    with session_scope() as s:
        assert s.exec(select(PushSubscription)).first() is None


def test_a_person_can_only_have_so_many_devices(client) -> None:
    uid, _ = member()
    with session_scope() as s:
        for i in range(push_api.MAX_PER_USER):
            tok = create_token(s, f"d{i}", user_id=uid)
            s.add(
                PushSubscription(
                    user_id=uid,
                    token_id=_token_id(s, tok),
                    endpoint=f"{FCM}x{i}",
                    p256dh="a",
                    auth="b",
                )
            )
        s.commit()
        extra = create_token(s, "one more", user_id=uid)
    r = client.post(
        "/api/push/subscriptions",
        json=sub_body(FCM + "new"),
        headers={"Authorization": f"Bearer {extra}"},
    )
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_devices"


def _token_id(session, token: str) -> str:
    from thinair.auth import verify_token

    return verify_token(session, token).id


def test_disabled_push_turns_the_feature_off(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_WEB_PUSH", "false")
    get_settings.cache_clear()
    with TestClient(create_app({})) as c:
        assert c.get("/api/push", headers=H).json() == {
            "enabled": False,
            "public_key": None,
            "subscribed": False,
        }
        assert c.post("/api/push/subscriptions", json=sub_body(), headers=H).status_code == 409
        assert c.post("/api/push/test", headers=H).status_code == 409
        assert "push" not in c.get("/api/meta").json()["features"]
        assert c.app.state.jobs._push_notifier is None
    assert not (get_settings().data_dir / "secrets" / "vapid.json").exists()


# ---- subscriptions die with their token ---------------------------------------------------------------
def test_revoking_a_token_deletes_its_subscriptions(client) -> None:
    from sqlmodel import select

    _, sam = member()
    client.post("/api/push/subscriptions", json=sub_body(FCM + "sam"), headers=sam)
    client.post("/api/push/subscriptions", json=sub_body(FCM + "owner"), headers=H)
    with session_scope() as s:
        sam_sub = s.exec(
            select(PushSubscription).where(PushSubscription.endpoint == FCM + "sam")
        ).one()
        assert revoke_token(s, sam_sub.token_id)
    with session_scope() as s:
        assert [x.endpoint for x in s.exec(select(PushSubscription))] == [FCM + "owner"]


def test_disabling_a_user_deletes_all_their_subscriptions(client) -> None:
    from sqlmodel import select

    member("amy")
    amy_token = None
    with session_scope() as s:
        u = s.exec(select(User).where(User.name == "amy")).one()
        amy_token = create_token(s, "second", user_id=u.id)
    client.post(
        "/api/push/subscriptions",
        json=sub_body(FCM + "amy"),
        headers={"Authorization": f"Bearer {amy_token}"},
    )
    client.post("/api/push/subscriptions", json=sub_body(FCM + "owner"), headers=H)
    with session_scope() as s:
        disable_user(s, "amy")
    with session_scope() as s:
        assert [x.endpoint for x in s.exec(select(PushSubscription))] == [FCM + "owner"]


# ---- the test notification ------------------------------------------------------------------------
def test_test_notification_reaches_only_your_own_devices(client, monkeypatch) -> None:
    svc = FakePushService()
    monkeypatch.setattr(
        WebPushNotifier,
        "__init__",
        lambda self, *a, **k: setattr(self, "_public", None) or setattr(self, "_session", svc),
    )
    _, sam = member()
    client.post("/api/push/subscriptions", json=sub_body(FCM + "sam"), headers=sam)
    client.post("/api/push/subscriptions", json=sub_body(FCM + "owner"), headers=H)
    r = client.post("/api/push/test", headers=H)
    assert r.status_code == 202 and r.json() == {"sent": 1}
    assert [c["url"] for c in svc.calls] == [FCM + "owner"]
    assert client.post("/api/push/test", headers=H).status_code == 429  # cooldown


# ---- job notifications: per owner, content rules, failures -------------------------------------------
def _job(
    repo_name="demo", user_id=None, status=JobStatus.SUCCEEDED, kind=JobKind.AGENT, **kw
) -> str:
    upgrade(get_engine())
    with session_scope() as s:
        repo = s.exec(__import__("sqlmodel").select(Repo).where(Repo.name == repo_name)).first()
        if repo is None:
            repo = Repo(url=f"https://github.com/o/{repo_name}", name=repo_name)
            s.add(repo)
            s.commit()
        job = Job(
            repo_id=repo.id,
            kind=kind,
            status=status,
            user_id=user_id,
            task="Fix the flaky date test\nsecret detail",
            **kw,
        )
        s.add(job)
        s.commit()
        s.add(
            JobResult(
                job_id=job.id,
                summary="SECRET SUMMARY TEXT",
                diff_text="SECRET DIFF",
                diff_stat={"files": 2, "insertions": 5, "deletions": 1},
            )
        )
        s.commit()
        return job.id


def _subscribe_directly(user_id, endpoint, token_name="t") -> None:
    upgrade(get_engine())
    _, _, _, keys = new_browser()
    with session_scope() as s:
        tok = create_token(s, token_name, user_id=user_id)
        s.add(
            PushSubscription(user_id=user_id, token_id=_token_id(s, tok), endpoint=endpoint, **keys)
        )
        s.commit()


def test_a_finished_job_notifies_its_owner_only_and_leaks_nothing_private() -> None:
    upgrade(get_engine())
    with session_scope() as s:
        u = User(name="sam")
        s.add(u)
        s.commit()
        sam_id = u.id
    _subscribe_directly(None, FCM + "owner", "o")
    _subscribe_directly(sam_id, FCM + "sam", "s")
    svc = FakePushService()
    job_id = _job(user_id=sam_id)
    asyncio.run(WebPushNotifier("https://h.example", session=svc)(job_id))
    assert [c["url"] for c in svc.calls] == [FCM + "sam"]  # not the owner's phone
    with session_scope() as s:
        sub = s.exec(
            __import__("sqlmodel")
            .select(PushSubscription)
            .where(PushSubscription.endpoint == FCM + "sam")
        ).one()
    # decrypt what sam's phone would receive
    payload = _decrypt_for(sub, svc.calls[0]["data"])
    n = payload["notification"]
    assert payload["web_push"] == 8030
    assert n["title"] == "✅ demo: Agent task done"
    assert n["body"].splitlines()[0] == "Fix the flaky date test"
    assert n["navigate"] == f"https://h.example/#/job/{job_id}" and n["tag"] == f"job-{job_id}"
    blob = json.dumps(payload)
    assert (
        "SECRET" not in blob and "secret detail" not in blob
    )  # no summary, diff or later task lines


_KEYS: dict[str, tuple] = {}


def _decrypt_for(sub: PushSubscription, body: bytes) -> dict:
    private, public, auth = _KEYS[sub.endpoint]
    return json.loads(decrypt(body, private, public, auth))


@pytest.fixture(autouse=True)
def _remember_browser_keys(monkeypatch):
    """Keep each fake browser's private key so tests can read what its phone received."""
    _KEYS.clear()
    real = globals()["new_browser"]

    def remembering():
        private, public, auth, keys = real()
        _KEYS[("pending")] = (private, public, auth)
        return private, public, auth, keys

    monkeypatch.setitem(globals(), "new_browser", remembering)
    orig_sub = globals()["_subscribe_directly"]

    def sub_directly(user_id, endpoint, token_name="t"):
        orig_sub(user_id, endpoint, token_name)
        _KEYS[endpoint] = _KEYS.pop("pending")

    monkeypatch.setitem(globals(), "_subscribe_directly", sub_directly)
    yield


def test_failures_are_counted_gone_subscriptions_deleted_and_success_resets() -> None:
    from sqlmodel import select

    upgrade(get_engine())
    _subscribe_directly(None, FCM + "a", "a")
    _subscribe_directly(None, FCM + "b", "b")
    _subscribe_directly(None, FCM + "c", "c")
    svc = FakePushService()
    svc.statuses = {FCM + "a": 410, FCM + "b": 500}
    job_id = _job()
    notifier = WebPushNotifier(session=svc)
    asyncio.run(notifier(job_id))
    with session_scope() as s:
        rows = {x.endpoint: x for x in s.exec(select(PushSubscription))}
    assert FCM + "a" not in rows  # 410: gone, deleted at once
    assert (
        rows[FCM + "b"].failures == 1
        and rows[FCM + "c"].failures == 0
        and rows[FCM + "c"].last_ok_at
    )
    for _ in range(4):
        asyncio.run(notifier(job_id))
    with session_scope() as s:
        left = {x.endpoint for x in s.exec(select(PushSubscription))}
    assert left == {FCM + "c"}  # five failures in a row removes b
    svc.statuses = {}


def test_cancelled_jobs_and_unknown_jobs_are_silent() -> None:
    _subscribe_directly(None, FCM + "x", "x")
    svc = FakePushService()
    cancelled = _job(status=JobStatus.CANCELLED)
    n = WebPushNotifier(session=svc)
    asyncio.run(n(cancelled))
    asyncio.run(n("does-not-exist"))
    assert svc.calls == []


def test_telegram_text_is_unchanged_by_the_shared_summary() -> None:
    job_id = _job(repo_name="api")
    text = job_message(job_id, "https://h.example/")
    assert (
        text
        == f"✅ api: Agent task done\nFix the flaky date test\n2 file(s) changed, +5 −1\nhttps://h.example/#/job/{job_id}"
    )
    assert job_message(job_id, None).endswith("+5 −1")


def test_payload_navigates_to_an_https_address_or_a_path() -> None:
    assert (
        push_payload("t", "b", "J", "tag", "https://h.example/")["notification"]["navigate"]
        == "https://h.example/#/job/J"
    )
    assert (
        push_payload("t", "b", "J", "tag", "http://insecure")["notification"]["navigate"]
        == "/#/job/J"
    )
    assert push_payload("t", "b", "J", "tag", None)["notification"]["navigate"] == "/#/job/J"


# ---- a broken or slow push never touches a job ------------------------------------------------------
def test_a_failing_or_slow_push_never_delays_or_fails_a_job(client) -> None:
    gate = threading.Event()
    called = threading.Event()

    async def slow_and_broken(job_id: str) -> None:
        called.set()
        while not gate.is_set():
            await asyncio.sleep(0.05)
        raise RuntimeError("push service exploded")

    client.app.state.jobs._push_notifier = slow_and_broken
    r = client.post("/api/jobs", json={"repo_url": "https://github.com/octo/demo"}, headers=H)
    job_id = r.json()["id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}", headers=H).json()
        if j["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert j["status"] == "succeeded"  # recorded while the notifier is still stuck
    assert called.wait(2)
    gate.set()  # let it fail: nothing happens to the job
    time.sleep(0.3)
    assert client.get(f"/api/jobs/{job_id}", headers=H).json()["status"] == "succeeded"


def test_telegram_failing_does_not_stop_web_push(client) -> None:
    seen = []

    async def telegram(job_id):
        raise RuntimeError("telegram down")

    async def web(job_id):
        seen.append(job_id)

    svc = client.app.state.jobs
    svc._notifier, svc._push_notifier = telegram, web
    r = client.post("/api/jobs", json={"repo_url": "https://github.com/octo/demo"}, headers=H)
    for _ in range(100):
        if seen:
            break
        time.sleep(0.05)
    assert seen == [r.json()["id"]]


# ---- attention pushes (approvals, your turn) ------------------------------------------------------------
def test_attention_is_debounced_to_one_push_per_job_per_minute(client, monkeypatch) -> None:
    calls = []

    async def attention(job_id: str, kind: str) -> None:
        calls.append((job_id, kind))

    svc = client.app.state.jobs
    svc._attention_notifier = attention
    clock = [1000.0]
    monkeypatch.setattr("thinair.jobs.service._now", lambda: clock[0])

    async def go():
        svc.attention("j1", "permission_request")
        svc.attention("j1", "waiting")  # within a minute: dropped
        svc.attention("j2", "waiting")  # another job: its own budget
        await asyncio.sleep(0.05)
        clock[0] += 61
        svc.attention("j1", "waiting")  # a minute later: allowed again
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert calls == [("j1", "permission_request"), ("j2", "waiting"), ("j1", "waiting")]


def test_attention_payload_names_the_need_and_goes_to_the_owner() -> None:
    _subscribe_directly(None, FCM + "me", "me")
    svc = FakePushService()
    job_id = _job(status=JobStatus.RUNNING)
    asyncio.run(WebPushNotifier(session=svc).attention(job_id, "permission_request"))
    with session_scope() as s:
        sub = s.exec(__import__("sqlmodel").select(PushSubscription)).one()
    n = _decrypt_for(sub, svc.calls[0]["data"])["notification"]
    assert n["title"] == "demo: Approval needed" and n["body"] == "Fix the flaky date test"


def test_the_agent_executor_raises_attention_for_the_right_events() -> None:
    import inspect

    from thinair.jobs import executors

    src = inspect.getsource(executors.agent_executor)
    assert '"permission_request", "waiting"' in src and "ctx.attention" in src


# ---- the web app side ---------------------------------------------------------------------------------
def test_service_worker_manifest_and_csp() -> None:
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "src" / "thinair" / "web" / "static"
    sw = (static / "sw.js").read_text(encoding="utf-8")
    assert 'addEventListener("push"' in sw and 'addEventListener("notificationclick"' in sw
    assert "sameOriginUrl" in sw  # a notification can only open this app's own addresses
    m = json.loads((static / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert (
        m["id"] == "/"
        and m["share_target"]["method"] == "GET"
        and m["share_target"]["action"] == "/"
    )
    assert set(m["share_target"]["params"]) == {"title", "text", "url"}


def test_doctor_push_check(monkeypatch) -> None:
    assert doctor._push().status == "ok" and "no phone" in doctor._push().detail
    push.load_vapid()
    if os.name == "posix":
        path = get_settings().data_dir / "secrets" / "vapid.json"
        path.chmod(0o644)
        c = doctor._push()
        assert c.status == "fail" and "chmod 600" in c.fix
    monkeypatch.setenv("THINAIR_WEB_PUSH", "false")
    get_settings.cache_clear()
    assert "off" in doctor._push().detail
