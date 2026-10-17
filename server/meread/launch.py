"""Build the reader, start its GPU service over SSH, and own the local processes."""
import json
import os
from pathlib import Path
import shutil
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


def remembered(home):
    path = home / 'launcher.json'
    if not path.exists():
        return {}
    saved = json.loads(path.read_text())
    if not isinstance(saved, dict):
        raise ValueError(f'{path} must contain a JSON object')
    return saved


def save(home, saved):
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=home, delete=False) as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(saved, file)
    os.replace(file.name, home / 'launcher.json')


def consent_for(args):
    consent = args.tts_consent
    saved = remembered(args.home)
    if consent is None:
        consent = saved.get('tts_consent')
    if consent is None:
        if not sys.stdin.isatty():
            raise ValueError('remote speech needs your own consent statement. Run ./start in a terminal '
                             'for the first-run prompt, or pass --tts-consent "YOUR OWN STATEMENT".')
        say('Remote speech needs your statement that you may use the reference voices. '
            'It will be saved for later starts of this library.')
        consent = input('Your consent statement: ')
    if not isinstance(consent, str):
        raise ValueError(f'tts_consent in {args.home / "launcher.json"} must be text')
    # Use the same validation as the server; never invent a default statement.
    speech.connect(args.tts_provider, args.tts_url, consent)
    if consent != saved.get('tts_consent'):
        save(args.home, {**saved, 'tts_consent': consent})
        say(f'Consent saved in {args.home / "launcher.json"}.')
    return consent


def gpu_host_for(args):
    saved = remembered(args.home)
    if args.forget_gpu_host:
        saved.pop('gpu_host', None)
        save(args.home, saved)
    elif args.gpu_host is not None:
        if not args.gpu_host or args.gpu_host.startswith('-'):
            raise ValueError('--gpu-host must be an SSH host name')
        save(args.home, {**saved, 'gpu_host': args.gpu_host})
        return args.gpu_host
    return saved.get('gpu_host')


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
    parser.description = ('Build and start me-read. With a GPU machine, also start its speech or image service '
                          'and open an SSH tunnel to it.')
    parser.add_argument('--gpu', choices=('speech', 'images', 'local'),
                        help='what the GPU machine does: speech, images (speech then runs here), or local to run '
                             'everything here (default: speech when a GPU machine is remembered, otherwise local)')
    machine = parser.add_mutually_exclusive_group()
    machine.add_argument('--gpu-host', help='SSH name of a machine with the me-read user services installed, '
                                            'remembered for later starts of this library')
    machine.add_argument('--forget-gpu-host', action='store_true', help='stop using the remembered GPU machine')
    args = parser.parse_args(argv)
    args.home = Path(args.home).expanduser().resolve()
    processes = Processes()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        host = gpu_host_for(args)
        mode = args.gpu or ('local' if args.tts_url or not host else 'speech')
        if mode != 'local':
            if args.tts_url is not None:
                raise ValueError('use --gpu local with a custom --tts-url; managed GPU modes create their own tunnel')
            if not host:
                raise ValueError(f'--gpu {mode} needs a GPU machine. Pass --gpu-host HOST once; '
                                 'see docs/gpu-machine.md')
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

        if not (shutil.which('claude') or shutil.which('codex')):
            say('Neither the claude nor the codex CLI is on PATH. Without one, the narrator reads every line. '
                'Install one and log in to get a cast.')
        say('Building the web app…')
        processes.run(['pnpm', 'install', '--frozen-lockfile', '--silent'], ROOT / 'web')
        processes.run(['pnpm', 'build'], ROOT / 'web')
        tunnel = None
        if mode != 'local':
            say(f'Starting {service} on {host} (the other GPU service stops).')
            processes.run(['ssh', *SSH_OPTIONS, host,
                           f'systemctl --user start {service}.service'], timeout=90)
            tunnel = processes.start(['ssh', *SSH_OPTIONS, '-o', 'ExitOnForwardFailure=yes', '-N',
                                      '-L', f'127.0.0.1:{port}:127.0.0.1:{port}', host])
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
