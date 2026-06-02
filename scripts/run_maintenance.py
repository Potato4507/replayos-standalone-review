from __future__ import annotations

import argparse
import json

from replayos.maintenance import run_maintenance_pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the cheap/high-value ReplayOS maintenance loop.")
    parser.add_argument("--parse-limit", type=int, default=24)
    parser.add_argument("--eval-limit", type=int, default=120)
    parser.add_argument("--ballchasing-count", type=int, default=8)
    parser.add_argument("--youtube-limit", type=int, default=6)
    parser.add_argument("--force-eval", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-ballchasing", action="store_true")
    parser.add_argument("--skip-carball", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-youtube", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args()

    result = run_maintenance_pass(
        trigger="script",
        refresh_index=not args.skip_index,
        refresh_ballchasing=not args.skip_ballchasing,
        backfill_names=not args.skip_carball,
        refresh_youtube=not args.skip_youtube,
        backfill_eval=not args.skip_eval,
        refresh_live=not args.skip_live,
        parse_limit=args.parse_limit,
        eval_limit=args.eval_limit,
        ballchasing_count=args.ballchasing_count,
        youtube_limit=args.youtube_limit,
        force_eval=args.force_eval,
    )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
