from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replayos.config import get_settings
from replayos.warehouse import refresh_warehouse


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build the ReplayOS serving warehouse from the raw DuckDB corpus.")
    parser.add_argument("--raw-db", type=Path, default=settings.raw_db)
    parser.add_argument("--serving-db", type=Path, default=settings.serving_db)
    parser.add_argument("--sample-limit", type=int, default=None, help="Optional replay limit for quick local smoke builds.")
    args = parser.parse_args()
    result = refresh_warehouse(args.raw_db, args.serving_db, sample_limit=args.sample_limit)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
