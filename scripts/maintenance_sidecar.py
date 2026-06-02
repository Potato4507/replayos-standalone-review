from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from replayos.maintenance import run_maintenance_pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _should_run(cycle: int, every: int, offset: int) -> bool:
    return every > 0 and cycle >= offset and (cycle - offset) % every == 0


def _build_payload(args: argparse.Namespace, cycle: int) -> dict[str, object]:
    return {
        "refresh_index": _should_run(cycle, args.index_every, args.index_offset),
        "refresh_ballchasing": _should_run(cycle, args.ballchasing_every, args.ballchasing_offset),
        "backfill_names": _should_run(cycle, args.carball_every, args.carball_offset),
        "refresh_youtube": _should_run(cycle, args.youtube_every, args.youtube_offset),
        "backfill_eval": _should_run(cycle, args.eval_every, args.eval_offset),
        "refresh_live": _should_run(cycle, args.live_every, args.live_offset),
        "parse_limit": args.parse_limit,
        "eval_limit": args.eval_limit,
        "ballchasing_count": args.ballchasing_count,
        "youtube_limit": args.youtube_limit,
        "force_eval": args.force_eval,
    }


def _print_summary(result: dict[str, object], cycle: int) -> None:
    steps = result.get("steps") if isinstance(result, dict) else {}
    carball = (steps or {}).get("carball") if isinstance(steps, dict) else {}
    eval_step = (steps or {}).get("eval") if isinstance(steps, dict) else {}
    ball = (steps or {}).get("ballchasing") if isinstance(steps, dict) else {}
    print(
        json.dumps(
            {
                "ts": _utcnow(),
                "cycle": cycle,
                "duration_seconds": result.get("duration_seconds"),
                "carball_requested": (carball or {}).get("requested"),
                "carball_parsed": (carball or {}).get("parsed"),
                "carball_failed": (carball or {}).get("failed"),
                "eval_processed": (eval_step or {}).get("processed"),
                "eval_computed": (eval_step or {}).get("computed"),
                "ballchasing_seen": (ball or {}).get("seen"),
                "ballchasing_downloaded": (ball or {}).get("downloaded"),
                "ballchasing_parsed": (ball or {}).get("parsed"),
            },
            default=str,
        ),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ReplayOS upkeep as an external sidecar loop alongside the local site.")
    parser.add_argument("--sleep-seconds", type=int, default=180)
    parser.add_argument("--parse-limit", type=int, default=2)
    parser.add_argument("--eval-limit", type=int, default=48)
    parser.add_argument("--ballchasing-count", type=int, default=6)
    parser.add_argument("--youtube-limit", type=int, default=4)
    parser.add_argument("--index-every", type=int, default=12)
    parser.add_argument("--ballchasing-every", type=int, default=12)
    parser.add_argument("--carball-every", type=int, default=1)
    parser.add_argument("--eval-every", type=int, default=1)
    parser.add_argument("--youtube-every", type=int, default=6)
    parser.add_argument("--live-every", type=int, default=1)
    parser.add_argument("--index-offset", type=int, default=1)
    parser.add_argument("--ballchasing-offset", type=int, default=4)
    parser.add_argument("--carball-offset", type=int, default=1)
    parser.add_argument("--eval-offset", type=int, default=1)
    parser.add_argument("--youtube-offset", type=int, default=3)
    parser.add_argument("--live-offset", type=int, default=1)
    parser.add_argument("--force-eval", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    cycle = 0

    while True:
        cycle += 1
        payload = _build_payload(args, cycle)
        try:
            result = run_maintenance_pass(trigger="sidecar", **payload)
            _print_summary(result, cycle)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ts": _utcnow(), "cycle": cycle, "status": "error", "error": str(exc)}), flush=True)

        if args.once:
            return
        time.sleep(max(15, int(args.sleep_seconds)))


if __name__ == "__main__":
    main()
