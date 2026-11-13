"""Ephemeral encryption for private input carried by the MCP App transport."""
import base64
import json
import secrets
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PanelCipher:
    def __init__(self):
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_id = secrets.token_urlsafe(24)
        self._seen = {}

    def descriptor(self):
        public_key = self._private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return {
            "keyId": self.key_id,
            "publicKey": base64.b64encode(public_key).decode("ascii"),
            "algorithm": "RSA-OAEP-256+A256GCM",
        }

    def decrypt(self, envelope):
        try:
            if not isinstance(envelope, dict) or envelope.get("keyId") != self.key_id:
                raise ValueError()
            def decode(field, maximum):
                value = envelope[field]
                if not isinstance(value, str) or len(value) > maximum:
                    raise ValueError()
                return base64.b64decode(value, validate=True)
            aes_key = self._private_key.decrypt(
                decode("wrappedKey", 512),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            )
            iv = decode("iv", 32)
            if len(aes_key) != 32 or len(iv) != 12:
                raise ValueError()
            plain = AESGCM(aes_key).decrypt(
                iv, decode("ciphertext", 2000000), self.key_id.encode("utf-8")
            )
            value = json.loads(plain)
            now = time.time()
            expires = value["expiresAt"] / 1000
            message_id = value["messageId"]
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry >= now}
            if (
                not now <= expires <= now + 120
                or not isinstance(message_id, str)
                or not 16 <= len(message_id) <= 100
                or message_id in self._seen
            ):
                raise ValueError()
            if value["path"] not in ("/api/input", "/api/upload-input", "/panel/clipboard") or not isinstance(value["payload"], dict):
                raise ValueError()
            self._seen[message_id] = expires
            return value["path"], value["payload"]
        except Exception:
            # Never include envelope contents or decrypted input in an error.
            raise ValueError("Private input is invalid, expired, or already used. Enter it again.") from None


def clipboard_reply_key(encoded):
    try:
        if not isinstance(encoded, str) or len(encoded) != 44:
            raise ValueError()
        key = base64.b64decode(encoded, validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except Exception:
        raise ValueError("Invalid clipboard response key.") from None


def encrypt_clipboard_reply(text, key):
    iv = secrets.token_bytes(12)
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    encrypted = AESGCM(key).encrypt(iv, payload, b"codex-ssh-clipboard-v1")
    return {"iv": base64.b64encode(iv).decode("ascii"), "ciphertext": base64.b64encode(encrypted).decode("ascii")}
