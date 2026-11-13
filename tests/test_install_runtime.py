import json

from scripts.install_runtime import install_hosts


def settings_for(tmp_path, monkeypatch):
    allowlist = tmp_path / 'windows' / 'relay-allowlist.json'
    monkeypatch.setattr('scripts.install_runtime.subprocess.check_output',
                        lambda command, **kwargs: str(allowlist) + '\n')
    return {
        'hosts': [], 'mfaHosts': ['bastion', 'gpu-a', 'gpu-b'],
        'passwordHost': 'password-host',
        'nodeWsl': '/mnt/c/Program Files/nodejs/node.exe',
        'relayWindows': 'C:\\SSH Terminal\\windows-tcp-proxy.cjs',
        'allowlistWindows': 'C:\\SSH Terminal\\relay-allowlist.json',
    }, allowlist


def test_empty_import_still_supports_manually_added_profiles(tmp_path, monkeypatch):
    settings, allowlist = settings_for(tmp_path, monkeypatch)
    state = tmp_path / 'state'
    assert install_hosts(state, settings) == 0
    assert 'Host *' in (state / 'ssh_config').read_text()
    assert json.loads((state / 'passwords.json').read_text())['profiles'] == {}
    assert json.loads(allowlist.read_text()) == []
    assert (state / 'passwords.json').stat().st_mode & 0o777 == 0o600


def test_import_update_retains_password_and_prior_aliases(tmp_path, monkeypatch):
    settings, allowlist = settings_for(tmp_path, monkeypatch)
    state = tmp_path / 'state'
    # Targets before their bastion must still have a valid password lookup chain.
    settings['hosts'] = [
        {'name': 'gpu-a', 'host': '192.0.2.11', 'user': 'alice', 'port': 22, 'proxyJump': 'bastion'},
        {'name': 'bastion', 'host': '192.0.2.10', 'user': 'alice', 'port': 2200, 'proxyJump': None},
    ]
    assert install_hosts(state, settings) == 2
    password_file = state / 'passwords.json'
    passwords = json.loads(password_file.read_text())
    passwords['profiles']['bastion']['password'] = 'test-only-saved-password'
    assert passwords['profiles']['gpu-a']['jump_profiles'] == ['bastion']
    password_file.write_text(json.dumps(passwords))
    settings['hosts'] = [{'name': 'bastion', 'host': '192.0.2.20', 'user': 'alice', 'port': 2200}]
    assert install_hosts(state, settings) == 2
    updated = json.loads(password_file.read_text())
    assert updated['profiles']['bastion']['password'] == 'test-only-saved-password'
    assert updated['profiles']['bastion']['host'] == '192.0.2.20'
    config = (state / 'ssh_config').read_text()
    assert 'Host "gpu-a"' in config
    assert 'ProxyJump bastion' in config
    assert "'/mnt/c/Program Files/nodejs/node.exe'" in config
    assert json.loads(allowlist.read_text()) == [{'host': '192.0.2.20', 'port': 2200}]
