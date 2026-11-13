import base64
import json
import os
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from panel_crypto import PanelCipher
import mcp_server


def encrypted_input(cipher, text="fake-otp-123456", expiry=None, path="/api/input", input_payload=None):
    descriptor = cipher.descriptor()
    public_key = serialization.load_der_public_key(base64.b64decode(descriptor["publicKey"]))
    aes_key, iv = os.urandom(32), os.urandom(12)
    payload = {
        "path": path,
        "payload": input_payload if input_payload is not None else {"sessionId": "test-session", "data": text, "enter": True, "private": True},
        "messageId": str(uuid.uuid4()),
        "expiresAt": expiry if expiry is not None else int((time.time() + 60) * 1000),
    }
    wrapped = public_key.encrypt(
        aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    ciphertext = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode(), descriptor["keyId"].encode())
    return {
        "keyId": descriptor["keyId"],
        "wrappedKey": base64.b64encode(wrapped).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


def test_private_input_round_trip_and_replay_rejection():
    cipher = PanelCipher()
    envelope = encrypted_input(cipher)
    assert "fake-otp-123456" not in json.dumps(envelope)
    path, payload = cipher.decrypt(envelope)
    assert path == "/api/input"
    assert payload["data"] == "fake-otp-123456"
    with pytest.raises(ValueError, match="already used"):
        cipher.decrypt(envelope)


def test_corrupted_expired_or_wrong_key_input_fails_closed():
    cipher = PanelCipher()
    expired = encrypted_input(cipher, expiry=int((time.time() - 10) * 1000))
    corrupt = encrypted_input(cipher)
    corrupt["ciphertext"] = base64.b64encode(b"invalid ciphertext").decode()
    wrong_key = encrypted_input(PanelCipher())
    wrong_path = encrypted_input(cipher, path="/api/start")
    for envelope in (expired, corrupt, wrong_key, wrong_path):
        with pytest.raises(ValueError) as exc:
            cipher.decrypt(envelope)
        assert "fake-otp" not in str(exc.value)


def test_private_tool_only_decrypts_locally_and_returns_no_plaintext(monkeypatch):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    tools._panel_cipher = PanelCipher()
    received = []
    monkeypatch.setattr(tools, "_post", lambda path, payload, timeout: received.append((path, payload)) or {"ok": True})
    response = mcp_server.McpServer(tools).handle({
        "id": 1, "method": "tools/call", "params": {
            "name": "panel_private_input",
            "arguments": {"envelope": encrypted_input(tools._panel_cipher)},
        },
    })
    assert received[0][1]["data"] == "fake-otp-123456"
    assert "fake-otp" not in json.dumps(response)


@pytest.mark.parametrize("initialized", [False, True])
def test_stale_panel_key_is_rejected_before_dispatch(monkeypatch, initialized):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    if initialized:
        tools._panel_cipher = PanelCipher()
    monkeypatch.setattr(tools, "_post", lambda *a, **kw: pytest.fail("Stale input reached SSH"))
    response = mcp_server.McpServer(tools).handle({
        "id": 1, "method": "tools/call", "params": {
            "name": "panel_private_input",
            "arguments": {"envelope": encrypted_input(PanelCipher())},
        },
    })["result"]
    assert response["isError"] is True
    assert json.loads(response["content"][0]["text"])["code"] == "PANEL_KEY_EXPIRED"
    assert "fake-otp" not in json.dumps(response)


def test_compatibility_clipboard_is_encrypted_both_ways_without_websocket(monkeypatch):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    tools._panel_cipher = PanelCipher()
    text = 'private clipboard 中文\nsecond line'
    reads, writes = [], []
    class Clipboard:
        def read(self):
            reads.append(True)
            return text
        def write(self, value):
            writes.append(value)
    tools._panel_clipboard = Clipboard()
    monkeypatch.setattr(tools, 'panel_transport', lambda: pytest.fail('Clipboard used WebSocket'))
    monkeypatch.setattr(tools, '_post', lambda *a, **kw: pytest.fail('Clipboard wrote to SSH'))
    reply_key = os.urandom(32)
    request = encrypted_input(tools._panel_cipher, path='/panel/clipboard', input_payload={
        'operation':'read', 'responseKey':base64.b64encode(reply_key).decode(),
    })
    result = tools.panel_private_input(request)
    assert text not in json.dumps(request) + json.dumps(result)
    sealed = result['_meta']['payload']['encryptedClipboard']
    plain = AESGCM(reply_key).decrypt(base64.b64decode(sealed['iv']), base64.b64decode(sealed['ciphertext']), b'codex-ssh-clipboard-v1')
    assert json.loads(plain)['text'] == text and reads == [True]
    with pytest.raises(ValueError):
        tools.panel_private_input(request)
    assert reads == [True]
    result = tools.panel_private_input(encrypted_input(tools._panel_cipher, path='/panel/clipboard', input_payload={
        'operation':'write', 'text':text,
    }))
    assert writes == [text] and text not in json.dumps(result)


@pytest.mark.parametrize('payload', [{'operation':'read','responseKey':'invalid'}, {'operation':'delete'}, {'operation':'write','text':123}])
def test_invalid_clipboard_requests_never_access_clipboard(payload):
    tools = mcp_server.TerminalTools('http://127.0.0.1:18766')
    tools._panel_cipher = PanelCipher()
    with pytest.raises(ValueError):
        tools.panel_private_input(encrypted_input(tools._panel_cipher, path='/panel/clipboard',input_payload=payload))
    assert tools._panel_clipboard is None
