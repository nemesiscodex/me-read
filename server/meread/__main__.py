import argparse
import logging
from pathlib import Path

import uvicorn

from . import HOME, speech
from .app import create_app


def main():
    parser = argparse.ArgumentParser(prog='meread', description='Run the me-read server.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--home', default=HOME, help='where books, voices, audio and models are kept')
    parser.add_argument('--tts-provider', choices=speech.PROVIDERS, default='chatterbox',
                        help='narrator, separate from the tagger setting (default: chatterbox; breeze is English-only; '
                             'breeze-gguf is English-only and narrates only through the GPU speech service at --tts-url)')
    parser.add_argument('--tts-url', help='address of the GPU speech service, for example http://127.0.0.1:8781. With it, '
                                          'the service designs voices, makes emotion takes and compares speakers, and '
                                          'narrates for chatterbox and breeze-gguf; fish and breeze still narrate here')
    parser.add_argument('--tts-consent', help='your own statement that you may use the reference voices; required with '
                                              '--tts-url, sent with every request, and has no default')
    args = parser.parse_args()
    try:
        speech.connect(args.tts_provider, args.tts_url, args.tts_consent)
    except ValueError as e:
        parser.error(str(e))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    web = Path(__file__).resolve().parents[2] / 'web/dist'
    app = create_app(args.home, web_dir=web, tts_provider=args.tts_provider, tts_url=args.tts_url, tts_consent=args.tts_consent)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            app.state.hub.close()
            super().handle_exit(sig, frame)

    # The limit is for a request that is still waiting on Claude or Codex when the server is asked to stop.
    Server(uvicorn.Config(app, host=args.host, port=args.port, timeout_graceful_shutdown=5)).run()


if __name__ == '__main__':
    main()
