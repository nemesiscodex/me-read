"""Draw portraits and scenes with the image service, which runs on another machine (see `imager/`)."""
import base64
import http.client
import json
import urllib.error
import urllib.request

from . import speech

PORTRAIT_SIZE = 512, 512
SCENE_SIZE = 640, 432
REFERENCE_PX = 256
TIMEOUT = 120
PNG = b'\x89PNG\r\n\x1a\n'
STYLE = ('Courtroom sketch illustration, loose pastel and charcoal on toned paper, quick expressive strokes, muted colours, '
         'visible hatching. No text, no signature, no caption. ')


class ImageError(RuntimeError):
    """The service answered that it could not draw this."""


class Unavailable(RuntimeError):
    """The service cannot be reached or is still loading its model, so nothing is known about the drawing."""


def _sentence(text):
    return text.strip().rstrip('.') + '.'


def portrait_prompt(character):
    who = speech.AGE_GENDER.get((character['age'], character['gender']), speech.AGE[character['age']])
    return f"{STYLE}Portrait of {who[0].lower()}{who[1:]}: {_sentence(character['appearance'])} Head and shoulders, plain background."


def scene_prompt(scene, referenced, described):
    """`referenced` are the names behind the reference images, in order; `described` are (name, appearance) of the rest."""
    return (STYLE + _sentence(scene) + ''.join(f' {name} is the person in image {n}.' for n, name in enumerate(referenced, 1))
            + ''.join(f' {name}: {_sentence(appearance)}' for name, appearance in described))


def generate(url, prompt, width, height, seed, references=()):
    """PNG bytes for the prompt. `references` are PNG files as bytes."""
    body = json.dumps({'prompt': prompt, 'width': width, 'height': height, 'seed': seed, 'reference_px': REFERENCE_PX,
                       'references': [base64.b64encode(png).decode() for png in references]}).encode()
    request = urllib.request.Request(f'{url}/generate', body, {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            png = reply.read()
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise Unavailable('is still loading its model') from e
        try:
            detail = json.loads(e.read())['detail']
            # A request the service rejects as malformed comes back as a list of problems.
            detail = detail if isinstance(detail, str) else json.dumps(detail)
        except Exception:
            detail = e.reason
        raise ImageError(f'{e.code}: {detail}') from e
    except (OSError, http.client.HTTPException) as e:
        raise Unavailable('cannot be reached') from e
    if not png.startswith(PNG):
        raise ImageError('the image service did not return a PNG')
    return png
