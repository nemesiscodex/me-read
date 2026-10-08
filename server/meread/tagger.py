"""Ask a subscription CLI (Claude Code or Codex) who says each quotation, and who is new to the cast."""
import json
import re
import subprocess
import tempfile
from pathlib import Path

GENDERS, AGES, PITCHES = ('male', 'female', 'unknown'), ('child', 'teen', 'adult', 'elderly'), ('low', 'medium', 'high')
LANGUAGE_NAMES = {'en': 'English', 'es': 'Spanish'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['new_characters', 'lines'],
    'properties': {
        'new_characters': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['ref', 'name', 'role', 'gender', 'age', 'pitch', 'description'],
            'properties': {
                'ref': {'type': 'string'}, 'name': {'type': 'string'},
                'role': {'type': 'string', 'enum': ['named', 'extra']},
                'gender': {'type': 'string', 'enum': list(GENDERS)},
                'age': {'type': 'string', 'enum': list(AGES)},
                'pitch': {'type': 'string', 'enum': list(PITCHES)},
                'description': {'type': 'string'}}}},
        'lines': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['q', 'speaker'],
            'properties': {'q': {'type': 'integer'}, 'speaker': {'type': 'string'}}}}}}
SYSTEM = """You attribute dialogue in a book so it can be read aloud with a different voice for each character.

You get the cast found so far, some earlier text for context, and the text to attribute. In that text every \
spoken line starts with a marker such as [[q7]]. A spoken line is a quotation, or in books that write dialogue with \
dashes (—Hola —dijo Ana—. ¿Vienes?) a stretch of what the character says.

Return one entry in `lines` for every marker: {"q": 7, "speaker": REF}. REF is one of:
- the ref of a cast member, such as "c12"
- the ref of a character you add in `new_characters`, such as "n1"
- "narrator", when the marked text is not said or thought by a character: a title, a sign, a quoted word or \
phrase, or plain narration that follows a dialogue tag (—Sí —dijo Ana. [[q2]]Luego salió de la sala.)

Rules:
- Use dialogue tags, turn-taking and context, and always pick the most likely speaker. A quoted thought belongs to \
whoever thinks it.
- Reuse a cast member when the text refers to them by another name, title or description. Add a character only for \
someone not in the cast.
- A new character with a name gets role "named" and the fullest name the text uses, such as "Mr. Darcy".
- A character the text calls by a capitalized title or kind, such as "the Duchess" or "the Mouse", is "named" with \
that title: "Duchess", "Mouse".
- A new character without a name gets role "extra" and a name made of a type and a number, such as "Student 1" or \
"Guard 2". Continue the numbering of extras already in the cast. People speaking together are one extra, such as "Crowd 1".
- Set gender, age and pitch from what the text says about the character, and make a sensible guess when it says nothing.
- Write names as the book does, in the book's language ("Guardia 1" in a Spanish book).
- `description` is always in English: at most fifteen words on how the voice sounds, such as "gruff, impatient old sailor with a \
rasping voice".
Reply with the JSON object only."""


class TaggerError(RuntimeError):
    pass


def _voice_line(c):
    traits = ' '.join(x for x in (c['gender'], c['age'], f"{c['pitch']} voice") if x != 'unknown')
    return f"c{c['id']} | {c['name']} | {c['role']} | {traits} | {c['description']}"


def book_line(book):
    return (f"BOOK: {book['title']}" + (f" by {book['author']}" if book['author'] else '')
            + f" (language: {LANGUAGE_NAMES.get(book.get('language'), 'English')})")


def build_prompt(book, cast, earlier, blocks):
    """The prompt, and the segment id behind each [[qN]] marker.

    `earlier` and `blocks` are blocks with their segments. Spoken lines of `blocks` without a speaker get a marker.
    """
    names = {c['id']: c['name'] for c in cast}
    markers = {}

    def render(block, mark):
        text = ''
        for seg in block['segments']:
            ask = mark and seg['kind'] == 'speech' and seg['character_id'] is None
            if ask:
                markers[len(markers) + 1] = seg['id']
                text += f'[[q{len(markers)}]]'
            text += seg['text']
            if not ask and seg['kind'] == 'speech' and seg['character_id'] in names:
                text += f"[{names[seg['character_id']]}]"
        return text
    context = [render(block, False) for block in earlier]
    target = [render(block, True) for block in blocks]
    speaking = [c for c in cast if c['role'] != 'narrator']
    prompt = '\n\n'.join([
        book_line(book),
        'CAST SO FAR (ref | name | role | voice | description):\n' + ('\n'.join(map(_voice_line, speaking)) or '(nobody yet)'),
        'EARLIER TEXT, for context only. A known speaker follows the quotation in brackets:\n' + ('\n'.join(context) or '(start of the book)'),
        'TEXT TO ATTRIBUTE:\n' + '\n\n'.join(target)])
    return prompt, markers


def _run(command, prompt, cwd, timeout):
    try:
        done = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except FileNotFoundError as e:
        raise TaggerError(f'{command[0]} is not installed') from e
    except subprocess.TimeoutExpired as e:
        raise TaggerError(f'{command[0]} did not answer within {timeout} seconds') from e
    return done


def _parse(text):
    match = re.search(r'\{.*\}', text or '', re.S)
    try:
        return json.loads(match.group()) if match else None
    except json.JSONDecodeError:
        return None


def ask_claude(prompt, model, cwd, timeout=180, system=SYSTEM, schema=SCHEMA):
    done = _run(['claude', '-p', '--model', model, '--setting-sources', '', '--strict-mcp-config',
                 '--disable-slash-commands', '--tools', '', '--no-session-persistence', '--output-format', 'json',
                 '--system-prompt', system, '--json-schema', json.dumps(schema)], prompt, cwd, timeout)
    reply = _parse(done.stdout) or {}
    if done.returncode != 0 or reply.get('is_error'):
        raise TaggerError(f"claude failed: {reply.get('result') or done.stderr.strip() or done.stdout.strip()[:300]}")
    answer = reply.get('structured_output') or _parse(reply.get('result'))
    if not isinstance(answer, dict):
        raise TaggerError('claude returned no structured answer')
    return answer, reply.get('total_cost_usd')


def ask_codex(prompt, model, cwd, timeout=240, system=SYSTEM, schema=SCHEMA):
    with tempfile.TemporaryDirectory() as tmp:
        schema_file, out = Path(tmp) / 'schema.json', Path(tmp) / 'answer.json'
        schema_file.write_text(json.dumps(schema))
        done = _run(['codex', 'exec', '-m', model, '-c', 'model_reasoning_effort=low', '-c', 'mcp_servers={}',
                     '--skip-git-repo-check', '--ephemeral', '-s', 'read-only', '--output-schema', str(schema_file),
                     '-o', str(out), '-'], f'{system}\n\n{prompt}', cwd, timeout)
        answer = _parse(out.read_text()) if out.exists() else None
    if not isinstance(answer, dict):
        errors = [line for line in (done.stdout + done.stderr).splitlines() if line.startswith('ERROR')]
        raise TaggerError(f"codex failed: {errors[-1][:300] if errors else 'no answer'}")
    return answer, None


def ask(settings, prompt, cwd, system=SYSTEM, schema=SCHEMA):
    """(answer, cost in US dollars as the CLI reports it, or None when it reports none)."""
    if settings['provider'] == 'codex':
        return ask_codex(prompt, settings['codex_model'], cwd, system=system, schema=schema)
    return ask_claude(prompt, settings['claude_model'], cwd, system=system, schema=schema)


def _pick(value, allowed, default):
    return value if value in allowed else default


def apply(store, book_id, markers, answer):
    """Store the answer. Returns the segment ids that got a speaker; markers the answer skipped stay unassigned."""
    cast = store.characters(book_id)
    refs = {f"c{c['id']}": c['id'] for c in cast}
    refs['narrator'] = next(c['id'] for c in cast if c['role'] == 'narrator')
    used = {line.get('speaker') for line in answer.get('lines', []) if isinstance(line, dict)}
    for new in answer.get('new_characters', []):
        if not isinstance(new, dict) or new.get('ref') not in used or not str(new.get('name', '')).strip():
            continue
        character = store.add_character(
            book_id, str(new['name']), _pick(new.get('role'), ('named', 'extra'), 'extra'),
            _pick(new.get('gender'), GENDERS, 'unknown'), _pick(new.get('age'), AGES, 'adult'),
            _pick(new.get('pitch'), PITCHES, 'medium'), str(new.get('description', ''))[:200])
        refs[new['ref']] = character['id']
    assigned = []
    for line in answer.get('lines', []):
        if not isinstance(line, dict):
            continue
        segment_id, character_id = markers.get(line.get('q')), refs.get(line.get('speaker'))
        if segment_id and character_id:
            with store.lock:
                if store.run('UPDATE segment SET character_id = ? WHERE id = ? AND character_id IS NULL',
                             (character_id, segment_id)).rowcount:
                    # The narrator may have read this line while tagging was paused.
                    store.invalidate_audio("id = ? AND audio_state != 'none'", (segment_id,))
            assigned.append(segment_id)
    return assigned
