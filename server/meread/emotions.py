"""How a spoken line is said. Neutral is the character's own voice. For every other emotion Breeze directs that voice
into a take: the emotion's script, said the way its instruction asks. Lines in that emotion are read from the take."""
DIRECTIONS = {
    'happy': ('Speak with joy and delight, smiling, upbeat and lively.',
              'Oh, this is wonderful! I can hardly believe it worked. Look at all of you here, after everything we went '
              'through. Come in, come in, sit down. Tonight we celebrate, and tomorrow we will laugh about it all over '
              'again.'),
    'sad': ('Speak with deep sadness, slow and heavy, close to tears.',
            'I kept the letter for years, and I never had the courage to open it. Now there is nobody left to answer. '
            'The house is empty, and the garden has gone wild. I only wish I had told her, just once, how much she '
            'meant to me.'),
    'angry': ('Speak with fury, loud, sharp and forceful.',
              'How dare you come back here after what you did? You lied to me, again and again, and I believed every '
              'word. No. Not this time. Get out of my house, and do not ever ask me for anything again!'),
    'scared': ('Speak in fear, trembling, breathless and urgent.',
               'Did you hear that? Something is moving out there, behind the door. Please, keep your voice down. I do '
               'not want it to find us. Oh no, the lights. Stay close to me, and whatever happens, do not let go of my '
               'hand.'),
    'whisper': ('Whisper softly and secretly, breathy and quiet.',
                'Come closer, and keep your voice down. Nobody else can know about this. The key is under the third '
                'stone by the well. Wait until they are all asleep, then go. I will leave the window open for you. '
                'Not a word to anyone.'),
}
EMOTIONS = 'neutral', *DIRECTIONS
