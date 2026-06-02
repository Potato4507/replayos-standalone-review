from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replayos.config import get_settings
from replayos.analytics import run_model_pipeline


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Train ReplayOS predictive and descriptive models.")
    parser.add_argument("--serving-db", type=Path, default=settings.serving_db)
    args = parser.parse_args()
    result = run_model_pipeline(args.serving_db)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
