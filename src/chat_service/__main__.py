import argparse
import json
import logging
import signal
import time

from .config import load_config
from .engine import Engine
from .matrix import Matrix
from .server import Server
from .store import Store
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description='Private Matrix chat adapter')
    parser.add_argument('--config', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5291)
    parser.add_argument('--init', action='store_true', help='Initialize tables before starting')
    parser.add_argument('--init-only', action='store_true', help='Initialize tables and exit')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--core-status', action='store_true', help='Read core ownership without changing data')
    parser.add_argument('--interval', type=float, default=1.0)
    args = parser.parse_args()
    if args.interval < 0.1:
        parser.error('--interval must be at least 0.1 seconds')
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('psycopg.pool').setLevel(logging.WARNING)
    config = load_config(args.config)
    with Store(config['postgres_dsn'], config['schema'], pool_size=config['postgres_pool_size']) as store:
        if args.init or args.init_only:
            store.initialize()
        if args.init_only:
            return
        engine = Engine(store, Matrix(config['matrix_url'], config['registration_shared_secret']), config['tenants'])
        if args.core_status:
            print(json.dumps({'core_protocol': 1, 'active_tenants': engine.core.active_tenants()}))
            return
        if args.worker:
            worker = Worker(engine)
            while True:
                failed = False
                for tenant in worker.sources:
                    try:
                        worker.once(tenant) if args.once else worker.step(tenant)
                    except Exception as error:
                        failed = True
                        logging.error('Tenant %s sync failed: %s', tenant, type(error).__name__)
                if args.once:
                    if failed:
                        raise SystemExit(1)
                    return
                time.sleep(args.interval)
        else:
            with Server((args.host, args.port), engine) as server:
                server.serve_forever()


def terminate(signum, frame):
    raise SystemExit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, terminate)
    main()
