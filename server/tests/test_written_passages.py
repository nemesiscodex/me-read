import re

from meread import pipeline, tagger
from meread.store import Store
from conftest import make_epub


def test_letters_cross_batches_and_keep_the_writer_through_quotes_and_postscripts(world, monkeypatch):
    paragraphs = [
        '<h2>Letters</h2>', '<p>Clara opened the envelope.</p>',
        '<p>Dear Clara,</p>', '<p>Ben sent his "apology" this morning.</p>',
        '<p>I hope you can visit.</p>', '<p>Love,</p>', '<p>Nora</p>', '<p>PS: Bring your books.</p>',
        '<p>Dear Nora,</p>', '<p>I will visit on Sunday.</p>', '<p>Love,</p>', '<p>Clare</p>',
        '<p>PS: Tell Ben I forgive him.</p>', '<p>Clara folded the letter and went outside.</p>',
    ]
    book = world.client.post('/api/books', files={'file': ('letters.epub', make_epub((paragraphs,)))}).json()
    nora = world.store.add_character(book['id'], 'Nora', 'named', 'female', 'teen', 'medium', '')
    clara = world.store.add_character(book['id'], 'Clara Reed', 'named', 'female', 'teen', 'medium', '')
    writers = {**dict.fromkeys(range(2, 8), nora['id']), **dict.fromkeys(range(8, 13), clara['id'])}
    monkeypatch.setattr(pipeline, 'TAG_BATCH', 2)

    def answer(prompt):
        assert '[[p6]]Nora' in prompt
        assert '[[p11]]Clare' in prompt
        assert prompt.index('TEXT TO ATTRIBUTE:') < prompt.index('LATER TEXT,')
        target = prompt.split('TEXT TO ATTRIBUTE:\n')[1].split('LATER TEXT,')[0]
        assert target.count('Dear ') <= 1
        if 'Dear ' in target:
            assert re.match(r'\[\[p\d+\]\]Dear ', target)
        indices = [int(n) for n in re.findall(r'\[\[p(\d+)\]\]', target)]
        return {'new_characters': [], 'lines': [], 'written_passages': [
            {'first': i, 'last': i, 'writer': f'c{writers[i]}'} for i in indices if i in writers]}

    world.ask.answer = answer
    world.pipeline.set_focus(book['id'], 0, 13, 0, 13)
    world.drain()
    narrator = world.store.narrator(book['id'])['id']
    for b in world.store.blocks(book['id'], 0, 13):
        assert b['tag_state'] == 'tagged'
        assert {s['character_id'] for s in b['segments']} == {writers.get(b['idx'], narrator)}
        assert all(s['audio_state'] == 'ready' for s in b['segments'])
    assert len(world.ask.prompts) > 1
    assert 'Written by Nora' in world.ask.prompts[-1]

    reopened = Store(world.store.home)
    assert reopened.blocks(book['id'], 4, 4)[0]['segments'][0]['character_id'] == nora['id']
    reopened.db.close()
    response = world.client.post(f"/api/books/{book['id']}/retag", json={'first': 4, 'last': 4})
    assert response.json() == {'retagged': 1}
    assert world.store.blocks(book['id'], 4, 4)[0]['segments'][0]['audio_state'] == 'none'
    world.drain()
    assert world.store.blocks(book['id'], 4, 4)[0]['segments'][0]['character_id'] == nora['id']


def test_context_is_same_chapter_and_cannot_change_other_pages(world):
    from test_pipeline import answer, focus
    world.ask.answer = answer
    focus(world)
    world.drain()
    assert 'Morning came.' not in world.ask.prompts[0]
    blocks = world.store.blocks(world.book['id'], 1, 1)
    writer = world.store.add_character(world.book['id'], 'Writer', 'named')['id']
    world.store.run("UPDATE block SET tag_state = 'tagging' WHERE id = ?", (blocks[0]['id'],))
    tagger.apply(world.store, world.book['id'], {}, {
        'written_passages': [{'first': 1, 'last': 7, 'writer': f'c{writer}'}]}, blocks)
    assert world.chapter()[1]['segments'][0]['character_id'] == writer
    assert world.chapter(1)[1]['tag_state'] == 'pending'
    assert world.chapter(1)[1]['writer_id'] is None


def test_a_writer_can_be_new_without_any_dialogue_and_old_audio_is_invalidated(world):
    from test_pipeline import focus
    focus(world, 0, 1)
    world.drain()
    block = world.chapter()[1]
    before = block['segments'][0]
    assert before['audio_state'] == 'ready'
    assert world.client.post(f"/api/blocks/{block['id']}/retag").status_code == 204
    world.ask.answer = {
        'new_characters': [{'ref': 'n1', 'name': 'Letter writer', 'role': 'named'}], 'lines': [],
        'written_passages': [{'first': 1, 'last': 1, 'writer': 'n1'}]}
    assert world.pipeline.tag_step()
    changed = world.chapter()[1]['segments'][0]
    writer = next(c for c in world.store.characters(world.book['id']) if c['name'] == 'Letter writer')
    assert changed['character_id'] == writer['id']
    assert changed['audio_state'] == 'none' and changed['audio_rev'] > before['audio_rev']


def test_manual_whole_paragraph_voice_survives_a_tagger_answer_in_flight(world):
    from test_pipeline import focus
    writer = world.store.add_character(world.book['id'], 'Letter writer', 'named')
    other = world.store.add_character(world.book['id'], 'Wrong writer', 'named')
    block = world.chapter()[3]
    focus(world)

    def answer(prompt):
        response = world.client.post(f"/api/blocks/{block['id']}/writer", json={'character_id': writer['id']})
        assert response.status_code == 200 and len(response.json()) == len(block['segments'])
        return {'lines': [], 'written_passages': [{'first': 3, 'last': 3, 'writer': f"c{other['id']}"}]}

    world.ask.answer = answer
    assert world.pipeline.tag_step()
    saved = world.chapter()[3]
    assert saved['writer_id'] == writer['id'] and saved['tag_state'] == 'tagged'
    assert all(s['character_id'] == writer['id'] for s in saved['segments'])
    foreign = world.client.post('/api/books', files={'file': ('other.epub', make_epub())}).json()
    foreign_writer = world.store.narrator(foreign['id'])
    assert world.client.post(f"/api/blocks/{block['id']}/writer", json={'character_id': foreign_writer['id']}).status_code == 422


def test_old_books_are_reviewed_once_without_losing_their_existing_audio(world):
    from test_pipeline import focus, answer
    focus(world)
    world.ask.answer = answer
    world.drain()
    before = world.chapter()
    world.store.run('ALTER TABLE block DROP COLUMN writer_id')
    reopened = Store(world.store.home)
    after = reopened.blocks(world.book['id'], 0, 5)
    assert all(b['tag_state'] == 'pending' for b in after if b['tag'] == 'p')
    assert [b['segments'] for b in after] == [b['segments'] for b in before]
    reopened.run("UPDATE block SET tag_state = 'tagged'")
    reopened.db.close()
    reopened = Store(world.store.home)
    assert all(b['tag_state'] == 'tagged' for b in reopened.blocks(world.book['id'], 0, 5))
    reopened.db.close()


def test_a_signature_using_a_unique_first_name_reuses_the_existing_voice(world):
    clara = world.store.add_character(world.book['id'], 'Clara Reed', 'named')
    world.pipeline.set_focus(world.book['id'], 1, 1, 1, 1)
    world.ask.answer = {
        'new_characters': [{'ref': 'n1', 'name': 'Clara', 'role': 'named'}], 'lines': [],
        'written_passages': [{'first': 1, 'last': 1, 'writer': 'n1'}]}
    assert world.pipeline.tag_step()
    assert world.chapter()[1]['writer_id'] == clara['id']
    assert len(world.store.characters(world.book['id'])) == 2
