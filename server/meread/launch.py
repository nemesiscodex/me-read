"""Build the reader, start its GPU service over SSH, and own the local processes."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from . import speech
from .__main__ import argument_parser
from .app import SettingsChanges

ROOT = Path(__file__).resolve().parents[2]
SSH_OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
SERVICES = {'speech': ('me-read-speech', 8781), 'images': ('me-read-imager', 8766)}


def say(message):
    print(f'me-read: {message}', flush=True)


def unused_port(host, port):
    """Fail before switching the GPU service when an app or tunnel already owns this port."""
    with socket.create_server((host, port)):
        pass


class Processes:
    def __init__(self):
        self.children = []

    def start(self, command, cwd=None):
        child = subprocess.Popen(command, cwd=cwd, start_new_session=True)
        self.children.append(child)
        return child

    def run(self, command, cwd=None, timeout=None):
        child = self.start(command, cwd)
        code = child.wait(timeout=timeout)
        if code:
            raise RuntimeError(f'{command[0]} exited with status {code}')

    def close(self):
        # Signal whole groups: pnpm, SSH, and the reader may each have children.
        for child in reversed(self.children):
            if child.poll() is not None:
                continue
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 10
        for child in reversed(self.children):
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def wait_for_service(url, tunnel, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tunnel.poll() is not None:
            raise RuntimeError('SSH tunnel closed. Check the SSH error above and the local forwarding port.')
        try:
            with urllib.request.urlopen(f'{url}/health', timeout=2) as response:
                health = json.load(response)
            if health.get('error'):
                raise RuntimeError(f"GPU service could not load: {health['error']}")
            if health.get('ready') is True:
                return
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f'{url}/health was not ready within {timeout} seconds. '
                       'Check the service with systemctl --user status on the GPU computer.')


def consent_for(args):
    path = Path(args.home).expanduser() / 'launcher.json'
    consent = args.tts_consent
    if consent is None and path.exists():
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict):
            raise ValueError(f'{path} must contain a JSON object with tts_consent')
        consent = saved.get('tts_consent')
    if consent is None:
        if not sys.stdin.isatty():
            raise ValueError('remote speech needs your own consent statement. Run ./start in a terminal '
                             'for the first-run prompt, or pass --tts-consent "YOUR OWN STATEMENT".')
        say('Remote speech needs your statement that you may use the reference voices. '
            'It will be saved for later starts of this library.')
        consent = input('Your consent statement: ')
    if not isinstance(consent, str):
        raise ValueError(f'tts_consent in {path} must be text')
    # Use the same validation as the server; never invent a default statement.
    speech.connect(args.tts_provider, args.tts_url, consent)
    if not path.exists() or args.tts_consent is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as saved:
            json.dump({'tts_consent': consent}, saved)
        os.replace(saved.name, path)
        say(f'Consent saved in {path}.')
    return consent


def server_command(args):
    command = [sys.executable, '-m', 'meread', '--host', args.host, '--port', str(args.port),
               '--home', str(args.home), '--tts-provider', args.tts_provider]
    for name in ('tts_url', 'tts_consent', 'image_url'):
        value = getattr(args, name)
        if value is not None:
            command.extend(['--' + name.replace('_', '-'), value])
    return command


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argument_parser()
    parser.prog = './start'
    parser.description = 'Build and start me-read, including its GPU service and SSH tunnel.'
    parser.add_argument('--gpu', choices=('speech', 'images', 'local'),
                        help='GPU work (default: speech on radeon; images uses local speech; local skips SSH)')
    parser.add_argument('--gpu-host', default='radeon', help='SSH host with installed me-read user services (default: radeon)')
    args = parser.parse_args(argv)
    args.home = Path(args.home).expanduser().resolve()
    mode = args.gpu or ('local' if args.tts_url else 'speech')
    processes = Processes()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if mode != 'local':
            if args.tts_url is not None:
                raise ValueError('use --gpu local with a custom --tts-url; managed GPU modes create their own tunnel')
            if not args.gpu_host or args.gpu_host.startswith('-'):
                raise ValueError('--gpu-host must be an SSH host name')
            service, port = SERVICES[mode]
            unused_port('127.0.0.1', port)
            if mode == 'speech':
                args.tts_url = f'http://127.0.0.1:{port}'
                args.image_url = ''
            else:
                args.image_url = f'http://127.0.0.1:{port}'
        unused_port(args.host, args.port)
        if mode == 'speech':
            args.tts_consent = consent_for(args)
        speech.connect(args.tts_provider, args.tts_url, args.tts_consent)
        if args.image_url is not None:
            args.image_url = SettingsChanges(image_url=args.image_url).image_url

        say('Building the web app…')
        processes.run(['pnpm', 'install', '--frozen-lockfile', '--silent'], ROOT / 'web')
        processes.run(['pnpm', 'build'], ROOT / 'web')
        tunnel = None
        if mode != 'local':
            say(f'Starting {service} on {args.gpu_host} (the other GPU service stops).')
            processes.run(['ssh', *SSH_OPTIONS, args.gpu_host,
                           f'systemctl --user start {service}.service'], timeout=90)
            tunnel = processes.start(['ssh', *SSH_OPTIONS, '-o', 'ExitOnForwardFailure=yes', '-N',
                                      '-L', f'127.0.0.1:{port}:127.0.0.1:{port}', args.gpu_host])
            url = args.tts_url if mode == 'speech' else args.image_url
            say(f'Waiting for {mode} at {url}…')
            wait_for_service(url, tunnel)
        say(f'Starting the reader at http://{args.host}:{args.port}. Press Ctrl-C to stop.')
        app = processes.start(server_command(args), ROOT / 'server')
        while app.poll() is None:
            if tunnel is not None and tunnel.poll() is not None:
                raise RuntimeError('SSH tunnel closed; stopping the reader. Run ./start to reconnect.')
            time.sleep(0.25)
        return app.returncode
    except KeyboardInterrupt:
        say('Stopping the reader and its tunnel. The GPU user service stays running.')
        return 130
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.TimeoutExpired) as error:
        print(f'me-read: {error}', file=sys.stderr)
        return 1
    finally:
        processes.close()


if __name__ == '__main__':
    sys.exit(main())
