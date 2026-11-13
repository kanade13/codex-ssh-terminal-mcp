"""Install code and local host metadata into an isolated WSL user directory."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import venv


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(value, encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def config_quote(value):
    value = str(value)
    if any(character in value for character in '\r\n\x00'):
        raise ValueError('SSH configuration values must be single-line strings')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def install_hosts(state, settings):
    imported_path = state / 'imported-hosts.json'
    previous = read_json(imported_path, [])
    by_alias = {host['name']: host for host in previous}
    by_alias.update({host['name']: host for host in settings['hosts']})
    imported = list(by_alias.values())
    profiles_path = state / 'connection-profiles.json'
    profiles = read_json(profiles_path, {'profiles': {}, 'defaultProfile': None})
    passwords_path = state / 'passwords.json'
    passwords = read_json(passwords_path, {'enabled': True, 'profiles': {}})
    destinations = []
    config = ['# Generated from the selected Windows SSH aliases by Install.ps1.']
    for host in imported:
        alias, hostname, user = host['name'], host['host'], host['user']
        port = int(host['port'])
        if not 1 <= port <= 65535:
            raise ValueError('SSH port must be between 1 and 65535')
        jump = host.get('proxyJump') or ''
        config.extend([
            '', 'Host ' + config_quote(alias),
            '    HostName ' + config_quote(hostname),
            '    User ' + config_quote(user),
            '    Port ' + str(port),
            '    ServerAliveInterval 60', '    ServerAliveCountMax 3',
            '    IPQoS none', '    BatchMode no', '    ConnectTimeout 20',
            '    StrictHostKeyChecking ask',
            '    UserKnownHostsFile ' + config_quote(state / 'known_hosts'),
        ])
        if jump:
            if any(character.isspace() or character == '\x00' for character in jump):
                raise ValueError('ProxyJump must be a comma-separated SSH jump specification')
            # OpenSSH retains quotes in this option and treats them as part of
            # the jump hostname; its syntax is already whitespace-free.
            config.append('    ProxyJump ' + jump)
        else:
            proxy = [settings['nodeWsl'], settings['relayWindows'], '%h', '%p', settings['allowlistWindows']]
            config.append('    ProxyCommand ' + shlex.join(proxy))
            destinations.append({'host': hostname, 'port': port})
        for identity in host.get('identityFiles', []):
            converted = subprocess.check_output(['wslpath', '-u', identity], text=True).strip()
            config.append('    IdentityFile ' + config_quote(converted))
        for option in ('PubkeyAuthentication', 'PreferredAuthentications', 'KexAlgorithms'):
            value = host.get(option)
            if value:
                config.append('    ' + option + ' ' + config_quote(value))
        profile = {'name': alias, 'mode': 'ssh', 'storageMode': 'plain',
                   'sshHost': alias, 'sshUser': user, 'sshPort': port,
                   'description': 'Imported Windows SSH alias'}
        if jump:
            profile['proxyJump'] = jump
        profiles.setdefault('profiles', {})[alias] = profile
        entry = passwords.setdefault('profiles', {}).setdefault(alias, {'password': ''})
        jump_names = [part.rsplit('@', 1)[-1].split(':', 1)[0] for part in jump.split(',') if part]
        entry.update(host=hostname, user=user, jump_profiles=jump_names)
    if not profiles.get('defaultProfile') and imported:
        profiles['defaultProfile'] = imported[0]['name']
    config.extend(['', 'Host *', '    ServerAliveInterval 60', '    ServerAliveCountMax 3'])
    write_private(state / 'ssh_config', '\n'.join(config) + '\n')
    write_private(imported_path, json.dumps(imported, ensure_ascii=False, indent=2) + '\n')
    write_private(profiles_path, json.dumps(profiles, ensure_ascii=False, indent=2) + '\n')
    write_private(passwords_path, json.dumps(passwords, ensure_ascii=False, indent=2) + '\n')
    group = {'mfa_profiles': settings['mfaHosts'], 'password_profile': settings['passwordHost']}
    write_private(state / 'batch-profiles.json', json.dumps(group, indent=2) + '\n')
    allowlist = subprocess.check_output(['wslpath', '-u', settings['allowlistWindows']], text=True).strip()
    write_private(Path(allowlist), json.dumps(destinations, indent=2) + '\n')
    return len(imported)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--settings', required=True)
    parser.add_argument('--pip', required=True)
    parser.add_argument('--index-url', default='https://pypi.org/simple')
    parser.add_argument('--proxy', default='')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ is required. Ubuntu 24.04 or newer is recommended.')
    settings = read_json(Path(args.settings), {})
    source = Path(__file__).resolve().parents[1]
    runtime, state = Path(settings['runtime']), Path(settings['state'])
    for target in (runtime, state):
        if not target.is_absolute() or len(target.parts) < 3 or target in source.parents or target == source:
            raise SystemExit('Choose a dedicated absolute runtime/state directory')
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
    for module in source.glob('*.py'):
        shutil.copy2(module, runtime / module.name)
    shutil.copytree(source / 'static', runtime / 'static', dirs_exist_ok=True)
    venv.EnvBuilder(with_pip=False).create(runtime / 'venv')
    python = runtime / 'venv/bin/python'
    command = [str(python), args.pip, 'install', '--disable-pip-version-check', '--upgrade',
               '--index-url', args.index_url, '-r', str(source / 'requirements.txt')]
    if args.proxy:
        command.extend(['--proxy', args.proxy])
    subprocess.run(command, check=True)
    write_private(state / 'runtime-config.json', json.dumps({'mode': 'python', 'pythonPath': str(python)}) + '\n')
    imported = install_hosts(state, settings)
    env = dict(os.environ, CODEX_SSH_TERMINAL_HOME=str(state), CODEX_SSH_CONFIG=str(state / 'ssh_config'))
    subprocess.run([str(python), '-c', 'import cryptography; from websockets.sync.server import serve; import mcp_server'],
                   cwd=runtime, env=env, check=True)
    print(json.dumps({'installed': True, 'importedProfiles': imported, 'passwordFile': str(state / 'passwords.json')}))


if __name__ == '__main__':
    main()
