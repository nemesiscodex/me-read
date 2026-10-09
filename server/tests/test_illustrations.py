import base64
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from meread import images, scenes
from meread.store import Store
from meread.tagger import TaggerError
from test_pipeline import answer, focus

URL = 'http://gpu.test:8000'
EMPTY = {'moments': [], 'ends': [], 'appearances': []}


def ref(prompt, name):
    return re.search(rf'^(c\d+) \| {name} \|', prompt, re.M).group(1)


def at_the_gate(prompt):
    """Tom arrives at the gate in the third paragraph of the scanned text; the narrator is wrongly listed too."""
    tom = ref(prompt, 'Tom')
    return {'moments': [{'p': 3, 'scene': 'Tom stands before a tall wooden gate at dusk', 'characters': [tom, 'narrator', 'c999']}],
            'ends': [], 'appearances': [{'character': tom, 'appearance': 'lanky boy in a patched grey cloak'},
                                        {'character': 'c999', 'appearance': 'nobody'}]}


def everywhere(prompt):
    """A detector that ignores the rules and wants a picture of every paragraph."""
    return {**EMPTY, 'moments': [{'p': n + 1, 'scene': f'picture {n + 1}', 'characters': []} for n in range(prompt.count('[[p'))]}


def illustrated(w, **settings):
    assert w.client.put('/api/settings', json={'image_url': URL, **settings}).status_code == 200
    w.ask.answer = answer


def moments(w, chapter=0):
    return w.client.get(f"/api/books/{w.book['id']}/chapters/{chapter}/moments").json()


def cast(w):
    return {c['name']: c for c in w.store.characters(w.book['id'])}


def test_a_moment_and_the_looks_of_its_people_are_stored_and_announced(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    tom = cast(world)['Tom']
    [moment] = moments(world)
    assert (moment['block_idx'], moment['kind'], moment['scene'], moment['character_ids']) == (
        2, 'picture', 'Tom stands before a tall wooden gate at dusk', [tom['id']])
    assert tom['appearance'] == 'lanky boy in a patched grey cloak'
    assert any(e['type'] == 'moments' and e['moments'][0]['id'] == moment['id'] for e in world.events)
    assert any(e['type'] == 'characters' and any(c['appearance'] for c in e['characters']) for e in world.events)
    prompt = world.ask.scene_prompts[0]
    assert 'LAST ILLUSTRATION:\n(none)\n\nEARLIER' in prompt
    assert '[[p1]]Chapter One' in prompt
    assert f"c{tom['id']} | Tom | named | male teen | (not described yet)" in prompt and 'Narrator' not in prompt
    assert world.client.get('/api/usage').json()['all_time']['calls'] == 2


def test_the_detector_knows_the_last_picture_and_keeps_a_described_look(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    tom = cast(world)['Tom']
    world.ask.scenes = lambda prompt: {**EMPTY, 'appearances': [{'character': ref(prompt, 'Tom'), 'appearance': 'someone else'}]}
    focus(world, 6, 8, 6, 8)
    world.drain()
    assert 'LAST ILLUSTRATION:\nTom stands before a tall wooden gate at dusk (4 paragraphs back)' in world.ask.scene_prompts[-1]
    assert f"c{tom['id']} | Tom | named | male teen | lanky boy in a patched grey cloak" in world.ask.scene_prompts[-1]
    assert cast(world)['Tom']['appearance'] == 'lanky boy in a patched grey cloak'
    assert len(world.ask.scene_prompts) == 2, 'a paragraph is scanned once'


@pytest.mark.parametrize('settings', [{'image_url': ''}, {'tagging_paused': True}, {'illustrations': False}])
def test_nothing_is_detected_or_drawn_while_illustrations_are_off_or_calls_are_paused(world, settings):
    illustrated(world, **settings)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    assert world.ask.scene_prompts == [] and world.draw.drawn == [] and moments(world) == []


def test_switching_illustrations_off_stops_drawing_what_was_already_found(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    while world.pipeline.tag_step() or world.pipeline.scene_step():
        pass
    world.client.put('/api/settings', json={'illustrations': False})
    world.drain()
    assert world.draw.drawn == [] and moments(world)[0]['image_state'] == 'none'
    world.client.put('/api/settings', json={'illustrations': True})
    world.drain()
    assert moments(world)[0]['image_state'] == 'ready'


def test_a_portrait_is_drawn_first_and_becomes_the_reference_of_the_scene(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    tom, narrator = cast(world)['Tom'], cast(world)['Narrator']
    portrait, scene = world.draw.drawn
    assert (portrait['size'], portrait['references'], portrait['seed'], portrait['url']) == ((512, 512), [], tom['id'] * 1000, URL)
    assert portrait['prompt'] == ('Close portrait of a teenage boy, head and shoulders only, the face large in the middle of the '
                                  'picture: lanky boy in a patched grey cloak. Nothing below the chest is shown. '
                                  'Plain background.' + images.STYLE)
    drawn = world.client.get(f"/api/characters/{tom['id']}/portrait")
    assert drawn.status_code == 200 and drawn.headers['content-type'] == 'image/png'
    assert (scene['size'], scene['references']) == (images.SCENE_SIZE, [drawn.content])
    assert scene['prompt'] == 'Tom stands before a tall wooden gate at dusk. Tom is the person in image 1.' + images.STYLE
    [moment] = moments(world)
    assert moment['image_state'] == 'ready' and world.client.get(f"/api/moments/{moment['id']}/image").status_code == 200
    assert narrator['portrait_state'] == 'none' and world.client.get(f"/api/characters/{narrator['id']}/portrait").status_code == 404


def test_the_narrator_is_never_drawn_even_with_an_appearance(world):
    illustrated(world)
    focus(world)
    narrator = world.store.narrator(world.book['id'])
    world.client.patch(f"/api/characters/{narrator['id']}", json={'appearance': 'a voice in the dark'})
    world.drain()
    assert world.draw.drawn == []
    assert world.client.post(f"/api/characters/{narrator['id']}/portrait/redraw").status_code == 422


def test_a_portrait_the_service_refuses_is_marked_and_the_scene_describes_the_person_in_words(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.draw.error = images.ImageError('422: prompt too long')
    world.pipeline.tag_step(), world.pipeline.scene_step()
    assert world.pipeline.image_step()
    tom = cast(world)['Tom']
    assert (tom['portrait_state'], tom['portrait_error']) == ('failed', '422: prompt too long')
    world.draw.error = None
    world.drain()
    [scene] = world.draw.drawn
    assert scene['references'] == [] and scene['prompt'].endswith('at dusk. Tom: lanky boy in a patched grey cloak.' + images.STYLE)


def test_only_the_current_and_next_page_are_scanned_and_two_moments_a_page_are_drawn(world):
    illustrated(world)
    world.ask.answer = lambda prompt: {'new_characters': [], 'lines': [{'q': n + 1, 'speaker': 'narrator'} for n in range(prompt.count('[[q'))]}
    world.ask.scenes = everywhere
    focus(world, 0, 8, current_first=2, current_last=4)
    world.drain()
    [prompt] = world.ask.scene_prompts
    assert prompt.count('[[p') == 7 and 'TEXT TO SCAN:\n[[p1]]“Who goes there?”' in prompt
    assert 'for context only:\nChapter One\nThe road was empty' in prompt
    found = moments(world) + moments(world, 1)
    assert [(m['block_idx'], m['image_state']) for m in found] == [
        (2, 'ready'), (3, 'ready'), (4, 'none'), (5, 'ready'), (6, 'ready'), (7, 'none'), (8, 'none')], 'every moment is kept'
    assert [d['prompt'] for d in world.draw.drawn] == [f'picture {n}.' + images.STYLE for n in (1, 2, 4, 5)]

    focus(world, 0, 8, current_first=5, current_last=8)
    world.drain()
    assert len(world.ask.scene_prompts) == 1, 'the page that is now behind is not scanned'
    assert [m['image_state'] for m in moments(world)] == ['ready', 'ready', 'none', 'ready']


@pytest.mark.parametrize('limit, drawn', [(1, [5]), (3, [3, 5, 7])])
def test_of_the_moments_on_a_page_the_most_important_are_drawn_in_reading_order(world, limit, drawn):
    illustrated(world, pictures_per_page=limit)
    world.ask.answer = lambda prompt: {'new_characters': [], 'lines': [{'q': n + 1, 'speaker': 'narrator'} for n in range(prompt.count('[[q'))]}
    weight = {2: 4, 3: 8, 4: 1, 5: 10, 6: 2, 7: 8, 8: 3}
    world.ask.scenes = lambda prompt: {**EMPTY, 'moments': [
        {'p': n, 'title': f'Plate {n}', 'summary': f'what happens in {n}', 'scene': f'picture {n}', 'importance': weight[n + 1],
         'characters': []} for n in range(1, 8)]}
    focus(world, 0, 8, current_first=2, current_last=8)
    world.drain()
    found = moments(world) + moments(world, 1)
    assert [(m['block_idx'], m['title'], m['summary'], m['importance']) for m in found] == [
        (idx, f'Plate {idx - 1}', f'what happens in {idx - 1}', weight[idx]) for idx in range(2, 9)]
    assert [m['block_idx'] for m in found if m['image_state'] == 'ready'] == drawn
    assert [d['prompt'] for d in world.draw.drawn] == [f'picture {idx - 1}.' + images.STYLE for idx in drawn]


def test_a_library_from_before_importance_is_scanned_again_and_old_moments_take_the_new_words(world, tmp_path):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    [old] = moments(world)
    assert (old['title'], old['importance'], old['image_state']) == ('', 5, 'ready')
    asked = len(world.ask.scene_prompts)
    for column in 'title', 'summary', 'importance':
        world.store.run(f'ALTER TABLE moment DROP COLUMN {column}')
    upgraded = Store(world.store.home)
    assert upgraded.one("SELECT COUNT(*) AS n FROM block WHERE scene_state = 'done'")['n'] == 0
    world.ask.scenes = lambda prompt: {**EMPTY, 'moments': [
        {'p': 3, 'title': 'At the gate', 'summary': 'Tom reaches the gate.', 'scene': 'Dusk, outdoors. One person.',
         'importance': 9, 'characters': []},
        {'p': 5, 'title': 'The long wait', 'summary': 'Nobody speaks.', 'scene': 'Night, outdoors.', 'importance': 3, 'characters': []}]}
    focus(world)
    world.drain()
    assert len(world.ask.scene_prompts) > asked
    first, second = moments(world)
    assert (first['id'], first['title'], first['scene'], first['importance'], first['image_state'], first['character_ids']) == (
        old['id'], 'At the gate', 'Dusk, outdoors. One person.', 9, 'ready', old['character_ids']), 'the picture is kept'
    assert (second['block_idx'], second['title']) == (4, 'The long wait')


def test_the_end_of_a_picture_is_a_moment_that_is_never_drawn_and_never_counted(world):
    illustrated(world)
    world.ask.scenes = lambda prompt: {**EMPTY, 'ends': [2, 3], 'moments': [
        {'p': n, 'scene': f'picture {n}', 'characters': []} for n in (1, 3, 4)]}
    focus(world, 0, 5, 0, 5)
    world.drain()
    found = moments(world)
    assert [(m['block_idx'], m['kind'], m['scene'], m['image_state']) for m in found] == [
        (0, 'picture', 'picture 1', 'ready'), (1, 'clear', '', 'none'), (2, 'picture', 'picture 3', 'ready'),
        (3, 'picture', 'picture 4', 'none')]
    assert len(world.draw.drawn) == 2
    clear = found[1]
    assert world.client.post(f"/api/moments/{clear['id']}/redraw").status_code == 404
    assert world.client.get(f"/api/moments/{clear['id']}/image").status_code == 404

    world.store.run('DELETE FROM moment WHERE block_idx > 1')
    world.ask.scenes = EMPTY
    focus(world, 6, 8, 6, 8)
    world.drain()
    assert 'LAST ILLUSTRATION:\n(none) The picture before it stopped matching the text 5 paragraphs back.' in world.ask.scene_prompts[-1]


def test_an_image_service_that_cannot_be_reached_fails_nothing(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.draw.error = images.Unavailable('cannot be reached')
    world.drain()
    assert world.pipeline.image_step() is False, 'it waits before trying again'
    assert cast(world)['Tom']['portrait_state'] == 'none' and moments(world)[0]['image_state'] == 'none'
    assert world.pipeline.idle['images'] == 'The image service cannot be reached; trying again shortly'
    world.draw.error = None
    world.pipeline.retry_at.clear()
    world.drain()
    assert cast(world)['Tom']['portrait_state'] == 'ready' and moments(world)[0]['image_state'] == 'ready'
    assert world.pipeline.idle['images'] is None


def test_a_failed_scene_call_is_asked_again_later_not_at_once(world):
    illustrated(world)
    world.ask.scene_error = TaggerError('claude failed: overloaded')
    focus(world)
    world.drain()
    world.drain()
    assert len(world.ask.scene_prompts) == 1
    assert world.pipeline.idle['scenes'] == 'Looking for moments to draw failed: claude failed: overloaded'
    world.ask.scene_error, world.ask.scenes = None, at_the_gate
    world.pipeline.retry_at.clear()
    world.drain()
    assert len(moments(world)) == 1 and world.pipeline.idle['scenes'] is None


def test_drawing_a_moment_again_takes_a_new_seed(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    [moment] = moments(world)
    again = world.client.post(f"/api/moments/{moment['id']}/redraw").json()
    assert (again['image_state'], again['image_rev']) == ('none', moment['image_rev'] + 1)
    assert world.client.get(f"/api/moments/{moment['id']}/image").status_code == 404
    world.drain()
    assert world.draw.drawn[-1]['seed'] == moment['id'] * 1000 + 1 and len(world.draw.drawn) == 3
    assert moments(world)[0]['image_state'] == 'ready'


def test_the_picture_carried_onto_a_later_page_can_be_drawn_again_but_nothing_else_behind_the_reader(world):
    illustrated(world)
    world.ask.answer = lambda prompt: {'new_characters': [], 'lines': [{'q': n + 1, 'speaker': 'narrator'} for n in range(prompt.count('[[q'))]}
    world.ask.scenes = lambda prompt: {**EMPTY, 'moments': [{'p': n, 'scene': f'picture {n}', 'characters': []} for n in (1, 2, 3, 4)]}
    focus(world, 0, 5, current_first=0, current_last=3)
    world.drain()
    assert [m['image_state'] for m in moments(world)] == ['ready', 'ready', 'none', 'none']
    world.ask.scenes = EMPTY
    focus(world, 0, 5, current_first=4, current_last=5)
    world.drain()
    assert len(world.draw.drawn) == 2, 'a picture the earlier page had no room for is not drawn from the next page'
    first, second = moments(world)[:2]
    world.client.post(f"/api/moments/{first['id']}/redraw")
    world.drain()
    assert len(world.draw.drawn) == 2, 'an older picture is not on screen'
    world.client.post(f"/api/moments/{second['id']}/redraw")
    world.drain()
    assert world.draw.drawn[-1]['seed'] == second['id'] * 1000 + 1
    assert [m['image_state'] for m in moments(world)] == ['none', 'ready', 'none', 'none']


def test_a_picture_not_drawn_before_its_page_was_turned_is_drawn_from_the_next_page_of_the_chapter(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    world.draw.error = images.Unavailable('cannot be reached')
    focus(world, 0, 5, current_first=0, current_last=2)
    world.drain()
    assert [(m['block_idx'], m['image_state']) for m in moments(world)] == [(2, 'none')]
    world.draw.error, world.ask.scenes = None, EMPTY
    world.pipeline.retry_at.clear()
    focus(world, 6, 8, current_first=6, current_last=8)
    world.drain()
    assert moments(world)[0]['image_state'] == 'none', 'a new chapter starts without a picture'
    focus(world, 0, 5, current_first=3, current_last=5)
    world.drain()
    assert moments(world)[0]['image_state'] == 'ready'


def test_a_new_appearance_redraws_the_portrait_and_the_scenes_it_was_used_in(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.drain()
    tom, [moment] = cast(world)['Tom'], moments(world)
    edited = world.client.patch(f"/api/characters/{tom['id']}", json={'appearance': 'stocky boy with red hair'}).json()
    assert (edited['portrait_state'], edited['portrait_rev']) == ('none', tom['portrait_rev'] + 1)
    assert moments(world)[0]['image_rev'] == moment['image_rev'] + 1
    assert world.client.get(f"/api/characters/{tom['id']}/portrait").status_code == 404
    world.drain()
    portrait, scene = world.draw.drawn[2:]
    assert 'stocky boy with red hair' in portrait['prompt'] and scene['references'] == [images.PNG + f"{tom['id'] * 1000 + 1}".encode()]

    unchanged = world.client.patch(f"/api/characters/{tom['id']}", json={'appearance': 'stocky boy with red hair', 'pitch': 'low'}).json()
    assert (unchanged['portrait_state'], unchanged['portrait_rev']) == ('ready', tom['portrait_rev'] + 1)


def test_a_drawing_overtaken_by_an_edit_lands_nowhere(world):
    illustrated(world)
    world.ask.scenes = at_the_gate
    focus(world)
    world.pipeline.tag_step(), world.pipeline.scene_step()
    tom = cast(world)['Tom']

    def edit_midway():
        world.draw.during = None
        world.client.patch(f"/api/characters/{tom['id']}", json={'appearance': 'stocky boy with red hair'})
    world.draw.during = edit_midway
    world.pipeline.image_step()
    after = cast(world)['Tom']
    assert (after['portrait_state'], after['portrait_rev']) == ('none', 1) and not world.store.portrait_path(tom).exists()
    world.pipeline.image_step()
    assert cast(world)['Tom']['portrait_state'] == 'ready'

    [moment] = moments(world)

    def redraw_midway():
        world.draw.during = None
        world.client.post(f"/api/characters/{tom['id']}/portrait/redraw")
    world.draw.during = redraw_midway
    world.pipeline.image_step()
    assert (moments(world)[0]['image_state'], moments(world)[0]['image_rev']) == ('none', moment['image_rev'] + 1)
    assert not world.store.moment_path(world.book['id'], moment['id']).exists()
    world.drain()
    assert moments(world)[0]['image_state'] == 'ready' and world.draw.drawn[-1]['references'] != []


def test_merging_moves_a_character_to_the_survivor_in_every_moment(world):
    illustrated(world)

    def two_moments(prompt):
        guard, tom = ref(prompt, 'Guard 1'), ref(prompt, 'Tom')
        return {'ends': [], 'moments': [{'p': 3, 'scene': 'a guard on the wall', 'characters': [guard]},
                                        {'p': 4, 'scene': 'the two face each other', 'characters': [guard, tom]}],
                'appearances': [{'character': guard, 'appearance': 'heavy man in mail'}]}
    world.ask.scenes = two_moments
    focus(world)
    world.drain()
    guard, tom = cast(world)['Guard 1'], cast(world)['Tom']
    assert world.store.portrait_path(guard).exists()
    assert world.client.post(f"/api/characters/{guard['id']}/merge", json={'into': tom['id']}).status_code == 204
    assert [m['character_ids'] for m in moments(world)] == [[tom['id']], [tom['id']]]
    assert not world.store.portrait_path(guard).exists()


def test_a_library_from_before_illustrations_opens(tmp_path):
    old = sqlite3.connect(tmp_path / 'library.db')
    old.executescript("""
        CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, block_count INTEGER NOT NULL,
          position_block INTEGER NOT NULL DEFAULT 0, position_seq INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, language TEXT NOT NULL DEFAULT 'en');
        CREATE TABLE block (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
          idx INTEGER NOT NULL, chapter INTEGER NOT NULL, tag TEXT NOT NULL, tag_state TEXT NOT NULL, UNIQUE (book_id, idx));
        CREATE TABLE character (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
          name TEXT NOT NULL, role TEXT NOT NULL, gender TEXT NOT NULL, age TEXT NOT NULL, pitch TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', voice_state TEXT NOT NULL DEFAULT 'none', voice_rev INTEGER NOT NULL DEFAULT 0,
          error TEXT, draft_state TEXT NOT NULL DEFAULT 'none', draft_rev INTEGER NOT NULL DEFAULT 0);
        INSERT INTO book (id, title, author, block_count) VALUES (1, 'Old', '', 1);
        INSERT INTO block VALUES (1, 1, 0, 0, 'p', 'tagged');
        INSERT INTO character (id, book_id, name, role, gender, age, pitch) VALUES (1, 1, 'Ann', 'named', 'female', 'adult', 'low');
    """)
    old.close()
    (tmp_path / 'settings.json').write_text(json.dumps({'provider': 'codex'}))
    store = Store(tmp_path)
    ann = store.character(1)
    assert (ann['appearance'], ann['portrait_state'], ann['portrait_rev']) == ('', 'none', 0)
    assert store.one('SELECT scene_state FROM block')['scene_state'] == 'pending'
    assert store.moments_where('m.book_id = 1') == []
    assert (store.settings()['image_url'], store.settings()['illustrations'], store.settings()['provider']) == ('', True, 'codex')


def test_the_image_service_address_is_checked_and_tidied(world):
    assert world.client.put('/api/settings', json={'image_url': ' http://gpu.local:8000/ '}).json()['image_url'] == 'http://gpu.local:8000'
    assert world.client.put('/api/settings', json={'image_url': 'gpu.local'}).status_code == 422
    saved = world.client.put('/api/settings', json={'image_url': '', 'illustrations': False}).json()
    assert (saved['image_url'], saved['illustrations']) == ('', False)


def test_pictures_a_page_is_two_until_changed_and_stays_between_one_and_twelve(world):
    assert world.client.get('/api/settings').json()['pictures_per_page'] == 2
    assert world.client.put('/api/settings', json={'pictures_per_page': 12}).json()['pictures_per_page'] == 12
    assert [world.client.put('/api/settings', json={'pictures_per_page': n}).status_code for n in (0, 13)] == [422, 422]
    assert world.client.get('/api/settings').json()['pictures_per_page'] == 12


# Starring a paragraph by hand

def block_at(w, idx):
    return next(b['id'] for b in w.chapter(0) + w.chapter(1) if b['idx'] == idx)


def star(w, idx):
    return w.client.post(f'/api/blocks/{block_at(w, idx)}/star')


def the_gate_opens(prompt):
    tom = ref(prompt, 'Tom')
    return {'moments': [{'p': 1, 'scene': 'The gate swings open before Tom', 'characters': [tom]}], 'ends': [1],
            'appearances': [{'character': tom, 'appearance': 'lanky boy in a patched grey cloak'}]}


def test_starring_a_paragraph_asks_once_what_it_shows_and_draws_it_though_it_is_behind_the_reader(world):
    illustrated(world)
    focus(world)
    world.drain()
    world.ask.star = the_gate_opens
    focus(world, 6, 8, 6, 8)
    calls = world.client.get('/api/usage').json()['all_time']['calls']
    made = star(world, 4)
    tom = cast(world)['Tom']
    assert made.status_code == 200 and made.json() == moments(world)[0]
    assert (made.json()['block_idx'], made.json()['kind'], made.json()['scene'], made.json()['starred'], made.json()['character_ids']) == (
        4, 'picture', 'The gate swings open before Tom', True, [tom['id']])
    assert world.store.one('SELECT wanted FROM moment')['wanted'] == 1
    [prompt] = world.ask.star_prompts
    assert prompt.endswith('TEXT TO SCAN:\n[[p1]]The gate creaked. Nobody spoke for a long time.')
    assert 'for context only:\nChapter One\n' in prompt and '| Tom |' in prompt and 'Narrator' not in prompt
    assert 'LAST ILLUSTRATION' not in prompt and 'LIMIT' not in prompt
    assert world.client.get('/api/usage').json()['all_time']['calls'] == calls + 1
    assert tom['appearance'] == 'lanky boy in a patched grey cloak'
    world.drain()
    assert moments(world)[0]['image_state'] == 'ready'
    assert [d['prompt'].split(' ')[0] for d in world.draw.drawn] == ['Close', 'The'], 'the portrait, then the scene'
    assert world.draw.drawn[-1]['prompt'].startswith('The gate swings open before Tom.')
    assert star(world, 4).json()['starred'] is True and len(world.ask.star_prompts) == 1, 'starring twice asks once'


def test_starred_pictures_are_drawn_first_and_take_no_place_on_their_page(world):
    illustrated(world, pictures_per_page=1)
    world.ask.answer = lambda prompt: {'new_characters': [], 'lines': [{'q': n + 1, 'speaker': 'narrator'} for n in range(prompt.count('[[q'))]}
    world.ask.scenes = lambda prompt: {**EMPTY, 'moments': [{'p': n, 'scene': f'picture {n}', 'characters': []} for n in (2, 3)]}
    focus(world)
    world.drain()
    assert [(m['block_idx'], m['image_state']) for m in moments(world)] == [(1, 'ready'), (2, 'none')], 'the page is at its limit'
    world.ask.star = {**EMPTY, 'moments': [{'p': 1, 'scene': 'starred picture', 'characters': []}]}
    assert star(world, 0).status_code == 200 and star(world, 7).status_code == 200
    world.client.post(f"/api/moments/{moments(world)[1]['id']}/redraw")
    assert world.pipeline.image_step()
    assert world.draw.drawn[-1]['prompt'].endswith('starred picture.' + images.STYLE), 'before the picture of the page in view'
    world.drain()
    assert [(m['block_idx'], m['starred'], m['image_state']) for m in moments(world) + moments(world, 1)] == [
        (0, True, 'ready'), (1, False, 'ready'), (2, False, 'none'), (7, True, 'ready')]


def test_starring_a_paragraph_that_has_a_picture_asks_nothing_and_draws_it_past_the_limit(world):
    illustrated(world, pictures_per_page=1)
    world.ask.scenes = lambda prompt: {**EMPTY, 'moments': [{'p': n, 'scene': f'picture {n}', 'characters': []} for n in (2, 3)]}
    focus(world)
    world.drain()
    assert [m['image_state'] for m in moments(world)] == ['ready', 'none']
    assert star(world, 2).json()['starred'] is True and world.ask.star_prompts == []
    world.draw.error = images.ImageError('500: out of memory')
    world.drain()
    assert moments(world)[1]['image_state'] == 'failed'
    world.draw.error = None
    again = star(world, 2).json()
    assert (again['image_state'], again['image_rev'], again['error']) == ('none', 1, None)
    world.drain()
    assert [m['image_state'] for m in moments(world)] == ['ready', 'ready'] and world.ask.star_prompts == []


def test_starring_where_a_picture_ends_replaces_the_end_with_the_picture(world):
    illustrated(world)
    world.ask.scenes = lambda prompt: {**EMPTY, 'ends': [2], 'moments': [{'p': 1, 'scene': 'picture 1', 'characters': []}]}
    focus(world)
    world.drain()
    assert [(m['block_idx'], m['kind']) for m in moments(world)] == [(0, 'picture'), (1, 'clear')]
    assert star(world, 1).status_code == 502, 'an answer with nothing to draw'
    world.ask.star_error = TaggerError('claude failed: overloaded')
    assert star(world, 1).status_code == 502
    assert [(m['block_idx'], m['kind']) for m in moments(world)] == [(0, 'picture'), (1, 'clear')], 'the end stays until there is a picture'
    world.ask.star_error, world.ask.star = None, the_gate_opens
    assert star(world, 1).status_code == 200
    assert [(m['block_idx'], m['kind'], m['starred']) for m in moments(world)] == [(0, 'picture', False), (1, 'picture', True)]


def test_starring_is_refused_while_illustrations_are_off_and_for_an_unknown_paragraph(world):
    world.ask.star = the_gate_opens
    for settings in ({'image_url': ''}, {'illustrations': False}):
        illustrated(world, **settings)
        refused = star(world, 4)
        assert (refused.status_code, refused.json()['detail']) == (409, 'illustrations are off, so nothing would be drawn')
    assert world.client.post('/api/blocks/9999/star').status_code == 404
    assert world.ask.star_prompts == [] and moments(world) == []


def test_starring_reports_a_failed_call_and_an_answer_with_nothing_to_draw(world):
    illustrated(world)
    world.ask.star_error = TaggerError('claude failed: Not logged in')
    failed = star(world, 4)
    assert (failed.status_code, failed.json()['detail']) == (502, 'claude failed: Not logged in')
    world.ask.star_error = None
    for unusable in (EMPTY, {**EMPTY, 'moments': [{'p': 2, 'scene': 'another paragraph', 'characters': []}]},
                     {**EMPTY, 'moments': [{'p': 1, 'scene': '  ', 'characters': []}], 'ends': [1]}):
        world.ask.star = unusable
        empty = star(world, 4)
        assert (empty.status_code, empty.json()['detail']) == (502, 'the answer described nothing to draw in this paragraph')
    assert moments(world) == [] and world.client.get('/api/usage').json()['all_time']['calls'] == 3


def test_starring_works_while_calls_are_paused(world):
    illustrated(world, tagging_paused=True)
    focus(world)
    world.ask.star = {**EMPTY, 'moments': [{'p': 1, 'scene': 'an empty road', 'characters': []}]}
    assert star(world, 1).status_code == 200
    world.drain()
    assert [(m['scene'], m['starred'], m['image_state']) for m in moments(world)] == [('an empty road', True, 'ready')]
    assert world.ask.scene_prompts == [] and world.ask.prompts == []


def test_a_library_from_before_starring_opens(tmp_path):
    old = sqlite3.connect(tmp_path / 'library.db')
    old.executescript("""
        CREATE TABLE moment (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL, block_id INTEGER NOT NULL UNIQUE,
          block_idx INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'picture', scene TEXT NOT NULL,
          image_state TEXT NOT NULL DEFAULT 'none', image_rev INTEGER NOT NULL DEFAULT 0, error TEXT);
        INSERT INTO moment (id, book_id, block_id, block_idx, scene) VALUES (1, 1, 1, 0, 'an old picture');
    """)
    old.close()
    (tmp_path / 'settings.json').write_text(json.dumps({'image_url': URL}))
    store = Store(tmp_path)
    assert [(m['scene'], m['starred']) for m in store.moments_where('m.book_id = 1')] == [('an old picture', False)]
    assert store.settings()['pictures_per_page'] == 2


# Finding a character's looks in the whole book

def test_finding_a_look_reads_the_whole_book_and_replaces_the_appearance(world):
    illustrated(world, illustrations=False)
    world.ask.scenes = at_the_gate
    focus(world, 0, 5, 0, 5)
    world.drain()
    world.client.put('/api/settings', json={'tagging_paused': True})
    anna, calls = cast(world)['Anna'], world.client.get('/api/usage').json()['all_time']['calls']
    world.client.patch(f"/api/characters/{anna['id']}", json={'appearance': 'a guess'})
    world.ask.look = {'appearance': '  small old woman,\n white hair in a bun, black shawl  '}
    found = world.client.post(f"/api/characters/{anna['id']}/appearance/find")
    assert found.status_code == 200 and found.json()['appearance'] == 'small old woman, white hair in a bun, black shawl'
    assert found.json()['portrait_rev'] == anna['portrait_rev'] + 2
    [prompt] = world.ask.look_prompts
    assert 'CHARACTER: Anna (female elderly)' in prompt
    assert prompt.endswith('PARAGRAPHS:\n“You are late,” said Anna, an old woman with a thin, high voice.\n\n“Tea?” asked Anna.')
    assert world.client.get('/api/usage').json()['all_time']['calls'] == calls + 1
    world.drain()
    assert world.draw.drawn == [], 'nothing is drawn while illustrations are off'


def test_finding_a_look_reports_a_failed_call_and_refuses_the_narrator(world):
    world.ask.answer = answer
    focus(world)
    world.drain()
    tom, narrator = cast(world)['Tom'], cast(world)['Narrator']
    world.ask.look_error = TaggerError('claude failed: Not logged in')
    failed = world.client.post(f"/api/characters/{tom['id']}/appearance/find")
    assert (failed.status_code, failed.json()['detail']) == (502, 'claude failed: Not logged in')
    world.ask.look_error = None
    assert world.client.post(f"/api/characters/{tom['id']}/appearance/find").status_code == 502, 'an empty answer is a failure'
    assert cast(world)['Tom']['appearance'] == ''
    assert world.client.post(f"/api/characters/{narrator['id']}/appearance/find").status_code == 422
    assert len(world.ask.look_prompts) == 2


BOOK = list(enumerate([
    'Mr. Bennet read in the library.',
    'Elizabeth Bennet walked to Meryton with her sisters.',
    'It rained all the next day.',
    '“I shall go,” she said.',
    'Lady Catherine de Bourgh did not approve.',
    'The lady of the house poured tea, and Lizzy laughed.',
    'ELIZABETH! cried her mother. Elizabeth’s bonnet was gone.',
    'Nobody at Bennetsfield knew.',
]))


def test_paragraphs_about_a_character_are_found_by_name_by_telling_word_and_by_their_lines():
    assert scenes.mentioning(BOOK, 'Elizabeth Bennet', spoken=[3]) == [BOOK[i][1] for i in (0, 1, 3, 6)]
    assert scenes.mentioning(BOOK, 'Lady Catherine') == [BOOK[4][1]]
    assert scenes.mentioning(BOOK, 'Lizzy') == [BOOK[5][1]]
    assert scenes.mentioning(BOOK, 'Señora Bennet') == [BOOK[i][1] for i in (0, 1)]
    assert scenes.mentioning(BOOK, 'Guard 1') == []


def test_a_long_book_is_sampled_evenly_instead_of_cut_at_the_start():
    book = [(n, f'Tom did thing number {n:03}.') for n in range(100)]
    kept = scenes.mentioning(book, 'Tom', budget=10 * len(book[0][1]))
    assert len(kept) == 10 and kept[0] == book[0][1] and kept[-1] == book[99][1]
    numbers = [int(text[-4:-1]) for text in kept]
    assert numbers == sorted(numbers) and max(b - a for a, b in zip(numbers, numbers[1:])) == 11
    assert scenes.mentioning(book, 'Tom', budget=10 ** 6) == [text for _, text in book]
    assert scenes.mentioning([(0, 'Tom ' * 50)], 'Tom', budget=40) == ['Tom ' * 10]


# The HTTP client, against a stand-in for the service on this machine

class Service(BaseHTTPRequestHandler):
    replies = {}

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Service.replies['request'] = request
        status, kind, body = Service.replies[request['prompt']]
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_the_client_tells_a_refusal_from_a_service_that_is_not_there():
    server = HTTPServer(('127.0.0.1', 0), Service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    Service.replies = {
        'ok': (200, 'image/png', images.PNG + b'pixels'),
        'loading': (503, 'application/json', b'{"detail": "model is loading"}'),
        'bad': (500, 'application/json', b'{"detail": "out of memory"}'),
        'malformed': (422, 'application/json', b'{"detail": [{"loc": ["body", "width"], "msg": "too wide"}]}'),
        'html': (200, 'text/html', b'<html>'),
    }
    try:
        assert images.generate(url, 'ok', 640, 432, 7, [b'one', b'two']) == images.PNG + b'pixels'
        assert Service.replies['request'] == {
            'prompt': 'ok', 'width': 640, 'height': 432, 'seed': 7, 'reference_px': 256,
            'references': [base64.b64encode(b'one').decode(), base64.b64encode(b'two').decode()]}
        with pytest.raises(images.Unavailable):
            images.generate(url, 'loading', 1, 1, 1)
        with pytest.raises(images.ImageError, match='500: out of memory'):
            images.generate(url, 'bad', 1, 1, 1)
        with pytest.raises(images.ImageError, match='too wide'):
            images.generate(url, 'malformed', 1, 1, 1)
        with pytest.raises(images.ImageError):
            images.generate(url, 'html', 1, 1, 1)
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(images.Unavailable):
        images.generate(url, 'ok', 1, 1, 1)
