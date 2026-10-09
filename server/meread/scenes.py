"""Ask the tagger's CLI which moments of the text are worth a picture, and how the people in them look."""
import re

from . import tagger

MAX_ENDS = 2
MAX_CHARACTERS = 3
LOOK_BUDGET = 24000
TITLE_CHARS = 80
SUMMARY_CHARS = 240
SCENE_CHARS = 1400
APPEARANCE_CHARS = 900
# Words of a name that say nothing about who is meant.
TITLES = {'miss', 'lady', 'lord', 'doctor', 'captain', 'mister', 'madam', 'madame', 'master', 'professor',
          'señor', 'señora', 'señorita', 'doña', 'doctora', 'capitán', 'capitana', 'profesor', 'profesora'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['moments', 'ends', 'appearances'],
    'properties': {
        'ends': {'type': 'array', 'items': {'type': 'integer'}},
        'moments': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['p', 'title', 'summary', 'scene', 'importance', 'characters'],
            'properties': {'p': {'type': 'integer'}, 'title': {'type': 'string'}, 'summary': {'type': 'string'},
                           'scene': {'type': 'string'}, 'importance': {'type': 'integer'},
                           'characters': {'type': 'array', 'items': {'type': 'string'}}}}},
        'appearances': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['character', 'appearance'],
            'properties': {'character': {'type': 'string'}, 'appearance': {'type': 'string'}}}}}}
# What an appearance has to say, for both prompts that write one.
LOOK = """80 to 120 English words that would let two artists draw the same person from the chest up: apparent age, \
build, skin, the shape of the face, eyes, nose and mouth, hair with its colour, length and style, anything that marks \
them out (scars, glasses, beard, freckles, jewellery), and what they usually wear on the head and upper body, garment \
by garment with colours and condition. Nothing below the waist, which the portrait does not show. Say what is seen, \
never what they are like"""
# What both prompts say about describing a moment and the people in it.
RULES = """- `title` is at most 6 words, like the caption under a plate in an illustrated book. `summary` is one sentence of at \
most 25 words on what happens. Both are in English and tell nothing the text has not reached at that paragraph.
- `scene` is what the artist draws from, and the artist has not read the book and knows no name in it. Write 110 to \
160 English words in the present tense on what is seen at that instant, and nothing else, in this order:
  1. Indoors or outdoors, the time of day, the weather and the light with where it comes from, then the place in \
plain words that say what it looks like: "Night, outdoors, in a dark grassy field beside a tall crooked farmhouse \
with lit windows", never only "at the Burrow".
  2. How many people are seen: "Two people." or "Three people and a large black dog."
  3. Each one in turn: where they are (on the left, in the middle, on the right, in the background), their pose, \
what their hands do, their expression and where they look. Call a cast member by their cast name, every time and \
with nothing about their face, hair or usual clothes: the artist has their portrait. Do say what is different from \
usual at this moment: soaked, bleeding, in a nightshirt, carrying a lantern. Say what anyone else looks like.
  4. The things that matter in the picture and where they are: furniture, objects held, doors, windows, animals.
  5. The background and the mood of the light: colours, shadows, smoke, rain.
  No dialogue, no written text, no thoughts or feelings that cannot be seen.
- `importance` is a whole number from 1 to 10 and is compared across the whole book, so use it the same way every \
time: 9 or 10 for a turning point of the story, 7 or 8 for a strong new sight such as an arrival, a fight, a \
discovery or a new place, 4 to 6 for a clear change in what is seen, 1 to 3 for a small one.
- `characters` lists the cast members visible in the scene by ref, such as "c12": at most three, most important \
first. Never the narrator, and nobody who is not in the cast.
- `appearances` has one entry for every cast member you list in a moment whose appearance is "(not described yet)": \
{"character": "c12", "appearance": "..."}. """ + LOOK + """. Take it from the book, and guess sensibly for the \
setting where the book says nothing.
- Never rewrite an appearance the cast already has.
Reply with the JSON object only."""
MOMENT = '{"p": 7, "title": "...", "summary": "...", "scene": "...", "importance": 6, "characters": [REF, ...]}'
SYSTEM = """You find the moments of a book that an artist could illustrate for someone who is reading it, the way a \
courtroom artist finds what to sketch. One artist draws the pictures, and a picture stays in front of the reader \
until the next one.

You get the cast, LAST ILLUSTRATION (the nearest moment before this text), some earlier text for context, and the \
text to scan. In that text every paragraph starts with a marker such as [[p7]].

Return in `moments` every moment of that text worth a picture: """ + MOMENT + """

Rules:
- Find them all, not only the best ones: the reader chooses how many pictures a page shows, and the ones with the \
highest `importance` are drawn. A page of action or travel has several; a page of talk in one room may have one.
- At most one moment a paragraph. Two moments must differ in what is seen: another place, other people, another \
action. More talk in the same room is not a new moment.
- Put the marker on the paragraph where the thing becomes visible, never earlier. The picture must not show the \
reader anything the text has not reached.
- `ends` lists the paragraphs, by marker number, from which LAST ILLUSTRATION no longer matches what is happening because the place or the people changed, while nothing there is worth a new picture yet. The picture is taken down at that paragraph. Use it sparingly and only for a clear change; an empty list is the normal answer, and it is always empty when LAST ILLUSTRATION is "(none)".
""" + RULES
STAR_SYSTEM = """You describe one moment of a book for the artist who illustrates it. The reader chose this paragraph to be illustrated.

You get the cast, some earlier text for context, and the paragraph, which starts with the marker [[p1]].

Return exactly one moment for it in `moments`: """ + MOMENT.replace('7', '1') + """

Rules:
- `ends` is always empty.
""" + RULES


def _cast_line(c):
    traits = ' '.join(x for x in (c['gender'], c['age']) if x != 'unknown')
    return f"c{c['id']} | {c['name']} | {c['role']} | {traits} | {c['appearance'] or '(not described yet)'}"


def _text(block):
    return ''.join(seg['text'] for seg in block['segments'])


def _prompt(book, cast, earlier, blocks, *about):
    markers = {n: block for n, block in enumerate(blocks, 1)}
    seen = [c for c in cast if c['role'] != 'narrator']
    prompt = '\n\n'.join([
        tagger.book_line(book),
        'CAST (ref | name | role | gender and age | appearance):\n' + ('\n'.join(map(_cast_line, seen)) or '(nobody yet)'),
        *about,
        'EARLIER TEXT, for context only:\n' + ('\n'.join(map(_text, earlier)) or '(start of the book)'),
        'TEXT TO SCAN:\n' + '\n\n'.join(f'[[p{n}]]{_text(block)}' for n, block in markers.items())])
    return prompt, markers


def build_prompt(book, cast, last, earlier, blocks):
    """The prompt, and the block behind each [[pN]] marker.

    `last` is the nearest moment before `blocks`, or None. `earlier` and `blocks` are blocks with their segments.
    """
    back = last and blocks[0]['idx'] - last['block_idx']
    back = last and f"{back} {'paragraph' if back == 1 else 'paragraphs'} back"
    if not last:
        showing = '(none)'
    elif last['kind'] == 'clear':
        showing = f'(none) The picture before it stopped matching the text {back}.'
    else:
        showing = f"{last['summary'] or last['scene']} ({back})"
    return _prompt(book, cast, earlier, blocks, 'LAST ILLUSTRATION:\n' + showing)


def star_prompt(book, cast, earlier, block):
    """The prompt for the one paragraph the reader wants drawn, and that block behind [[p1]]."""
    return _prompt(book, cast, earlier, [block])


def _clean(value, limit):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


def _importance(value):
    return min(10, max(1, value)) if isinstance(value, int) and not isinstance(value, bool) else 5


def apply(store, book_id, markers, answer, most=None):
    """Store the answer, with at most `most` pictures when given. Returns the ids of the moment rows it wrote. A
    paragraph that already has a moment keeps it, except that one found before moments had titles takes the new
    words and keeps its picture."""
    cast = {f"c{c['id']}": c['id'] for c in store.characters(book_id) if c['role'] != 'narrator'}
    wanted, created = answer.get('moments'), []
    found = sorted((m for m in wanted if isinstance(m, dict) and isinstance(m.get('p'), int)),
                   key=lambda m: m['p']) if isinstance(wanted, list) else []
    for moment in found[:most]:
        block, scene = markers.get(moment['p']), _clean(moment.get('scene'), SCENE_CHARS)
        if not block or not scene:
            continue
        words = (_clean(moment.get('title'), TITLE_CHARS), _clean(moment.get('summary'), SUMMARY_CHARS), scene,
                 _importance(moment.get('importance')))
        refs = moment.get('characters') if isinstance(moment.get('characters'), list) else []
        present = list(dict.fromkeys(cast[ref] for ref in refs if isinstance(ref, str) and ref in cast))[:MAX_CHARACTERS]
        with store.lock:
            added = store.run('INSERT OR IGNORE INTO moment (book_id, block_id, block_idx, title, summary, scene, importance) '
                              'VALUES (?, ?, ?, ?, ?, ?, ?)', (book_id, block['id'], block['idx'], *words))
            if not added.rowcount:
                if words[0] and store.run("UPDATE moment SET title = ?, summary = ?, scene = ?, importance = ? "
                                          "WHERE block_id = ? AND kind = 'picture' AND title = ''", (*words, block['id'])).rowcount:
                    created.append(store.one('SELECT id FROM moment WHERE block_id = ?', (block['id'],))['id'])
                continue
            for pos, character_id in enumerate(present):
                store.run('INSERT INTO moment_character (moment_id, character_id, pos) VALUES (?, ?, ?)',
                          (added.lastrowid, character_id, pos))
        created.append(added.lastrowid)
    # After the pictures, so that a paragraph given both keeps its picture.
    ends = answer.get('ends')
    for p in sorted(p for p in ends if isinstance(p, int))[:MAX_ENDS] if isinstance(ends, list) else []:
        block = markers.get(p)
        added = block and store.run("INSERT OR IGNORE INTO moment (book_id, block_id, block_idx, kind, scene) "
                                    "VALUES (?, ?, ?, 'clear', '')", (book_id, block['id'], block['idx']))
        if added and added.rowcount:
            created.append(added.lastrowid)
    looks = answer.get('appearances')
    for look in looks if isinstance(looks, list) else []:
        character_id = cast.get(look.get('character')) if isinstance(look, dict) and isinstance(look.get('character'), str) else None
        appearance = _clean(look.get('appearance'), APPEARANCE_CHARS) if character_id else ''
        if appearance:
            store.run("UPDATE character SET appearance = ? WHERE id = ? AND appearance = ''", (appearance, character_id))
    return created


# A character's looks, from the whole book

LOOK_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['appearance'],
               'properties': {'appearance': {'type': 'string'}}}
LOOK_SYSTEM = """You describe how a character of a book looks, so that an artist can draw them the same way every time.

You get the character and the paragraphs of the book that mention them or hold their lines, in book order.

Return {"appearance": "..."}: """ + LOOK + """. Take it from the text, and guess sensibly for the setting where \
the text says nothing. Describe how they look for most of the book, not a \
disguise or a single scene.
Reply with the JSON object only."""


def name_pattern(name):
    """Matches the name, or any word of it that tells this character apart: four letters or more and not a title."""
    words = name.split()
    telling = [w for w in words if len(w) >= 4 and w.isalpha() and w.lower() not in TITLES] if len(words) > 1 else []
    return re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(x) for x in [name.strip(), *telling]) + r')(?!\w)', re.I)


def spread(paragraphs, budget):
    """As many of the paragraphs as fit the budget of characters, spaced evenly across the book when not all do."""
    keep = len(paragraphs)
    while keep > 1:
        last = len(paragraphs) - 1
        chosen = paragraphs if keep > last else [paragraphs[round(i * last / (keep - 1))] for i in range(keep)]
        if sum(map(len, chosen)) <= budget:
            return chosen
        keep -= 1
    return [p[:budget] for p in paragraphs[:1]]


def mentioning(paragraphs, name, spoken=(), budget=LOOK_BUDGET):
    """The text of the paragraphs about a character, in book order and within the budget.

    `paragraphs` are (block idx, text) for the whole book; `spoken` are the idx of the blocks holding their lines.
    """
    pattern, spoken = name_pattern(name), set(spoken)
    return spread([text for idx, text in paragraphs if idx in spoken or pattern.search(text)], budget)


def look_prompt(book, character, paragraphs):
    traits = ' '.join(x for x in (character['gender'], character['age']) if x != 'unknown')
    return '\n\n'.join([tagger.book_line(book), f"CHARACTER: {character['name']} ({traits})",
                          'PARAGRAPHS:\n' + '\n\n'.join(paragraphs)])


def look_from(answer):
    return _clean(answer.get('appearance'), APPEARANCE_CHARS)
