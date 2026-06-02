from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from replayos.ballchasing import BallchasingError, ensure_ballchasing_replay_download
from replayos.carball_ingest import PARSER_VERSION, ReplayParseError, load_parsed_replay_frames, parsed_events
from replayos.config import PROJECT_ROOT, get_settings
from replayos.native_viewer import (
    build_native_viewer_payload,
    load_native_viewer_payload_cache,
    native_viewer_gzip_response,
    store_native_viewer_payload_cache,
)
from replayos.site import get_library_replay, replay_viewer


APP_TITLE = "ReplayOS Standalone Review"
STANDALONE_ROOT = PROJECT_ROOT / "output" / "standalone-review"
NATIVE_VIEWER_DIR = PROJECT_ROOT / "frontend" / "public" / "native-viewer"


class ReviewRequest(BaseModel):
    replay_input: str = Field(min_length=3, max_length=300)
    ballchasing_api_token: str | None = Field(default=None, max_length=200)
    force_refresh: bool = False


app = FastAPI(title=APP_TITLE)

if NATIVE_VIEWER_DIR.exists():
    app.mount("/native-viewer", StaticFiles(directory=str(NATIVE_VIEWER_DIR), html=True), name="native-viewer")


def normalize_replay_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Replay id is required.")
    match = re.search(r"/replays/([a-f0-9-]{8,})", raw, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.search(r"([a-f0-9]{8}-[a-f0-9-]{27,})", raw, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if re.fullmatch(r"[a-z0-9-]{8,}", raw, re.IGNORECASE):
        return raw.lower()
    raise ValueError("Paste a Ballchasing replay id or replay URL.")


def workspace_paths(replay_id: str) -> dict[str, Path]:
    root = STANDALONE_ROOT / replay_id
    return {
        "root": root,
        "serving_db": root / "standalone.duckdb",
        "download_dir": root / "replays",
        "replay_file": root / "replays" / f"{replay_id}.replay",
    }


@contextmanager
def settings_override(
    *,
    serving_db: Path,
    replay_download_dir: Path,
    ballchasing_api_token: str | None = None,
) -> Iterator[None]:
    keys = {
        "REPLAYOS_SERVING_DB": str(serving_db),
        "REPLAYOS_REPLAY_DOWNLOAD_DIR": str(replay_download_dir),
    }
    if ballchasing_api_token:
        keys["BALLCHASING_API_TOKEN"] = ballchasing_api_token

    previous: dict[str, str | None] = {key: os.environ.get(key) for key in keys}
    try:
        for key, value in keys.items():
            os.environ[key] = value
        get_settings.cache_clear()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def local_review_payload(
    replay_id: str,
    *,
    replay_token: str | None,
    force_refresh: bool,
) -> dict[str, Any]:
    paths = workspace_paths(replay_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    has_local_copy = paths["serving_db"].exists() and paths["replay_file"].exists()
    if not replay_token and not has_local_copy:
        raise HTTPException(
            status_code=400,
            detail="Ballchasing API token is required the first time you prepare a replay.",
        )

    with settings_override(
        serving_db=paths["serving_db"],
        replay_download_dir=paths["download_dir"],
        ballchasing_api_token=replay_token,
    ):
        if replay_token:
            try:
                ensure_ballchasing_replay_download(
                    replay_id,
                    serving_db=paths["serving_db"],
                    force_download=force_refresh,
                    parse_download=True,
                )
            except BallchasingError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except ReplayParseError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            with duckdb.connect(str(paths["serving_db"])) as con:
                viewer = replay_viewer(con, replay_id)
        except duckdb.Error as exc:
            raise HTTPException(status_code=500, detail=f"Could not open the standalone replay database: {exc}") from exc

        if viewer is None:
            raise HTTPException(status_code=404, detail="Replay not found in the standalone workspace.")

    return {
        "replay_id": replay_id,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "workspace_dir": str(paths["root"]),
        "viewer": viewer,
        "native_viewer_url": f"/native-viewer/index.html?replayId={replay_id}&apiBase=",
        "file_url": f"/library/replays/{replay_id}/file",
        "json_url": f"/library/replays/{replay_id}/viewer",
    }


def homepage_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ReplayOS Standalone Review</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0d0f;
      --panel: #121518;
      --panel-2: #191d21;
      --line: rgba(255,255,255,0.1);
      --text: #f4efe6;
      --muted: #b6b9bd;
      --teal: #20b3aa;
      --gold: #f0c76a;
      --red: #df6e63;
      --purple: #b896ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: linear-gradient(180deg, #0b0d0f, #101317);
      color: var(--text);
      font-family: Inter, system-ui, sans-serif;
    }
    .shell {
      margin: 0 auto;
      max-width: 1280px;
      padding: 24px;
    }
    .hero, .panel {
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }
    .hero {
      display: grid;
      gap: 16px;
      margin-bottom: 18px;
    }
    .hero p, .note, .meta, .stack-row em, .card em { color: var(--muted); }
    h1, h2, h3, p { margin: 0; }
    .hero-copy { display: grid; gap: 8px; }
    .hero-copy h1 { font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1; }
    .hero-copy p { font-size: 1rem; max-width: 58rem; }
    .kicker {
      color: var(--gold);
      font-size: 0.8rem;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    form {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 0.92rem;
    }
    label.full { grid-column: 1 / -1; }
    input[type="text"], input[type="password"] {
      width: 100%;
      background: #0c0f12;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--text);
      padding: 12px 14px;
      font: inherit;
    }
    .check {
      align-items: center;
      display: inline-flex;
      gap: 8px;
      padding-top: 12px;
    }
    .actions {
      align-items: center;
      display: flex;
      gap: 12px;
      grid-column: 1 / -1;
      flex-wrap: wrap;
    }
    button, .link-button {
      appearance: none;
      background: var(--teal);
      border: 1px solid transparent;
      border-radius: 8px;
      color: #061011;
      cursor: pointer;
      font: inherit;
      font-weight: 800;
      padding: 11px 16px;
      text-decoration: none;
    }
    button.secondary, .link-button.secondary {
      background: transparent;
      border-color: var(--line);
      color: var(--text);
    }
    .status { margin-bottom: 18px; min-height: 1.5rem; }
    .error { color: #ff9f95; }
    .results {
      display: none;
      gap: 18px;
    }
    .results.ready { display: grid; }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .summary {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      gap: 6px;
    }
    .card strong { font-size: 1.5rem; line-height: 1; }
    .edge-wrap {
      display: grid;
      gap: 8px;
    }
    .edge-bar {
      position: relative;
      display: grid;
      gap: 2px;
      grid-template-columns: repeat(42, minmax(0, 1fr));
      min-height: 72px;
      background: #0a0d10;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      padding: 6px;
    }
    .edge-segment { border-radius: 2px; }
    .edge-labels, .legend {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 0.88rem;
    }
    .legend { justify-content: flex-start; }
    .legend-chip {
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      display: inline-flex;
      gap: 8px;
      padding: 5px 10px;
    }
    .legend-chip strong {
      align-items: center;
      display: inline-flex;
      justify-content: center;
      min-width: 1.5rem;
    }
    .viewer-panel {
      display: grid;
      gap: 14px;
    }
    iframe {
      width: 100%;
      min-height: 760px;
      background: #040608;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .columns {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .stack {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      gap: 10px;
    }
    .stack h3 { font-size: 1rem; }
    .stack-list { display: grid; gap: 8px; }
    .stack-row {
      display: grid;
      grid-template-columns: minmax(72px, auto) minmax(0, 1fr);
      gap: 8px 12px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }
    .stack-row strong { display: block; }
    .boxscores {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .boxscore-side {
      display: grid;
      gap: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .boxscore-row {
      display: grid;
      gap: 6px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }
    .meta {
      font-size: 0.9rem;
      line-height: 1.45;
    }
    @media (max-width: 980px) {
      form, .summary, .columns, .boxscores { grid-template-columns: 1fr; }
      iframe { min-height: 560px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-copy">
        <p class="kicker">Standalone Replay Review</p>
        <h1>Review one Ballchasing replay without the full site stack.</h1>
        <p>Paste a Ballchasing replay id or URL, add your Ballchasing API token, and this local tool will download the replay, run the 60 Hz parse, build the review cards, and open the native 3D viewer. No GPT, no paid stream APIs, no hosted backend.</p>
      </div>
      <form id="review-form">
        <label class="full">
          Replay URL or replay id
          <input id="replay-input" type="text" placeholder="https://ballchasing.com/replay/... or 92aa7211-d35d-4b3c-b93f-8a9faf21ac24" required>
        </label>
        <label>
          Ballchasing API token
          <input id="token-input" type="password" placeholder="Paste token here the first time" autocomplete="off">
        </label>
        <label class="check">
          <input id="force-refresh" type="checkbox">
          Force re-download and re-parse
        </label>
        <div class="actions">
          <button type="submit">Prepare review</button>
          <span class="meta">First run downloads the replay and builds a tiny standalone workspace under <code>output/standalone-review/&lt;replay-id&gt;</code>.</span>
        </div>
      </form>
    </section>

    <div id="status" class="status note">Ready.</div>

    <section id="results" class="results">
      <div class="toolbar">
        <a id="open-native" class="link-button" href="#" target="_blank" rel="noreferrer">Open full 3D viewer</a>
        <a id="download-replay" class="link-button secondary" href="#" target="_blank" rel="noreferrer">Download local replay</a>
        <a id="json-link" class="link-button secondary" href="#" target="_blank" rel="noreferrer">Open review JSON</a>
      </div>

      <div id="summary" class="summary"></div>

      <div class="edge-wrap panel">
        <h2>Win edge</h2>
        <div id="edge-bar" class="edge-bar"></div>
        <div class="edge-labels">
          <span>Orange edge</span>
          <span>Blue edge</span>
        </div>
        <div class="legend">
          <span class="legend-chip"><strong>Vol</strong><span>Total movement in win probability.</span></span>
          <span class="legend-chip"><strong>TO</strong><span>Turnover swing.</span></span>
          <span class="legend-chip"><strong>PR</strong><span>Pressure swing.</span></span>
          <span class="legend-chip"><strong>G</strong><span>Goal swing.</span></span>
        </div>
      </div>

      <section class="viewer-panel panel">
        <div>
          <p class="kicker">Native 3D Viewer</p>
          <h2 id="viewer-title">Replay viewer</h2>
          <p id="viewer-meta" class="meta"></p>
        </div>
        <iframe id="native-frame" title="Standalone native replay viewer" allow="fullscreen; autoplay; clipboard-write"></iframe>
      </section>

      <div class="columns">
        <section class="stack">
          <h3>Turning points</h3>
          <div id="turning-points" class="stack-list"></div>
        </section>
        <section class="stack">
          <h3>Blunders</h3>
          <div id="blunders" class="stack-list"></div>
        </section>
        <section class="stack">
          <h3>Best plays</h3>
          <div id="plays" class="stack-list"></div>
        </section>
      </div>

      <div class="columns">
        <section class="stack">
          <h3>Clutch plays</h3>
          <div id="clutch-plays" class="stack-list"></div>
        </section>
        <section class="stack">
          <h3>Player impact</h3>
          <div id="player-impact" class="stack-list"></div>
        </section>
        <section class="stack">
          <h3>Model reasons</h3>
          <div id="model-reasons" class="stack-list"></div>
        </section>
      </div>

      <div class="boxscores">
        <section class="boxscore-side">
          <h3 id="blue-box-title">Blue</h3>
          <div id="blue-box"></div>
        </section>
        <section class="boxscore-side">
          <h3 id="orange-box-title">Orange</h3>
          <div id="orange-box"></div>
        </section>
      </div>
    </section>
  </main>

  <script>
    const statusNode = document.getElementById('status');
    const resultsNode = document.getElementById('results');
    const form = document.getElementById('review-form');

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      return Number(value).toFixed(digits);
    }

    function fmtPercent(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      return `${(Number(value) * 100).toFixed(1)}%`;
    }

    function fmtSwingPoints(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      const numeric = Number(value);
      const prefix = numeric > 0 ? '+' : '';
      return `${prefix}${numeric.toFixed(1)} pts`;
    }

    function buildSummary(viewer) {
      const replay = viewer.replay || {};
      const evalData = viewer.eval || {};
      const impact = viewer.player_impact || [];
      const cards = [
        ['Blue win', fmtPercent(evalData.final_blue_probability), `Start ${fmtPercent(evalData.base_blue_probability)}`],
        ['Orange win', evalData.final_blue_probability === null || evalData.final_blue_probability === undefined ? 'n/a' : fmtPercent(1 - Number(evalData.final_blue_probability)), 'Live edge from replay events'],
        ['Volatility', fmtSwingPoints(evalData.volatility_points), `${(evalData.plays || []).length + (evalData.blunders || []).length} major swings`],
        ['Swing count', fmtNumber(evalData.swing_count, 0), evalData.largest_swing ? `${fmtSwingPoints(evalData.largest_swing.swing_points)} at ${fmtNumber(evalData.largest_swing.t, 1)}s` : 'No swing registered'],
        ['Largest blunder', (evalData.blunders || [])[0]?.player_name || 'None yet', (evalData.blunders || [])[0] ? `${fmtSwingPoints(evalData.blunders[0].swing_points)} at ${fmtNumber(evalData.blunders[0].t, 1)}s` : 'No major blunder flagged'],
        ['Clutch play', (evalData.clutch_plays || [])[0]?.player_name || 'None yet', (evalData.clutch_plays || [])[0] ? `${fmtSwingPoints(evalData.clutch_plays[0].swing_points)} at ${fmtNumber(evalData.clutch_plays[0].t, 1)}s` : 'No late-game dagger flagged'],
        ['Impact leader', impact[0]?.player_name || 'None yet', impact[0] ? `${impact[0].goals || 0} G, ${impact[0].positive_swings || 0} positive swings` : 'Waiting for impact breakdown'],
        ['Scoreline', `${replay.blue_team_name || 'Blue'} ${replay.blue_goals ?? 0} - ${replay.orange_goals ?? 0} ${replay.orange_team_name || 'Orange'}`, replay.map_code || 'Map pending'],
      ];
      return cards.map(([label, value, note]) => `<article class="card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><em>${escapeHtml(note)}</em></article>`).join('');
    }

    function buildEdgeBar(edge) {
      const segments = edge?.segments || [];
      return segments.map((segment) => {
        const intensity = Math.abs(Number(segment.blue_edge || 0)) * 1.9 + 0.18;
        const color = Number(segment.blue_edge || 0) >= 0
          ? `rgba(19, 168, 154, ${Math.min(1, intensity)})`
          : `rgba(217, 86, 77, ${Math.min(1, intensity)})`;
        return `<span class="edge-segment" title="${escapeHtml(`${segment.end_t}s | Blue ${fmtPercent(segment.blue_probability)}`)}" style="background:${color}"></span>`;
      }).join('');
    }

    function stackRows(rows, formatter) {
      if (!rows?.length) return '<div class="stack-row"><strong>No data yet.</strong></div>';
      return rows.map((row) => formatter(row)).join('');
    }

    function fillBoxscores(players, blueName, orangeName) {
      const blue = [];
      const orange = [];
      for (const player of players || []) {
        const side = String(player.side || '').toLowerCase();
        if (side === 'orange') orange.push(player);
        else blue.push(player);
      }
      document.getElementById('blue-box-title').textContent = blueName || 'Blue';
      document.getElementById('orange-box-title').textContent = orangeName || 'Orange';
      document.getElementById('blue-box').innerHTML = stackRows(blue, (player) => `<div class="boxscore-row"><strong>${escapeHtml(player.player_name || 'Unknown')}</strong><span>${escapeHtml(player.car_name || player.car_family || 'Body pending')}</span><span>${player.goals ?? 0} G, ${player.assists ?? 0} A, ${player.saves ?? 0} S, ${player.score ?? '-'} score</span></div>`);
      document.getElementById('orange-box').innerHTML = stackRows(orange, (player) => `<div class="boxscore-row"><strong>${escapeHtml(player.player_name || 'Unknown')}</strong><span>${escapeHtml(player.car_name || player.car_family || 'Body pending')}</span><span>${player.goals ?? 0} G, ${player.assists ?? 0} A, ${player.saves ?? 0} S, ${player.score ?? '-'} score</span></div>`);
    }

    function renderReview(payload) {
      const viewer = payload.viewer || {};
      const replay = viewer.replay || {};
      const evalData = viewer.eval || {};
      const prediction = (viewer.predictions || [])[0] || {};

      document.getElementById('summary').innerHTML = buildSummary(viewer);
      document.getElementById('edge-bar').innerHTML = buildEdgeBar(viewer.win_edge || {});
      document.getElementById('viewer-title').textContent = replay.title || payload.replay_id;
      document.getElementById('viewer-meta').textContent = `${replay.blue_team_name || 'Blue'} vs ${replay.orange_team_name || 'Orange'} | ${replay.map_code || 'Map pending'} | 60 Hz native viewer`;
      document.getElementById('native-frame').src = `${payload.native_viewer_url}${encodeURIComponent(window.location.origin)}`;
      document.getElementById('open-native').href = document.getElementById('native-frame').src;
      document.getElementById('download-replay').href = payload.file_url;
      document.getElementById('json-link').href = payload.json_url;

      document.getElementById('turning-points').innerHTML = stackRows((viewer.timeline?.turning_points || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || row.event_type || 'Moment')}</strong><em>${escapeHtml(row.event_type || 'event')}</em></div></div>`);
      document.getElementById('blunders').innerHTML = stackRows((evalData.blunders || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || 'Blunder')}</strong><em>${escapeHtml(fmtSwingPoints(row.swing_points))}</em></div></div>`);
      document.getElementById('plays').innerHTML = stackRows((evalData.plays || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || 'Play')}</strong><em>${escapeHtml(fmtSwingPoints(row.swing_points))}</em></div></div>`);
      document.getElementById('clutch-plays').innerHTML = stackRows((evalData.clutch_plays || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || 'Clutch play')}</strong><em>${escapeHtml(fmtSwingPoints(row.swing_points))}</em></div></div>`);
      document.getElementById('player-impact').innerHTML = stackRows((viewer.player_impact || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.net_impact, 3)}</span><div><strong>${escapeHtml(row.player_name || 'Player')}</strong><em>${row.goals || 0} G, ${row.touches || 0} touches, ${row.positive_swings || 0}/${row.negative_swings || 0} swings</em></div></div>`);
      document.getElementById('model-reasons').innerHTML = stackRows((prediction.reasons || []).slice(0, 8), (row) => `<div class="stack-row"><span>${escapeHtml(row.feature || row.name || 'Reason')}</span><div><strong>${escapeHtml(fmtNumber(row.contribution ?? row.value_z ?? 0, 3))}</strong><em>${escapeHtml(fmtNumber(row.value_z ?? row.value ?? 0, 3))}</em></div></div>`);
      fillBoxscores(replay.players || [], replay.blue_team_name, replay.orange_team_name);
      resultsNode.classList.add('ready');
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      statusNode.textContent = 'Preparing replay review. Download and parse can take a minute on the first run.';
      statusNode.className = 'status note';
      resultsNode.classList.remove('ready');
      try {
        const response = await fetch('/api/review', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            replay_input: document.getElementById('replay-input').value.trim(),
            ballchasing_api_token: document.getElementById('token-input').value.trim() || null,
            force_refresh: document.getElementById('force-refresh').checked,
          }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Standalone review failed.');
        renderReview(payload);
        statusNode.textContent = `Prepared ${payload.replay_id}. Workspace: ${payload.workspace_dir}`;
        statusNode.className = 'status note';
      } catch (error) {
        statusNode.textContent = error.message || 'Standalone review failed.';
        statusNode.className = 'status error';
      }
    });
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return homepage_html()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "title": APP_TITLE}


@app.post("/api/review")
def prepare_review(payload: ReviewRequest) -> dict[str, Any]:
    replay_id = normalize_replay_id(payload.replay_input)
    review = local_review_payload(
        replay_id,
        replay_token=(payload.ballchasing_api_token or "").strip() or None,
        force_refresh=bool(payload.force_refresh),
    )
    return review


@app.get("/library/replays/{replay_id}/viewer")
def standalone_viewer(replay_id: str) -> dict[str, Any]:
    replay_id = normalize_replay_id(replay_id)
    paths = workspace_paths(replay_id)
    if not paths["serving_db"].exists():
        raise HTTPException(status_code=404, detail="Replay has not been prepared in the standalone workspace yet.")
    with settings_override(serving_db=paths["serving_db"], replay_download_dir=paths["download_dir"]):
        with duckdb.connect(str(paths["serving_db"])) as con:
            viewer = replay_viewer(con, replay_id)
    if viewer is None:
        raise HTTPException(status_code=404, detail="Replay review is unavailable in the standalone workspace.")
    return viewer


@app.get("/library/replays/{replay_id}/file")
def standalone_file(replay_id: str) -> FileResponse:
    replay_id = normalize_replay_id(replay_id)
    replay_file = workspace_paths(replay_id)["replay_file"]
    if not replay_file.exists():
        raise HTTPException(status_code=404, detail="Local replay file is not present in the standalone workspace.")
    return FileResponse(path=replay_file, media_type="application/octet-stream", filename=f"{replay_id}.replay")


@app.get("/library/replays/{replay_id}/native-viewer")
def standalone_native_viewer(
    request: Request,
    replay_id: str,
    hz: int = 60,
    max_frames: int = 24000,
    start_frame: int = 0,
) -> Response:
    replay_id = normalize_replay_id(replay_id)
    paths = workspace_paths(replay_id)
    if not paths["serving_db"].exists():
        raise HTTPException(status_code=404, detail="Replay has not been prepared in the standalone workspace yet.")

    cached_payload = load_native_viewer_payload_cache(
        replay_id,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=PARSER_VERSION,
    )
    if cached_payload:
        return native_viewer_gzip_response(cached_payload, request)

    with settings_override(serving_db=paths["serving_db"], replay_download_dir=paths["download_dir"]):
        with duckdb.connect(str(paths["serving_db"])) as con:
            replay = get_library_replay(con, replay_id)
            events = parsed_events(con, replay_id)
        if replay is None:
            raise HTTPException(status_code=404, detail="Replay metadata is missing from the standalone workspace.")
        try:
            parsed_payload = load_parsed_replay_frames(
                replay_id,
                hz=hz,
                max_frames=max_frames,
                start_frame=start_frame,
                serving_db=paths["serving_db"],
                local_file_path=replay.get("local_file_path"),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReplayParseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = build_native_viewer_payload(replay, parsed_payload, events)
        encoded = store_native_viewer_payload_cache(
            replay_id,
            payload,
            hz=hz,
            max_frames=max_frames,
            start_frame=start_frame,
            parser_version=PARSER_VERSION,
        )
        return native_viewer_gzip_response(encoded, request)


if __name__ == "__main__":
    uvicorn.run("standalone_replay_review:app", host="127.0.0.1", port=8010, reload=False)
