import pytest

from conftest import make_epub
from test_pipeline import answer, focus


def saved_voice(world, name='Professor Reed'):
    book = world.client.post('/api/books', files={'file': ('other.epub', make_epub(), 'application/epub+zip')}).json()
    world.store.run('UPDATE book SET title = ? WHERE id = ?', ('The Other Adventure', book['id']))
    character = world.store.add_character(book['id'], name, description='A resonant, gentle voice.')
    world.pipeline.design_voice(character)
    return world.store.character(character['id'])


def reuse(world, target, source):
    return world.client.post(f"/api/characters/{target['id']}/voice/reuse",
                             json={'source_id': source['id'], 'source_rev': source['voice_rev']})


def test_lists_ready_voices_from_every_book_with_their_origin(world):
    assert world.client.get('/api/voices').json() == []
    source = saved_voice(world)
    same_book = world.store.add_character(world.book['id'], 'Owen')
    world.pipeline.design_voice(same_book)
    missing = saved_voice(world, 'Missing sample')
    world.store.voice_path(missing).unlink()
    world.store.add_character(world.book['id'], 'Not designed yet')
    voices = world.client.get('/api/voices').json()
    assert {v['id'] for v in voices} == {source['id'], same_book['id']}
    voice = next(v for v in voices if v['id'] == source['id'])
    assert (voice['name'], voice['book_title'], voice['book_author'], voice['language']) == (
        'Professor Reed', 'The Other Adventure', 'A. Writer', 'en')
    assert voice['voice_rev'] == source['voice_rev']
    assert world.client.get(f"/api/characters/{voice['id']}/voice").status_code == 200


def test_reuse_copies_the_voice_and_rereads_only_the_destination_character(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    target = next(c for c in world.store.characters(world.book['id']) if c['name'] == 'Tom')
    source = saved_voice(world)
    sample = world.store.voice_path(source).read_bytes()
    source_before = world.store.character(source['id'])
    designs, spoken = len(world.engine.designed), len(world.engine.spoken)
    before = {s['id']: s for b in world.chapter() for s in b['segments']}
    reply = reuse(world, target, source)
    assert reply.status_code == 200
    changed = reply.json()
    assert (changed['voice_state'], changed['voice_rev']) == ('ready', target['voice_rev'] + 1)
    assert (changed['name'], changed['description'], changed['book_id']) == (target['name'], target['description'], target['book_id'])
    assert world.store.voice_path(changed).read_bytes() == sample
    assert world.store.character(source['id']) == source_before
    for segment in (s for b in world.chapter() for s in b['segments']):
        old = before[segment['id']]
        if segment['character_id'] == target['id']:
            assert segment['audio_state'] == 'none'
            assert segment['audio_rev'] == old['audio_rev'] + 1
        else:
            assert segment == old
    world.drain()
    assert len(world.engine.designed) == designs
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me', 'Open up.']

    # Copies stay usable after the source voice changes and its book is removed.
    world.store.voice_path(source).write_bytes(b'replaced source')
    assert world.store.voice_path(changed).read_bytes() == sample
    assert world.client.delete(f"/api/books/{source['book_id']}").status_code == 204
    assert world.client.get(f"/api/characters/{target['id']}/voice").content == sample


def test_reuse_also_accepts_another_character_in_the_same_book(world):
    source = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(source)
    source = world.store.character(source['id'])
    target = world.store.add_character(world.book['id'], 'Owen')
    assert reuse(world, target, source).status_code == 200


@pytest.mark.parametrize('unavailable', ['changed', 'missing_file', 'deleted', 'not_ready'])
def test_stale_or_unavailable_sources_leave_the_current_voice_untouched(world, unavailable):
    source = saved_voice(world)
    target = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(target)
    target = world.store.character(target['id'])
    sample = world.store.voice_path(target).read_bytes()
    if unavailable == 'changed':
        world.store.run('UPDATE character SET voice_rev = voice_rev + 1 WHERE id = ?', (source['id'],))
    elif unavailable == 'missing_file':
        world.store.voice_path(source).unlink()
    elif unavailable == 'deleted':
        world.client.delete(f"/api/books/{source['book_id']}")
    else:
        world.store.run("UPDATE character SET voice_state = 'designing' WHERE id = ?", (source['id'],))
    assert reuse(world, target, source).status_code == 409
    assert world.store.character(target['id']) == target
    assert world.store.voice_path(target).read_bytes() == sample


def test_reuse_rejects_self_and_unknown_destination(world):
    source = saved_voice(world)
    assert reuse(world, source, source).status_code == 422
    assert reuse(world, {'id': 99999}, source).status_code == 404


def test_reuse_cancels_a_candidate_that_is_already_being_designed(world):
    source = saved_voice(world)
    target = world.store.narrator(world.book['id'])
    world.store.request_draft(target['id'])
    candidate = world.store.character(target['id'])
    world.engine.during_design = lambda: reuse(world, target, source).raise_for_status()
    world.pipeline.design_draft(candidate)
    changed = world.store.character(target['id'])
    assert changed['draft_state'] == 'none'
    assert changed['draft_rev'] > candidate['draft_rev']
    assert changed['draft_status'] is None
    assert not world.store.draft_path(changed).exists()
    assert world.store.voice_path(changed).read_bytes() == world.store.voice_path(source).read_bytes()


def test_reuse_cancels_an_emotion_take_in_flight(world):
    source = saved_voice(world)
    target = world.store.narrator(world.book['id'])
    world.pipeline.design_voice(target)
    target = world.store.character(target['id'])
    world.store.request_take(target['id'], 'happy')
    take = world.store.take(target['id'], 'happy')
    direct = world.engine.direct

    def replace_during_take(*args, **kwargs):
        reuse(world, target, source).raise_for_status()
        return direct(*args, **kwargs)

    world.engine.direct = replace_during_take
    world.pipeline.make_take(target, take)
    changed_take = world.store.take(target['id'], 'happy')
    assert changed_take['state'] == 'none'
    assert changed_take['rev'] > take['rev']
    assert not world.store.take_path(target, 'happy').exists()
