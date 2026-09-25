import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from app.db import SessionFactory
from app.regions.importer import import_geojson


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.regions.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--file", required=True, type=Path)
    import_parser.add_argument("--version", required=True)
    import_parser.add_argument("--name", required=True)
    import_parser.add_argument("--activate", action="store_true")
    return parser


async def _import(args: argparse.Namespace) -> int:
    async with SessionFactory() as session:
        async with session.begin():
            result = await import_geojson(
                session,
                args.file,
                version=args.version,
                name=args.name,
                activate=args.activate,
            )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "import":
        return asyncio.run(_import(args))
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
