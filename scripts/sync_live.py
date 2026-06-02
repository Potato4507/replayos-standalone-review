from __future__ import annotations

import argparse
import json
import sys

from replayos.config import get_settings
from replayos.live_sync import sync_live_data


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh cached RLCS/pro live schedule and stream data.")
    parser.add_argument("--force", action="store_true", help="Ignore cache windows and fetch remote sources now.")
    args = parser.parse_args()
    result = sync_live_data(get_settings().serving_db, force=args.force)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
