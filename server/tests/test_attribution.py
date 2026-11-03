import pytest

from conftest import make_epub
from meread import tagger


def setup_scene(world, paragraphs):
    book = world.client.post('/api/books', files={
        'file': ('scene.epub', make_epub((['<h2>Scene</h2>', *[f'<p>{p}</p>' for p in paragraphs]],)))}).json()
    owen = world.store.add_character(book['id'], 'Owen Vale', 'named')
    father = world.store.add_character(book['id'], 'Mr Vale', 'named')
    nora = world.store.add_character(book['id'], 'Nora', 'named')
    return book, owen, father, nora


def attribute(world, book, speaker):
    blocks = world.store.blocks(book['id'], 1, book['block_count'] - 1)
    _, markers = tagger.build_prompt(book, world.store.characters(book['id']), [], blocks)
    world.store.run("UPDATE block SET tag_state = 'tagging' WHERE book_id = ?", (book['id'],))
    tagger.apply(world.store, book['id'], markers, {'lines': [
        {'q': q, 'speaker': f'c{speaker}', 'emotion': 'neutral'} for q in markers]}, blocks)
    return [world.store.segment(s['id']) for b in blocks for s in b['segments'] if s['kind'] == 'speech']


@pytest.mark.parametrize('paragraph', [
    '"Did you pack the blue notebook?" Owen asked her.',
    '"Is the next train already here?" Owen said with a raised brow.',
    'Owen said, "We should leave."',
    '—Deberíamos irnos —dijo Owen.',
    '"' + 'We should leave now. ' * 30 + '" Owen said.',
])
def test_explicit_owen_tag_overrides_a_model_guess_of_his_father(world, paragraph):
    book, owen, father, _ = setup_scene(world, [paragraph])
    lines = attribute(world, book, father['id'])
    assert lines and {s['character_id'] for s in lines} == {owen['id']}


def test_father_and_son_stay_separate_and_an_address_is_not_a_speaker_tag(world):
    book, owen, father, nora = setup_scene(world, [
        '"The gate is open," Mr Vale whispered.',
        '"Owen, the bus is here," Nora said.',
        '"No," Owen said. "Yes," Nora replied.',
        '"I know," Owen\'s father said.',
    ])
    lines = attribute(world, book, father['id'])
    assert [s['character_id'] for s in lines] == [father['id'], nora['id'], owen['id'], nora['id'], father['id']]


def test_ambiguous_first_name_does_not_override_the_model(world):
    book, _, father, _ = setup_scene(world, ['"Hello," Owen said.'])
    world.store.add_character(book['id'], 'Owen Brooks', 'named')
    assert attribute(world, book, father['id'])[0]['character_id'] == father['id']


def test_manual_correction_and_letter_writer_win_over_dialogue_tags(world):
    book, owen, father, nora = setup_scene(world, ['"Hello," Owen said.', '"Goodbye," Owen said.'])
    blocks = world.store.blocks(book['id'], 1, 2)
    _, markers = tagger.build_prompt(book, world.store.characters(book['id']), [], blocks)
    world.store.correct_line(blocks[0]['segments'][0]['id'], nora['id'])
    world.store.run("UPDATE block SET tag_state = 'tagging' WHERE book_id = ?", (book['id'],))
    tagger.apply(world.store, book['id'], markers, {
        'lines': [{'q': q, 'speaker': f"c{father['id']}"} for q in markers],
        'written_passages': [{'first': 2, 'last': 2, 'writer': f"c{nora['id']}"}],
    }, blocks)
    assert world.store.segment(blocks[0]['segments'][0]['id'])['character_id'] == nora['id']
    assert world.store.segment(blocks[1]['segments'][0]['id'])['character_id'] == nora['id']


def test_wrong_saved_context_label_cannot_override_an_explicit_speaker(world):
    book, owen, father, _ = setup_scene(world, ['"Hello," Owen said.', '"Goodbye."'])
    earlier = world.store.blocks(book['id'], 1, 1)
    earlier[0]['segments'][0]['character_id'] = father['id']
    prompt, _ = tagger.build_prompt(book, world.store.characters(book['id']), earlier,
                                  world.store.blocks(book['id'], 2, 2))
    assert '"Hello,"[Owen Vale]' in prompt
    assert '"Hello,"[Mr Vale]' not in prompt


@pytest.mark.parametrize('paragraph', [
    "\"Hello,\" said Owen Vale's father.",
    "\"Hello,\" Owen Vale's father said.",
    '"Hello," Owen and Nora said.',
    '"Hello," said Owen and Nora.',
    '"Hello," said Owen Brooks.',
    'Owen said nothing to Nora, "Hello," she replied.',
    'Owen said nothing. "Hello," she replied.',
    'Owen said, "Hello," Nora replied.',
    '"Owen said hello," she explained.',
])
def test_mentions_possessives_and_conflicting_tags_are_not_hard_evidence(world, paragraph):
    book, _, father, _ = setup_scene(world, [paragraph])
    assert {s['character_id'] for s in attribute(world, book, father['id'])} == {father['id']}


def test_a_title_with_a_period_matches_the_same_father(world):
    book, owen, father, _ = setup_scene(world, ['"Hello," Mr. Vale said.'])
    assert attribute(world, book, owen['id'])[0]['character_id'] == father['id']
