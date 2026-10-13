import asyncio
import re

import numpy as np
import pytest

from meread import speech
from meread.app import Hub
from meread.tagger import TaggerError

CAST = [{'ref': 'n1', 'name': 'Guard 1', 'role': 'extra', 'gender': 'male', 'age': 'adult', 'pitch': 'low',
         'description': 'bored sentry'},
        {'ref': 'n2', 'name': 'Tom', 'role': 'named', 'gender': 'male', 'age': 'teen', 'pitch': 'medium', 'description': ''},
        {'ref': 'n3', 'name': 'Anna', 'role': 'named', 'gender': 'female', 'age': 'elderly', 'pitch': 'high',
         'description': 'thin, high voice'}]
SPEAKERS = ['n1', 'n2', 'n2', 'n3']


def answer(prompt):
    count = len(re.findall(r'\[\[q\d+\]\]', prompt))
    return {'new_characters': CAST, 'lines': [{'q': i + 1, 'speaker': SPEAKERS[i]} for i in range(count)]}


def focus(w, first=0, last=5, current_first=0, current_last=5):
    body = {'first': first, 'last': last, 'current_first': current_first, 'current_last': current_last}
    assert w.client.post(f"/api/books/{w.book['id']}/focus", json=body).status_code == 204


def speakers(w, chapter=0):
    names = {c['id']: c['name'] for c in w.client.get(f"/api/books/{w.book['id']}").json()['characters']}
    return [(s['text'], names.get(s['character_id'])) for b in w.chapter(chapter) for s in b['segments'] if s['kind'] == 'speech']


def test_imported_book_keeps_its_text_and_leaves_quotes_unattributed(world):
    blocks = world.chapter()
    assert [''.join(s['text'] for s in b['segments']) for b in blocks][:3] == [
        'Chapter One', 'The road was empty when Tom reached the gate.', '“Who goes there?” called a guard from the wall.']
    assert [b['tag_state'] for b in blocks] == ['tagged', 'tagged', 'pending', 'pending', 'tagged', 'pending']
    assert all(s['character_id'] is None for b in blocks for s in b['segments'] if s['kind'] == 'speech')
    detail = world.client.get(f"/api/books/{world.book['id']}").json()
    assert [(c['index'], c['title'], c['first_block'], c['block_count']) for c in detail['chapters']] == [
        (0, 'Chapter One', 0, 6), (1, 'Chapter Two', 6, 3)]
    assert [c['role'] for c in detail['characters']] == ['narrator']


def test_nothing_happens_outside_the_pages_in_focus(world):
    world.ask.answer = answer
    world.drain()
    assert world.ask.prompts == [] and world.engine.spoken == []
    focus(world, 0, 5)
    world.drain()
    assert all(s['audio_state'] == 'ready' for b in world.chapter(0) for s in b['segments'])
    assert all(s['audio_state'] == 'none' for b in world.chapter(1) for s in b['segments'])
    assert [b['tag_state'] for b in world.chapter(1)] == ['tagged', 'tagged', 'pending']


def test_tagging_attributes_lines_and_builds_the_cast(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    assert speakers(world) == [('“Who goes there?”', 'Guard 1'), ('“Only me,”', 'Tom'), ('“Open up.”', 'Tom'),
                               ('“You are late,”', 'Anna')]
    cast = {c['name']: c for c in world.client.get(f"/api/books/{world.book['id']}").json()['characters']}
    assert (cast['Anna']['gender'], cast['Anna']['age'], cast['Anna']['pitch'], cast['Anna']['role']) == (
        'female', 'elderly', 'high', 'named')
    assert cast['Guard 1']['role'] == 'extra' and cast['Tom']['line_count'] == 2
    assert all(c['voice_state'] == 'ready' for c in cast.values())
    ceilings = dict(zip(world.engine.designed, world.engine.ceilings))
    assert ceilings['An elderly woman with a high, bright voice. Thin, high voice.'] is None
    assert ceilings['An adult man with a low, deep voice. Bored sentry.'] == 160
    assert ceilings['A young man of about eighteen with a medium-pitched voice.'] == 210


def test_later_pages_reuse_the_cast_by_ref_and_by_name(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    anna = next(c for c in world.store.characters(world.book['id']) if c['name'] == 'Anna')
    world.ask.answer = {'new_characters': [{**CAST[2], 'ref': 'n9', 'name': 'anna'}], 'lines': [{'q': 1, 'speaker': 'n9'}]}
    focus(world, 6, 8, 6, 8)
    world.drain()
    assert speakers(world, 1) == [('“Tea?”', 'Anna')]
    assert f"c{anna['id']} | Anna" in world.ask.prompts[-1]
    assert '“You are late,”[Anna]' in world.ask.prompts[-1]
    assert len(world.store.characters(world.book['id'])) == 4


def test_a_line_the_tagger_skips_is_read_by_the_narrator_and_flagged(world):
    world.ask.answer = {'new_characters': CAST[:1], 'lines': [{'q': 1, 'speaker': 'n1'}, {'q': 2, 'speaker': 'nobody'}]}
    focus(world)
    world.drain()
    assert speakers(world)[:2] == [('“Who goes there?”', 'Guard 1'), ('“Only me,”', 'Narrator')]
    assert [b['tag_state'] for b in world.chapter()][2:4] == ['tagged', 'failed']
    assert all(s['audio_state'] == 'ready' for b in world.chapter() for s in b['segments'])


def test_when_the_tagger_is_down_the_narrator_reads_everything(world):
    world.ask.error = TaggerError('claude failed: Not logged in')
    focus(world)
    world.drain()
    assert {name for _, name in speakers(world)} == {'Narrator'}
    assert [b['tag_state'] for b in world.chapter()].count('failed') == 3
    assert world.pipeline.idle['tagger'] == 'Tagging failed: claude failed: Not logged in'
    assert len(world.ask.prompts) == 1


def test_clips_start_at_the_current_page_then_go_back(world):
    world.ask.answer = answer
    focus(world, 0, 5, current_first=4, current_last=5)
    world.drain()
    said = [text for text, _, _ in world.engine.spoken]
    assert said[0] == 'The gate creaked. Nobody spoke for a long time.'
    assert said.index('You are late') < said.index('Chapter One')
    assert world.client.get(f"/api/books/{world.book['id']}").json()['position_block'] == 4


def test_changing_the_speaker_rereads_the_line_in_the_new_voice(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    cast = {c['name']: c for c in world.store.characters(world.book['id'])}
    line = next(s for b in world.chapter() for s in b['segments'] if s['text'] == '“Open up.”')
    assert world.client.get(f"/api/segments/{line['id']}/audio").status_code == 200
    changed = world.client.patch(f"/api/segments/{line['id']}", json={'character_id': cast['Anna']['id']}).json()
    assert (changed['audio_state'], changed['audio_rev']) == ('none', line['audio_rev'] + 1)
    assert world.client.get(f"/api/segments/{line['id']}/audio").status_code == 404
    world.drain()
    assert world.engine.spoken[-1][:2] == ('Open up.', f"{cast['Anna']['id']}.wav")
    assert world.client.get(f"/api/segments/{line['id']}/audio").status_code == 200


def test_regenerating_takes_another_reading_with_a_new_seed(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    line = world.chapter()[1]['segments'][0]
    first = next(seed for text, _, seed in world.engine.spoken if text.startswith('The road'))
    world.client.post(f"/api/segments/{line['id']}/regenerate")
    world.drain()
    assert world.engine.spoken[-1][0].startswith('The road') and world.engine.spoken[-1][2] != first


def test_a_clip_made_for_an_outdated_speaker_is_thrown_away(world):
    world.ask.answer = answer
    focus(world, 0, 0, 0, 0)
    title = world.chapter()[0]['segments'][0]
    narrator = world.store.narrator(world.book['id'])
    world.pipeline.speech_step()  # designs the narrator's voice

    def correct_midway():
        world.engine.during_speak = None
        world.client.post(f"/api/segments/{title['id']}/regenerate")
    world.engine.during_speak = correct_midway
    world.pipeline.speech_step()
    after = world.store.segment(title['id'])
    assert (after['audio_state'], after['audio_rev']) == ('none', 1)
    assert not world.store.audio_path(world.book['id'], title['id']).exists()
    world.drain()
    assert world.store.segment(title['id'])['audio_state'] == 'ready' and narrator['role'] == 'narrator'


def test_a_new_voice_is_auditioned_before_it_replaces_the_old_one(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    tom = next(c for c in world.store.characters(world.book['id']) if c['name'] == 'Tom')
    url = f"/api/characters/{tom['id']}"
    edited = world.client.patch(url, json={'pitch': 'low', 'age': 'adult'}).json()
    assert (edited['voice_state'], edited['voice_rev']) == ('ready', tom['voice_rev'])
    spoken = len(world.engine.spoken)

    assert world.client.post(f'{url}/voice/draft').json()['draft_state'] == 'queued'
    world.drain()
    assert world.engine.designed[-1].startswith('An adult man with a low, deep voice.')
    assert world.engine.ceilings[-1] == 160, 'a candidate male voice is held to the male range too'
    voices = {c['name']: world.store.voice_path(c) for c in world.store.characters(world.book['id'])}
    assert sorted(world.engine.others[-1]) == sorted(path for name, path in voices.items() if name != 'Tom'), \
        'a new voice is kept apart from every other voice of the book, not from the one it replaces'
    assert world.client.get(f'{url}/voice/draft').status_code == 200
    candidate = world.store.character(tom['id'])
    assert (candidate['draft_state'], candidate['voice_rev']) == ('ready', tom['voice_rev'])
    assert len(world.engine.spoken) == spoken, 'nothing is re-read while the candidate is only being auditioned'

    assert world.client.post(f'{url}/voice/draft').status_code == 200  # not good enough, try another
    world.drain()
    assert world.client.delete(f'{url}/voice/draft').json()['draft_state'] == 'none'
    assert world.client.get(f'{url}/voice/draft').status_code == 404
    assert world.client.post(f'{url}/voice/draft/approve').status_code == 409

    world.client.post(f'{url}/voice/draft')
    world.drain()
    approved = world.client.post(f'{url}/voice/draft/approve').json()
    assert (approved['voice_state'], approved['draft_state'], approved['voice_rev']) == ('ready', 'none', tom['voice_rev'] + 1)
    world.drain()
    assert [text for text, _, _ in world.engine.spoken[spoken:]] == ['Only me', 'Open up.']
    assert len(world.engine.designed) == 4 + 3, 'approving reuses the auditioned sample instead of designing again'


def test_pausing_the_tagger_stops_calls_and_the_narrator_fills_in(world):
    world.ask.answer = answer
    world.client.put('/api/settings', json={'tagging_paused': True})
    focus(world)
    world.drain()
    assert world.ask.prompts == []
    assert world.client.get('/api/usage').json()['all_time']['calls'] == 0
    blocks = world.chapter()
    assert all(s['audio_state'] == 'ready' for b in blocks for s in b['segments'])
    assert all(s['character_id'] is None for b in blocks for s in b['segments'] if s['kind'] == 'speech')
    assert {voice for _, voice, _ in world.engine.spoken} == {f"{world.store.narrator(world.book['id'])['id']}.wav"}

    world.client.put('/api/settings', json={'tagging_paused': False})
    spoken = len(world.engine.spoken)
    world.drain()
    assert speakers(world)[0] == ('“Who goes there?”', 'Guard 1')
    assert sorted(text for text, _, _ in world.engine.spoken[spoken:]) == ['Only me', 'Open up.', 'Who goes there?', 'You are late']
    usage = world.client.get('/api/usage').json()
    assert usage['today'] == usage['all_time'] == {'calls': 1, 'paragraphs': 3, 'cost_usd': 0.002}


def test_reopening_the_library_keeps_tags_voices_clips_and_position(world, tmp_path):
    from fastapi.testclient import TestClient
    from meread.app import create_app
    world.ask.answer = answer
    focus(world, 0, 5, current_first=3, current_last=5)
    world.drain()
    before = (world.chapter(), world.store.characters(world.book['id']))
    calls, spoken, designed = len(world.ask.prompts), len(world.engine.spoken), len(world.engine.designed)

    reopened = create_app(tmp_path, engine=world.engine, ask=world.ask, run_workers=False)
    with TestClient(reopened) as client:
        book = client.get(f"/api/books/{world.book['id']}").json()
        assert book['position_block'] == 3
        assert (client.get(f"/api/books/{world.book['id']}/chapters/0").json(), book['characters']) == before
        line = before[0][0]['segments'][0]
        assert client.get(f"/api/segments/{line['id']}/audio").status_code == 200
        client.post(f"/api/books/{world.book['id']}/focus", json={'first': 0, 'last': 5, 'current_first': 3, 'current_last': 5})
        pipeline = reopened.state.pipeline
        while pipeline.tag_step() or pipeline.speech_step():
            pass
    assert (len(world.ask.prompts), len(world.engine.spoken), len(world.engine.designed)) == (calls, spoken, designed)


def test_merging_moves_lines_and_removes_the_duplicate(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    cast = {c['name']: c for c in world.store.characters(world.book['id'])}
    assert world.client.post(f"/api/characters/{cast['Guard 1']['id']}/merge", json={'into': cast['Tom']['id']}).status_code == 204
    assert speakers(world)[0] == ('“Who goes there?”', 'Tom')
    assert 'Guard 1' not in {c['name'] for c in world.store.characters(world.book['id'])}
    narrator = world.store.narrator(world.book['id'])
    assert world.client.post(f"/api/characters/{narrator['id']}/merge", json={'into': cast['Tom']['id']}).status_code == 422


def test_retagging_a_paragraph_asks_again(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    block = world.chapter()[2]
    world.ask.answer = lambda prompt: {'new_characters': [], 'lines': [{'q': 1, 'speaker': 'narrator'}]}
    assert world.client.post(f"/api/blocks/{block['id']}/retag").status_code == 204
    assert world.chapter()[2]['tag_state'] == 'pending'
    world.drain()
    assert speakers(world)[0] == ('“Who goes there?”', 'Narrator')
    assert world.ask.prompts[-1].count('[[q') == 1


def test_retagging_a_page_asks_again_about_its_spoken_paragraphs(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    before = speakers(world)
    world.ask.answer = lambda prompt: {
        'new_characters': [], 'lines': [{'q': q + 1, 'speaker': 'narrator'} for q in range(prompt.count('[[q'))]}
    retag = world.client.post(f"/api/books/{world.book['id']}/retag", json={'first': 2, 'last': 4})
    assert retag.status_code == 200 and retag.json() == {'retagged': 2}
    assert [b['tag_state'] for b in world.chapter()][2:] == ['pending', 'pending', 'tagged', 'tagged']
    assert all(s['audio_state'] == 'none' for b in world.chapter()[2:4] for s in b['segments'] if s['kind'] == 'speech')
    world.drain()
    after = speakers(world)
    assert {name for _, name in after[:-1]} == {'Narrator'}
    assert after[-1] == before[-1] and after[-1][1] != 'Narrator'
    assert world.client.post(f"/api/books/{world.book['id']}/retag", json={'first': 0, 'last': 1}).json() == {'retagged': 0}
    assert world.client.post(f"/api/books/{world.book['id']}/retag", json={'first': 4, 'last': 2}).status_code == 422


def test_bad_uploads_are_refused(world):
    assert world.client.post('/api/books', files={'file': ('x.epub', b'not a zip', 'application/epub+zip')}).status_code == 400
    narrator = world.store.narrator(world.book['id'])
    reply = world.client.post(f"/api/characters/{narrator['id']}/voice", files={'file': ('v.wav', b'nope', 'audio/wav')})
    assert reply.status_code == 400


def test_deleting_a_book_removes_it_and_its_files(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    folder = world.store.book_dir(world.book['id'])
    assert folder.exists()
    assert world.client.delete(f"/api/books/{world.book['id']}").status_code == 204
    assert world.client.get('/api/books').json() == [] and not folder.exists()
    assert world.pipeline.focus is None


def test_a_spanish_book_is_tagged_and_spoken_in_spanish(world):
    from conftest import SPANISH, make_epub
    book = world.client.post('/api/books', files={'file': ('es.epub', make_epub((SPANISH,)), 'application/epub+zip')}).json()
    assert book['language'] == 'es'
    blocks = world.client.get(f"/api/books/{book['id']}/chapters/0").json()
    assert [(s['kind'], s['text']) for s in blocks[2]['segments']] == [
        ('speech', '—¿Quién anda ahí? '), ('narration', '—gritó un guardia desde lo alto—.'), ('speech', ' ¡Conteste!')]
    world.ask.answer = lambda prompt: {
        'new_characters': [{'ref': 'n1', 'name': 'Guardia 1', 'role': 'extra', 'gender': 'male', 'age': 'adult',
                            'pitch': 'low', 'description': 'gruff sentry'}],
        'lines': [{'q': i + 1, 'speaker': 'n1'} for i in range(prompt.count('[[q'))]}
    world.engine.languages.clear()
    world.client.post(f"/api/books/{book['id']}/focus", json={'first': 0, 'last': 5, 'current_first': 0, 'current_last': 5})
    world.drain()
    assert '(language: Spanish)' in world.ask.prompts[-1] and '[[q1]]—¿Quién anda ahí?' in world.ask.prompts[-1]
    assert world.engine.languages == {'es'}
    said = [text for text, _, _ in world.engine.spoken]
    assert '¿Quién anda ahí?' in said and 'gritó un guardia desde lo alto.' in said and 'Vuelve antes de que anochezca.' in said


def test_the_line_at_the_top_of_the_page_is_where_the_book_opens_again(world):
    body = {'first': 0, 'last': 5, 'current_first': 2, 'current_last': 5, 'position': {'block': 3, 'seq': 1}}
    assert world.client.post(f"/api/books/{world.book['id']}/focus", json=body).status_code == 204
    book = world.client.get(f"/api/books/{world.book['id']}").json()
    assert (book['position_block'], book['position_seq']) == (3, 1)


def test_a_sample_that_passes_for_an_existing_voice_is_shifted_by_as_little_as_it_takes():
    alike = {0: 0.91, -2: 0.88, 2: 0.80, -3: 0.60, 3: 0.70}
    asked = []
    assert speech.most_distinct(speech.SHIFTS, lambda shift: asked.append(shift) or alike[shift]) == 2
    assert asked == [0, -2, 2], 'larger shifts are not tried once a small one is enough'
    assert speech.most_distinct(speech.SHIFTS, lambda shift: 0.5) == 0, 'a voice that is already its own is left alone'
    assert speech.most_distinct(speech.SHIFTS, {0: 0.95, -2: 0.93, 2: 0.94, -3: 0.90, 3: 0.92}.get) == -3


def test_shifting_a_sample_down_plays_it_slower_and_so_lower():
    rate = 24000
    tone = np.sin(2 * np.pi * 220 * np.arange(rate) / rate).astype('float32')
    lower = speech.shifted(tone, -12)
    assert abs(len(lower) - 2 * rate) <= 2
    crossings = np.count_nonzero(np.diff(np.signbit(lower)))
    assert abs(crossings / 2 / (len(lower) / rate) - 110) < 1
    assert speech.shifted(tone, 0) is tone


def test_closing_the_hub_ends_open_event_streams_and_refuses_new_ones():
    async def scenario():
        hub = Hub()
        hub.loop = asyncio.get_running_loop()
        stream = hub.stream(1, {'type': 'activity'})
        first = await anext(stream)
        hub.close()
        rest = [chunk async for chunk in stream]
        late = [chunk async for chunk in hub.stream(1, {'type': 'activity'})]
        return first, rest, late
    first, rest, late = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert first.startswith('data: ') and rest == [] and late == []


@pytest.mark.parametrize('provider', ['fish', 'breeze'])
def test_provider_switch_preserves_cast_candidates_tags_and_exact_position(world, tmp_path, provider):
    from fastapi.testclient import TestClient
    from meread.app import create_app

    world.ask.answer = answer
    focus(world)
    world.drain()
    narrator = world.store.narrator(world.book['id'])
    world.client.post(f"/api/characters/{narrator['id']}/voice/draft")
    world.drain()
    world.store.run('UPDATE book SET position_block = 3, position_seq = 1 WHERE id = ?', (world.book['id'],))
    cast = world.store.characters(world.book['id'])
    samples = {path: path.read_bytes() for c in cast for path in (world.store.voice_path(c), world.store.draft_path(c))
               if path.exists()}
    tags = world.store.all('SELECT id, tag_state FROM block')
    clips = world.store.all('SELECT id, audio_rev FROM segment')
    calls, designs = len(world.ask.prompts), len(world.engine.designed)

    reopened = create_app(tmp_path, engine=world.engine, ask=world.ask, tts_provider=provider, run_workers=False)
    with TestClient(reopened) as client:
        store = reopened.state.store
        assert store.characters(world.book['id']) == cast
        assert store.all('SELECT id, tag_state FROM block') == tags
        assert {path: path.read_bytes() for path in samples} == samples
        assert store.book(world.book['id'])['position_block'] == 3
        assert store.book(world.book['id'])['position_seq'] == 1
        for clip in clips:
            after = store.segment(clip['id'])
            assert (after['audio_state'], after['audio_rev'], after['duration'], after['error']) == (
                'none', clip['audio_rev'] + 1, None, None)
            assert client.get(f"/api/segments/{clip['id']}/audio").status_code == 404
        reopened.state.pipeline.set_focus(world.book['id'], 0, 5, 3, 5, (3, 1))
        while reopened.state.pipeline.tag_step() or reopened.state.pipeline.speech_step():
            pass
        assert all(s['audio_state'] == 'ready' for b in store.chapter_blocks(world.book['id'], 0) for s in b['segments'])
    assert (len(world.ask.prompts), len(world.engine.designed)) == (calls, designs)

    again = create_app(tmp_path, engine=world.engine, tts_provider=provider, run_workers=False)
    assert again.state.store.all('SELECT id, audio_state, audio_rev, duration FROM segment') == (
        reopened.state.store.all('SELECT id, audio_state, audio_rev, duration FROM segment'))
    returned = create_app(tmp_path, engine=world.engine, run_workers=False)
    assert all(s['audio_state'] == 'none' and s['audio_rev'] == clip['audio_rev'] + 2
               for clip in clips for s in [returned.state.store.segment(clip['id'])])
    assert {path: path.read_bytes() for path in samples} == samples


@pytest.mark.parametrize('provider, bump', [('chatterbox', 0), ('fish', 1)])
def test_legacy_library_without_identity_is_chatterbox(world, tmp_path, provider, bump):
    from meread.app import create_app

    focus(world, 0, 0, 0, 0)
    world.drain()
    clip = world.chapter()[0]['segments'][0]
    world.store.run('DELETE FROM generation_identity')
    reopened = create_app(tmp_path, engine=world.engine, tts_provider=provider, run_workers=False)
    after = reopened.state.store.segment(clip['id'])
    assert after['audio_rev'] == clip['audio_rev'] + bump
    assert after['audio_state'] == ('none' if bump else 'ready')
    assert reopened.state.store.one('SELECT COUNT(*) AS count FROM generation_identity')['count'] == 1


def test_identity_change_and_audio_invalidation_roll_back_together(world, monkeypatch):
    focus(world, 0, 0, 0, 0)
    world.drain()
    clip = world.chapter()[0]['segments'][0]
    before = world.store.one('SELECT identity FROM generation_identity')
    invalidate = world.store.invalidate_audio

    def failed(where, args=()):
        invalidate(where, args)
        raise RuntimeError('interrupted migration')
    monkeypatch.setattr(world.store, 'invalidate_audio', failed)
    with pytest.raises(RuntimeError, match='interrupted migration'):
        world.store.reconcile_generation(speech.PROVIDERS['fish'].identity, speech.PROVIDERS['chatterbox'].identity)
    assert world.store.segment(clip['id']) == clip
    assert world.store.one('SELECT identity FROM generation_identity') == before
    assert not world.store.db.in_transaction


@pytest.mark.parametrize('change', ['policy', 'checkpoint'])
def test_policy_or_checkpoint_change_invalidates_ready_and_failed_audio(world, change):
    import copy

    focus(world, 0, 0, 0, 0)
    world.drain()
    clips = world.store.all('SELECT id, audio_rev FROM segment')
    failed = clips[-1]['id']
    world.store.run("UPDATE segment SET audio_state = 'failed', duration = 12, error = 'old failure' WHERE id = ?", (failed,))
    identity = copy.deepcopy(speech.PROVIDERS['chatterbox'].identity)
    if change == 'policy':
        identity['policy'] += 1
    else:
        identity['models'][0]['revision'] = 'new-fixed-checkpoint'
    assert world.store.reconcile_generation(identity, speech.PROVIDERS['chatterbox'].identity)
    for clip in clips:
        after = world.store.segment(clip['id'])
        assert (after['audio_state'], after['audio_rev'], after['duration'], after['error']) == (
            'none', clip['audio_rev'] + 1, None, None)
    assert not world.store.reconcile_generation(identity, speech.PROVIDERS['chatterbox'].identity)


def test_switching_to_breeze_gguf_and_back_resets_narration_and_keeps_everything_else(world):
    book_id = world.book['id']
    world.ask.answer = {'new_characters': [], 'lines': []}
    focus(world, 0, 2, 1, 2)
    world.drain()
    narrator = world.store.narrator(book_id)
    assert world.client.post(f"/api/characters/{narrator['id']}/voice/draft").status_code == 200
    world.drain()
    before = {'cast': world.store.characters(book_id), 'tags': world.store.all('SELECT id, tag_state FROM block ORDER BY id'),
              'book': world.store.book(book_id), 'draft': world.store.draft_path(narrator).read_bytes()}
    assert before['book']['position_block'] == 1 and world.store.all('SELECT id FROM segment WHERE audio_state = ?', ('ready',))
    assert speech.PROVIDERS['breeze-gguf'].identity == {
        'provider': 'breeze-gguf', 'policy': 1, 'transcription_policy': speech.TRANSCRIPTION_POLICY, 'spoken_disclaimer': False,
        'models': [{'id': 'cstr/breeze-tts-2-GGUF@q4_k'}, {'id': speech.DESIGNER},
                   {'id': speech.TRANSCRIBER, 'revision': speech.TRANSCRIBER_REVISION}]}
    assert world.store.reconcile_generation(speech.PROVIDERS['breeze-gguf'].identity, speech.PROVIDERS['chatterbox'].identity)
    assert world.store.all('SELECT id FROM segment WHERE audio_state != ?', ('none',)) == []
    assert world.store.all('SELECT id, tag_state FROM block ORDER BY id') == before['tags']
    assert world.store.characters(book_id) == before['cast']
    assert world.store.book(book_id) == before['book']
    assert world.store.draft_path(narrator).read_bytes() == before['draft']
    assert not world.store.reconcile_generation(speech.PROVIDERS['breeze-gguf'].identity, speech.PROVIDERS['chatterbox'].identity)
    assert world.store.reconcile_generation(speech.PROVIDERS['chatterbox'].identity, speech.PROVIDERS['breeze-gguf'].identity)
    assert world.store.all('SELECT id FROM segment WHERE audio_state != ?', ('none',)) == []
    assert world.store.characters(book_id) == before['cast']


def test_provider_change_discards_a_clip_that_was_already_generating(world):
    focus(world, 0, 0, 0, 0)
    title = world.chapter()[0]['segments'][0]
    world.pipeline.speech_step()
    world.engine.during_speak = lambda: world.store.reconcile_generation(
        speech.PROVIDERS['fish'].identity, speech.PROVIDERS['chatterbox'].identity)
    world.pipeline.speech_step()
    after = world.store.segment(title['id'])
    assert (after['audio_state'], after['audio_rev']) == ('none', title['audio_rev'] + 1)
    assert not world.store.audio_path(world.book['id'], title['id']).exists()
