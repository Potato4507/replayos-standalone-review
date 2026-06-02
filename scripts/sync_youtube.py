from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replayos.config import get_settings
from replayos.youtube_sync import sync_youtube_videos


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Search and attach YouTube videos and VOD clips to ReplayOS replays.")
    parser.add_argument("--replay-id", default=None)
    parser.add_argument("--limit", type=int, default=settings.youtube_default_count)
    args = parser.parse_args()
    result = sync_youtube_videos(settings.serving_db, replay_id=args.replay_id, limit=args.limit)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
