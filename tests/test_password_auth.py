import json
import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import password_auth as auth
import server


def entries():
    return {
        'jump': {'host': '192.0.2.8', 'user': 'alice', 'password': 'jump-test-only!'},
        'gpu': {'host': '192.0.2.6', 'user': 'alice', 'password': 'gpu-测试-"$`', 'jump_profiles': ['jump']},
        'other': {'host': '192.0.2.7', 'user': 'alice', 'password': 'other-test-only!'},
    }


def config(tmp_path, data=None):
    path = tmp_path / 'passwords.json'
    path.write_text(json.dumps(data or {'enabled': True, 'profiles': entries()}), encoding='utf-8')
    path.chmod(0o600)
    return path


def helper(broker, prompt, hint=''):
    env = broker.environment()
    env['SSH_ASKPASS_PROMPT'] = hint
    return subprocess.run([str(broker.helper), prompt], env=env, text=True, capture_output=True, timeout=8)


def test_private_file_and_disabled_config(tmp_path):
    path = config(tmp_path)
    assert auth.load_password_profiles(path)['gpu']['host'] == '192.0.2.6'
    path.chmod(0o644)
    with pytest.raises(ValueError, match='mode 600'):
        auth.load_password_profiles(path)
    path.chmod(0o600)
    path.write_text('{invalid-test-secret')
    with pytest.raises(ValueError) as error:
        auth.load_password_profiles(path)
    assert 'invalid-test-secret' not in str(error.value)
    path.write_text(json.dumps({'enabled': False, 'profiles': entries()}))
    assert auth.load_password_profiles(path) == {}


def test_refuse_symlinks_and_multiline_passwords(tmp_path):
    path = config(tmp_path)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(ValueError):
        auth.load_password_profiles(link)
    data = entries()
    data['gpu']['password'] = 'a\nb'
    config(tmp_path, {'profiles': data})
    with pytest.raises(ValueError, match='single-line'):
        auth.load_password_profiles(path)


def test_profile_scope_includes_only_declared_jumps(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(auth, 'state_file', lambda name: path)
    broker = auth.AskpassBroker.for_profile('gpu', lambda *args: None)
    try:
        assert set(broker.entries) == {'gpu', 'jump'}
        assert broker.environment()['SSH_ASKPASS_REQUIRE'] == 'force'
        assert not any(entry['password'] in json.dumps(broker.environment()) for entry in broker.entries.values())
    finally:
        broker.close()
    assert auth.AskpassBroker.for_profile('missing', lambda *args: None) is None


def test_real_helper_autofills_two_hosts_but_keeps_otp_manual():
    history = []
    prompt_ready = threading.Event()

    def publish(kind, text):
        history.append((kind, text))
        if kind == 'auth_prompt':
            prompt_ready.set()

    broker = auth.AskpassBroker(entries(), publish, timeout=5)
    values = [entry['password'] for entry in broker.entries.values()]
    try:
        assert helper(broker, '(alice@192.0.2.8) Password: ').stdout.rstrip('\n') == values[0]
        with ThreadPoolExecutor() as executor:
            for host, otp in [('192.0.2.8', '123456'), ('192.0.2.6', '654321')]:
                if host.endswith('.6'):
                    assert helper(broker, "alice@192.0.2.6's password: ").stdout.rstrip('\n') == values[1]
                prompt_ready.clear()
                result = executor.submit(helper, broker, f'(alice@{host}) Verification code: ')
                assert prompt_ready.wait(5)
                assert broker.pending
                assert broker.submit(otp + '\r')
                completed = result.result(5)
                assert completed.returncode == 0
                assert completed.stdout == otp + '\n'
                assert not broker.pending
        output = json.dumps(history)
        assert all(secret not in output for secret in values + ['123456', '654321'])
    finally:
        broker.close()
    assert not Path(broker.socket_path).exists()


@pytest.mark.parametrize('prompt,hint', [
    ('(alice@192.0.2.6) Verification code: ', ''),
    ('(alice@192.0.2.6) Password + OTP: ', ''),
    ('(mallory@192.0.2.6) Password: ', ''),
    ('(alice@192.0.2.99) Password: ', ''),
    ('[sudo] password for alice: ', ''),
    ('Password: ', ''),
    ('(alice@192.0.2.6) Password: ', 'confirm'),
])
def test_ambiguous_and_nonpassword_prompts_are_never_autofilled(prompt, hint):
    broker = auth.AskpassBroker(entries(), lambda *args: None)
    try:
        assert broker._auto_password(prompt, hint) is None
    finally:
        broker.close()


def test_each_host_is_autofilled_only_once():
    broker = auth.AskpassBroker(entries(), lambda *args: None)
    try:
        prompt = '(alice@192.0.2.6) Password: '
        assert broker._auto_password(prompt, '')
        assert broker._auto_password(prompt, '') is None
    finally:
        broker.close()


def test_helper_requires_session_token():
    broker = auth.AskpassBroker(entries(), lambda *args: None)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(broker.socket_path)
            auth.send_json(sock, {'token': 'wrong', 'prompt': '(alice@192.0.2.6) Password: '})
            assert auth.receive_json(sock) == {'ok': False}
        assert not broker.attempted
    finally:
        broker.close()


def test_session_routes_private_input_without_logging_or_writing_to_pty(monkeypatch):
    session = server.TerminalSession(['ssh', 'gpu'], profile_name='gpu')
    ready = threading.Event()

    def publish(kind, text):
        session._publish(kind, text)
        if kind == 'auth_prompt':
            ready.set()

    broker = auth.AskpassBroker(entries(), publish, timeout=5)
    session.auth_broker = broker
    writes = []
    monkeypatch.setattr(session, '_write', writes.append)
    try:
        with ThreadPoolExecutor() as executor:
            request = executor.submit(helper, broker, '(alice@192.0.2.6) Verification code: ')
            assert ready.wait(5)
            with pytest.raises(PermissionError):
                session.write_agent_command('hostname')
            session.write_user_input('012345\r')
            assert request.result(5).stdout == '012345\n'
        assert writes == []
        assert '012345' not in json.dumps(session.snapshot())
        assert 'password' not in session.status()
        session.write_agent_command('hostname')
        assert writes == ['hostname\n']
    finally:
        broker.close()


def test_stop_cancels_pending_authentication():
    ready = threading.Event()
    broker = auth.AskpassBroker(entries(), lambda kind, text: ready.set() if kind == 'auth_prompt' else None)
    with ThreadPoolExecutor() as executor:
        request = executor.submit(helper, broker, '(alice@192.0.2.6) Verification code: ')
        assert ready.wait(5)
        broker.close()
        assert request.result(5).returncode == 1
