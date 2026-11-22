"""openfield: run the daemon (`openfield serve`) or check the configuration (`openfield check`)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(os.environ.get("OPENFIELD_CONFIG", Path(__file__).resolve().parents[2] / "config"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="openfield", description="openField daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config folder (daemon.yaml, services/)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the daemon")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("check", help="validate the config and show each service's availability")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "check":
        from .config import Config
        from .db import DB
        from .jobs import JobManager

        config = Config(args.config).load()
        manager = JobManager(config, DB(config.data_dir / "openfield.db"))
        print(f"config: {args.config}\nlisten: {config.daemon.host}:{config.daemon.port}  data: {config.data_dir}")
        for spec in config.services.values():
            ok, why = manager.availability(spec)
            print(f"  {'ok ' if ok else 'OFF'} {spec.id:28s} {spec.kind:11s} {spec.name}" + (f"  ({why})" if why else ""))
        for name, err in config.errors.items():
            print(f"  ERR {name}: {err}")
        sys.exit(1 if config.errors else 0)

    import uvicorn

    from .api import create_app
    from .config import Config

    daemon = Config(args.config).load().daemon
    uvicorn.run(create_app(args.config), host=args.host or daemon.host, port=args.port or daemon.port,
                log_level="info", proxy_headers=False)
