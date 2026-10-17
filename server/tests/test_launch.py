import io
import json
import signal
import sys
from types import SimpleNamespace

import pytest

from meread import launch

CONSENT = 'I may use these reference voices for my books.'


class Child:
    returncode = 0

    def __init__(self, running=False):
        self.running = running

    def poll(self):
        return None if self.running else self.returncode


class Commands:
    def __init__(self):
        self.commands = []
        self.closed = False
        self.fail_build = False
        self.app_running = False

    def run(self, command, cwd=None, timeout=None):
        self.commands.append(command)
        if self.fail_build:
            raise RuntimeError('build failed')

    def start(self, command, cwd=None):
        self.commands.append(command)
        return Child(running=command[0] == 'ssh' or self.app_running)

    def close(self):
        self.closed = True


@pytest.fixture
def commands(monkeypatch):
    commands = Commands()
    monkeypatch.setattr(launch, 'Processes', lambda: commands)
    monkeypatch.setattr(launch, 'unused_port', lambda *args: None)
    monkeypatch.setattr(launch, 'wait_for_service', lambda *args: None)
    monkeypatch.setattr(launch.signal, 'signal', lambda *args: None)
    return commands


def remember_host(home, host='gpu-box'):
    (home / 'launcher.json').write_text(json.dumps({'gpu_host': host}))


def test_without_a_gpu_machine_start_runs_everything_here(tmp_path, commands):
    assert launch.main(['--home', str(tmp_path)]) == 0
    install, build, app = commands.commands
    assert install == ['pnpm', 'install', '--frozen-lockfile', '--silent']
    assert build == ['pnpm', 'build']
    assert '--tts-url' not in app and '--image-url' not in app
    assert not (tmp_path / 'launcher.json').exists()


def test_remembered_gpu_machine_owns_speech_tunnel_and_wires_remote_reader(tmp_path, commands):
    remember_host(tmp_path)
    assert launch.main(['--home', str(tmp_path), '--tts-consent', CONSENT, '--port', '9000']) == 0
    install, build, service, tunnel, app = commands.commands
    assert service[-2:] == ['gpu-box', 'systemctl --user start me-read-speech.service']
    assert '-N' in tunnel and '127.0.0.1:8781:127.0.0.1:8781' in tunnel
    assert app[app.index('--tts-url') + 1] == 'http://127.0.0.1:8781'
    assert app[app.index('--tts-consent') + 1] == CONSENT
    assert app[app.index('--image-url') + 1] == ''
    assert app[app.index('--port') + 1] == '9000'
    assert commands.closed


def test_images_mode_uses_image_tunnel_and_local_narration(tmp_path, commands):
    assert launch.main(['--home', str(tmp_path), '--gpu', 'images', '--gpu-host', 'other-gpu']) == 0
    assert commands.commands[2][-2:] == ['other-gpu', 'systemctl --user start me-read-imager.service']
    assert '127.0.0.1:8766:127.0.0.1:8766' in commands.commands[3]
    app = commands.commands[-1]
    assert app[app.index('--image-url') + 1] == 'http://127.0.0.1:8766'
    assert '--tts-url' not in app and '--tts-consent' not in app
    assert json.loads((tmp_path / 'launcher.json').read_text()) == {'gpu_host': 'other-gpu'}


def test_given_gpu_host_is_remembered_until_forgotten(tmp_path, commands):
    assert launch.main(['--home', str(tmp_path), '--gpu-host', 'gpu-box', '--tts-consent', CONSENT]) == 0
    assert json.loads((tmp_path / 'launcher.json').read_text()) == {'gpu_host': 'gpu-box', 'tts_consent': CONSENT}
    commands.commands.clear()
    assert launch.main(['--home', str(tmp_path)]) == 0
    assert commands.commands[2][-2:] == ['gpu-box', 'systemctl --user start me-read-speech.service']
    commands.commands.clear()
    assert launch.main(['--home', str(tmp_path), '--forget-gpu-host']) == 0
    assert all(command[0] != 'ssh' for command in commands.commands)
    assert json.loads((tmp_path / 'launcher.json').read_text()) == {'tts_consent': CONSENT}


def test_start_warns_when_no_tagger_cli_is_installed(tmp_path, commands, monkeypatch, capsys):
    monkeypatch.setattr(launch.shutil, 'which', lambda name: None)
    assert launch.main(['--home', str(tmp_path)]) == 0
    assert 'Neither the claude nor the codex CLI' in capsys.readouterr().out
    monkeypatch.setattr(launch.shutil, 'which', lambda name: '/bin/codex' if name == 'codex' else None)
    assert launch.main(['--home', str(tmp_path)]) == 0
    assert 'Neither' not in capsys.readouterr().out


@pytest.mark.parametrize('mode', ['speech', 'images'])
def test_gpu_mode_without_a_machine_fails_before_any_work(tmp_path, commands, mode):
    assert launch.main(['--home', str(tmp_path), '--gpu', mode]) == 1
    assert commands.commands == []


@pytest.mark.parametrize('flags', [
    ['--gpu', 'local'],
    ['--tts-url', 'http://custom:8781', '--tts-consent', CONSENT],
])
def test_local_mode_and_explicit_external_service_skip_ssh_even_with_a_machine(tmp_path, commands, flags):
    remember_host(tmp_path)
    assert launch.main(['--home', str(tmp_path), *flags]) == 0
    assert len(commands.commands) == 3
    assert all(command[0] != 'ssh' for command in commands.commands)


@pytest.mark.parametrize('flags', [
    ['--gpu', 'images', '--tts-provider', 'breeze-gguf'],
    ['--gpu', 'speech', '--tts-url', 'http://custom:8781'],
    ['--gpu-host=-bad'],
    ['--gpu-host='],
    ['--gpu', 'local', '--tts-consent', CONSENT],
    ['--gpu', 'local', '--image-url', 'ftp://bad'],
])
def test_invalid_combination_fails_before_build_or_remote_changes(tmp_path, commands, flags):
    assert launch.main(['--home', str(tmp_path), *flags]) == 1
    assert commands.commands == []
    assert commands.closed


def test_build_failure_never_switches_remote_service(tmp_path, commands):
    remember_host(tmp_path)
    commands.fail_build = True
    assert launch.main(['--home', str(tmp_path), '--tts-consent', CONSENT]) == 1
    assert len(commands.commands) == 1 and commands.closed


def test_port_conflict_fails_before_remote_changes(tmp_path, commands, monkeypatch):
    remember_host(tmp_path)
    def occupied(*args):
        raise OSError('address already in use')
    monkeypatch.setattr(launch, 'unused_port', occupied)
    assert launch.main(['--home', str(tmp_path), '--tts-consent', CONSENT]) == 1
    assert not commands.commands


def test_tunnel_loss_stops_app_and_cleans_up(tmp_path, commands, monkeypatch):
    remember_host(tmp_path)
    commands.app_running = True
    def start(command, cwd=None):
        commands.commands.append(command)
        return Child(running=command[0] != 'ssh')
    monkeypatch.setattr(commands, 'start', start)
    assert launch.main(['--home', str(tmp_path), '--tts-consent', CONSENT]) == 1
    assert commands.closed


def test_consent_is_saved_exactly_and_reused_without_prompt(tmp_path, monkeypatch):
    remember_host(tmp_path)
    args = SimpleNamespace(home=tmp_path, tts_consent=CONSENT,
                           tts_provider='chatterbox', tts_url='http://127.0.0.1:8781')
    assert launch.consent_for(args) == CONSENT
    saved = tmp_path / 'launcher.json'
    assert json.loads(saved.read_text()) == {'gpu_host': 'gpu-box', 'tts_consent': CONSENT}
    assert saved.stat().st_mode & 0o777 == 0o600
    args.tts_consent = None
    monkeypatch.setattr(sys, 'stdin', io.StringIO())
    assert launch.consent_for(args) == CONSENT


def test_missing_consent_never_invents_one_or_starts_remote_work(tmp_path, commands, monkeypatch):
    remember_host(tmp_path)
    monkeypatch.setattr(sys, 'stdin', io.StringIO())
    assert launch.main(['--home', str(tmp_path)]) == 1
    assert not commands.commands
    assert json.loads((tmp_path / 'launcher.json').read_text()) == {'gpu_host': 'gpu-box'}


def test_first_run_prompts_and_remembers_statement(tmp_path, commands, monkeypatch):
    remember_host(tmp_path)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr('builtins.input', lambda prompt: CONSENT)
    assert launch.main(['--home', str(tmp_path)]) == 0
    assert json.loads((tmp_path / 'launcher.json').read_text())['tts_consent'] == CONSENT


def test_help_creates_no_files_and_starts_no_processes(tmp_path, commands, capsys):
    home = tmp_path / 'new-library'
    with pytest.raises(SystemExit) as exited:
        launch.main(['--home', str(home), '--help'])
    assert exited.value.code == 0
    assert '--gpu {speech,images,local}' in capsys.readouterr().out
    assert not commands.commands and not home.exists()


def test_readiness_waits_for_model_and_surfaces_load_failure(monkeypatch):
    replies = iter([{'ready': False}, {'ready': True}])
    monkeypatch.setattr(launch.urllib.request, 'urlopen', lambda *args, **kwargs: io.StringIO(json.dumps(next(replies))))
    monkeypatch.setattr(launch.time, 'sleep', lambda *args: None)
    launch.wait_for_service('http://127.0.0.1:8766', Child(running=True), timeout=1)
    monkeypatch.setattr(launch.urllib.request, 'urlopen', lambda *args, **kwargs: io.StringIO(
        '{"ready": false, "error": "out of memory"}'))
    with pytest.raises(RuntimeError, match='out of memory'):
        launch.wait_for_service('http://127.0.0.1:8766', Child(running=True), timeout=1)


def test_readiness_fails_on_closed_tunnel_or_deadline():
    with pytest.raises(RuntimeError, match='SSH tunnel closed'):
        launch.wait_for_service('http://127.0.0.1:8781', Child(), timeout=1)
    with pytest.raises(RuntimeError, match='not ready within'):
        launch.wait_for_service('http://127.0.0.1:8781', Child(running=True), timeout=0)


def test_process_cleanup_terminates_owned_process():
    processes = launch.Processes()
    child = processes.start([sys.executable, '-c', 'import time; time.sleep(60)'])
    processes.close()
    assert child.wait(timeout=1) == -signal.SIGTERM


def test_nonzero_child_exit_is_reported_and_reaped():
    processes = launch.Processes()
    try:
        with pytest.raises(RuntimeError, match='status 7'):
            processes.run([sys.executable, '-c', 'raise SystemExit(7)'])
    finally:
        processes.close()
