from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replayos.ballchasing import configured_ballchasing_sources, resolve_ballchasing_source, sync_ballchasing_replays, sync_ballchasing_source_set
from replayos.config import get_settings


def main() -> None:
    settings = get_settings()
    defaults = configured_ballchasing_sources()
    parser = argparse.ArgumentParser(description="Download replay metadata and files from Ballchasing into ReplayOS.")
    parser.add_argument("--group-id", default=settings.ballchasing_default_group)
    parser.add_argument("--creator-id", default=None)
    parser.add_argument("--playlist", default=None)
    parser.add_argument("--player-name", action="append", default=[])
    parser.add_argument("--player-id", action="append", default=[])
    parser.add_argument("--count", type=int, default=settings.ballchasing_default_count)
    parser.add_argument("--metadata-only", action="store_true", help="Fetch replay metadata without downloading .replay files.")
    parser.add_argument("--no-details", action="store_true", help="Use list payloads only and skip replay-detail requests.")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--no-parse", action="store_true", help="Skip the Carball 60 Hz replay parse after download.")
    args = parser.parse_args()

    filters: dict[str, object] = {}
    if args.playlist:
        filters["playlist"] = args.playlist
    if args.player_name:
        filters["player-name"] = args.player_name
    if args.player_id:
        filters["player-id"] = args.player_id
    resolved_group = None
    resolved_creator = None
    if args.group_id:
        resolved = resolve_ballchasing_source(args.group_id)
        if resolved:
            if resolved[0] == "creator":
                resolved_creator = resolved[1]
            else:
                resolved_group = resolved[1]
        else:
            resolved_group = args.group_id
    if args.creator_id:
        resolved = resolve_ballchasing_source(args.creator_id)
        resolved_creator = resolved[1] if resolved and resolved[0] == "creator" else args.creator_id

    if resolved_group or resolved_creator:
        result = sync_ballchasing_source_set(
            settings.serving_db,
            group_ids=[resolved_group] if resolved_group else None,
            creator_ids=[resolved_creator] if resolved_creator else None,
            base_filters=filters,
            count=args.count,
            download_files=not args.metadata_only,
            fetch_details=not args.no_details,
            force_download=args.force_download,
            parse_downloads=not args.no_parse,
        )
    elif not resolved_group and not resolved_creator and (defaults["groups"] or defaults["creators"]):
        result = sync_ballchasing_source_set(
            settings.serving_db,
            group_ids=list(defaults["groups"]),
            creator_ids=list(defaults["creators"]),
            creator_group_limit=settings.ballchasing_default_creator_group_limit,
            base_filters=filters,
            count=args.count,
            download_files=not args.metadata_only,
            fetch_details=not args.no_details,
            force_download=args.force_download,
            parse_downloads=not args.no_parse,
        )
    else:
        result = sync_ballchasing_replays(
            settings.serving_db,
            filters=filters,
            count=args.count,
            download_files=not args.metadata_only,
            fetch_details=not args.no_details,
            force_download=args.force_download,
            parse_downloads=not args.no_parse,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
