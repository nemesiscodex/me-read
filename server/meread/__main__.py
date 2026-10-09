import argparse
import logging
from pathlib import Path

import uvicorn

from . import HOME
from .app import create_app


def main():
    parser = argparse.ArgumentParser(prog='meread', description='Run the me-read server.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--home', default=HOME, help='where books, voices, audio and models are kept')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    web = Path(__file__).resolve().parents[2] / 'web/dist'
    app = create_app(args.home, web_dir=web)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            app.state.hub.close()
            super().handle_exit(sig, frame)

    # The limit is for a request that is still waiting on Claude or Codex when the server is asked to stop.
    Server(uvicorn.Config(app, host=args.host, port=args.port, timeout_graceful_shutdown=5)).run()


if __name__ == '__main__':
    main()
