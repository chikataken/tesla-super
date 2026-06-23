"""Fetch the live Tesla TOTP code from a Vaultwarden (Bitwarden-compatible) server.

Why this exists: the Tesla supplier portal forces 2FA on every login, and the
clipboard feeder proved unreliable. Vaultwarden stores the TOTP seed; this module
pulls it over the API and computes the current 6-digit code on demand — no browser,
no clipboard.

How it works (all pure-Python, no `bw` CLI / Node needed):
  1. auth with a personal API key (client_credentials) -> access token + the
     account's protected symmetric key + KDF params
  2. derive the master key from the master password (PBKDF2 or Argon2id)
  3. HKDF-stretch the master key and decrypt the protected symmetric key -> user key
  4. GET /api/sync, find the Tesla item, decrypt its login.totp EncString
  5. compute the current TOTP

Secrets (in the shared, gitignored secrets/.env — values are NEVER logged):
  BW_SERVER        e.g. https://vault.example.com   (your Vaultwarden base URL)
  BW_EMAIL         the vault account email
  BW_PASSWORD      the vault master password
  BW_CLIENT_ID     personal API key client_id   (Vaultwarden: Account Settings ->
  BW_CLIENT_SECRET personal API key client_secret  Security -> Keys -> View API Key)
  BW_TESLA_ITEM    item name (substring match) OR the item's UUID. default "Tesla"
"""
import base64
import hashlib
import hmac
import os
import uuid

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

import config


class VaultError(RuntimeError):
    pass


# --- small helpers -------------------------------------------------------
def _ci(d: dict, key: str):
    """Case-insensitive dict get — Bitwarden/Vaultwarden vary between PascalCase
    and camelCase across versions/endpoints."""
    if d is None:
        return None
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC5869 HKDF-Expand with SHA-256 (Bitwarden uses the master key as the PRK
    directly — expand only, no extract)."""
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def _stretch(master_key: bytes) -> tuple[bytes, bytes]:
    return _hkdf_expand(master_key, b"enc", 32), _hkdf_expand(master_key, b"mac", 32)


def _decrypt_encstring(s: str, enc_key: bytes, mac_key: bytes | None) -> bytes:
    """Decrypt a Bitwarden EncString. Supports type 0 (AES-CBC, no MAC) and type 2
    (AES-256-CBC + HMAC-SHA256). Raises on MAC mismatch."""
    if not s or "." not in s:
        raise VaultError("not an EncString")
    etype, rest = s.split(".", 1)
    parts = rest.split("|")
    if etype == "2":
        iv, ct, mac = (base64.b64decode(p) for p in parts[:3])
        if mac_key is not None:
            expect = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
            if not hmac.compare_digest(expect, mac):
                raise VaultError("EncString MAC mismatch")
    elif etype == "0":
        iv, ct = (base64.b64decode(p) for p in parts[:2])
    else:
        raise VaultError(f"unsupported EncString type {etype}")
    dec = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    return pt[: -pt[-1]]            # strip PKCS7 padding


def _device_headers() -> dict:
    return {"Device-Type": "21", "User-Agent": "tesla-reconcile-vault/1.0"}


# --- the flow ------------------------------------------------------------
def _master_key(server: str, email: str, password: str) -> bytes:
    r = requests.post(f"{server}/identity/accounts/prelogin",
                      json={"email": email}, timeout=15, headers=_device_headers())
    r.raise_for_status()
    p = r.json()
    kdf = _ci(p, "kdf") or 0
    iters = _ci(p, "kdfIterations") or 600000
    pw, salt = password.encode(), email.strip().lower().encode()
    if kdf == 0:                                   # PBKDF2-SHA256
        return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                          salt=salt, iterations=iters).derive(pw)
    if kdf == 1:                                   # Argon2id
        from argon2.low_level import hash_secret_raw, Type
        mem_mib = _ci(p, "kdfMemory") or 64
        par = _ci(p, "kdfParallelism") or 4
        return hash_secret_raw(secret=pw, salt=hashlib.sha256(salt).digest(),
                               time_cost=iters, memory_cost=mem_mib * 1024,
                               parallelism=par, hash_len=32, type=Type.ID)
    raise VaultError(f"unknown KDF type {kdf}")


def _token_and_key(server: str, client_id: str, client_secret: str):
    data = {
        "grant_type": "client_credentials",
        "scope": "api",
        "client_id": client_id,
        "client_secret": client_secret,
        "deviceType": "21",
        "deviceIdentifier": str(uuid.uuid4()),
        "deviceName": "tesla-reconcile",
    }
    r = requests.post(f"{server}/identity/connect/token", data=data,
                      timeout=15, headers=_device_headers())
    if r.status_code != 200:
        raise VaultError(f"token request failed ({r.status_code}): {r.text[:200]}")
    j = r.json()
    return j["access_token"], _ci(j, "Key")


def _user_key(master_key: bytes, protected_key: str) -> tuple[bytes, bytes]:
    senc, smac = _stretch(master_key)
    raw = _decrypt_encstring(protected_key, senc, smac)
    if len(raw) == 64:
        return raw[:32], raw[32:]
    if len(raw) == 32:
        return raw, None
    raise VaultError(f"unexpected user key length {len(raw)}")


def _find_totp(server: str, token: str, enc_key: bytes, mac_key, want: str) -> str:
    r = requests.get(f"{server}/api/sync?excludeDomains=true",
                     headers={"Authorization": f"Bearer {token}", **_device_headers()},
                     timeout=30)
    r.raise_for_status()
    ciphers = _ci(r.json(), "ciphers") or []
    want_l = want.strip().lower()
    for c in ciphers:
        login = _ci(c, "login")
        totp = _ci(login, "totp") if login else None
        if not totp:
            continue
        cid = (_ci(c, "id") or "").lower()
        name_enc = _ci(c, "name")
        try:
            name = _decrypt_encstring(name_enc, enc_key, mac_key).decode("utf-8", "replace") if name_enc else ""
        except Exception:
            name = ""
        if want_l == cid or (want_l in name.lower()):
            return _decrypt_encstring(totp, enc_key, mac_key).decode("utf-8", "replace")
    raise VaultError(f"no vault item with a TOTP matching {want!r}")


def _totp_now(secret: str) -> tuple[str, int]:
    import pyotp, time
    secret = secret.strip()
    if secret.lower().startswith("otpauth://"):
        otp = pyotp.parse_uri(secret)
    else:
        otp = pyotp.TOTP(secret.replace(" ", ""))
    remaining = otp.interval - (int(time.time()) % otp.interval)
    return otp.now(), remaining


def get_tesla_totp() -> tuple[str, int]:
    """Return (current 6-digit code, seconds remaining in its window)."""
    server = (os.getenv("BW_SERVER", "")).strip().rstrip("/")
    # Tolerate a missing/typo'd scheme (e.g. "https//host" or bare "host").
    if server and not server.startswith(("http://", "https://")):
        server = "https://" + server.split("//", 1)[-1]
    email = os.getenv("BW_EMAIL", "").strip()
    password = os.getenv("BW_PASSWORD", "")
    cid = os.getenv("BW_CLIENT_ID", "").strip()
    csecret = os.getenv("BW_CLIENT_SECRET", "").strip()
    item = os.getenv("BW_TESLA_ITEM", "Tesla").strip()
    missing = [k for k, v in {"BW_SERVER": server, "BW_EMAIL": email,
               "BW_PASSWORD": password, "BW_CLIENT_ID": cid,
               "BW_CLIENT_SECRET": csecret}.items() if not v]
    if missing:
        raise VaultError("missing secrets: " + ", ".join(missing))
    mk = _master_key(server, email, password)
    token, protected = _token_and_key(server, cid, csecret)
    if not protected:
        raise VaultError("token response had no protected Key")
    enc_key, mac_key = _user_key(mk, protected)
    secret = _find_totp(server, token, enc_key, mac_key, item)
    return _totp_now(secret)


if __name__ == "__main__":
    try:
        code, secs = get_tesla_totp()
        print(f"OK: code {code[0]}****{code[-1]} ({len(code)} digits), {secs}s left in window")
    except Exception as e:
        print("FAILED:", type(e).__name__, e)
        raise SystemExit(1)
