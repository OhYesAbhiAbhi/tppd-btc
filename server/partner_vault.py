"""Vault for partner Delta keys.

The sign-up page encrypts the key and secret IN THE BROWSER (WebCrypto): a random AES-256-GCM key seals
the JSON {"key","secret"}, and RSA-OAEP-3072 (SHA-256) wraps that AES key with the vault's PUBLIC key.
Only ciphertext reaches the server. The PRIVATE key lives in a file readable only by the vault user
(/etc/tppd/vault.key, mode 600); the web portal can't read it.

    python partner_vault.py init  /etc/tppd/vault.key  /etc/tppd/vault.pub     # once, as root
    python partner_vault.py open  /etc/tppd/vault.key  < envelope.json         # -> {"key","secret"} on stdout
"""
import base64
import json
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OAEP = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def make_keys():
    """(private PEM bytes, public key as base64 SPKI DER for the browser)."""
    k = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    spki = k.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.b64encode(spki).decode()


def seal(public_spki_b64, payload: dict):
    """What the browser does (used by tests and tools): -> envelope dict of base64 strings."""
    pub = serialization.load_der_public_key(base64.b64decode(public_spki_b64))
    aes = AESGCM.generate_key(bit_length=256)
    iv = os.urandom(12)
    ct = AESGCM(aes).encrypt(iv, json.dumps(payload).encode(), None)
    return {"v": 1, "wk": base64.b64encode(pub.encrypt(aes, OAEP)).decode(),
            "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}


def open_envelope(private_pem: bytes, env: dict) -> dict:
    if not isinstance(env, dict) or env.get("v") != 1:
        raise ValueError("unknown envelope")
    k = serialization.load_pem_private_key(private_pem, password=None)
    aes = k.decrypt(base64.b64decode(env["wk"]), OAEP)
    data = json.loads(AESGCM(aes).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None))
    if not (isinstance(data, dict) and data.get("key") and data.get("secret")):
        raise ValueError("envelope has no key/secret")
    return {"key": str(data["key"]).strip(), "secret": str(data["secret"]).strip()}


def init(priv_path, pub_path):
    if os.path.exists(priv_path):
        print(f"vault key already exists: {priv_path} (kept)")
        return
    priv, pub = make_keys()
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv)
    with open(pub_path, "w") as f:
        f.write(pub + "\n")
    print(f"vault key made: {priv_path} (600), public key: {pub_path}")


def main(argv):
    if len(argv) >= 3 and argv[0] == "init":
        init(argv[1], argv[2])
    elif len(argv) >= 2 and argv[0] == "open":
        with open(argv[1], "rb") as f:
            print(json.dumps(open_envelope(f.read(), json.load(sys.stdin))))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
