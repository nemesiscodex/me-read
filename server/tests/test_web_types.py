from meread import web_types


def test_the_web_clients_types_are_written_from_the_current_schema():
    assert web_types.PATH.read_text() == web_types.source(), f'{web_types.PATH} is stale. Run: {web_types.COMMAND}'


def test_every_row_the_api_returns_has_exactly_the_fields_of_its_web_type(world):
    book_id = world.book['id']
    narrator = world.store.narrator(book_id)
    world.pipeline.design_voice(narrator)
    world.store.request_take(narrator['id'], 'happy')
    paragraph = world.chapter()[1]
    world.store.run('INSERT INTO moment (book_id, block_id, block_idx, scene) VALUES (?, ?, ?, ?)',
                    (book_id, paragraph['id'], paragraph['idx'], 'A gate at dusk'))
    detail = world.client.get(f'/api/books/{book_id}').json()
    blocks = world.chapter()
    rows = {'BookSummary': world.client.get('/api/books').json(), 'BookDetail': [detail], 'Chapter': detail['chapters'],
            'Block': blocks, 'Segment': [s for b in blocks for s in b['segments']], 'Character': detail['characters'],
            'Take': [t for c in detail['characters'] for t in c['takes']], 'ReusableVoice': world.client.get('/api/voices').json(),
            'Moment': world.client.get(f'/api/books/{book_id}/chapters/0/moments').json()}
    types = web_types.shapes()
    assert rows.keys() == types.keys()
    for name, fields in types.items():
        assert rows[name], name
        for row in rows[name]:
            assert set(row) == set(fields), name
