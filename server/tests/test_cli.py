import json
import logging
import sys

import pytest
import uvicorn

from meread import __main__, speech
from meread.app import create_app
from meread.pipeline import Pipeline


def forbid_snapshot(*args, **kwargs):
    pytest.fail('help or startup attempted a model download')


def test_help_lists_registry_choices_and_creates_no_library(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'library'
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    monkeypatch.setattr(__main__, 'create_app', lambda *args, **kwargs: pytest.fail('help created an app'))
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(home), '--help'])
    with pytest.raises(SystemExit) as exited:
        __main__.main()
    assert exited.value.code == 0
    help_text = ' '.join(capsys.readouterr().out.split())
    assert '--tts-provider {chatterbox,fish,breeze,breeze-gguf}' in help_text
    assert 'default: chatterbox' in help_text and 'breeze is English-only' in help_text
    assert '--tts-url' in help_text and 'for example http://127.0.0.1:8781' in help_text and 'you may use the reference voices' in help_text
    assert not home.exists()


@pytest.mark.parametrize('provider', [None, 'fish', 'breeze'])
def test_cli_wires_the_narrator_without_loading_any_models(tmp_path, monkeypatch, provider):
    apps = []
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    monkeypatch.setattr(uvicorn.Server, 'run', lambda self: apps.append(self.config.app))
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(tmp_path), *(['--tts-provider', provider] if provider else [])])
    __main__.main()
    engine = apps[0].state.pipeline.engine
    assert engine.provider.name == (provider or 'chatterbox')
    assert engine.narrator is engine.chatterbox is engine.multilingual is engine.designer is None
    identity = json.loads(apps[0].state.store.one('SELECT identity FROM generation_identity')['identity'])
    assert identity == speech.PROVIDERS[provider or 'chatterbox'].identity
    assert apps[0].state.store.settings()['provider'] == 'claude'


def test_invalid_provider_exits_before_creating_a_library(tmp_path, monkeypatch):
    home = tmp_path / 'library'
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(home), '--tts-provider', 'remote'])
    with pytest.raises(SystemExit) as exited:
        __main__.main()
    assert exited.value.code == 2
    assert not home.exists()


CONSENT = 'I own these voices and allow them to be cloned for my books.'


@pytest.mark.parametrize('settings, message', [
    ([], '--tts-url is required'),
    (['--tts-url', 'http://127.0.0.1:8781'], '--tts-consent is required'),
    (['--tts-consent', CONSENT], '--tts-url is required'),
    (['--tts-url', 'http://127.0.0.1:8781', '--tts-consent', '  '], '--tts-consent is required'),
    (['--tts-url', 'ftp://127.0.0.1:8781', '--tts-consent', CONSENT], 'must start with http://'),
    (['--tts-url', 'http://user:pw@127.0.0.1:8781', '--tts-consent', CONSENT], 'must not contain a login'),
    (['--tts-url', 'http://127.0.0.1:8781', '--tts-consent', 'not \udc80 utf-8'], 'must be plain UTF-8 text'),
])
def test_breeze_gguf_refuses_missing_or_bad_settings_before_creating_a_library(tmp_path, monkeypatch, capsys, settings, message):
    home = tmp_path / 'library'
    monkeypatch.setattr(__main__, 'create_app', lambda *args, **kwargs: pytest.fail('a library or app was created'))
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(home), '--tts-provider', 'breeze-gguf', *settings])
    with pytest.raises(SystemExit) as exited:
        __main__.main()
    assert exited.value.code == 2
    error = capsys.readouterr().err
    assert message in error and 'pw' not in error
    assert not home.exists()


@pytest.mark.parametrize('narrator', [[], ['--tts-provider', 'chatterbox'], ['--tts-provider', 'fish'], ['--tts-provider', 'breeze']])
@pytest.mark.parametrize('flag, message', [(['--tts-url', 'http://127.0.0.1:8781'], '--tts-consent is required with --tts-url'),
                                           (['--tts-consent', CONSENT], '--tts-url is required')])
def test_a_url_needs_consent_and_consent_needs_a_url_with_any_narrator(tmp_path, monkeypatch, capsys, narrator, flag, message):
    home = tmp_path / 'library'
    monkeypatch.setattr(__main__, 'create_app', lambda *args, **kwargs: pytest.fail('an app was created'))
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(home), *narrator, *flag])
    with pytest.raises(SystemExit) as exited:
        __main__.main()
    assert exited.value.code == 2
    assert message in capsys.readouterr().err
    assert not home.exists()


@pytest.mark.parametrize('provider, narration', [
    ('chatterbox', 'the speech service at 127.0.0.1:8781'),
    ('breeze-gguf', 'the speech service at 127.0.0.1:8781'),
    ('fish', 'this Mac with mlx-community/fish-audio-s2-pro-8bit'),
    ('breeze', 'this Mac with mlx-community/Breeze-TTS-2-mlx-4bit'),
])
def test_every_narrator_wires_the_speech_service_and_logs_where_work_runs_but_never_the_consent(
        tmp_path, monkeypatch, caplog, provider, narration):
    apps = []
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    monkeypatch.setattr(uvicorn.Server, 'run', lambda self: apps.append(self.config.app))
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(tmp_path), '--tts-provider', provider,
                                      '--tts-url', 'http://127.0.0.1:8781', '--tts-consent', CONSENT])
    __main__.main()
    engine = apps[0].state.pipeline.engine
    assert engine.remote.host == '127.0.0.1:8781'
    assert engine.narrator is engine.chatterbox is engine.multilingual is engine.designer is engine.breeze is None
    identity = json.loads(apps[0].state.store.one('SELECT identity FROM generation_identity')['identity'])
    assert identity == speech.PROVIDERS[provider].identity
    assert (f'Narrator {provider} on {narration},' in caplog.text and
            'voice design, emotion takes and speaker comparison on the speech service at 127.0.0.1:8781' in caplog.text)
    assert CONSENT not in caplog.text


def test_without_a_url_everything_is_logged_as_running_on_this_mac(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    create_app(tmp_path, run_workers=False)
    assert f'Narrator chatterbox on this Mac with {speech.CHATTERBOX},' in caplog.text
    assert 'voice design, emotion takes and speaker comparison on this Mac' in caplog.text


def test_identity_reconciliation_is_complete_before_workers_start(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    observed = []
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    monkeypatch.setattr(Pipeline, 'start', lambda self: observed.append(
        json.loads(self.store.one('SELECT identity FROM generation_identity')['identity'])))
    app = create_app(tmp_path, tts_provider='fish')
    with TestClient(app):
        assert observed == [speech.PROVIDERS['fish'].identity]


def test_injected_engine_identity_is_used_instead_of_the_default_cli_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    engine = speech.Engine(tmp_path / 'hub', 'breeze')
    app = create_app(tmp_path, engine=engine, run_workers=False)
    identity = json.loads(app.state.store.one('SELECT identity FROM generation_identity')['identity'])
    assert identity == speech.PROVIDERS['breeze'].identity


@pytest.mark.parametrize('url', ['', 'http://127.0.0.1:8766'])
def test_image_service_setting_is_applied_before_server_starts(tmp_path, monkeypatch, url):
    seen = []
    monkeypatch.setattr(speech.Engine, '_snapshot', forbid_snapshot)
    monkeypatch.setattr(uvicorn.Server, 'run', lambda self: seen.append(self.config.app.state.store.settings()))
    monkeypatch.setattr(sys, 'argv', ['meread', '--home', str(tmp_path), '--image-url', url])
    __main__.main()
    assert seen[0]['image_url'] == url
