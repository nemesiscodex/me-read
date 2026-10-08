"""Read an EPUB into chapters of plain-text blocks, in reading order."""
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import etree, html

NS = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container',
      'opf': 'http://www.idpf.org/2007/opf',
      'dc': 'http://purl.org/dc/elements/1.1/'}
HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
BLOCKS = HEADINGS | {'p', 'li', 'blockquote', 'pre', 'dt', 'dd', 'div', 'td'}
SKIPPED = {'script', 'style', 'head', 'nav', 'svg', 'math', 'aside', 'figure', 'table'}


@dataclass
class Chapter:
    title: str
    blocks: list = field(default_factory=list)  # [(tag, text)] with tag 'p' or 'h'


@dataclass
class Book:
    title: str
    author: str
    chapters: list
    language: str = 'en'


class EpubError(ValueError):
    pass


def _local(el):
    return el.tag.rsplit('}', 1)[-1].lower() if isinstance(el.tag, str) else ''


def _text(el):
    return re.sub(r'\s+', ' ', ''.join(el.itertext())).strip()


def _blocks(root):
    out = []

    def walk(el):
        name = _local(el)
        if not name or name in SKIPPED:
            return
        if name in BLOCKS and not any(_local(d) in BLOCKS for d in el.iterdescendants()):
            text = _text(el)
            if text:
                out.append(('h' if name in HEADINGS else 'p', text))
            return
        for child in el:
            walk(child)
    walk(root)
    return out


SPANISH = re.compile(r'\b(?:el|la|los|las|que|de|del|en|por|con|una|pero|como|más|también|había|estaba)\b', re.I)
ENGLISH = re.compile(r'\b(?:the|and|of|to|was|that|with|had|but|for|not|she|he|his|her|were)\b', re.I)
LANGUAGES = ('en', 'es')


def language(declared, texts):
    """The book's language, from its text. EPUB metadata is often wrong, so it only breaks a tie."""
    sample = ' '.join(texts[:400])
    spanish, english = len(SPANISH.findall(sample)), len(ENGLISH.findall(sample))
    if spanish > 2 * english:
        return 'es'
    if english > 2 * spanish:
        return 'en'
    return declared if declared in LANGUAGES else 'en'


def _parse(data):
    # XHTML is UTF-8 unless it says otherwise, but the HTML parser would guess Latin-1.
    try:
        return html.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', data.decode('utf-8')))
    except UnicodeDecodeError:
        return html.fromstring(data)


def _split_at_headings(blocks):
    """One file often holds a whole book. Each heading that is followed by text starts a chapter."""
    parts = [[]]
    for tag, text in blocks:
        if tag == 'h' and any(t == 'p' for t, _ in parts[-1]):
            parts.append([])
        parts[-1].append((tag, text))
    return parts


def read(path):
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise EpubError('not an EPUB file') from e
    with archive:
        try:
            container = etree.fromstring(archive.read('META-INF/container.xml'))
            opf_path = container.find('.//c:rootfile', NS).get('full-path')
            opf = etree.fromstring(archive.read(opf_path))
        except (KeyError, AttributeError, etree.XMLSyntaxError) as e:
            raise EpubError('EPUB has no readable package file') from e
        title = (opf.findtext('.//dc:title', namespaces=NS) or '').strip() or 'Untitled'
        author = (opf.findtext('.//dc:creator', namespaces=NS) or '').strip()
        items = {i.get('id'): i for i in opf.findall('.//opf:manifest/opf:item', NS)}
        chapters = []
        for ref in opf.findall('.//opf:spine/opf:itemref', NS):
            item = items.get(ref.get('idref'))
            if item is None or 'nav' in (item.get('properties') or '').split():
                continue
            if 'html' not in (item.get('media-type') or ''):
                continue
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(item.get('href'))))
            try:
                root = _parse(archive.read(name))
            except (KeyError, etree.ParserError):
                continue
            body = root.find('.//body')
            blocks = _blocks(body if body is not None else root)
            if not blocks:
                continue
            for part in _split_at_headings(blocks):
                # The heading right above the text names the chapter; earlier ones are book or part titles.
                lead = [text for tag, text in part[:next((i for i, (tag, _) in enumerate(part) if tag == 'p'), len(part))]]
                heading = lead[-1] if lead else None
                chapters.append(Chapter(heading or f'Section {len(chapters) + 1}', part))
    if not chapters:
        raise EpubError('EPUB has no readable text')
    declared = (opf.findtext('.//dc:language', namespaces=NS) or '').strip().lower()[:2]
    return Book(title, author, chapters, language(declared, [text for c in chapters for _, text in c.blocks]))
