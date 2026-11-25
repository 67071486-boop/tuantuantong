import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from .server import Server
from .service import Pilot


def main():
    parser = argparse.ArgumentParser(description='Local-only Synapse / Web K pilot')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--webk-root', type=Path, required=True)
    parser.add_argument('--host', choices=['127.0.0.1'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5288)
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    for key in ('matrix_url', 'legacy_url'):
        parsed = urlsplit(config[key])
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1'):
            parser.error(f'{key} must be loopback HTTP for this pilot')
    pilot = Pilot(config)
    if args.bootstrap:
        pilot.bootstrap()
    Server((args.host, args.port), pilot, args.webk_root).serve_forever()


if __name__ == '__main__':
    main()
