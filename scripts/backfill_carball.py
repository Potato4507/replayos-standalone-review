from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replayos.carball_ingest import backfill_replay_names, refresh_local_replay_index
from replayos.config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Backfill Carball 60 Hz parses for local ReplayOS replays.")
    parser.add_argument("--limit", type=int, default=25, help="How many candidate replays to parse in this batch.")
    parser.add_argument("--force", action="store_true", help="Reparse even if a replay already has a completed Carball cache.")
    parser.add_argument("--scan-only", action="store_true", help="Refresh the local replay index without parsing a batch.")
    args = parser.parse_args()

    if args.scan_only:
        result = {"index": refresh_local_replay_index(serving_db=settings.serving_db)}
    else:
        result = backfill_replay_names(
            serving_db=settings.serving_db,
            limit=args.limit,
            force=args.force,
            refresh_index=True,
        )

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
