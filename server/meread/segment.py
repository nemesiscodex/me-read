"""Split a paragraph into narration and spoken lines, sized for one speech clip each.

The pieces of a block always concatenate back to the block's exact text, so the reader can
render them as spans and the tagger never has to reproduce prose.
"""
import re

MAX_WORDS = 40
OPENERS = '“«'
CLOSERS = '”»'
SENTENCE_END = re.compile(r'[.!?…]+[”’"»)\]]*\s+')
CLAUSE_END = re.compile(r'[,;:—]["”’]?\s+')
ABBREVIATION = re.compile(r'(?:\b(?:Mr|Mrs|Ms|Dr|St|Prof|Sr|Sra|Srta|Jr|Mme|Mlle|Ud|Uds|vs|etc|No)|\b[A-Z])\.$')
DASHES = '—–―'
DASH_OPENING = re.compile(rf'^\s*[{DASHES}](?=\S)')
INNER_DASH = re.compile(rf'[{DASHES}][.,;:]*')
ROMAN = re.compile(r'^[IVXLCDM]+$')


def quote_style(texts):
    """How the book marks dialogue: 'dash' (—Hola —dijo.), 'single' (‘Hello,’) or 'double' (“Hello,”)."""
    double = sum(t.count('“') + t.count('"') + t.count('«') for t in texts)
    single = sum(len(re.findall(r'(?:^|[\s(—])‘', t)) for t in texts)
    dashed = sum(bool(DASH_OPENING.match(t)) for t in texts)
    if dashed > 0.05 * len(texts) and dashed * 2 > double:
        return 'dash'
    return 'single' if single > 2 * double else 'double'


def _double_quotes(text):
    ranges, start = [], None
    for i, ch in enumerate(text):
        if start is None and (ch in OPENERS or ch == '"'):
            start = i
        elif start is not None and (ch in CLOSERS or ch == '"'):
            ranges.append((start, i + 1))
            start = None
    if start is not None:
        ranges.append((start, len(text)))
    return ranges


def _single_quotes(text):
    ranges, i = [], 0
    while i < len(text):
        if text[i] == '‘' and (i == 0 or text[i - 1] in ' \n\t(—'):
            close = fallback = None
            for j in range(i + 1, len(text)):
                if text[j] != '’':
                    continue
                ends_word = j + 1 == len(text) or not text[j + 1].isalnum()
                if text[j - 1] in ',.!?—…-;:' or j + 1 == len(text):
                    close = j
                    break
                if ends_word:
                    fallback = j
            end = (close if close is not None else fallback if fallback is not None else len(text) - 1) + 1
            ranges.append((i, end))
            i = end
        else:
            i += 1
    return ranges


def _dash_dialogue(text):
    """Speech ranges of a paragraph that opens with a dash.

    After the opening dash, dashes alternate: one starts an aside by the narrator (—dijo Ana—) and the next
    ends it. That holds whether the edition spaces them (—Hola —dijo—. Ven.) or not (—Hola—dijo—ven.).
    """
    start = DASH_OPENING.match(text).end()
    ranges, pos, speaking = [], 0, True
    for dash in INNER_DASH.finditer(text, start):
        if speaking:
            ranges.append((pos, dash.start()))
        else:
            pos = dash.end()
        speaking = not speaking
    if speaking:
        ranges.append((pos, len(text)))
    else:
        # Older editions end an aside with a full stop and let the character go on (—Sí—repuso. No conozco...).
        # Whatever follows the aside's sentence is offered to the tagger, which can hand it back to the narrator.
        aside = max((b for _, b in ranges), default=start)
        end = SENTENCE_END.search(text, aside)
        if end and any(c.isalnum() for c in text[end.end():]):
            ranges.append((end.end(), len(text)))
    return [(a, b) for a, b in ranges if b > a]


def spans(text, style='double'):
    """[(kind, text)] covering `text`; kind is 'speech' for dialogue and 'narration' otherwise."""
    if style == 'dash' and DASH_OPENING.match(text):
        quotes = _dash_dialogue(text)
    else:
        quotes = _single_quotes(text) if style == 'single' else _double_quotes(text)
    out, pos = [], 0
    for start, end in quotes:
        if start > pos:
            out.append(['narration', text[pos:start]])
        out.append(['speech', text[start:end]])
        pos = end
    if pos < len(text):
        out.append(['narration', text[pos:]])
    merged = []
    for kind, piece in out:
        if merged and not any(c.isalnum() for c in piece):
            merged[-1][1] += piece
        elif merged and not any(c.isalnum() for c in merged[-1][1]):
            merged[-1] = [kind, merged[-1][1] + piece]
        else:
            merged.append([kind, piece])
    return [(kind, piece) for kind, piece in merged]


def _cut(text, pattern, keep_abbreviations=False):
    pieces, pos = [], 0
    for m in pattern.finditer(text):
        if keep_abbreviations and ABBREVIATION.search(text[pos:m.end()].rstrip()):
            continue
        pieces.append(text[pos:m.end()])
        pos = m.end()
    if pos < len(text):
        pieces.append(text[pos:])
    return pieces


def _pack(pieces, max_words):
    packed = []
    for piece in pieces:
        if packed and len((packed[-1] + piece).split()) <= max_words:
            packed[-1] += piece
        else:
            packed.append(piece)
    if len(packed) > 1 and len(packed[-1].split()) < 4:
        packed[-2:] = [packed[-2] + packed[-1]]
    return packed


def chunks(text, max_words=MAX_WORDS):
    """Cut `text` at sentence ends, then clause ends, into pieces of about `max_words` words."""
    if len(text.split()) <= max_words:
        return [text]
    out = []
    for sentence in _cut(text, SENTENCE_END, keep_abbreviations=True):
        if len(sentence.split()) > max_words * 1.5:
            out.extend(_pack(_cut(sentence, CLAUSE_END), max_words))
        else:
            out.append(sentence)
    return _pack(out, max_words)


def segments(text, tag='p', style='double'):
    """[(kind, text)] for one block: the unit that gets a speaker and an audio clip."""
    if tag == 'h':
        return [('narration', text)]
    return [(kind, piece) for kind, span in spans(text, style) for piece in chunks(span)]


def speakable(text):
    """The words to synthesize for a segment, or '' when there is nothing to say."""
    text = re.sub(r'[“”«»"‘*_\[\]]', '', text)
    text = re.sub(r"(?<!\w)[’']|[’'](?!\w)", '', text)
    text = re.sub(r'\s*[—–―]+\s*', ', ', text)
    text = re.sub(r',\s*([.;:!?,])', r'\1', text)
    text = re.sub(r'\s+', ' ', text).strip(' ,').lstrip('.;: ')

    def soften(m):
        word = m.group()
        return word if ROMAN.match(word) else word.capitalize()
    text = re.sub(r"\b[A-Z][A-Z']+\b", soften, text)
    return text if any(c.isalnum() for c in text) else ''
