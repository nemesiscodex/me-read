import pytest

from meread import epub
from meread.segment import chunks, quote_style, segments, spans, speakable

ALICE = '/Users/julio/.local/share/me-read/samples/gutenberg/alice.epub'


def test_quotes_become_speech_and_the_rest_narration():
    text = 'Alice sighed. “Oh dear!” she said, “I shall be late.”'
    assert spans(text) == [('narration', 'Alice sighed. '), ('speech', '“Oh dear!”'),
                           ('narration', ' she said, '), ('speech', '“I shall be late.”')]


def test_unclosed_quote_runs_to_the_end_of_the_paragraph():
    assert spans('He began: “It was long ago, and far away.') == [
        ('narration', 'He began: '), ('speech', '“It was long ago, and far away.')]


def test_british_single_quotes_ignore_apostrophes():
    text = '‘It’s the dogs’ dinner, isn’t it?’ said Tom. ‘Don’t.’'
    assert spans(text, 'single') == [('speech', '‘It’s the dogs’ dinner, isn’t it?’'),
                                     ('narration', ' said Tom. '), ('speech', '‘Don’t.’')]
    assert quote_style([text]) == 'single'
    assert quote_style(['“Hello,” she said. It’s late.']) == 'double'


def test_long_narration_is_cut_at_sentence_ends_but_not_after_titles():
    text = ' '.join(['Mr. Darcy walked slowly across the wide lawn toward the house.'] * 8)
    parts = chunks(text)
    assert ''.join(parts) == text
    assert len(parts) > 1
    assert all(len(p.split()) <= 40 for p in parts)
    assert not any(p.rstrip().endswith('Mr.') for p in parts)


def test_speakable_drops_quote_marks_and_softens_shouting():
    assert speakable('“ORANGE MARMALADE”, ') == 'Orange Marmalade'
    assert speakable('CHAPTER IV. The Rabbit’s House') == 'Chapter IV. The Rabbit’s House'
    assert speakable('“Wait—stop!” ') == 'Wait, stop!'
    assert speakable(' — ') == ''


@pytest.mark.skipif(not __import__('os').path.exists(ALICE), reason='sample book not downloaded')
def test_a_real_book_splits_losslessly_into_speakable_pieces():
    book = epub.read(ALICE)
    assert book.title == "Alice's Adventures in Wonderland" and book.author == 'Lewis Carroll'
    assert any(c.title.startswith('CHAPTER I.') for c in book.chapters)
    speech = 0
    for chapter in book.chapters:
        for tag, text in chapter.blocks:
            pieces = segments(text, tag)
            assert ''.join(p for _, p in pieces) == text
            for kind, piece in pieces:
                assert bool(speakable(piece)) == any(c.isalnum() for c in piece), (text, piece)
                assert len(piece.split()) <= 70, piece
                speech += kind == 'speech'
    assert speech > 1000


def test_spanish_dash_dialogue_separates_speech_from_the_narrators_asides():
    assert spans('—¿Quién anda ahí? —gritó un guardia desde lo alto—. ¡Conteste!', 'dash') == [
        ('speech', '—¿Quién anda ahí? '), ('narration', '—gritó un guardia desde lo alto—.'), ('speech', ' ¡Conteste!')]
    assert spans('—Soy yo —dijo Tomás.', 'dash') == [('speech', '—Soy yo '), ('narration', '—dijo Tomás.')]
    assert spans('—Llegas tarde.', 'dash') == [('speech', '—Llegas tarde.')]
    assert spans('—Es un hombre bien-nacido —dijo.', 'dash')[0] == ('speech', '—Es un hombre bien-nacido ')
    # Older editions set the dashes tight against the words.
    assert spans('—Ese ruido—replicó el ciego deteniéndose—y que parece un río.', 'dash') == [
        ('speech', '—Ese ruido'), ('narration', '—replicó el ciego deteniéndose—'), ('speech', 'y que parece un río.')]
    assert spans('—Sí, señor—repuso el ciego con naturalidad. No conozco el mundo.', 'dash') == [
        ('speech', '—Sí, señor'), ('narration', '—repuso el ciego con naturalidad. '), ('speech', 'No conozco el mundo.')]
    assert spans('—Por aquí hay agua—dijo a su compañero.', 'dash') == [
        ('speech', '—Por aquí hay agua'), ('narration', '—dijo a su compañero.')]


def test_a_dash_book_still_reads_quotation_marks_in_narration():
    text = 'Recordó las palabras de su madre: «Vuelve antes de que anochezca».'
    assert spans(text, 'dash') == [('narration', 'Recordó las palabras de su madre: '),
                                   ('speech', '«Vuelve antes de que anochezca».')]


def test_dialogue_style_is_detected_from_the_book():
    assert quote_style(['—Hola —dijo.', 'Llovía.', '—Adiós.', 'Recordó: «Vuelve».']) == 'dash'
    assert quote_style(['“Hello,” she said — and left.', 'It rained.']) == 'double'


def test_speakable_drops_dialogue_dashes_and_stray_leading_punctuation():
    assert speakable('—¿Quién anda ahí? ') == '¿Quién anda ahí?'
    assert speakable('—gritó un guardia desde lo alto—.') == 'gritó un guardia desde lo alto.'
    assert speakable(' ¡Conteste!') == '¡Conteste!'


def test_language_comes_from_the_text_when_the_metadata_is_wrong():
    spanish = ['El camino estaba vacío cuando llegó a la puerta de la muralla, y no había nadie más que él.'] * 5
    english = ['The road was empty when he reached the gate, and nobody was there but him and his dog.'] * 5
    assert epub.language('en', spanish) == 'es'
    assert epub.language('es', english) == 'en'
    assert epub.language('es', ['Hmm.']) == 'es' and epub.language('', ['Hmm.']) == 'en'
