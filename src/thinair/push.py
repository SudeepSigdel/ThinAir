"""Web Push: notifications to the phone without Telegram or any account.

Thinair sends an encrypted message to the push service the browser chose (Google, Mozilla,
Apple or Microsoft) and that service wakes the phone. Outbound only, nothing polls, idle cost is
zero. Standards: RFC 8291 (message encryption, `aes128gcm`), RFC 8188 (the record format) and
RFC 8292 (VAPID, the server's signed identity). Only `cryptography` is needed; no pywebpush.

Privacy: the payload is encrypted end to end to the browser, so the push service sees only the
endpoint and the size. Content rules are Telegram's: repo name, job kind, outcome, the first line
of the task, a short error. Never logs, diffs or summaries.

Safety: the server will POST to the subscription's endpoint, so an arbitrary URL would be a
request-forgery primitive. Endpoints must be https on a known push service host (`ALLOWED_HOSTS`),
with no credentials, port or IP literal, and redirects are never followed. The VAPID private key
lives in `secrets/vapid.json` (0600, like the GitHub App key); if stolen, someone could send
notifications to subscribed phones until it is regenerated. It grants access to nothing else.
"""

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from thinair.config import get_settings
from thinair.secrets import SecretError, read_secret, write_secret

log = logging.getLogger("thinair.push")

VAPID_SECRET = "vapid.json"  # noqa: S105 - a file name, not a secret
RECORD_SIZE = 4096
MAX_PAYLOAD = 3800  # RFC 8030 push services accept at least 4096 bytes of encrypted body
TTL_S = 86400
# Hosts the server may POST to. Exact names or `*.suffix` (the push service's own subdomains).
ALLOWED_SUFFIXES = (
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)
ALLOWED_HOSTS = ("fcm.googleapis.com",)
_HOST_OK = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class PushError(Exception):
    """A push could not be sent. `gone` means the subscription no longer exists (delete it)."""

    def __init__(self, message: str, *, gone: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.gone, self.status = gone, status


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---- endpoint allowlist ---------------------------------------------------------------------
def endpoint_allowed(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    if len(endpoint) > 2000 or not _HOST_OK.match(host) or host.replace(".", "").isdigit():
        return False
    return host in ALLOWED_HOSTS or any(host.endswith("." + s) for s in ALLOWED_SUFFIXES)


# ---- VAPID (RFC 8292) -----------------------------------------------------------------------
@dataclass(frozen=True, repr=False)
class VapidKey:
    private: ec.EllipticCurvePrivateKey

    def __repr__(self) -> str:
        return "VapidKey(***)"

    @property
    def public_b64u(self) -> str:
        raw = self.private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return b64u(raw)


def load_vapid(create: bool = True) -> VapidKey | None:
    """The server's VAPID key, generated on first use (when THINAIR_WEB_PUSH is on)."""
    try:
        raw = read_secret(VAPID_SECRET)
    except SecretError:
        raw = None
    if raw is not None:
        try:
            key = serialization.load_pem_private_key(json.loads(raw)["pem"].encode(), password=None)
            if isinstance(key, ec.EllipticCurvePrivateKey):
                return VapidKey(key)
        except (ValueError, KeyError, TypeError):
            log.warning("the VAPID key file is damaged; generating a new one")
    if not create or not get_settings().web_push:
        return None
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    write_secret(VAPID_SECRET, json.dumps({"pem": pem}).encode())
    return VapidKey(key)


def vapid_subject() -> str:
    url = (get_settings().public_url or "").strip()
    return url.rstrip("/") if url.startswith("https://") else "mailto:thinair@example.com"


def vapid_jwt(key: VapidKey, audience: str, subject: str, now: float | None = None) -> str:
    """ES256 JWT proving the push comes from the holder of the key the browser subscribed to.
    `aud` is the push service's origin; `exp` at most 24 h (we use 12 h)."""
    t = int(time.time() if now is None else now)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(
        json.dumps(
            {"aud": audience, "exp": t + 43200, "sub": subject}, separators=(",", ":")
        ).encode()
    )
    der = key.private.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


# ---- message encryption (RFC 8291 + RFC 8188) -------------------------------------------------
def check_subscription_keys(p256dh: str, auth: str) -> tuple[bytes, bytes]:
    """Decode and validate a subscription's keys; ValueError if they can't be right."""
    try:
        ua_public, auth_secret = unb64u(p256dh), unb64u(auth)
    except (ValueError, TypeError):
        raise ValueError("the subscription keys are not base64url") from None
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("the subscription keys have the wrong size")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    except ValueError:
        raise ValueError("the subscription's public key is not a valid P-256 point") from None
    return ua_public, auth_secret


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """The `aes128gcm` request body for one message: header (salt, record size, our public key)
    followed by one encrypted record. `as_private` and `salt` are only for the RFC's test vector."""
    if len(plaintext) > MAX_PAYLOAD:
        raise PushError("the notification is too large")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = as_private.exchange(ec.ECDH(), ua_key)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, key_info).derive(ecdh)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 0x02: the last record
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + record


# ---- sending ----------------------------------------------------------------------------------
def send(
    endpoint: str,
    p256dh: str,
    auth: str,
    payload: dict,
    *,
    key: VapidKey | None = None,
    session=None,
) -> None:
    """POST one encrypted notification. Blocking: call from a thread. Raises PushError; `gone`
    when the push service says the subscription no longer exists (404/410)."""
    import requests

    if not endpoint_allowed(endpoint):
        raise PushError("that push endpoint is not allowed")
    key = key or load_vapid()
    if key is None:
        raise PushError("web push is turned off")
    try:
        ua_public, auth_secret = check_subscription_keys(p256dh, auth)
    except ValueError as e:
        raise PushError(str(e), gone=True) from None
    body = encrypt(json.dumps(payload, separators=(",", ":")).encode(), ua_public, auth_secret)
    parts = urlsplit(endpoint)
    jwt = vapid_jwt(key, f"{parts.scheme}://{parts.netloc}", vapid_subject())
    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_S),
        "Urgency": "normal",
        "Authorization": f"vapid t={jwt}, k={key.public_b64u}",
    }
    http = session or requests
    try:
        resp = http.post(endpoint, data=body, headers=headers, timeout=15, allow_redirects=False)
    except requests.RequestException as e:
        # The exception text contains the endpoint, which identifies the device: type only.
        raise PushError(f"could not reach the push service ({type(e).__name__})") from None
    if resp.status_code in (200, 201, 202):
        return
    if resp.status_code in (404, 410):
        raise PushError("the subscription is gone", gone=True, status=resp.status_code)
    raise PushError(f"the push service answered {resp.status_code}", status=resp.status_code)
