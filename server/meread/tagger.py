"""Ask a subscription CLI (Claude Code or Codex) who says each quotation, and who is new to the cast."""
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .emotions import EMOTIONS

GENDERS, AGES, PITCHES = ('male', 'female', 'unknown'), ('child', 'teen', 'adult', 'elderly'), ('low', 'medium', 'high')
LANGUAGE_NAMES = {'en': 'English', 'es': 'Spanish'}
LETTER_GREETING = re.compile(r'^(?:my dear|dear|querid[oa]s?|estimad[oa]s?)\s+[^\n,]+,', re.I)
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['new_characters', 'lines', 'written_passages'],
    'properties': {
        'written_passages': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['first', 'last', 'writer'],
            'properties': {'first': {'type': 'integer'}, 'last': {'type': 'integer'}, 'writer': {'type': 'string'}}}},
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
            'type': 'object', 'additionalProperties': False, 'required': ['q', 'speaker', 'emotion'],
            'properties': {'q': {'type': 'integer'}, 'speaker': {'type': 'string'},
                           'emotion': {'type': 'string', 'enum': list(EMOTIONS)}}}}}}
SYSTEM = f"""You attribute dialogue in a book so it can be read aloud with a different voice for each character.

You get the cast found so far, some earlier text for context, and the text to attribute. In that text every \
spoken line starts with a marker such as [[q7]]. A spoken line is a quotation, or in books that write dialogue with \
dashes (—Hola —dijo Ana—. ¿Vienes?) a stretch of what the character says.

Return one entry in `lines` for every marker: {{"q": 7, "speaker": REF, "emotion": EMOTION}}. REF is one of:
- the ref of a cast member, such as "c12"
- the ref of a character you add in `new_characters`, such as "n1"
- "narrator", when the marked text is not said or thought by a character: a title, a sign, a quoted word or \
phrase, or plain narration that follows a dialogue tag (—Sí —dijo Ana. [[q2]]Luego salió de la sala.)

EMOTION is how the line is said: {', '.join(EMOTIONS)}. It is "neutral" unless the text shows how it is said: \
a dialogue tag such as shouted, whispered or sobbed, an exclamation, or the scene. Use "whisper" for whispered or \
murmured lines. Use "yelling" only for calling out to someone far away. Use "shouting" for a loud warning, command \
or announcement without anger. Use "angry_shouting" when the speaker shouts in fury. Use "angry" for restrained \
anger at a normal or quiet speaking volume; anger alone does not imply shouting. These distinctions also apply to \
Spanish dialogue: llamar a lo lejos, gritar una advertencia, gritar con ira, and hablar con ira contenida. \
A line whose speaker is "narrator" is "neutral".

Rules:
- Read letters, notes and diary entries in their writer's voice, even without quotation marks. Every paragraph
starts with [[pN]]. Return written_passages: an array of {{"first": N, "last": N, "writer": REF}} inclusive
paragraph ranges for letters that overlap TEXT TO ATTRIBUTE; return [] when there are none. Use greetings, signatures,
postscripts, surrounding story and both earlier and later context to identify the writer, not the recipient.
Resolve nicknames and signatures against the cast (for example Clare can be Clara). A writer may be added in
new_characters even if they have no quoted dialogue. Include the greeting, body, sign-off, signature and all
postscripts. Keep the writer across paragraphs, even when the letter started earlier or ends later than the
target batch. Return the full visible range, including context paragraphs when the letter crosses a batch boundary;
only target paragraphs will be changed. A greeting at the end of the target still belongs to the letter whose
body and signature appear in LATER TEXT. Stop at the next letter or the return to the surrounding story;
dates and narrative introductions outside the letter stay with the narrator. Do not include chapter headings.
The writer reads the entire passage, including quoted words or dialogue within it. Existing speaker annotations
can be wrong and must not override evidence of who wrote the letter. Still return every q marker,
using the writer as speaker inside these ranges. Ordinary unquoted narration stays with the narrator.
Use only a writer supported by the text; leave an unidentified writer's passage with the narrator.
- Use dialogue tags, turn-taking and context, and always pick the most likely speaker. A quoted thought belongs to \
whoever thinks it.
- Dialogue tags and the current scene override previous speaker annotations, which may be wrong.
- Relatives sharing a surname are different people. Do not confuse a parent with their child.
- Reuse a cast member when the text refers to them by another name, title or description. Add a character only for \
someone not in the cast.
- A new character with a name gets role "named" and the fullest name the text uses, such as "Mr. Vale".
- A character the text calls by a capitalized title or kind, such as "the Duchess" or "the Mouse", is "named" with \
that title: "Duchess", "Mouse".
- A new character without a name gets role "extra" and a name made of a type and a number, such as "Student 1" or \
"Guard 2". Continue the numbering of extras already in the cast. People speaking together are one extra, such as "Crowd 1".
- Set gender, age and pitch from what the text says about the character, and make a sensible guess when it says nothing.
- Write names as the book does, in the book's language ("Guardia 1" in a Spanish book).
- `description` is always in English: at most fifteen words on how the voice sounds and nothing on who the character \
is. Give its texture (husky, breathy, nasal, raspy, smooth, thin, full, creaky), how loud and how fast it is, and an \
accent when the book gives one, such as "raspy and gruff, loud, slow and heavy". Make it differ from the voices of \
the cast so far. Leave out age, gender and pitch, which have their own fields.
Reply with the JSON object only."""


class TaggerError(RuntimeError):
    pass


def starts_letter(block):
    """An explicit salutation starts a fresh attribution batch, keeping adjacent writers apart."""
    return bool(LETTER_GREETING.match(''.join(s['text'] for s in block['segments']).strip()))


def _voice_line(c):
    traits = ' '.join(x for x in (c['gender'], c['age'], f"{c['pitch']} voice") if x != 'unknown')
    return f"c{c['id']} | {c['name']} | {c['role']} | {traits} | {c['description']}"


# Only quotation-adjacent, named dialogue tags are hard evidence. Surnames and
# pronouns need story context, so they must never silently resolve to a relative.
SPEECH_VERBS = (
    'said|asked|replied|answered|whispered|murmured|shouted|yelled|cried|called|'
    'added|continued|mumbled|muttered|exclaimed|admitted|clarified|quoted|'
    'dijo|preguntó|respondió|contestó|susurró|murmuró|gritó|exclamó|añadió|replicó'
)
TITLES = {'mr', 'mrs', 'ms', 'miss', 'dr', 'professor', 'prof', 'sir', 'lady', 'lord',
          'señor', 'señora', 'sr', 'sra', 'don', 'doña'}


def explicit_speakers(cast, blocks):
    """Segment -> speaker, only where an adjacent tag names one unambiguous cast member.

    Work on contiguous speech/narration spans, since a long quotation or narration
    can be split across several audio clips. Conflicting tags are left to the model.
    """
    def normalize(name):
        return ' '.join(name.casefold().replace('.', '').split())

    aliases = {}
    for character in cast:
        if character['role'] != 'named':
            continue
        name = normalize(character['name'])
        choices = {name}
        first = name.split()[0] if name else ''
        if first and first not in TITLES:
            choices.add(first)
        for alias in choices - {''}:
            aliases.setdefault(alias, set()).add(character['id'])
    unique = {name: next(iter(ids)) for name, ids in aliases.items() if len(ids) == 1}
    if not unique:
        return {}
    names = '|'.join(r'\s+'.join(re.escape(word) + (r'\.?' if word in TITLES else '')
                                 for word in name.split()) for name in sorted(unique, key=len, reverse=True))
    tag = (rf'(?:(?P<before>(?>{names}))\s+(?:{SPEECH_VERBS})(?=[\s.,;:!?—]|$)'
           rf'|(?:{SPEECH_VERBS})\s+(?P<after>(?>{names}))(?=[\s.,;:!?—]|$))')
    following = re.compile(r'^\s*[—–―]?\s*' + tag, re.I)
    preceding = re.compile(r'(?:^|[.!?]\s+)' + tag + r'\s*[:,]\s*$', re.I)
    evidence = {}
    for block in blocks:
        if block.get('writer_id') is not None:
            continue
        groups = []
        for segment in block['segments']:
            if groups and groups[-1][0]['kind'] == segment['kind']:
                groups[-1].append(segment)
            else:
                groups.append([segment])
        candidates = {}
        for i, group in enumerate(groups):
            if group[0]['kind'] != 'narration':
                continue
            text = ''.join(s['text'] for s in group)
            for match, neighbor in ((following.match(text), i - 1), (preceding.search(text), i + 1)):
                if not match or not 0 <= neighbor < len(groups) or groups[neighbor][0]['kind'] != 'speech':
                    continue
                if match['after']:
                    tail = text[match.end('after'):]
                    # A following surname or conjunction means the name is incomplete or plural.
                    word = re.match(r'\s+(\w+)', tail)
                    if word and (word[1][0].isupper() or word[1].casefold() in {'and', 'y', 'e'}):
                        continue
                speaker = unique[normalize(match['before'] or match['after'])]
                for segment in groups[neighbor]:
                    candidates.setdefault(segment['id'], set()).add(speaker)
        evidence.update({sid: next(iter(ids)) for sid, ids in candidates.items() if len(ids) == 1})
    return evidence


def book_line(book):
    return (f"BOOK: {book['title']}" + (f" by {book['author']}" if book['author'] else '')
            + f" (language: {LANGUAGE_NAMES.get(book.get('language'), 'English')})")


def build_prompt(book, cast, earlier, blocks, later=()):
    """The prompt, and the segment id behind each [[qN]] marker.

    `earlier` and `blocks` are blocks with their segments. Spoken lines of `blocks` without a speaker get a marker.
    """
    names = {c['id']: c['name'] for c in cast}
    markers = {}
    evidence = explicit_speakers(cast, [*earlier, *blocks, *later])

    def render(block, mark):
        text = f"[[p{block['idx']}]]"
        for seg in block['segments']:
            ask = mark and seg['kind'] == 'speech' and seg['character_id'] is None
            if ask:
                markers[len(markers) + 1] = seg['id']
                text += f'[[q{len(markers)}]]'
            text += seg['text']
            speaker = evidence.get(seg['id'], seg['character_id'])
            if not ask and seg['kind'] == 'speech' and speaker in names:
                text += f"[{names[speaker]}]"
        if block.get('writer_id') in names:
            text += f" [Written by {names[block['writer_id']]}]"
        return text
    context = [render(block, False) for block in earlier]
    target = [render(block, True) for block in blocks]
    speaking = [c for c in cast if c['role'] != 'narrator']
    prompt = '\n\n'.join([
        book_line(book),
        'CAST SO FAR (ref | name | role | voice | description):\n' + ('\n'.join(map(_voice_line, speaking)) or '(nobody yet)'),
        'EARLIER TEXT, for context only. A previous speaker attribution follows the quotation in brackets; it may be wrong:\n' + ('\n'.join(context) or '(start of the book)'),
        'TEXT TO ATTRIBUTE:\n' + '\n\n'.join(target),
        'LATER TEXT, for context only. Use signatures here to identify writers:\n'
        + ('\n\n'.join(render(block, False) for block in later) or '(end of the chapter)')])
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


def apply(store, book_id, markers, answer, blocks=()):
    """Store the answer. Returns the segment ids that got a speaker; markers the answer skipped stay unassigned."""
    cast = store.characters(book_id)
    refs = {f"c{c['id']}": c['id'] for c in cast}
    refs['narrator'] = next(c['id'] for c in cast if c['role'] == 'narrator')
    used = {line.get('speaker') for line in answer.get('lines', []) if isinstance(line, dict)}
    used.update(p.get('writer') for p in answer.get('written_passages', []) if isinstance(p, dict))
    for new in answer.get('new_characters', []):
        if not isinstance(new, dict) or new.get('ref') not in used or not str(new.get('name', '')).strip():
            continue
        name = str(new['name']).strip().casefold()
        matches = [c for c in cast if c['name'].casefold() == name]
        if not matches and new.get('role') == 'named' and len(name.split()) == 1:
            matches = [c for c in cast if c['role'] == 'named' and c['name'].casefold().startswith(name + ' ')]
        if len(matches) == 1:
            refs[new['ref']] = matches[0]['id']
            continue
        character = store.add_character(
            book_id, str(new['name']), _pick(new.get('role'), ('named', 'extra'), 'extra'),
            _pick(new.get('gender'), GENDERS, 'unknown'), _pick(new.get('age'), AGES, 'adult'),
            _pick(new.get('pitch'), PITCHES, 'medium'), str(new.get('description', ''))[:200])
        refs[new['ref']] = character['id']
    evidence = explicit_speakers(store.characters(book_id), blocks)
    assigned = []
    targets = {b['idx']: b for b in blocks if b['tag'] == 'p'}
    for passage in answer.get('written_passages', []):
        if not isinstance(passage, dict):
            continue
        first, last = passage.get('first'), passage.get('last')
        writer = refs.get(passage.get('writer'))
        if (type(first) is not int or type(last) is not int or first > last
                or not writer or writer == refs['narrator']):
            continue
        with store.lock:
            for idx, block in targets.items():
                if not first <= idx <= last:
                    continue
                if store.one('SELECT tag_state FROM block WHERE id = ?', (block['id'],))['tag_state'] != 'tagging':
                    continue
                store.run('UPDATE block SET writer_id = ? WHERE id = ?', (writer, block['id']))
                for seg in block['segments']:
                    # The snapshot comparison preserves corrections made while this request was running.
                    changed = store.run(
                        'UPDATE segment SET character_id = ?, emotion = ? WHERE id = ? '
                        'AND character_id IS ? AND audio_rev = ?',
                        (writer, 'neutral', seg['id'], seg['character_id'], seg['audio_rev'])).rowcount
                    if changed:
                        store.invalidate_audio('id = ?', (seg['id'],))
                        assigned.append(seg['id'])
    for line in answer.get('lines', []):
        if not isinstance(line, dict):
            continue
        segment_id = markers.get(line.get('q'))
        character_id = evidence.get(segment_id, refs.get(line.get('speaker')))
        emotion = 'neutral' if character_id == refs['narrator'] else _pick(line.get('emotion'), EMOTIONS, 'neutral')
        if segment_id and character_id:
            with store.lock:
                if store.run('UPDATE segment SET character_id = ?, emotion = ? WHERE id = ? AND character_id IS NULL',
                             (character_id, emotion, segment_id)).rowcount:
                    # The narrator may have read this line while tagging was paused.
                    store.invalidate_audio("id = ? AND audio_state != 'none'", (segment_id,))
            assigned.append(segment_id)
    return assigned
