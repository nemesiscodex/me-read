"""Write the web client's types for the rows the API returns, from the database schema and the columns the store
selects. The client then never restates a column or the values a column may hold. Run `python -m meread.web_types`
after changing either; a test fails until the written file matches."""
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

from . import store
from .emotions import EMOTIONS

PATH = Path(__file__).resolve().parents[2] / 'web' / 'src' / 'generated' / 'server.ts'
COMMAND = 'cd server && uv run python -m meread.web_types'


class Shape(NamedTuple):
    """A row the web client reads: the columns the store selects and the tables their aliases name. `types` gives the
    fields Python adds to the row or converts, `docs` the docs of fields the schema does not describe."""
    name: str
    columns: str
    tables: dict
    types: dict = {}
    docs: dict = {}
    doc: str = ''


SHAPES = (
    Shape('BookSummary', store.BOOK, {'': 'book'}),
    Shape('BookDetail', store.BOOK, {'': 'book'}, {'emotions': 'Emotion[]', 'chapters': 'Chapter[]', 'characters': 'Character[]'},
          {'emotions': "Delivery options supported by this book's language and the selected narrator."}),
    Shape('Chapter', store.CHAPTER, {'': 'chapter'}),
    Shape('Block', store.BLOCK, {'': 'block'}, {'segments': 'Segment[]'}),
    Shape('Segment', store.SEGMENT, {'': 'segment'}),
    Shape('Character', store.CHARACTER, {'c': 'character'}, {'line_count': 'number', 'takes': 'Take[]'},
          {'takes': 'One for each emotion other than neutral, in the order of Emotion. Neutral is the voice itself.'}),
    Shape('Take', store.TAKE, {'t': 'voice_take'},
          docs={'state': "'none' also for a take made from a voice the character no longer has. It is made again when a "
                         'line needs it.'},
          doc="The character's voice directed into an emotion. Lines in that emotion are read from it once it is ready."),
    Shape('ReusableVoice', store.REUSABLE_VOICE, {'c': 'character', 'b': 'book'}),
    Shape('Moment', store.MOMENT, {'m': 'moment'}, {'starred': 'boolean', 'character_ids': 'number[]'},
          {'character_ids': 'The characters in the picture, most important first.'},
          "A paragraph where the picture changes: a new one is shown, or with kind 'clear' the last one is taken down."),
)


def literals(values):
    return ' | '.join(f"'{value}'" for value in values)


def schema():
    """The TypeScript type of each column, by (table, column)."""
    db = sqlite3.connect(':memory:')
    db.executescript(store.SCHEMA)
    types = {}
    for table, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall():
        allowed = {column: re.findall(r"'([^']*)'", values) for column, values in re.findall(r'CHECK \((\w+) IN \(([^)]*)\)\)', sql)}
        for _, column, kind, not_null, _, key in db.execute(f'PRAGMA table_info({table})'):
            if column in allowed:
                named = 'Emotion' if tuple(allowed[column]) == EMOTIONS else literals(allowed[column])
            else:
                named = {'INTEGER': 'number', 'REAL': 'number', 'TEXT': 'string'}[kind]
            types[table, column] = named if not_null or key else f'{named} | null'
    return types


def comments():
    """The `--` comment above each column in the schema, by (table, column)."""
    found, table, lines = {}, None, []
    for line in map(str.strip, store.SCHEMA.splitlines()):
        if line.startswith('CREATE TABLE'):
            table = line.split()[-2]
        elif line.startswith('--'):
            lines.append(line[2:].strip())
        elif lines:
            found[table, line.split()[0]] = ' '.join(lines)
            lines = []
    return found


def fields(shape, types, notes):
    """The shape's fields in order, as {field: (TypeScript type, doc)}."""
    found = {}
    for item in re.split(r',\s*(?![^()]*\))', shape.columns):
        expression, alias = re.fullmatch(r'(.+?)(?:\s+AS\s+"?(\w+)"?)?', item.strip()).groups()
        reference = re.fullmatch(r'(?:(\w+)\.)?(\w+)', expression)
        # An expression stands for the column of the row's own table it is named after, as a take's state does.
        origin = (shape.tables[reference[1] or ''], reference[2]) if reference else (next(iter(shape.tables.values())), alias)
        field = alias or origin[1]
        found[field] = (shape.types.get(field) or types[origin], shape.docs.get(field) or notes.get(origin))
    for field, named in shape.types.items():
        found.setdefault(field, (named, shape.docs.get(field)))
    return found


def shapes():
    """Each type the web client reads, as {name: {field: (TypeScript type, doc)}}."""
    types, notes = schema(), comments()
    return {shape.name: fields(shape, types, notes) for shape in SHAPES}


def source():
    lines = ['// Written by server/meread/web_types.py from server/meread/store.py and emotions.py. Do not edit.',
             f'// Regenerate with: {COMMAND}', '',
             f'export const MAX_PICTURES_PER_PAGE = {store.MAX_PICTURES_PER_PAGE}', '',
             f'export type Emotion = {literals(EMOTIONS)}']
    docs = {shape.name: shape.doc for shape in SHAPES}
    for name, found in shapes().items():
        lines += ['', *([f'/** {docs[name]} */'] if docs[name] else []), f'export interface {name} {{']
        for field, (named, doc) in found.items():
            lines += [*([f'  /** {doc} */'] if doc else []), f'  {field}: {named}']
        lines.append('}')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(source())
    print(f'wrote {PATH}')
