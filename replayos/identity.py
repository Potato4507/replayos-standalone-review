from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import duckdb

from .db import rows_to_dicts


PLACEHOLDER_TEAM_KEYS = {"blue", "orange", "blue side", "orange side"}
TEAM_GENERIC_TOKENS = {"team", "esports", "esport", "gaming", "club", "rl", "clan"}
TEAM_ALIAS_SEEDS = {
    "the general nrg": "NRG",
    "nrg": "NRG",
    "team vitality": "Vitality",
    "vitality": "Vitality",
    "moist": "Moist Esports",
    "moist esports": "Moist Esports",
    "faze": "FaZe Clan",
    "faze clan": "FaZe Clan",
    "team bds": "Team BDS",
    "bds": "Team BDS",
    "endpoint": "Endpoint CEX",
    "endpoint cex": "Endpoint CEX",
    "williams resolve": "Williams Resolve",
    "rule one": "Rule One",
    "team falcons": "Team Falcons",
    "falcons": "Team Falcons",
    "heet": "HEET",
    "ghost": "GHOST",
    "01 esports": "01 Esports",
    "renegades": "Renegades",
    "solary": "Solary",
    "eg": "EG",
    "spacestation": "Spacestation",
    "spacestation gaming": "Spacestation",
    "gentle mates": "Gentle Mates",
    "gentlemates": "Gentle Mates",
    "gm8": "Gentle Mates",
    "gmates": "Gentle Mates",
    "m8": "Gentle Mates",
    "ninjas in pyjamas": "Ninjas in Pyjamas",
    "nip": "Ninjas in Pyjamas",
    "furia esports": "Furia",
    "furia": "Furia",
    "r8 esports": "R8 Esports",
    "r8": "R8 Esports",
    "kinotrope gaming": "KINOTROPE gaming",
    "kinotrope": "KINOTROPE gaming",
}
PLAYER_ALIAS_SEEDS: dict[str, str] = {}


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_name = ?
        """,
        [table_name],
    ).fetchone()
    return row is not None


def ensure_identity_schema(con: duckdb.DuckDBPyConnection) -> None:
    required_tables = {
        "identity_team_aliases",
        "identity_player_aliases",
        "identity_player_refs",
    }
    if all(_table_exists(con, table_name) for table_name in required_tables):
        return
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_team_aliases (
                alias_key VARCHAR PRIMARY KEY,
                canonical_name VARCHAR,
                source VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_player_aliases (
                alias_key VARCHAR PRIMARY KEY,
                canonical_name VARCHAR,
                source VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_player_refs (
                player_ref VARCHAR PRIMARY KEY,
                canonical_name VARCHAR,
                source VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
    except duckdb.Error as exc:
        if "read-only mode" in str(exc).lower():
            return
        raise

    now = datetime.now(timezone.utc)
    if _table_exists(con, "identity_team_aliases"):
        con.executemany(
            """
            INSERT OR IGNORE INTO identity_team_aliases VALUES (?, ?, ?, ?)
            """,
            [[alias_key, canonical, "seed", now] for alias_key, canonical in TEAM_ALIAS_SEEDS.items()],
        )
    if PLAYER_ALIAS_SEEDS and _table_exists(con, "identity_player_aliases"):
        con.executemany(
            """
            INSERT OR IGNORE INTO identity_player_aliases VALUES (?, ?, ?, ?)
            """,
            [[alias_key, canonical, "seed", now] for alias_key, canonical in PLAYER_ALIAS_SEEDS.items()],
        )


def clean_identity_text(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if any(marker in text for marker in ("Ã", "ã", "â", "ð")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
        except UnicodeError:
            repaired = text
        else:
            if repaired.strip():
                text = repaired.strip()
    return unicodedata.normalize("NFKC", text)


def alias_key(value: Any) -> str:
    cleaned = clean_identity_text(value)
    if not cleaned:
        return ""
    simplified = unicodedata.normalize("NFKD", cleaned.casefold())
    simplified = "".join(char for char in simplified if not unicodedata.combining(char))
    simplified = re.sub(r"[^\w]+", " ", simplified, flags=re.UNICODE)
    simplified = simplified.replace("_", " ")
    return " ".join(simplified.split())


def core_team_key(value: Any) -> str:
    tokens = [token for token in alias_key(value).split() if token and token not in TEAM_GENERIC_TOKENS]
    return " ".join(tokens) if tokens else alias_key(value)


def is_placeholder_team_name(team_name: str | None) -> bool:
    canonical = alias_key(team_name)
    return canonical in PLACEHOLDER_TEAM_KEYS or core_team_key(team_name) in PLACEHOLDER_TEAM_KEYS


def canonicalize_team_name(con: duckdb.DuckDBPyConnection, team_name: str | None) -> str:
    return IdentityResolver(con).canonical_team_name(team_name)


class IdentityResolver:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        ensure_identity_schema(con)
        self.team_aliases: dict[str, str] = {}
        self.team_core_aliases: dict[str, str] = {}
        self.player_aliases: dict[str, str] = {}
        self.player_refs: dict[str, str] = {}
        self.player_ref_inferred: dict[str, str] = {}
        self._load()

    def canonical_team_name(self, team_name: str | None) -> str:
        cleaned = clean_identity_text(team_name)
        if not cleaned:
            return "Unknown"
        exact_key = alias_key(cleaned)
        core_key = core_team_key(cleaned)
        if exact_key in self.team_aliases:
            exact_name = self.team_aliases[exact_key]
            core_name = self.team_core_aliases.get(core_key)
            if core_name and _display_name_score(core_name) > _display_name_score(exact_name):
                return core_name
            return exact_name
        if core_key in self.team_core_aliases:
            return self.team_core_aliases[core_key]
        return cleaned

    def team_key(self, team_name: str | None) -> str:
        return alias_key(self.canonical_team_name(team_name))

    def resolve_player(
        self,
        player_id: str | None,
        player_name: str | None,
        *,
        platform: str | None = None,
    ) -> dict[str, str]:
        ref = self.player_ref(player_id, platform=platform)
        cleaned_name = clean_identity_text(player_name) or clean_identity_text(player_id) or "Unknown"
        alias = alias_key(cleaned_name)

        if ref and ref in self.player_refs:
            canonical = self.player_refs[ref]
            return {"player_key": f"player:{alias_key(canonical)}", "player_name": canonical}
        if ref and ref in self.player_ref_inferred:
            canonical = self.player_ref_inferred[ref]
            return {"player_key": f"ref:{ref}", "player_name": canonical}
        if alias and alias in self.player_aliases:
            canonical = self.player_aliases[alias]
            return {"player_key": f"player:{alias_key(canonical)}", "player_name": canonical}
        if ref:
            return {"player_key": f"ref:{ref}", "player_name": cleaned_name}
        return {"player_key": f"name:{alias or cleaned_name.casefold()}", "player_name": cleaned_name}

    def player_ref(self, player_id: str | None, *, platform: str | None = None) -> str | None:
        identifier = clean_identity_text(player_id)
        if not identifier:
            return None
        prefix = (platform or "id").casefold()
        return f"{prefix}:{identifier.casefold()}"

    def _load(self) -> None:
        for seed_key, canonical_name in TEAM_ALIAS_SEEDS.items():
            if seed_key and canonical_name:
                self.team_aliases[seed_key] = canonical_name
                core_key = core_team_key(seed_key)
                if core_key:
                    self._choose_team_alias(self.team_core_aliases, core_key, canonical_name)

        if _table_exists(self.con, "identity_team_aliases"):
            for row in rows_to_dicts(self.con.execute("SELECT alias_key, canonical_name FROM identity_team_aliases")):
                if row["alias_key"] and row["canonical_name"]:
                    self.team_aliases[row["alias_key"]] = row["canonical_name"]
                    core_key = core_team_key(row["alias_key"])
                    if core_key:
                        self._choose_team_alias(self.team_core_aliases, core_key, row["canonical_name"])

        for candidate in self._candidate_team_names():
            cleaned = clean_identity_text(candidate)
            if not cleaned or is_placeholder_team_name(cleaned):
                continue
            self._choose_team_alias(self.team_aliases, alias_key(cleaned), cleaned)
            self._choose_team_alias(self.team_core_aliases, core_team_key(cleaned), cleaned)

        if _table_exists(self.con, "identity_player_aliases"):
            for row in rows_to_dicts(self.con.execute("SELECT alias_key, canonical_name FROM identity_player_aliases")):
                if row["alias_key"] and row["canonical_name"]:
                    self.player_aliases[row["alias_key"]] = row["canonical_name"]

        if _table_exists(self.con, "identity_player_refs"):
            for row in rows_to_dicts(self.con.execute("SELECT player_ref, canonical_name FROM identity_player_refs")):
                if row["player_ref"] and row["canonical_name"]:
                    self.player_refs[row["player_ref"]] = row["canonical_name"]

        inferred: dict[str, list[tuple[str, int]]] = {}
        for ref, player_name, sample_count in self._candidate_player_refs():
            if not ref:
                continue
            inferred.setdefault(ref, []).append((clean_identity_text(player_name), int(sample_count or 0)))
        for ref, candidates in inferred.items():
            best_name = ""
            best_score = (-1, -1, -1)
            for candidate, sample_count in candidates:
                score = (sample_count, _display_name_score(candidate), len(candidate))
                if score > best_score:
                    best_name = candidate
                    best_score = score
            if best_name:
                self.player_ref_inferred[ref] = best_name

    def _candidate_team_names(self) -> list[str]:
        values: set[str] = set()
        queries = [
            "SELECT DISTINCT team_name FROM live_leaderboards WHERE team_name IS NOT NULL",
            "SELECT DISTINCT blue_team_name FROM remote_replays WHERE blue_team_name IS NOT NULL",
            "SELECT DISTINCT orange_team_name FROM remote_replays WHERE orange_team_name IS NOT NULL",
            "SELECT DISTINCT blue_team_name FROM replay_parsed_status WHERE status = 'completed' AND blue_team_name IS NOT NULL",
            "SELECT DISTINCT orange_team_name FROM replay_parsed_status WHERE status = 'completed' AND orange_team_name IS NOT NULL",
        ]
        for query in queries:
            try:
                rows = self.con.execute(query).fetchall()
            except duckdb.Error:
                continue
            for (value,) in rows:
                cleaned = clean_identity_text(value)
                if cleaned:
                    values.add(cleaned)
        return sorted(values)

    def _candidate_player_refs(self) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        queries = [
            """
            SELECT CONCAT('id:', lower(COALESCE(player_id, ''))), player_name, COUNT(*) AS sample_count
            FROM replay_parsed_events
            WHERE player_id IS NOT NULL AND player_name IS NOT NULL
            GROUP BY 1, 2
            """,
            """
            SELECT CONCAT(lower(COALESCE(platform, 'id')), ':', lower(COALESCE(platform_player_id, ''))), player_name, COUNT(*) AS sample_count
            FROM remote_players
            WHERE platform_player_id IS NOT NULL AND player_name IS NOT NULL
            GROUP BY 1, 2
            """,
        ]
        for query in queries:
            try:
                rows.extend(self.con.execute(query).fetchall())
            except duckdb.Error:
                continue
        return rows

    def _choose_team_alias(self, mapping: dict[str, str], key: str, candidate: str) -> None:
        if not key or not candidate:
            return
        current = mapping.get(key)
        if current is None or _display_name_score(candidate) > _display_name_score(current):
            mapping[key] = candidate


def _display_name_score(value: str) -> int:
    cleaned = clean_identity_text(value)
    if not cleaned:
        return -1
    score = 0
    normalized = alias_key(cleaned)
    if normalized in TEAM_ALIAS_SEEDS and TEAM_ALIAS_SEEDS[normalized] == cleaned:
        score += 40
    if cleaned.isupper():
        score += 6
    if cleaned.islower():
        score -= 2
    if any(char.islower() for char in cleaned) and any(char.isupper() for char in cleaned):
        score += 10
    if not any(token in cleaned.casefold().split() for token in TEAM_GENERIC_TOKENS):
        score += 4
    score -= abs(len(cleaned) - len(normalized))
    return score
