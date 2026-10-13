"""Exercise every path of a running speech service and save what it says.

    uv run --frozen python check.py URL OUT EN_WAV EN_TRANSCRIPT ES_WAV ES_TRANSCRIPT

Prints one JSON line per case and writes the audio to OUT. Uploading the same voices again is fine (409).
"""
import base64
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

CONSENT = 'my own voice and voices I designed, personal use'
EN = ('I was not sure what I would find when I opened the door that morning. The garden was quiet, and the path ran '
      'down toward the river. Have you ever seen it in the early light?')
ES = ('No sabía qué iba a encontrar cuando abrí la puerta aquella mañana. El jardín estaba en silencio, y el sendero '
      'bajaba hacia el río.')
ANGRY = 'I cannot believe you would do this to me, after everything we went through together!'


def call(url, path, body=None, form=None, timeout=1200):
    headers = {}
    if form is not None:
        boundary = uuid.uuid4().hex
        data = b''
        for name, value, filename in form:
            disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else '')
            data += f'--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n'.encode() + value + b'\r\n'
        data += f'--{boundary}--\r\n'.encode()
        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    elif body is not None:
        data, headers['Content-Type'] = json.dumps(body).encode(), 'application/json'
    else:
        data = None
    request = urllib.request.Request(url + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, reply.headers, reply.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def report(case, **fields):
    print(json.dumps({'case': case, **fields}), flush=True)


def upload(url, name, wav, transcript, consent=CONSENT):
    status, _, body = call(url, '/v1/voices', form=[('voice', wav, 'v.wav'), ('name', name.encode(), None),
                                                    ('consent_attestation', consent.encode(), None),
                                                    ('transcript', transcript.encode(), None)])
    return status, body


def speak(url, out, case, **fields):
    body = {'response_format': 'wav', 'seed': 7, 'spoken_disclaimer': False, **fields}
    if 'voice' in body:
        body.setdefault('consent_attestation', CONSENT)
    unavailable = 0
    start = time.perf_counter()
    while True:
        attempt = time.perf_counter()
        status, headers, data = call(url, '/v1/audio/speech', body)
        if status != 503:
            break
        unavailable += 1
        report(case, status=503, error=json.loads(data)['error'])
        time.sleep(3)
    elapsed = time.perf_counter() - attempt
    if status != 200:
        report(case, status=status, error=json.loads(data)['error'])
        return None
    audio, rate = sf.read(io.BytesIO(data))
    (out / f'{case}.wav').write_bytes(data)
    _, _, health = call(url, '/health')
    report(case, status=200, audio_s=round(len(audio) / rate, 2), rate=rate, channels=audio.ndim,
           request_s=round(elapsed, 1), total_s=round(time.perf_counter() - start, 1), retries_503=unavailable,
           disclaimer=headers.get('X-Crispasr-Spoken-Disclaimer'), loaded=json.loads(health)['loaded'])
    return data


def same(a, b):
    """CrispASR's WAV carries per-response metadata, so compare the samples, not the bytes."""
    return a is not None and b is not None and np.array_equal(sf.read(io.BytesIO(a))[0], sf.read(io.BytesIO(b))[0])


def embedding(url, wav, language):
    status, _, data = call(url, '/v1/speaker', {'audio': base64.b64encode(wav).decode(), 'language': language})
    if status != 200:
        report('speaker', status=status, error=json.loads(data)['error'])
        return None
    return np.array(json.loads(data)['embedding'])


def main():
    url, out = sys.argv[1].rstrip('/'), Path(sys.argv[2])
    en_wav, es_wav = Path(sys.argv[3]).read_bytes(), Path(sys.argv[5]).read_bytes()
    en_text, es_text = sys.argv[4], sys.argv[6]
    out.mkdir(parents=True, exist_ok=True)
    en, es = (f'check-{hashlib.sha256(w).hexdigest()[:16]}' for w in (en_wav, es_wav))

    report('health', body=json.loads(call(url, '/health')[2]))
    for name, wav, text in ((en, en_wav, en_text), (es, es_wav, es_text)):
        status, body = upload(url, name, wav, text)
        report('upload', name=name, status=status)
    report('upload-again', status=upload(url, en, en_wav, en_text)[0], expect=409)
    status, body = upload(url, 'check-noconsent', en_wav, en_text, consent='')
    report('upload-without-consent', status=status, body=json.loads(body), expect=400)
    untold = f'{en}-notext'
    report('upload-without-transcript', status=upload(url, untold, en_wav, '')[0], expect='201 or 409')
    status, _, body = call(url, '/v1/audio/speech', {'model': 'breeze', 'input': EN, 'voice': untold,
                                                     'consent_attestation': CONSENT})
    report('breeze-voice-without-transcript', status=status, expect=400, error=json.loads(body)['error'])
    for case, fields, expect in (
            ('unknown-model', {'model': 'tortoise', 'input': EN}, 400),
            ('chatterbox-without-voice', {'model': 'chatterbox', 'input': EN, 'language': 'en'}, 400),
            ('voice-without-consent', {'model': 'breeze', 'input': EN, 'voice': en, 'consent_attestation': ''}, 400),
            ('qwen-with-voice', {'model': 'qwen', 'input': ES, 'voice': en, 'instructions': 'calm'}, 400),
            ('unknown-voice', {'model': 'breeze', 'input': EN, 'voice': 'check-missing'}, 500),
            ('bad-name', {'model': 'breeze', 'input': EN, 'voice': '../etc'}, 400)):
        status, _, body = call(url, '/v1/audio/speech', {'consent_attestation': CONSENT, **fields}
                               if case != 'voice-without-consent' else fields)
        report(case, status=status, expect=expect, error=json.loads(body)['error'])

    male = 'A man in his forties with a deep, low voice, speaking calmly and warmly.'
    first = speak(url, out, 'breeze-design', model='breeze', input=EN, instructions=male, cfg_scale=4, language='en')
    again = speak(url, out, 'breeze-design-again', model='breeze', input=EN, instructions=male, cfg_scale=4,
                  language='en')
    report('breeze-same-seed', identical_samples=same(first, again))
    speak(url, out, 'breeze-clone', model='breeze', input=EN, voice=en, language='en')
    speak(url, out, 'breeze-direction', model='breeze', input=ANGRY, voice=en, language='en',
          instructions='Speak angrily, voice tight and raised.', cfg_scale=4)
    speak(url, out, 'qwen-design-es', model='qwen', input=ES, language='es',
          instructions='An adult man with a low, deep voice. Calm and warm.')
    first = speak(url, out, 'chatterbox-en', model='chatterbox', input=EN, voice=en, language='en')
    again = speak(url, out, 'chatterbox-en-again', model='chatterbox', input=EN, voice=en, language='en')
    report('chatterbox-same-seed', identical_samples=same(first, again))
    speak(url, out, 'chatterbox-voice-without-transcript', model='chatterbox', input=EN, voice=untold, language='en')
    speak(url, out, 'chatterbox-es', model='chatterbox', input=ES, voice=es, language='es')
    speak(url, out, 'breeze-clone-after-eviction', model='breeze', input=EN, voice=en, language='en')

    ref_en, ref_es = embedding(url, en_wav, 'en'), embedding(url, es_wav, 'en')
    narrated = embedding(url, (out / 'chatterbox-en.wav').read_bytes(), 'en')
    ref_es_ml = embedding(url, es_wav, 'es')
    narrated_es = embedding(url, (out / 'chatterbox-es.wav').read_bytes(), 'es')
    report('speaker', dims=len(ref_en), norm=round(float(np.linalg.norm(ref_en)), 4),
           en_ref_vs_en_narration=round(float(ref_en @ narrated), 3),
           en_ref_vs_es_ref=round(float(ref_en @ ref_es), 3),
           es_ref_vs_es_narration_multilingual=round(float(ref_es_ml @ narrated_es), 3))
    report('health', body=json.loads(call(url, '/health')[2]))


if __name__ == '__main__':
    main()
