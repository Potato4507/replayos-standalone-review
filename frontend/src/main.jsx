import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import {
  BOOST_PAD_LAYOUT as DEFAULT_BOOST_PAD_LAYOUT,
  buildArenaModel,
  buildBallModel,
  buildBoostPadModel,
  buildCarModel,
  carQuaternionFromTelemetry,
  loadViewerAssets,
  sceneVectorFromRl,
} from './viewerAssets';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000';

async function fetchJson(path, options = {}) {
  const { headers, ...rest } = options;
  const response = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...(headers || {}),
    },
    ...rest,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

function formatDate(value) {
  if (!value) return 'Date pending';
  let normalized = value;
  if (typeof value === 'number') {
    normalized = value < 1_000_000_000_000 ? value * 1000 : value;
  }
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatAgo(value) {
  if (!value) return 'not refreshed yet';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function formatClock(totalSeconds) {
  if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(Number(totalSeconds))) return 'n/a';
  const whole = Math.max(0, Math.round(Number(totalSeconds)));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = whole % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function formatVideoWindow(video) {
  if (!video || video.segment_start_seconds === null || video.segment_start_seconds === undefined) return null;
  if (video.segment_end_seconds === null || video.segment_end_seconds === undefined) return `${formatClock(video.segment_start_seconds)} onward`;
  return `${formatClock(video.segment_start_seconds)} - ${formatClock(video.segment_end_seconds)}`;
}

function videoKindLabel(video) {
  if (!video) return 'Replay video';
  if (video.video_kind === 'vod_segment') return 'Replay clip from tournament VOD';
  if (video.video_kind === 'vod_estimate') return 'Estimated game jump inside full series VOD';
  return 'Full match video';
}

function number(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
  return Number(value).toFixed(digits);
}

function signedNumber(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
  const raw = Number(value);
  const prefix = raw > 0 ? '+' : '';
  return `${prefix}${raw.toFixed(digits)}`;
}

function percent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function metricValue(value, mode = 'number') {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
  if (mode === 'percent') return percent(value);
  if (mode === 'decimal') return number(value, 3);
  return number(value, 0);
}

function scorelineText(match) {
  if (match?.series_score_a === null || match?.series_score_a === undefined || match?.series_score_b === null || match?.series_score_b === undefined) {
    return match?.status === 'live' ? 'live' : 'scheduled';
  }
  return `${match.series_score_a} - ${match.series_score_b}`;
}

function replayScoreText(match) {
  if (match?.blue_goals === null || match?.blue_goals === undefined || match?.orange_goals === null || match?.orange_goals === undefined) {
    return 'vs';
  }
  return `${match.blue_goals}:${match.orange_goals}`;
}

function recordText(team) {
  const wins = team?.wins ?? 0;
  const losses = team?.losses ?? 0;
  return `${wins}-${losses}`;
}

function powerFactors(team) {
  const factors = [
    ['Standings', team?.standings_score],
    ['Dominance', team?.dominance_score],
    ['Form', team?.form_score],
    ['Schedule', team?.schedule_score],
    ['Telemetry', team?.quality_score],
    ['Tier', team?.tier_score],
  ];
  return factors.filter(([, value]) => Math.abs(Number(value) || 0) >= 0.1).slice(0, 4);
}

function impactTone(item) {
  if (!item) return 'minor';
  return item.severity || 'minor';
}

const REVIEW_TIMELINE_LEGEND = [
  { type: 'goal', short: 'G', label: 'Goal' },
  { type: 'save', short: 'SV', label: 'Save' },
  { type: 'shot', short: 'S', label: 'Shot' },
  { type: 'turnover', short: 'TO', label: 'Turnover' },
  { type: 'pressure_phase', short: 'PR', label: 'Pressure' },
  { type: 'demo', short: 'D', label: 'Demo' },
];

function reviewMarkerMeta(eventType) {
  const normalized = String(eventType || '').trim().toLowerCase();
  if (normalized === 'goal') return { tone: 'goal', short: 'G', label: 'Goal' };
  if (normalized === 'save') return { tone: 'save', short: 'SV', label: 'Save' };
  if (normalized === 'shot') return { tone: 'shot', short: 'S', label: 'Shot' };
  if (normalized === 'turnover') return { tone: 'turnover', short: 'TO', label: 'Turnover' };
  if (normalized === 'pressure_phase' || normalized === 'pressure') return { tone: 'pressure', short: 'PR', label: 'Pressure' };
  if (normalized === 'demo') return { tone: 'demo', short: 'D', label: 'Demo' };
  if (normalized === 'touch') return { tone: 'touch', short: 'T', label: 'Touch' };
  return { tone: 'event', short: 'E', label: labelFromSlug(normalized) || 'Event' };
}

function swingPointsText(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
  const numeric = Number(value);
  const prefix = numeric > 0 ? '+' : '';
  return `${prefix}${numeric.toFixed(digits)} pts`;
}

function impactSwingText(item) {
  if (!item) return 'n/a';
  if (item.swing_points !== null && item.swing_points !== undefined) return swingPointsText(item.swing_points, 1);
  if (item.probability_swing !== null && item.probability_swing !== undefined) return swingPointsText(Number(item.probability_swing) * 100, 1);
  return signedNumber(item.impact, 3);
}

function playerImpactSummaryText(item) {
  if (!item) return 'Waiting for player impact breakdown';
  return `${item.goals || 0} G, ${item.positive_swings || 0} positive swings, ${swingPointsText(item.net_swing_points, 1)} net`;
}

function impactWindowText(item) {
  if (!item) return null;
  if (item.before_blue_probability === null || item.before_blue_probability === undefined) return null;
  if (item.after_blue_probability === null || item.after_blue_probability === undefined) return null;
  return `Blue ${percent(item.before_blue_probability)} -> ${percent(item.after_blue_probability)}`;
}

function shortText(value, max = 44) {
  if (!value) return 'Untitled';
  if (value.length <= max) return value;
  return `${value.slice(0, max - 3)}...`;
}

function labelFromSlug(value) {
  if (!value) return null;
  return String(value)
    .split(/[_-\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function carBodyLabel(item) {
  if (item?.car_name) return item.car_name;
  if (item?.car_family) return `${labelFromSlug(item.car_family)} body`;
  return 'Body pending';
}

const ANALYST_PROMPTS = [
  'Which players had the biggest impact in this replay?',
  'Where did the momentum swing in this replay?',
  'Which teams look most fragile under pressure?',
  'Give me a useful coverage summary.',
];

function reviewNote(review) {
  if (!review) return 'Review pending';
  if (review.largest_blunder?.player_name) return `${shortText(review.largest_blunder.player_name, 18)} blunder`;
  if (review.best_play?.player_name) return `${shortText(review.best_play.player_name, 18)} big play`;
  if (review.turning_point?.label) return shortText(review.turning_point.label, 28);
  return 'Low-swing replay';
}

function trackerProfileUrl(platform, platformPlayerId, playerName) {
  const normalized = String(platform || '').trim().toLowerCase();
  const profileId = platformPlayerId ? encodeURIComponent(platformPlayerId) : '';
  const fallbackName = encodeURIComponent(playerName || platformPlayerId || '');
  const platformMap = {
    steam: 'steam',
    epic: 'epic',
    xbox: 'xbl',
    xbl: 'xbl',
    xboxlive: 'xbl',
    psn: 'psn',
    playstation: 'psn',
    ps4: 'psn',
    ps5: 'psn',
  };
  const trackerPlatform = platformMap[normalized];
  if (trackerPlatform && profileId) {
    return `https://tracker.gg/rocket-league/profile/${trackerPlatform}/${profileId}/overview`;
  }
  if (fallbackName) {
    return `https://tracker.gg/rocket-league/search?query=${fallbackName}`;
  }
  return null;
}

function preferredReplayStartFrameFromEval(replayEval, hz = 60) {
  const actionTimes = [
    ...(replayEval?.plays || []),
    ...(replayEval?.blunders || []),
    ...(replayEval?.best_plays || []),
    ...(replayEval?.turning_points || []),
  ]
    .filter((item) => {
      const type = String(item?.event_type || item?.type || '').toLowerCase();
      const label = String(item?.label || '').toLowerCase();
      return type !== 'goal' && !label.includes('reset');
    })
    .map((item) => Number(item?.t))
    .filter((value) => Number.isFinite(value) && value > 0);

  if (actionTimes.length) {
    const actionMoment = Math.max(9, Math.min(...actionTimes) + 1.25);
    return Math.max(0, Math.round(actionMoment * hz));
  }

  const numericTimes = [
    replayEval?.best_play?.t,
    replayEval?.turning_point?.t,
    replayEval?.largest_swing?.t,
    replayEval?.largest_blunder?.t,
    ...(replayEval?.plays || []).map((item) => item?.t),
    ...(replayEval?.blunders || []).map((item) => item?.t),
    ...(replayEval?.best_plays || []).map((item) => item?.t),
    ...(replayEval?.turning_points || []).map((item) => item?.t),
  ]
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value > 0);

  if (!numericTimes.length) return null;
  const earliestMoment = Math.max(9, Math.min(...numericTimes) - 2.5);
  return Math.max(0, Math.round(earliestMoment * hz));
}

function groupPlayers(players) {
  const grouped = { blue: [], orange: [] };
  (players || []).forEach((player) => {
    if (player.side === 'orange') grouped.orange.push(player);
    else grouped.blue.push(player);
  });
  return grouped;
}

const VIEWER_PLAYBACK_HZ = 60;
const VIEWER_CHUNK_FRAMES = 1800;
const REPLAY_LIBRARY_PAGE_SIZE = 60;
const MAX_VIEWER_CACHE_ENTRIES = 14;
const replayFrameChunkCache = new Map();
const replayFrameChunkInflight = new Map();
const MAX_FRAME_CHUNK_CACHE_ENTRIES = 18;

function replayFrameChunkKey(replayId, startFrame, hz = VIEWER_PLAYBACK_HZ, maxFrames = VIEWER_CHUNK_FRAMES) {
  return `${replayId || ''}:${hz}:${maxFrames}:${Math.max(0, Number(startFrame) || 0)}`;
}

function pruneReplayFrameCache() {
  while (replayFrameChunkCache.size > MAX_FRAME_CHUNK_CACHE_ENTRIES) {
    const firstKey = replayFrameChunkCache.keys().next().value;
    replayFrameChunkCache.delete(firstKey);
  }
}

async function loadReplayFrameChunk(replayId, startFrame = 0, options = {}) {
  const hz = Number(options.hz || VIEWER_PLAYBACK_HZ);
  const maxFrames = Number(options.maxFrames || VIEWER_CHUNK_FRAMES);
  const key = replayFrameChunkKey(replayId, startFrame, hz, maxFrames);
  if (replayFrameChunkCache.has(key)) {
    return replayFrameChunkCache.get(key);
  }
  if (replayFrameChunkInflight.has(key)) {
    return replayFrameChunkInflight.get(key);
  }
  const boundedStart = Math.max(0, Number(startFrame) || 0);
  const promise = fetchJson(`/library/replays/${encodeURIComponent(replayId)}/frames?hz=${hz}&max_frames=${maxFrames}&start_frame=${boundedStart}`, {
    signal: options.signal,
    headers: { 'X-ReplayOS-Frame-Request': `${replayId}:${boundedStart}` },
  })
    .then((payload) => {
      const returnedId = String(payload?.replay_id || '');
      if (returnedId && returnedId !== String(replayId)) {
        throw new Error(`Frame payload replay mismatch: requested ${replayId}, got ${returnedId}`);
      }
      replayFrameChunkCache.set(key, payload);
      pruneReplayFrameCache();
      replayFrameChunkInflight.delete(key);
      return payload;
    })
    .catch((error) => {
      replayFrameChunkInflight.delete(key);
      throw error;
    });
  replayFrameChunkInflight.set(key, promise);
  return promise;
}

function prefetchReplayFrameChunk(replayId, payload) {
  if (!replayId || !payload) return;
  const nextStart = Number(payload.start_frame || 0) + Number(payload.frame_count || 0);
  const totalFrameCount = Number(payload.total_frame_count || 0);
  if (!totalFrameCount || nextStart >= totalFrameCount) return;
  loadReplayFrameChunk(replayId, nextStart).catch(() => {});
}

function useReplayFrames(replayId, startFrame = 0) {
  const [framesPayload, setFramesPayload] = useState(null);
  const [frameError, setFrameError] = useState('');
  const [frameLoading, setFrameLoading] = useState(false);

  useEffect(() => {
    if (!replayId) {
      setFramesPayload(null);
      setFrameError('Missing replay id.');
      return undefined;
    }
    const controller = new AbortController();
    const key = replayFrameChunkKey(replayId, startFrame);
    const cached = replayFrameChunkCache.get(key);
    let cancelled = false;
    setFramesPayload(cached || null);
    setFrameLoading(!cached);
    setFrameError('');
    if (cached) {
      prefetchReplayFrameChunk(replayId, cached);
      return () => {
        cancelled = true;
        controller.abort();
      };
    }
    loadReplayFrameChunk(replayId, startFrame, { signal: controller.signal })
      .then((payload) => {
        if (cancelled || controller.signal.aborted) return;
        setFramesPayload(payload);
        prefetchReplayFrameChunk(replayId, payload);
      })
      .catch((error) => {
        if (cancelled || controller.signal.aborted) return;
        setFramesPayload(null);
        setFrameError(error.message);
      })
      .finally(() => {
        if (!cancelled && !controller.signal.aborted) {
          setFrameLoading(false);
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [replayId, startFrame]);

  return { framesPayload, frameError, frameLoading };
}

function BallchasingSyncWidget({ status, onRefresh, onError }) {
  const [groupId, setGroupId] = useState('');
  const [count, setCount] = useState(12);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    const singlePinnedGroup = (status?.default_groups?.length || 0) === 1 && (status?.default_creators?.length || 0) === 0;
    setGroupId(singlePinnedGroup ? (status?.default_group || '') : '');
  }, [status?.default_group, status?.default_groups, status?.default_creators]);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setMessage('');
    try {
      const payload = await fetchJson('/sources/ballchasing/sync', {
        method: 'POST',
        body: JSON.stringify({
          group_id: groupId || null,
          count,
          download_files: true,
          fetch_details: true,
          force_download: false,
          parse_downloads: true,
        }),
      });
      setMessage(`Synced ${payload.seen}, downloaded ${payload.downloaded}, parsed ${payload.parsed}, failed ${payload.parse_failed}.`);
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="sync-widget" onSubmit={submit}>
      <div className="sync-head">
        <p className="kicker">Ballchasing Sync</p>
        <strong>{status?.token_configured ? 'Ready to pull replays' : 'Add BALLCHASING_API_TOKEN to enable downloads'}</strong>
      </div>
      <label>
        <span>Group id or creator URL</span>
        <input value={groupId} onChange={(event) => setGroupId(event.target.value)} placeholder="https://ballchasing.com/groups?creator=76561199225615730" />
      </label>
      <label>
        <span>Replay count</span>
        <input type="number" min="1" max="200" value={count} onChange={(event) => setCount(Number(event.target.value))} />
      </label>
      <button type="submit" disabled={busy || !status?.token_configured}>{busy ? 'Syncing...' : 'Download latest replays'}</button>
      <small>{message || status?.api_ping?.error || `Defaults: ${(status?.default_creators?.length || 0)} creator feeds and ${(status?.default_groups?.length || 0)} pinned groups. Downloads are parsed through Carball and cached into 60 Hz replay telemetry as the sync runs.`}</small>
    </form>
  );
}

function YouTubeSyncWidget({ status, selectedReplayId, onRefresh, onError }) {
  const [count, setCount] = useState(8);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const syncEnabled = !!status?.sync_enabled;
  const providerLabel = status?.provider === 'youtube_data_api'
    ? 'Using YouTube Data API'
    : status?.provider === 'yt_dlp_public'
      ? 'Using public YouTube search'
      : 'Video sync unavailable';

  async function syncBatch() {
    setBusy(true);
    setMessage('');
    try {
      const payload = await fetchJson('/sources/youtube/sync', {
        method: 'POST',
        body: JSON.stringify({ replay_id: null, limit: count }),
      });
      setMessage(`Linked ${payload.linked} videos across ${payload.replay_count} replays, including ${payload.segmented || 0} VOD clips.`);
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function syncReplay() {
    if (!selectedReplayId) return;
    setBusy(true);
    setMessage('');
    try {
      const payload = await fetchJson('/sources/youtube/sync', {
        method: 'POST',
        body: JSON.stringify({ replay_id: selectedReplayId, limit: 1 }),
      });
      setMessage(`Linked ${payload.linked} videos for the selected replay, including ${payload.segmented || 0} VOD clips.`);
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sync-widget youtube">
      <div className="sync-head">
        <p className="kicker">YouTube Sync</p>
        <strong>{providerLabel}</strong>
      </div>
      <label>
        <span>Replay search count</span>
        <input type="number" min="1" max="100" value={count} onChange={(event) => setCount(Number(event.target.value))} />
      </label>
      <div className="button-row">
        <button type="button" onClick={syncBatch} disabled={busy || !syncEnabled}>{busy ? 'Syncing...' : 'Sync match videos'}</button>
        <button type="button" onClick={syncReplay} disabled={busy || !syncEnabled || !selectedReplayId}>Sync selected replay</button>
      </div>
      <small>{message || 'Queries score team overlap, series overlap, Rocket League context, publish date alignment, and then try to slice long VODs by chapter timestamps into replay-sized clips. Google API keys are optional.'}</small>
    </div>
  );
}

function CarballBackfillWidget({ status, onRefresh, onError }) {
  const [count, setCount] = useState(24);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  async function refreshIndex() {
    setBusy(true);
    setMessage('');
    try {
      const payload = await fetchJson('/sources/carball/index', { method: 'POST' });
      setMessage(`Indexed ${payload.indexed_replays} replay files and found ${payload.orphan_local_replays} local-only files.`);
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function runBackfill() {
    setBusy(true);
    setMessage('');
    try {
      const payload = await fetchJson('/sources/carball/backfill', {
        method: 'POST',
        body: JSON.stringify({
          limit: count,
          force: false,
          refresh_index: true,
        }),
      });
      const before = payload.coverage_before?.named_team_replays || 0;
      const after = payload.coverage_after?.named_team_replays || 0;
      setMessage(`Parsed ${payload.parsed}, cached ${payload.cached}, failed ${payload.failed}. Named team coverage moved from ${before} to ${after}.`);
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sync-widget carball">
      <div className="sync-head">
        <p className="kicker">Carball Coverage</p>
        <strong>{status ? `${percent(status.coverage_rate || 0)} named-team coverage` : 'Checking local replay coverage'}</strong>
      </div>
      <div className="coverage-grid">
        <div>
          <span>Indexed</span>
          <strong>{status?.indexed_local_replays ?? '...'}</strong>
        </div>
        <div>
          <span>Named teams</span>
          <strong>{status?.named_team_replays ?? '...'}</strong>
        </div>
        <div>
          <span>Named players</span>
          <strong>{status?.named_player_replays ?? '...'}</strong>
        </div>
        <div>
          <span>Orphans</span>
          <strong>{status?.orphan_local_replays ?? '...'}</strong>
        </div>
      </div>
      <label>
        <span>Batch size</span>
        <input type="number" min="1" max="500" value={count} onChange={(event) => setCount(Number(event.target.value))} />
      </label>
      <div className="button-row">
        <button type="button" onClick={runBackfill} disabled={busy}>{busy ? 'Parsing...' : 'Expand name coverage'}</button>
        <button type="button" className="ghost-button" onClick={refreshIndex} disabled={busy}>Refresh replay index</button>
      </div>
      <small>{message || 'This scans the local replay library, picks the highest-value unparsed games first, and grows the named team and player corpus that powers records, rankings, and the 3D viewer.'}</small>
    </div>
  );
}

function MaintenanceWidget({ status, onRefresh, onError }) {
  const [parseLimit, setParseLimit] = useState(24);
  const [evalLimit, setEvalLimit] = useState(120);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!status?.carball?.batch_recommended) return;
    setParseLimit(Math.min(250, Math.max(24, status.carball.batch_recommended)));
  }, [status?.carball?.batch_recommended]);

  async function runMaintenance() {
    setBusy(true);
    setMessage('');
    try {
      const payload = await fetchJson('/sources/maintenance/run', {
        method: 'POST',
        body: JSON.stringify({
          refresh_index: true,
          backfill_names: true,
          backfill_eval: true,
          refresh_live: true,
          parse_limit: parseLimit,
          eval_limit: evalLimit,
          force_eval: false,
        }),
      });
      setMessage(`Indexed local files, parsed ${payload.carball?.parsed || 0}, cached ${payload.eval?.cached || 0}, computed ${payload.eval?.computed || 0}, and refreshed live coverage.`);
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="control-panel">
      <div className="panel-filter-head">
        <div>
          <p className="kicker">Pipeline Ops</p>
          <h3>Keep the library warm</h3>
        </div>
        <button type="button" onClick={runMaintenance} disabled={busy}>{busy ? 'Running...' : 'Run upkeep'}</button>
      </div>
      <div className="maintenance-grid">
        <div>
          <span>Parse backlog</span>
          <strong>{status?.health?.parse_backlog ?? '...'}</strong>
        </div>
        <div>
          <span>Review backlog</span>
          <strong>{status?.health?.review_backlog ?? '...'}</strong>
        </div>
        <div>
          <span>Live streams</span>
          <strong>{status?.live?.streams ?? '...'}</strong>
        </div>
        <div>
          <span>Last live sync</span>
          <strong>{formatAgo(status?.live?.last_run?.completed_at)}</strong>
        </div>
      </div>
      <div className="filter-row">
        <label>
          <span>Replay parse batch</span>
          <input type="number" min="1" max="500" value={parseLimit} onChange={(event) => setParseLimit(Number(event.target.value))} />
        </label>
        <label>
          <span>Eval batch</span>
          <input type="number" min="1" max="2000" value={evalLimit} onChange={(event) => setEvalLimit(Number(event.target.value))} />
        </label>
      </div>
      <small className="control-note">{message || `Replay reviews ready for ${status?.eval?.cached_replays ?? 0} replays. Missing ${status?.eval?.missing_replays ?? 0}.`}</small>
    </div>
  );
}

function ReplayFilters({
  search,
  onChangeSearch,
  parsedOnly,
  onChangeParsedOnly,
  reviewReady,
  onChangeReviewReady,
  sortMode,
  onChangeSortMode,
  resultCount,
  totalCount,
  loading,
  onRefresh,
}) {
  return (
    <div className="control-panel">
      <div className="panel-filter-head">
        <div>
          <p className="kicker">Replay Library</p>
          <h3>Find the games worth opening</h3>
        </div>
        <button type="button" onClick={onRefresh} disabled={loading}>{loading ? 'Refreshing...' : 'Refresh library'}</button>
      </div>
      <div className="filter-row wide">
        <label className="grow">
          <span>Search team or replay</span>
          <input value={search} onChange={(event) => onChangeSearch(event.target.value)} placeholder="NRG, Vitality, FaZe, replay id..." />
        </label>
        <label>
          <span>Sort shelf</span>
          <select value={sortMode} onChange={(event) => onChangeSortMode(event.target.value)}>
            <option value="recent">Newest first</option>
            <option value="series">By round / series</option>
          </select>
        </label>
      </div>
      <div className="checkbox-row">
        <label>
          <input type="checkbox" checked={parsedOnly} onChange={(event) => onChangeParsedOnly(event.target.checked)} />
          <span>3D-ready only</span>
        </label>
        <label>
          <input type="checkbox" checked={reviewReady} onChange={(event) => onChangeReviewReady(event.target.checked)} />
          <span>Eval-ready only</span>
        </label>
      </div>
      <small className="control-note">
        {loading ? 'Refreshing the filtered replay shelf...' : `${resultCount} loaded${totalCount ? ` of ${totalCount.toLocaleString()}` : ''} replay cards.`}
      </small>
    </div>
  );
}

function MatchTicker({ matches, onSelect, selectedReplayId }) {
  return (
    <div className="match-ticker">
      {matches.map((match) => (
        <button
          key={match.replay_id}
          className={`match-chip ${selectedReplayId === match.replay_id ? 'active' : ''}`}
          onClick={() => onSelect(match.replay_id)}
        >
          <div className="match-chip-score">
            <span>{shortText(match.blue_team_name, 18)}</span>
            <strong>{replayScoreText(match)}</strong>
            <span>{shortText(match.orange_team_name, 18)}</span>
          </div>
          <div className="match-chip-meta">
            <em>{match.review ? `Vol ${number(match.review.volatility, 2)}` : 'Review pending'}</em>
            <em>{match.review?.swing_count ? `${match.review.swing_count} swings` : 'No major swings yet'}</em>
            <em>{reviewNote(match.review)}</em>
          </div>
        </button>
      ))}
    </div>
  );
}

function pruneViewerCache(cache) {
  while (cache.size > MAX_VIEWER_CACHE_ENTRIES) {
    const firstKey = cache.keys().next().value;
    cache.delete(firstKey);
  }
}

function SeriesGrid({ series }) {
  return (
    <div className="series-grid">
      {series.map((item) => (
        <article className="series-card" key={item.group_id}>
          <p className="kicker">{item.kind_label || 'Series'}</p>
          <h3>{shortText(item.name, 54)}</h3>
          <div className="series-meta">
            <span>{Number(item.replay_count || item.direct_replays || item.indirect_replays || 0)} replays</span>
            {item.matchup_count > 1 ? <span>{item.matchup_count} matchups</span> : null}
            {item.matchup_name && item.matchup_name !== item.name ? <span>{shortText(item.matchup_name, 38)}</span> : null}
            <span>{formatDate(item.last_match_date || item.created_at)}</span>
          </div>
        </article>
      ))}
    </div>
  );
}

function LiveLeaderboardBoards({ boards }) {
  return (
    <div className="records-grid matchup-grid">
      {(boards || []).slice(0, 4).map((board) => (
        <div className="record-panel" key={board.board_key}>
          <div className="section-head">
            <div>
              <p className="kicker">This RLCS</p>
              <h3>{board.board_name}</h3>
            </div>
            <span className="section-note">{board.region}</span>
          </div>
          <div className="stack-list">
            {(board.items || []).slice(0, 6).map((item) => (
              <div className="stack-row" key={`${board.board_key}-${item.rank}-${item.team_name}`}>
                <span>#{item.rank}</span>
                <strong>{item.team_name}</strong>
                <em>{item.points} pts</em>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function Ladder({ items }) {
  return (
    <div className="ladder">
      {items.length ? items.map((team, index) => (
        <div className="ladder-row" key={`${team.team_name}-${index}`}>
          <span className="ladder-rank">#{index + 1}</span>
          <div className="ladder-main">
            <div className="ladder-headline">
              <div>
                <strong>{team.team_name}</strong>
                <em>
                  {team.standings_points ? `${team.standings_points} pts` : `${team.games} matches`} {team.standings_region ? `- ${team.standings_region}` : ''}
                </em>
              </div>
              <div className="ladder-score">
                <span>Power</span>
                <b>{number(team.rating ?? team.elo, 1)}</b>
              </div>
            </div>
            <div className="ladder-meta">
              <span>{recordText(team)} record</span>
              <span>Base {number(team.power_score ?? team.elo, 0)}</span>
              <span>SOS {number(team.strength_of_schedule, 0)}</span>
              <span>Form {signedNumber(team.recent_form, 1)}</span>
            </div>
            <div className="ladder-factors">
              {powerFactors(team).map(([label, value]) => (
                <div className="ladder-factor" key={`${team.team_name}-${label}`}>
                  <span>{label}</span>
                  <strong>{signedNumber(value, 1)}</strong>
                </div>
              ))}
            </div>
          </div>
        </div>
      )) : <div className="empty-state">Once public standings or parsed replays are available, the power board will rank teams by match results plus leaderboard points.</div>}
    </div>
  );
}

function PlayersBoard({ players }) {
  return (
    <div className="players-board">
      {players.map((player, index) => (
        <div className="player-row" key={`${player.player_name}-${player.platform_player_id || index}`}>
          <div className="player-row-main">
            <strong>{player.player_name}</strong>
            <span>{player.goals ?? 0} goals</span>
            <span>{player.replays} replays</span>
          </div>
          {trackerProfileUrl(player.platform, player.platform_player_id, player.player_name) ? (
            <a
              className="inline-link"
              href={trackerProfileUrl(player.platform, player.platform_player_id, player.player_name)}
              target="_blank"
              rel="noreferrer"
            >
              Tracker
            </a>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function StatMini({ label, value, note }) {
  return (
    <div className="stat-mini">
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <em>{note}</em> : null}
    </div>
  );
}

function RecordShelf({ kicker, title, items, mode = 'number' }) {
  return (
    <div className="record-panel">
      <div className="section-head">
        <div>
          <p className="kicker">{kicker}</p>
          <h3>{title}</h3>
        </div>
      </div>
      <div className="stack-list">
        {(items || []).length ? items.map((item, index) => (
          <div className="record-row" key={`${item.name}-${index}`}>
            <div className="record-copy">
              <strong>{item.name}</strong>
              <span>
                {item.games || 0} games
                {item.wins !== undefined && item.losses !== undefined ? ` - ${item.wins}-${item.losses}` : ''}
                {item.confidence !== undefined ? ` - ${Math.round(Number(item.confidence || 0) * 100)}% sample` : ''}
              </span>
            </div>
            <b>{metricValue(item.value, mode)}</b>
          </div>
        )) : <div className="empty-state">No record rows yet.</div>}
      </div>
    </div>
  );
}

function FrequencyBoard({ items, telemetryGames }) {
  return (
    <div className="frequency-board">
      <div className="section-head">
        <div>
          <p className="kicker">How Often</p>
          <h3>Event frequency</h3>
        </div>
        <span className="section-note">Parsed telemetry across {telemetryGames || 0} replays.</span>
      </div>
      <div className="stack-list">
        {(items || []).map((item) => (
          <div className="frequency-row" key={item.event_type}>
            <div className="frequency-copy">
              <strong>{item.label}</strong>
              <span>{item.total} total</span>
            </div>
            <b>{number(item.per_game, 2)}/match</b>
          </div>
        ))}
      </div>
    </div>
  );
}

function SearchPicker({ options, value, onChange, placeholder = 'Search...', emptyLabel = 'No matches yet.' }) {
  const uniqueOptions = useMemo(() => Array.from(new Set(options || [])), [options]);
  const [query, setQuery] = useState(value || '');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setQuery(value || '');
  }, [value]);

  const filteredOptions = useMemo(() => {
    const normalized = String(query || '').trim().toLowerCase();
    const source = uniqueOptions.filter(Boolean);
    if (!normalized) return source.slice(0, 14);
    const starts = [];
    const contains = [];
    source.forEach((option) => {
      const haystack = String(option).toLowerCase();
      if (haystack.startsWith(normalized)) {
        starts.push(option);
      } else if (haystack.includes(normalized)) {
        contains.push(option);
      }
    });
    return [...starts, ...contains].slice(0, 16);
  }, [query, uniqueOptions]);

  function commitSelection(option) {
    const nextValue = option || '';
    setQuery(nextValue);
    setOpen(false);
    if (nextValue && nextValue !== value) {
      onChange(nextValue);
    }
  }

  function handleBlur() {
    window.setTimeout(() => {
      const exact = uniqueOptions.find((option) => option.toLowerCase() === String(query || '').trim().toLowerCase());
      if (exact) {
        commitSelection(exact);
        return;
      }
      setQuery(value || '');
      setOpen(false);
    }, 120);
  }

  return (
    <div className="search-picker">
      <input
        type="search"
        value={query}
        placeholder={placeholder}
        onFocus={() => setOpen(true)}
        onBlur={handleBlur}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            const exact = uniqueOptions.find((option) => option.toLowerCase() === String(query || '').trim().toLowerCase());
            commitSelection(exact || filteredOptions[0] || value || '');
          }
          if (event.key === 'Escape') {
            setQuery(value || '');
            setOpen(false);
          }
        }}
      />
      {open ? (
        <div className="search-picker-menu">
          {filteredOptions.length ? filteredOptions.map((option) => (
            <button key={option} type="button" className="search-picker-option" onMouseDown={() => commitSelection(option)}>
              {option}
            </button>
          )) : <div className="search-picker-empty">{emptyLabel}</div>}
        </div>
      ) : null}
    </div>
  );
}

function TeamRecordPanel({ options, value, onChange, profile }) {
  const record = profile?.record;
  const historyNote = record?.has_history ? `${record?.games || 0} matches` : 'Known team, history still building from replay parses.';
  return (
    <div className="record-panel">
      <div className="panel-filter-head">
        <div>
          <p className="kicker">Team Record</p>
          <h3>All-time team profile</h3>
        </div>
        <SearchPicker options={options} value={value} onChange={onChange} placeholder="Find team..." />
      </div>
      {profile ? (
        <>
          <div className="stat-grid">
            <StatMini label="Record" value={`${record?.wins || 0}-${record?.losses || 0}`} note={historyNote} />
            <StatMini label="Win rate" value={percent(record?.win_rate)} note={`${record?.goals_for || 0} goals for`} />
            <StatMini label="Goal diff" value={metricValue(record?.goal_diff)} note={`${record?.goals_against || 0} against`} />
            <StatMini label="Streak" value={`${record?.current_streak || 0} ${record?.current_streak_type || 'n/a'}`} note={`Best ${record?.longest_win_streak || 0}W`} />
          </div>
          <FrequencyBoard items={profile.frequencies} telemetryGames={profile.telemetry_games} />
          <div className="record-subgrid">
            <div className="record-subpanel">
              <h3>Core lineup</h3>
              <div className="stack-list">
                {(profile.players || []).map((player, index) => (
                  <div className="stack-row" key={`${player.player_name}-${player.games}-${index}`}>
                    <span>{player.games} games</span>
                    <strong>{player.player_name}</strong>
                    <em>{player.goals} goals</em>
                  </div>
                ))}
              </div>
            </div>
            <div className="record-subpanel">
              <h3>Top rosters</h3>
              <div className="stack-list">
                {(profile.rosters || []).length ? (profile.rosters || []).map((roster, index) => (
                  <div className="stack-row" key={`${roster.roster_name}-${index}`}>
                    <span>{roster.games} games</span>
                    <strong>{shortText(roster.roster_name, 36)}</strong>
                    <em>{roster.wins} wins</em>
                  </div>
                )) : <div className="empty-state">Roster splits will appear as soon as enough named matches land.</div>}
              </div>
            </div>
            <div className="record-subpanel">
              <h3>Recent results</h3>
              <div className="stack-list">
                {(profile.recent_matches || []).length ? (profile.recent_matches || []).map((match) => (
                  <div className="stack-row" key={`${match.replay_id}-${match.match_date}`}>
                    <span>{formatDate(match.match_date)}</span>
                    <strong>{match.opponent_name}</strong>
                    <em>{match.scoreline} {match.result}</em>
                  </div>
                )) : <div className="empty-state">No completed match history for this team yet.</div>}
              </div>
            </div>
            <div className="record-subpanel">
              <h3>Live boards</h3>
              <div className="stack-list">
                {(profile.leaderboard_snapshot || []).length ? (profile.leaderboard_snapshot || []).map((row) => (
                  <div className="stack-row" key={`${row.board_name}-${row.region}-${row.rank}`}>
                    <span>{row.region}</span>
                    <strong>#{row.rank} {row.board_name}</strong>
                    <em>{row.points} pts</em>
                  </div>
                )) : <div className="empty-state">No public standings row cached for this team yet.</div>}
              </div>
            </div>
          </div>
        </>
      ) : <div className="empty-state">Pick a team to load its all-time record.</div>}
    </div>
  );
}

function PlayerRecordPanel({ options, value, onChange, profile }) {
  const record = profile?.record;
  const trackerUrl = profile?.tracker_profile_url || trackerProfileUrl(profile?.platform, profile?.platform_player_id, profile?.player_name);
  return (
    <div className="record-panel">
      <div className="panel-filter-head">
        <div>
          <p className="kicker">Player Record</p>
          <h3>All-time player profile</h3>
        </div>
        <div className="panel-actions">
          <SearchPicker options={options} value={value} onChange={onChange} placeholder="Find player..." />
          {trackerUrl ? <a className="inline-link inline-link-button" href={trackerUrl} target="_blank" rel="noreferrer">Open RL Tracker</a> : null}
        </div>
      </div>
      {profile ? (
        <>
          <div className="stat-grid">
            <StatMini label="Record" value={`${record?.wins || 0}-${record?.losses || 0}`} note={record?.has_history ? `${record?.games || 0} matches` : 'Known player, history still building from replay parses.'} />
            <StatMini label="Goals" value={metricValue(record?.goals)} note={`${number(record?.goals_per_game, 2)} per match`} />
            <StatMini label="Touches" value={metricValue(record?.touches)} note={`${number(record?.touches_per_game, 2)} per match`} />
            <StatMini label="Demos" value={metricValue(record?.demos)} note={`${number(record?.demos_per_game, 2)} per match`} />
          </div>
          <div className="tag-row compact">
            {profile?.platform ? <span className="tag muted">{profile.platform}</span> : null}
            {profile?.platform_player_id ? <span className="tag muted">{shortText(profile.platform_player_id, 26)}</span> : null}
            <span className="tag muted">Tracker MMR stays on Tracker because they do not expose a public Rocket League API.</span>
          </div>
          <FrequencyBoard items={profile.frequencies} telemetryGames={profile.telemetry_games} />
          <div className="record-subgrid">
            <div className="record-subpanel">
              <h3>Frequent teammates</h3>
              <div className="stack-list">
                {(profile.teammates || []).map((mate, index) => (
                  <div className="stack-row" key={`${mate.teammate_name}-${mate.games}-${index}`}>
                    <span>{mate.games} games</span>
                    <strong>{mate.teammate_name}</strong>
                    <em>{mate.wins} wins</em>
                  </div>
                ))}
              </div>
            </div>
            <div className="record-subpanel">
              <h3>Frequent opponents</h3>
              <div className="stack-list">
                {(profile.opponents || []).map((opponent, index) => (
                  <div className="stack-row" key={`${opponent.opponent_name}-${opponent.games}-${index}`}>
                    <span>{opponent.games} games</span>
                    <strong>{opponent.opponent_name}</strong>
                    <em>{opponent.wins} wins</em>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      ) : <div className="empty-state">Pick a player to load their all-time record.</div>}
    </div>
  );
}

function TeamHeadToHeadPanel({ options, left, right, onChangeLeft, onChangeRight, payload }) {
  const summary = payload?.summary;
  return (
    <div className="record-panel">
      <div className="panel-filter-head dual">
        <div>
          <p className="kicker">Team vs Team</p>
          <h3>Head to head</h3>
        </div>
        <div className="select-row">
          <SearchPicker options={options} value={left} onChange={onChangeLeft} placeholder="Left team..." />
          <SearchPicker options={options} value={right} onChange={onChangeRight} placeholder="Right team..." />
        </div>
      </div>
      {payload ? (
        <>
          <div className="stat-grid">
            <StatMini label="Meetings" value={metricValue(summary?.games)} note={formatDate(summary?.last_played)} />
            <StatMini label={payload.left_name} value={metricValue(summary?.left_wins)} note={`${summary?.left_goals || 0} goals`} />
            <StatMini label={payload.right_name} value={metricValue(summary?.right_wins)} note={`${summary?.right_goals || 0} goals`} />
            <StatMini label="Goal diff" value={metricValue(summary?.goal_diff)} note={`${payload.left_name} edge`} />
          </div>
          <div className="stack-list">
            {(payload.meetings || []).map((meeting) => (
              <div className="stack-row" key={`${meeting.replay_id}-${meeting.match_date}`}>
                <span>{formatDate(meeting.match_date)}</span>
                <strong>{meeting.scoreline}</strong>
                <em>{meeting.winner}</em>
              </div>
            ))}
          </div>
        </>
      ) : <div className="empty-state">Pick two teams to compare their record.</div>}
    </div>
  );
}

function PlayerHeadToHeadPanel({ options, left, right, onChangeLeft, onChangeRight, payload }) {
  const summary = payload?.summary;
  return (
    <div className="record-panel">
      <div className="panel-filter-head dual">
        <div>
          <p className="kicker">Player vs Player</p>
          <h3>Head to head</h3>
        </div>
        <div className="select-row">
          <SearchPicker options={options} value={left} onChange={onChangeLeft} placeholder="Left player..." />
          <SearchPicker options={options} value={right} onChange={onChangeRight} placeholder="Right player..." />
        </div>
      </div>
      {payload ? (
        <>
          <div className="stat-grid">
            <StatMini label="Shared" value={metricValue(summary?.shared_games)} note={`${summary?.teammate_games || 0} as teammates`} />
            <StatMini label="Opposed" value={`${summary?.left_wins || 0}-${summary?.right_wins || 0}`} note={`${summary?.opposed_games || 0} matches`} />
            <StatMini label={payload.left_name} value={metricValue(summary?.left_goals)} note={`${summary?.left_touches || 0} touches`} />
            <StatMini label={payload.right_name} value={metricValue(summary?.right_goals)} note={`${summary?.right_touches || 0} touches`} />
          </div>
          <div className="record-subgrid">
            <div className="record-subpanel">
              <h3>Opposed meetings</h3>
              <div className="stack-list">
                {(payload.opposed_meetings || []).map((meeting) => (
                  <div className="stack-row" key={`${meeting.replay_id}-${meeting.match_date}`}>
                    <span>{formatDate(meeting.match_date)}</span>
                    <strong>{meeting.left_score}-{meeting.right_score}</strong>
                    <em>{meeting.left_team_name} vs {meeting.right_team_name}</em>
                  </div>
                ))}
              </div>
            </div>
            <div className="record-subpanel">
              <h3>Shared rosters</h3>
              <div className="stack-list">
                {(payload.teammate_meetings || []).map((meeting) => (
                  <div className="stack-row" key={`${meeting.replay_id}-${meeting.match_date}`}>
                    <span>{formatDate(meeting.match_date)}</span>
                    <strong>{meeting.left_team_name}</strong>
                    <em>{meeting.left_score}-{meeting.right_score}</em>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      ) : <div className="empty-state">Pick two players to compare their history.</div>}
    </div>
  );
}

function LiveRadar({ live, liveStatus, onRefresh, onError }) {
  const [busy, setBusy] = useState(false);
  const featured = live?.featured_tournament;
  const streams = live?.streams || [];
  const matches = featured?.matches || [];
  const leaderboards = live?.leaderboards || [];

  async function refreshNow() {
    setBusy(true);
    try {
      await fetchJson('/sources/live/sync?force=true', { method: 'POST' });
      await onRefresh();
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id="live" className="content-band">
      <div className="section-head">
        <div>
          <p className="kicker">Live Radar</p>
          <h2>RLCS and pro-play watch desk</h2>
        </div>
        <div className="button-row live-actions">
          <span className="section-note">Refreshes every {live?.auto_refresh_seconds || liveStatus?.stream_cache_seconds || 60}s. Last sync {formatAgo(live?.refreshed_at || liveStatus?.last_run?.completed_at)}.</span>
          <button type="button" onClick={refreshNow} disabled={busy}>{busy ? 'Refreshing...' : 'Refresh live now'}</button>
        </div>
      </div>

      <div className="live-grid">
        <div className="live-stage">
          {featured ? (
            <>
              <div className="live-stage-head">
                <div>
                  <p className="kicker">{featured.status === 'live' ? 'Live Tournament' : 'Upcoming Tournament'}</p>
                  <h3>{featured.name}</h3>
                </div>
                <div className={`live-status ${featured.status}`}>{featured.status}</div>
              </div>
              <div className="live-meta-row">
                <span>{formatDate(featured.start_at)}</span>
                <span>{featured.location_name || 'Location pending'}</span>
                <span>{featured.prize_pool ? `$${featured.prize_pool}` : 'Prize pool pending'}</span>
              </div>
              <p className="live-summary">{featured.description || 'Schedule details are coming from the BLAST Rocket League tournament pages and cached locally for fast reloads.'}</p>
              {featured.watch_channels?.length ? (
                <div className="live-channel-row">
                  {(featured.watch_channels || []).map((channel) => (
                    <a key={`${channel.language}-${channel.channel_name}`} className="channel-pill" href={channel.channel_url} target="_blank" rel="noreferrer">
                      <span>{channel.language}</span>
                      <strong>{channel.channel_name}</strong>
                    </a>
                  ))}
                </div>
              ) : null}
              <div className="live-match-list">
                {matches.length ? matches.map((match, index) => (
                  <div className="live-match-row" key={`${match.match_label}-${index}`}>
                    <span>{match.scheduled_label}</span>
                    {match.match_url ? (
                      <a className="live-match-link" href={match.match_url} target="_blank" rel="noreferrer">
                        <strong>{match.team_a || 'TBD'} vs {match.team_b || 'TBD'}</strong>
                      </a>
                    ) : (
                      <strong>{match.team_a || 'TBD'} vs {match.team_b || 'TBD'}</strong>
                    )}
                    <em>{match.stage} {match.best_of}</em>
                    <b className={`live-score ${match.status}`}>{scorelineText(match)}</b>
                    {match.games?.length ? (
                      <div className="live-game-strip">
                        {match.games.slice(0, 7).map((game) => (
                          <span className="live-game-chip" key={`${match.match_label}-${game.label}`}>
                            {game.label.replace('Game ', 'G')} {game.score_a ?? '-'}:{game.score_b ?? '-'}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )) : <div className="empty-state">Match times will appear here when the tournament page publishes the series slate.</div>}
              </div>
            </>
          ) : (
            <div className="empty-state">No live or upcoming RLCS tournament is cached yet.</div>
          )}
        </div>

        <div className="stream-column">
          <div className="stream-column-head">
            <div>
              <p className="kicker">Live Streams</p>
              <h3>Official, co-stream, and pro-watch coverage</h3>
            </div>
          </div>
          <div className="leaderboard-stack">
            {leaderboards.slice(0, 3).map((board) => (
              <div className="leaderboard-card" key={board.board_key}>
                <div className="leaderboard-head">
                  <strong>{board.region}</strong>
                  <a href={board.source_url} target="_blank" rel="noreferrer">standings</a>
                </div>
                <div className="leaderboard-list">
                  {board.items.slice(0, 4).map((team, index) => (
                    <div className="leaderboard-row" key={`${board.board_key}-${team.rank}-${team.team_name || index}`}>
                      <span>#{team.rank}</span>
                      <strong>{team.team_name}</strong>
                      <em>{team.points} pts</em>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div className="stream-list">
            {streams.length ? streams.map((stream, index) => (
              <a className="stream-card" key={`${stream.channel_name}-${stream.stream_url || index}`} href={stream.stream_url} target="_blank" rel="noreferrer">
                <img src={stream.thumbnail_url} alt={stream.title} />
                <div>
                  <div className="tag-row compact">
                    <span className={`tag ${stream.classification === 'rlcs' ? '' : 'muted'}`}>{stream.classification}</span>
                    <span className="tag muted">{stream.platform}</span>
                    <span className="tag muted">{stream.viewer_count || 0} viewers</span>
                  </div>
                  <strong>{shortText(stream.title, 76)}</strong>
                  <span>{stream.author_name}</span>
                  <em>{stream.tournament_slug ? stream.tournament_slug.replaceAll('-', ' ') : 'rocket league live'}</em>
                </div>
              </a>
            )) : <div className="empty-state">No live RLCS or pro streams are cached yet.</div>}
          </div>
        </div>
      </div>
    </section>
  );
}

function WinEdgeBar({ edge }) {
  const segments = edge?.segments || [];
  const duration = Number(segments.length ? segments[segments.length - 1]?.end_t : 0);
  const highlights = (edge?.highlights || []).slice(0, 8);
  const lanes = [];
  const laneGapPercent = 4.2;
  const laidOutHighlights = highlights.map((highlight, index) => {
    const rawLeft = duration > 0 ? (Number(highlight.t || 0) / duration) * 100 : 0;
    const left = Math.max(0, Math.min(100, rawLeft));
    let lane = lanes.findIndex((lastLeft) => left - lastLeft >= laneGapPercent);
    if (lane === -1) {
      lane = lanes.length;
      lanes.push(left);
    } else {
      lanes[lane] = left;
    }
    return {
      ...highlight,
      _lane: lane,
      _left: left,
      _key: `${highlight.event_type}-${highlight.t}-${index}`,
    };
  });
  const laneCount = Math.max(1, lanes.length);
  return (
    <div className="edge-wrap">
      <div
        className="edge-bar"
        aria-label="Win edge timeline"
        style={{ '--edge-highlight-lanes': laneCount }}
      >
        {duration > 0 ? (
          <div
            className="edge-highlight-layer"
            aria-hidden="true"
          >
            {laidOutHighlights.map((highlight) => {
              const marker = reviewMarkerMeta(highlight.event_type);
              return (
                <button
                  key={highlight._key}
                  type="button"
                  className={`edge-highlight ${marker.tone}`}
                  style={{ left: `${highlight._left}%`, top: `calc(${highlight._lane} * 1.7rem)` }}
                  title={`${marker.label} at ${number(highlight.t, 1)}s | Blue edge ${signedNumber(highlight.swing, 3)}`}
                >
                  {marker.short}
                </button>
              );
            })}
          </div>
        ) : null}
        <div className="edge-segment-grid">
          {segments.map((segment) => {
            const intensity = Math.abs(segment.blue_edge) * 1.9 + 0.18;
            const color = segment.blue_edge >= 0
              ? `rgba(19, 168, 154, ${Math.min(1, intensity)})`
              : `rgba(217, 86, 77, ${Math.min(1, intensity)})`;
            return (
              <span
                key={segment.bucket}
                className="edge-segment"
                title={`${segment.end_t}s | Blue ${percent(segment.blue_probability)}`}
                style={{ background: color }}
              />
            );
          })}
        </div>
      </div>
      <div className="edge-labels">
        <span>Orange edge</span>
        <span>Blue edge</span>
      </div>
    </div>
  );
}

function ReviewTimelineLegend() {
  return (
    <div className="review-legend" aria-label="Replay review marker legend">
      {REVIEW_TIMELINE_LEGEND.map((item) => (
        <span className={`review-legend-chip ${reviewMarkerMeta(item.type).tone}`} key={item.type}>
          <strong>{item.short}</strong>
          <em>{item.label}</em>
        </span>
      ))}
    </div>
  );
}

const SMALL_BOOST_PAD_COORDS = [
  [1792, 4184], [-1792, 4184], [1792, -4184], [-1792, -4184],
  [940, 3308], [-940, 3308], [940, -3308], [-940, -3308],
  [1788, 2300], [-1788, 2300], [1788, -2300], [-1788, -2300],
  [2048, 1036], [-2048, 1036], [2048, -1036], [-2048, -1036],
  [3584, 2484], [-3584, 2484], [3584, -2484], [-3584, -2484],
  [0, 4240], [0, -4240],
  [0, 2816], [0, -2816],
  [0, 1024], [0, -1024],
  [1024, 0], [-1024, 0],
];

const LARGE_BOOST_PAD_COORDS = [
  [3072, 4096], [-3072, 4096], [3072, -4096], [-3072, -4096],
  [3584, 0], [-3584, 0],
];

const RL_SCENE_SCALE = 0.01;
const ARENA_DIMENSIONS = {
  halfWidth: 40.96,
  halfLength: 51.2,
  cornerRadius: 10.4,
  sideRampRadius: 2.7,
  upperCurveRadius: 2.2,
  wallHeight: 8.6,
  ceilingHeight: 20,
  goalWidth: 8.92,
  goalHeight: 2.9,
  goalDepth: 3.2,
  backboardWidth: 14.2,
  backboardHeight: 6.8,
};

function rlToScene(vector) {
  return new THREE.Vector3((vector?.[0] || 0) * RL_SCENE_SCALE, (vector?.[2] || 0) * RL_SCENE_SCALE, (vector?.[1] || 0) * RL_SCENE_SCALE);
}

function rlScalarToScene(value) {
  return value * RL_SCENE_SCALE;
}

function buildLine(points, color = '#d8d5ce', closed = true) {
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({ color });
  return closed ? new THREE.LineLoop(geometry, material) : new THREE.Line(geometry, material);
}

function createRoundedRectShape(halfWidth, halfLength, radius) {
  const shape = new THREE.Shape();
  shape.moveTo(-halfWidth + radius, -halfLength);
  shape.lineTo(halfWidth - radius, -halfLength);
  shape.absarc(halfWidth - radius, -halfLength + radius, radius, -Math.PI / 2, 0, false);
  shape.lineTo(halfWidth, halfLength - radius);
  shape.absarc(halfWidth - radius, halfLength - radius, radius, 0, Math.PI / 2, false);
  shape.lineTo(-halfWidth + radius, halfLength);
  shape.absarc(-halfWidth + radius, halfLength - radius, radius, Math.PI / 2, Math.PI, false);
  shape.lineTo(-halfWidth, -halfLength + radius);
  shape.absarc(-halfWidth + radius, -halfLength + radius, radius, Math.PI, Math.PI * 1.5, false);
  return shape;
}

function createPitchTexture(renderer) {
  const canvas = document.createElement('canvas');
  canvas.width = 2048;
  canvas.height = 2048;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    return null;
  }

  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
  gradient.addColorStop(0, '#0f4338');
  gradient.addColorStop(0.52, '#14584b');
  gradient.addColorStop(1, '#0c342b');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const stripeHeight = canvas.height / 14;
  for (let index = 0; index < 14; index += 1) {
    ctx.fillStyle = index % 2 === 0 ? 'rgba(255,255,255,0.026)' : 'rgba(0,0,0,0.052)';
    ctx.fillRect(0, index * stripeHeight, canvas.width, stripeHeight);
  }

  const vignette = ctx.createRadialGradient(canvas.width / 2, canvas.height / 2, canvas.width * 0.12, canvas.width / 2, canvas.height / 2, canvas.width * 0.58);
  vignette.addColorStop(0, 'rgba(255,255,255,0.09)');
  vignette.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = vignette;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  return texture;
}

function createArenaBoundaryLine(color = '#d8d5ce') {
  const shape = createRoundedRectShape(ARENA_DIMENSIONS.halfWidth, ARENA_DIMENSIONS.halfLength, ARENA_DIMENSIONS.cornerRadius);
  return buildLine(shape.getPoints(96).map((point) => new THREE.Vector3(point.x, 0.06, point.y)), color, true);
}

function createWallPanel(width, height, depth, color = '#7ed6ff', opacity = 0.14) {
  return new THREE.Mesh(
    new THREE.BoxGeometry(width, height, depth),
    new THREE.MeshPhysicalMaterial({
      color,
      transparent: true,
      opacity,
      metalness: 0.02,
      roughness: 0.05,
      transmission: 0.36,
      clearcoat: 0.65,
    })
  );
}

function createRampSection(length, radius, color) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, length, 28, 1, true, Math.PI, Math.PI / 2),
    new THREE.MeshStandardMaterial({
      color,
      roughness: 0.82,
      metalness: 0.06,
      transparent: true,
      opacity: 0.92,
    })
  );
  mesh.rotation.z = Math.PI / 2;
  mesh.receiveShadow = true;
  return mesh;
}

function createUpperCurveSection(length, radius, color) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, length, 22, 1, true, Math.PI * 1.5, Math.PI / 2),
    new THREE.MeshPhysicalMaterial({
      color,
      roughness: 0.12,
      metalness: 0.03,
      transparent: true,
      opacity: 0.12,
      transmission: 0.25,
      clearcoat: 0.45,
    })
  );
  mesh.rotation.z = Math.PI / 2;
  return mesh;
}

function createQuarterPipe(radius, color, startAngle, opacity = 0.92) {
  const mesh = new THREE.Mesh(
    new THREE.TorusGeometry(radius, radius * 0.5, 12, 28, Math.PI / 2),
    new THREE.MeshStandardMaterial({
      color,
      roughness: 0.84,
      metalness: 0.08,
      transparent: true,
      opacity,
    })
  );
  mesh.rotation.y = startAngle;
  mesh.rotation.z = Math.PI / 2;
  mesh.receiveShadow = true;
  return mesh;
}

function createGoalTunnel(teamColor, zPosition) {
  const frameColor = teamColor === 'blue' ? '#248fda' : '#f0844a';
  const glassColor = teamColor === 'blue' ? '#95dfff' : '#ffc9ae';
  const sign = zPosition > 0 ? 1 : -1;
  const group = new THREE.Group();

  const floor = new THREE.Mesh(
    new THREE.BoxGeometry(ARENA_DIMENSIONS.goalWidth + 1.6, 0.2, ARENA_DIMENSIONS.goalDepth + 1.8),
    new THREE.MeshStandardMaterial({ color: '#102229', roughness: 0.96, metalness: 0.05 })
  );
  floor.position.set(0, 0.02, zPosition + sign * (ARENA_DIMENSIONS.goalDepth * 0.48));
  floor.receiveShadow = true;
  group.add(floor);

  [-1, 1].forEach((dir) => {
    const sideWall = new THREE.Mesh(
      new THREE.BoxGeometry(0.26, 2.9, ARENA_DIMENSIONS.goalDepth + 1.8),
      new THREE.MeshPhysicalMaterial({
        color: glassColor,
        roughness: 0.12,
        metalness: 0.02,
        transparent: true,
        opacity: 0.16,
        transmission: 0.22,
      })
    );
    sideWall.position.set(dir * (ARENA_DIMENSIONS.goalWidth / 2 + 0.66), 1.46, zPosition + sign * (ARENA_DIMENSIONS.goalDepth * 0.48));
    group.add(sideWall);
  });

  const roof = new THREE.Mesh(
    new THREE.BoxGeometry(ARENA_DIMENSIONS.goalWidth + 1.2, 0.18, ARENA_DIMENSIONS.goalDepth + 1.6),
    new THREE.MeshStandardMaterial({ color: frameColor, roughness: 0.34, metalness: 0.18 })
  );
  roof.position.set(0, 2.86, zPosition + sign * (ARENA_DIMENSIONS.goalDepth * 0.52));
  roof.castShadow = true;
  group.add(roof);

  const rearFrame = new THREE.Mesh(
    new THREE.PlaneGeometry(ARENA_DIMENSIONS.goalWidth + 0.8, ARENA_DIMENSIONS.goalHeight + 0.7),
    new THREE.MeshBasicMaterial({ color: glassColor, transparent: true, opacity: 0.14, side: THREE.DoubleSide })
  );
  rearFrame.position.set(0, 1.54, zPosition + sign * (ARENA_DIMENSIONS.goalDepth + 0.55));
  rearFrame.rotation.y = sign > 0 ? Math.PI : 0;
  group.add(rearFrame);

  return group;
}

function createArenaArchitecture() {
  const group = new THREE.Group();
  const blueGlass = '#95e6ff';
  const orangeGlass = '#ffd1b8';
  const bowlColor = '#11333b';
  const rampColor = '#11343a';
  const sideLength = ARENA_DIMENSIONS.halfLength * 2 - ARENA_DIMENSIONS.cornerRadius * 2;
  const endLength = ARENA_DIMENSIONS.halfWidth * 2 - ARENA_DIMENSIONS.cornerRadius * 2;

  const northRamp = createRampSection(sideLength, ARENA_DIMENSIONS.sideRampRadius, rampColor);
  northRamp.position.set(0, ARENA_DIMENSIONS.sideRampRadius, -ARENA_DIMENSIONS.halfLength + ARENA_DIMENSIONS.sideRampRadius);
  group.add(northRamp);

  const southRamp = createRampSection(sideLength, ARENA_DIMENSIONS.sideRampRadius, rampColor);
  southRamp.rotation.y = Math.PI;
  southRamp.position.set(0, ARENA_DIMENSIONS.sideRampRadius, ARENA_DIMENSIONS.halfLength - ARENA_DIMENSIONS.sideRampRadius);
  group.add(southRamp);

  const eastRamp = createRampSection(endLength, ARENA_DIMENSIONS.sideRampRadius, rampColor);
  eastRamp.rotation.y = Math.PI / 2;
  eastRamp.position.set(ARENA_DIMENSIONS.halfWidth - ARENA_DIMENSIONS.sideRampRadius, ARENA_DIMENSIONS.sideRampRadius, 0);
  group.add(eastRamp);

  const westRamp = createRampSection(endLength, ARENA_DIMENSIONS.sideRampRadius, rampColor);
  westRamp.rotation.y = -Math.PI / 2;
  westRamp.position.set(-ARENA_DIMENSIONS.halfWidth + ARENA_DIMENSIONS.sideRampRadius, ARENA_DIMENSIONS.sideRampRadius, 0);
  group.add(westRamp);

  [
    { x: 1, z: 1, angle: 0, color: orangeGlass },
    { x: -1, z: 1, angle: Math.PI / 2, color: orangeGlass },
    { x: -1, z: -1, angle: Math.PI, color: blueGlass },
    { x: 1, z: -1, angle: Math.PI * 1.5, color: blueGlass },
  ].forEach((corner) => {
    const cornerRamp = createQuarterPipe(ARENA_DIMENSIONS.cornerRadius, rampColor, corner.angle, 0.94);
    cornerRamp.position.set(
      corner.x * (ARENA_DIMENSIONS.halfWidth - ARENA_DIMENSIONS.cornerRadius),
      ARENA_DIMENSIONS.sideRampRadius,
      corner.z * (ARENA_DIMENSIONS.halfLength - ARENA_DIMENSIONS.cornerRadius)
    );
    group.add(cornerRamp);

    const cornerGlass = new THREE.Mesh(
      new THREE.CylinderGeometry(ARENA_DIMENSIONS.cornerRadius, ARENA_DIMENSIONS.cornerRadius, ARENA_DIMENSIONS.wallHeight, 36, 1, true, corner.angle, Math.PI / 2),
      new THREE.MeshPhysicalMaterial({
        color: corner.color,
        transparent: true,
        opacity: 0.15,
        transmission: 0.28,
        roughness: 0.08,
        clearcoat: 0.48,
      })
    );
    cornerGlass.position.set(
      corner.x * (ARENA_DIMENSIONS.halfWidth - ARENA_DIMENSIONS.cornerRadius),
      ARENA_DIMENSIONS.wallHeight / 2,
      corner.z * (ARENA_DIMENSIONS.halfLength - ARENA_DIMENSIONS.cornerRadius)
    );
    group.add(cornerGlass);
  });

  const northUpper = createUpperCurveSection(sideLength, ARENA_DIMENSIONS.upperCurveRadius, blueGlass);
  northUpper.position.set(0, ARENA_DIMENSIONS.wallHeight, -ARENA_DIMENSIONS.halfLength + ARENA_DIMENSIONS.upperCurveRadius * 1.2);
  group.add(northUpper);

  const southUpper = createUpperCurveSection(sideLength, ARENA_DIMENSIONS.upperCurveRadius, orangeGlass);
  southUpper.rotation.y = Math.PI;
  southUpper.position.set(0, ARENA_DIMENSIONS.wallHeight, ARENA_DIMENSIONS.halfLength - ARENA_DIMENSIONS.upperCurveRadius * 1.2);
  group.add(southUpper);

  const eastUpper = createUpperCurveSection(endLength, ARENA_DIMENSIONS.upperCurveRadius, blueGlass);
  eastUpper.rotation.y = Math.PI / 2;
  eastUpper.position.set(ARENA_DIMENSIONS.halfWidth - ARENA_DIMENSIONS.upperCurveRadius * 1.2, ARENA_DIMENSIONS.wallHeight, 0);
  group.add(eastUpper);

  const westUpper = createUpperCurveSection(endLength, ARENA_DIMENSIONS.upperCurveRadius, orangeGlass);
  westUpper.rotation.y = -Math.PI / 2;
  westUpper.position.set(-ARENA_DIMENSIONS.halfWidth + ARENA_DIMENSIONS.upperCurveRadius * 1.2, ARENA_DIMENSIONS.wallHeight, 0);
  group.add(westUpper);

  return group;
}

function createStands() {
  const group = new THREE.Group();
  const shellMaterial = new THREE.MeshStandardMaterial({ color: '#071114', roughness: 1, metalness: 0.02 });
  const accentMaterial = new THREE.MeshStandardMaterial({ color: '#102930', roughness: 0.92 });

  const sideStand = new THREE.Mesh(new THREE.BoxGeometry(18, 8.5, 124), shellMaterial);
  sideStand.position.set(ARENA_DIMENSIONS.halfWidth + 11.8, 3.8, 0);
  group.add(sideStand);

  const mirroredSideStand = sideStand.clone();
  mirroredSideStand.position.x *= -1;
  group.add(mirroredSideStand);

  const endStand = new THREE.Mesh(new THREE.BoxGeometry(114, 10, 16), shellMaterial);
  endStand.position.set(0, 4.2, ARENA_DIMENSIONS.halfLength + 11.2);
  group.add(endStand);

  const mirroredEndStand = endStand.clone();
  mirroredEndStand.position.z *= -1;
  group.add(mirroredEndStand);

  const upperRibbon = new THREE.Mesh(new THREE.TorusGeometry(77, 1.2, 14, 80), accentMaterial);
  upperRibbon.rotation.x = Math.PI / 2;
  upperRibbon.position.y = 10.6;
  group.add(upperRibbon);

  [-1, 1].forEach((dir) => {
    const lowerTier = new THREE.Mesh(new THREE.BoxGeometry(10, 2.4, 118), accentMaterial);
    lowerTier.position.set(dir * (ARENA_DIMENSIONS.halfWidth + 7.2), 1.35, 0);
    group.add(lowerTier);

    const ribbon = new THREE.Mesh(
      new THREE.BoxGeometry(0.32, 0.9, 110),
      new THREE.MeshBasicMaterial({ color: dir > 0 ? '#ff9559' : '#4ac9ff' })
    );
    ribbon.position.set(dir * (ARENA_DIMENSIONS.halfWidth + 3.25), 5.6, 0);
    group.add(ribbon);
  });

  [-1, 1].forEach((dir) => {
    const endRibbon = new THREE.Mesh(
      new THREE.BoxGeometry(96, 0.9, 0.32),
      new THREE.MeshBasicMaterial({ color: dir > 0 ? '#ff9559' : '#4ac9ff' })
    );
    endRibbon.position.set(0, 6.0, dir * (ARENA_DIMENSIONS.halfLength + 3.8));
    group.add(endRibbon);
  });

  return group;
}

function createBoostPickup(x, y, fullBoost = false) {
  const group = new THREE.Group();
  const baseMaterial = new THREE.MeshStandardMaterial({
    color: fullBoost ? '#f5b64c' : '#d8d5ce',
    emissive: fullBoost ? '#8b5a0e' : '#59554f',
    metalness: 0.08,
    roughness: 0.42,
  });
  const glowMaterial = new THREE.MeshBasicMaterial({
    color: fullBoost ? '#f0a41f' : '#b8b39d',
    transparent: true,
    opacity: fullBoost ? 0.3 : 0.22,
  });
  const base = new THREE.Mesh(
    new THREE.CylinderGeometry(fullBoost ? 1.1 : 0.55, fullBoost ? 1.1 : 0.55, 0.12, 24),
    baseMaterial
  );
  const basePos = rlToScene([x, y, 0]);
  base.position.set(basePos.x, 0.06, basePos.z);
  group.add(base);

  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(fullBoost ? 1.35 : 0.76, fullBoost ? 0.08 : 0.05, 12, 28),
    glowMaterial
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.set(basePos.x, 0.11, basePos.z);
  group.add(ring);

  if (fullBoost) {
    const orb = new THREE.Mesh(
      new THREE.SphereGeometry(0.34, 18, 14),
      new THREE.MeshStandardMaterial({
        color: '#ffd27c',
        emissive: '#d47a00',
        emissiveIntensity: 0.95,
        metalness: 0.05,
        roughness: 0.18,
      })
    );
    orb.position.set(basePos.x, 1.18, basePos.z);
    group.add(orb);
  }

  return group;
}

function createGoalFrame(teamColor, zPosition) {
  const frameColor = teamColor === 'blue' ? '#2aa8ff' : '#ff9056';
  const glassColor = teamColor === 'blue' ? '#83dfff' : '#ffc0a2';
  const material = new THREE.MeshStandardMaterial({ color: frameColor, metalness: 0.18, roughness: 0.28 });
  const group = new THREE.Group();
  const uprightGeometry = new RoundedBoxGeometry(0.28, 2.8, 0.28, 4, 0.08);
  const crossbarGeometry = new RoundedBoxGeometry(ARENA_DIMENSIONS.goalWidth, 0.26, 0.26, 4, 0.07);
  const depthGeometry = new RoundedBoxGeometry(0.24, 0.24, ARENA_DIMENSIONS.goalDepth, 4, 0.06);
  const opening = ARENA_DIMENSIONS.goalWidth / 2;

  [-opening, opening].forEach((x) => {
    const upright = new THREE.Mesh(uprightGeometry, material);
    upright.position.set(x, 1.4, zPosition);
    upright.castShadow = true;
    group.add(upright);

    const depthBar = new THREE.Mesh(depthGeometry, material);
    depthBar.position.set(x, 2.72, zPosition + (zPosition > 0 ? -(ARENA_DIMENSIONS.goalDepth / 2) : ARENA_DIMENSIONS.goalDepth / 2));
    depthBar.castShadow = true;
    group.add(depthBar);
  });

  const crossbar = new THREE.Mesh(crossbarGeometry, material);
  crossbar.position.set(0, 2.78, zPosition);
  crossbar.castShadow = true;
  group.add(crossbar);

  const lowerBar = new THREE.Mesh(
    new RoundedBoxGeometry(ARENA_DIMENSIONS.goalWidth - 0.18, 0.18, 0.18, 4, 0.05),
    material
  );
  lowerBar.position.set(0, 0.15, zPosition);
  group.add(lowerBar);

  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(ARENA_DIMENSIONS.goalWidth, ARENA_DIMENSIONS.goalDepth),
    new THREE.MeshBasicMaterial({ color: frameColor, transparent: true, opacity: 0.09, side: THREE.DoubleSide })
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(0, 0.03, zPosition + (zPosition > 0 ? -(ARENA_DIMENSIONS.goalDepth / 2) : ARENA_DIMENSIONS.goalDepth / 2));
  group.add(floor);

  const net = new THREE.Mesh(
    new THREE.PlaneGeometry(ARENA_DIMENSIONS.goalWidth, ARENA_DIMENSIONS.goalHeight),
    new THREE.MeshBasicMaterial({ color: glassColor, transparent: true, opacity: 0.12, side: THREE.DoubleSide })
  );
  net.position.set(0, 1.45, zPosition + (zPosition > 0 ? -ARENA_DIMENSIONS.goalDepth : ARENA_DIMENSIONS.goalDepth));
  group.add(net);

  const backboard = new THREE.Mesh(
    new THREE.PlaneGeometry(ARENA_DIMENSIONS.backboardWidth, ARENA_DIMENSIONS.backboardHeight),
    new THREE.MeshPhysicalMaterial({
      color: glassColor,
      transparent: true,
      opacity: 0.16,
      transmission: 0.22,
      roughness: 0.08,
      clearcoat: 0.5,
    })
  );
  backboard.position.set(0, 4.8, zPosition + (zPosition > 0 ? -1.6 : 1.6));
  group.add(backboard);

  const sideBackboardGeometry = new THREE.PlaneGeometry(3.8, 5.3);
  [-1, 1].forEach((dir) => {
    const sideBackboard = new THREE.Mesh(
      sideBackboardGeometry,
      new THREE.MeshPhysicalMaterial({
        color: glassColor,
        transparent: true,
        opacity: 0.11,
        transmission: 0.2,
        roughness: 0.08,
      })
    );
    sideBackboard.position.set(dir * (ARENA_DIMENSIONS.goalWidth / 2 + 1.75), 3.45, zPosition + (zPosition > 0 ? -0.3 : 0.3));
    sideBackboard.rotation.y = dir > 0 ? -Math.PI / 2 : Math.PI / 2;
    group.add(sideBackboard);
  });

  return group;
}

function createArenaShell() {
  const shell = new THREE.Group();
  const goalZ = ARENA_DIMENSIONS.halfLength - 1.05;
  shell.add(createArenaArchitecture());
  const wallHeight = ARENA_DIMENSIONS.wallHeight;
  const northWall = createWallPanel(rlScalarToScene(5600), wallHeight, 0.3, '#95e6ff', 0.18);
  northWall.position.set(0, wallHeight / 2 + 0.3, -ARENA_DIMENSIONS.halfLength + 0.6);
  shell.add(northWall);

  const southWall = northWall.clone();
  southWall.position.z *= -1;
  southWall.material = northWall.material.clone();
  southWall.material.color = new THREE.Color('#ffd1b8');
  shell.add(southWall);

  const eastWall = createWallPanel(0.3, wallHeight, rlScalarToScene(8200), '#95e6ff', 0.14);
  eastWall.position.set(ARENA_DIMENSIONS.halfWidth - 0.3, wallHeight / 2 + 0.3, 0);
  shell.add(eastWall);

  const westWall = eastWall.clone();
  westWall.position.x *= -1;
  westWall.material = eastWall.material.clone();
  westWall.material.color = new THREE.Color('#ffd1b8');
  shell.add(westWall);

  const ceilingRing = new THREE.Mesh(
    new THREE.TorusGeometry(59, 0.65, 12, 80),
    new THREE.MeshStandardMaterial({ color: '#13323a', emissive: '#113640', emissiveIntensity: 0.45, roughness: 0.55 })
  );
  ceilingRing.rotation.x = Math.PI / 2;
  ceilingRing.position.y = ARENA_DIMENSIONS.ceilingHeight;
  shell.add(ceilingRing);
  shell.add(createGoalFrame('blue', -goalZ));
  shell.add(createGoalFrame('orange', goalZ));
  shell.add(createGoalTunnel('blue', -goalZ));
  shell.add(createGoalTunnel('orange', goalZ));
  shell.add(createStands());
  return shell;
}

function addArena(renderer) {
  const arena = new THREE.Group();
  const lineColor = '#d8d5ce';
  const goalZ = ARENA_DIMENSIONS.halfLength - 1.05;

  const pitchTexture = createPitchTexture(renderer);
  const grassShape = createRoundedRectShape(ARENA_DIMENSIONS.halfWidth, ARENA_DIMENSIONS.halfLength, ARENA_DIMENSIONS.cornerRadius);
  const grass = new THREE.Mesh(
    new THREE.ShapeGeometry(grassShape, 96),
    new THREE.MeshStandardMaterial({
      color: '#135648',
      map: pitchTexture,
      roughness: 0.96,
      metalness: 0.02,
    })
  );
  grass.rotation.x = -Math.PI / 2;
  grass.receiveShadow = true;
  arena.add(grass);

  const apron = new THREE.Mesh(
    new THREE.PlaneGeometry(124, 142),
    new THREE.MeshStandardMaterial({ color: '#08151b', roughness: 1, metalness: 0.02 })
  );
  apron.rotation.x = -Math.PI / 2;
  apron.position.y = -0.05;
  arena.add(apron);
  arena.add(createArenaShell());

  const blueZone = new THREE.Mesh(
    new THREE.PlaneGeometry(rlScalarToScene(8192), rlScalarToScene(1900)),
    new THREE.MeshBasicMaterial({ color: '#0d5f8f', transparent: true, opacity: 0.12 })
  );
  blueZone.rotation.x = -Math.PI / 2;
  blueZone.position.set(0, 0.025, -ARENA_DIMENSIONS.halfLength + 9.8);
  arena.add(blueZone);

  const orangeZone = blueZone.clone();
  orangeZone.material = blueZone.material.clone();
  orangeZone.material.color = new THREE.Color('#9b4b18');
  orangeZone.position.z *= -1;
  arena.add(orangeZone);

  arena.add(createArenaBoundaryLine(lineColor));

  const centerLine = new THREE.Mesh(
    new THREE.BoxGeometry(ARENA_DIMENSIONS.halfWidth * 2 - 1.4, 0.05, 0.22),
    new THREE.MeshStandardMaterial({ color: lineColor })
  );
  centerLine.position.set(0, 0.04, 0);
  arena.add(centerLine);

  const centerCircle = new THREE.Mesh(
    new THREE.RingGeometry(7.8, 8.15, 64),
    new THREE.MeshStandardMaterial({ color: lineColor, metalness: 0.04, roughness: 0.85 })
  );
  centerCircle.rotation.x = -Math.PI / 2;
  centerCircle.position.y = 0.045;
  arena.add(centerCircle);

  const kickoffDots = [
    [0, 0],
    [2048, 2560],
    [-2048, 2560],
    [2048, -2560],
    [-2048, -2560],
    [0, 1024],
    [0, -1024],
  ];
  kickoffDots.forEach(([x, y]) => {
    const dot = new THREE.Mesh(
      new THREE.CylinderGeometry(0.34, 0.34, 0.05, 18),
      new THREE.MeshStandardMaterial({ color: lineColor })
    );
    const pos = rlToScene([x, y, 0]);
    dot.position.set(pos.x, 0.05, pos.z);
    arena.add(dot);
  });

  SMALL_BOOST_PAD_COORDS.forEach(([x, y]) => arena.add(createBoostPickup(x, y, false)));
  LARGE_BOOST_PAD_COORDS.forEach(([x, y]) => arena.add(createBoostPickup(x, y, true)));

  return arena;
}

const CAR_BODY_STYLES = {
  generic: {
    body: [1.9, 0.7, 3.28],
    bodyY: 0.46,
    bumper: [1.76, 0.22, 0.84, 1.48],
    rearDeck: [1.46, 0.16, 0.68, -1.24, 0.76],
    cabin: [1.12, 0.46, 1.36, -0.04, 0.88],
    windshield: [0.98, 0.28, 0.82, 0.44, 0.94, -0.42],
    rearGlass: [0.92, 0.22, 0.68, -0.78, 0.88, 0.16],
    spoiler: false,
    wheelX: 0.77,
    wheelZ: 1.08,
    wheelRadius: 0.33,
  },
  octane: {
    body: [1.88, 0.76, 3.24],
    bodyY: 0.48,
    bumper: [1.72, 0.24, 0.84, 1.44],
    rearDeck: [1.4, 0.18, 0.7, -1.18, 0.82],
    cabin: [1.16, 0.58, 1.5, -0.1, 1.02],
    windshield: [1.0, 0.36, 0.78, 0.42, 1.08, -0.34],
    rearGlass: [0.92, 0.3, 0.62, -0.72, 1.02, 0.26],
    spoiler: true,
    spoilerWidth: 1.28,
    wheelX: 0.75,
    wheelZ: 1.04,
    wheelRadius: 0.34,
  },
  fennec: {
    body: [1.94, 0.82, 3.26],
    bodyY: 0.5,
    bumper: [1.78, 0.26, 0.8, 1.46],
    rearDeck: [1.48, 0.2, 0.72, -1.14, 0.84],
    cabin: [1.28, 0.66, 1.68, -0.04, 1.03],
    windshield: [1.08, 0.4, 0.8, 0.48, 1.1, -0.28],
    rearGlass: [1.0, 0.36, 0.72, -0.82, 1.06, 0.18],
    spoiler: false,
    wheelX: 0.78,
    wheelZ: 1.06,
    wheelRadius: 0.35,
  },
  dominus: {
    body: [1.92, 0.62, 3.54],
    bodyY: 0.42,
    bumper: [1.78, 0.18, 0.9, 1.58],
    rearDeck: [1.54, 0.16, 0.72, -1.34, 0.74],
    cabin: [1.08, 0.42, 1.34, -0.06, 0.82],
    windshield: [0.96, 0.26, 0.82, 0.44, 0.88, -0.48],
    rearGlass: [0.9, 0.2, 0.72, -0.78, 0.84, 0.16],
    spoiler: true,
    spoilerWidth: 1.42,
    wheelX: 0.78,
    wheelZ: 1.14,
    wheelRadius: 0.33,
  },
  breakout: {
    body: [1.88, 0.58, 3.42],
    bodyY: 0.4,
    bumper: [1.76, 0.18, 0.96, 1.52],
    rearDeck: [1.4, 0.14, 0.62, -1.28, 0.68],
    cabin: [1.0, 0.36, 1.28, -0.06, 0.76],
    windshield: [0.9, 0.22, 0.84, 0.48, 0.8, -0.58],
    rearGlass: [0.84, 0.18, 0.62, -0.82, 0.78, 0.12],
    spoiler: true,
    spoilerWidth: 1.2,
    wheelX: 0.76,
    wheelZ: 1.12,
    wheelRadius: 0.32,
  },
  merc: {
    body: [1.98, 0.94, 3.3],
    bodyY: 0.58,
    bumper: [1.84, 0.28, 0.84, 1.46],
    rearDeck: [1.56, 0.2, 0.74, -1.12, 0.92],
    cabin: [1.4, 0.78, 1.84, -0.02, 1.16],
    windshield: [1.16, 0.42, 0.86, 0.46, 1.22, -0.24],
    rearGlass: [1.04, 0.34, 0.82, -0.88, 1.18, 0.18],
    spoiler: false,
    wheelX: 0.8,
    wheelZ: 1.02,
    wheelRadius: 0.36,
  },
  plank: {
    body: [1.94, 0.5, 3.76],
    bodyY: 0.38,
    bumper: [1.8, 0.14, 0.86, 1.7],
    rearDeck: [1.56, 0.14, 0.74, -1.5, 0.62],
    cabin: [1.02, 0.32, 1.08, -0.06, 0.68],
    windshield: [0.94, 0.18, 0.9, 0.4, 0.72, -0.62],
    rearGlass: [0.9, 0.16, 0.76, -0.82, 0.7, 0.08],
    spoiler: true,
    spoilerWidth: 1.48,
    wheelX: 0.8,
    wheelZ: 1.2,
    wheelRadius: 0.31,
  },
  hybrid: {
    body: [1.9, 0.68, 3.34],
    bodyY: 0.46,
    bumper: [1.76, 0.22, 0.86, 1.5],
    rearDeck: [1.44, 0.16, 0.7, -1.24, 0.76],
    cabin: [1.12, 0.48, 1.42, -0.04, 0.9],
    windshield: [0.98, 0.3, 0.8, 0.44, 0.96, -0.4],
    rearGlass: [0.92, 0.24, 0.66, -0.8, 0.9, 0.18],
    spoiler: true,
    spoilerWidth: 1.32,
    wheelX: 0.77,
    wheelZ: 1.08,
    wheelRadius: 0.33,
  },
};

function carStyleKey(car) {
  const key = String(car?.car_family || '').trim().toLowerCase();
  if (CAR_BODY_STYLES[key]) return key;
  const name = String(car?.car_name || '').trim().toLowerCase();
  if (!key && !name) return 'generic';
  if (name.includes('fennec')) return 'fennec';
  if (name.includes('dominus')) return 'dominus';
  if (name.includes('breakout') || name.includes('animus')) return 'breakout';
  if (name.includes('merc') || name.includes('road hog')) return 'merc';
  if (name.includes('batmobile') || name.includes('mantis')) return 'plank';
  if (name.includes('skyline') || name.includes('primo')) return 'hybrid';
  return name ? 'octane' : 'generic';
}

function shouldUseAssetCarModel(car) {
  const style = carStyleKey(car);
  const name = String(car?.car_name || '').trim().toLowerCase();
  return style === 'octane' && (name.includes('octane') || name.includes('takumi') || name.includes('paladin') || name.includes('hotshot'));
}

function createNameTagSprite(playerName, team = 0) {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 128;
  const context = canvas.getContext('2d');
  if (!context) return null;
  const label = String(playerName || 'Unknown').toUpperCase();
  const fill = Number(team) === 1 ? '#f2874d' : '#2da9ff';
  const stroke = 'rgba(244, 242, 237, 0.94)';
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = 'rgba(8, 12, 17, 0.78)';
  context.strokeStyle = stroke;
  context.lineWidth = 8;
  const x = 18;
  const y = 22;
  const width = 476;
  const height = 72;
  const radius = 28;
  context.beginPath();
  context.moveTo(x + radius, y);
  context.arcTo(x + width, y, x + width, y + height, radius);
  context.arcTo(x + width, y + height, x, y + height, radius);
  context.arcTo(x, y + height, x, y, radius);
  context.arcTo(x, y, x + width, y, radius);
  context.closePath();
  context.fill();
  context.stroke();
  context.fillStyle = fill;
  context.fillRect(x + 14, y + 14, 16, height - 28);
  context.font = '700 42px Inter, Arial, sans-serif';
  context.textBaseline = 'middle';
  context.fillStyle = '#f6f1e8';
  context.fillText(shortText(label, 18), 54, y + height / 2 + 2, width - 70);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const material = new THREE.SpriteMaterial({
    map: texture,
    depthTest: false,
    depthWrite: false,
    transparent: true,
  });
  const sprite = new THREE.Sprite(material);
  sprite.position.set(0, 2.2, 0);
  sprite.scale.set(4.6, 1.15, 1);
  sprite.renderOrder = 12;
  return sprite;
}

function attachNameTag(mesh, car) {
  if (!mesh || mesh.userData?.nameTagAttached) return;
  const sprite = createNameTagSprite(car?.player_name, car?.team);
  if (!sprite) return;
  mesh.add(sprite);
  mesh.userData.nameTag = sprite;
  mesh.userData.nameTagAttached = true;
}

function carEulerToQuaternion(euler = [0, 0, 0]) {
  return carQuaternionFromTelemetry(euler);
}

function interpolateNumber(a, b, alpha) {
  const left = Number(a || 0);
  const right = Number(b ?? a ?? 0);
  return left + (right - left) * alpha;
}

function interpolateRlVector(a = [0, 0, 0], b = a, alpha = 0) {
  return new THREE.Vector3(
    interpolateNumber(a?.[0], b?.[0], alpha),
    interpolateNumber(a?.[2], b?.[2], alpha),
    interpolateNumber(a?.[1], b?.[1], alpha)
  ).multiplyScalar(RL_SCENE_SCALE);
}

function rlDistance(a = [0, 0, 0], b = [0, 0, 0]) {
  const dx = Number(b?.[0] || 0) - Number(a?.[0] || 0);
  const dy = Number(b?.[1] || 0) - Number(a?.[1] || 0);
  const dz = Number(b?.[2] || 0) - Number(a?.[2] || 0);
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

function quaternionDistance(a, b) {
  const qa = a instanceof THREE.Quaternion ? a : new THREE.Quaternion();
  const qb = b instanceof THREE.Quaternion ? b : new THREE.Quaternion();
  return 1 - Math.abs(qa.dot(qb));
}

function isKickoffLikeFrame(frame) {
  const ball = frame?.ball?.pos || [0, 0, 0];
  return Math.hypot(Number(ball[0] || 0), Number(ball[1] || 0)) <= 160 && Math.abs(Number(ball[2] || 0) - 92.75) <= 120;
}

function isActionFrame(frame) {
  if (!frame) return false;
  return !isKickoffLikeFrame(frame);
}

function findActionStartFrame(frames = [], scanLimit = 720) {
  const limit = Math.min(frames.length, Math.max(1, scanLimit));
  for (let index = 0; index < limit; index += 1) {
    if (isActionFrame(frames[index])) return index;
  }
  return 0;
}

function blendFrameCars(currentFrame, nextFrame, blend = 0) {
  const currentCars = currentFrame?.cars || [];
  const nextById = new Map((nextFrame?.cars || []).map((car) => [car.player_id, car]));
  return currentCars.map((car) => {
    const nextCar = nextById.get(car.player_id) || car;
    return {
      ...car,
      boost: interpolateNumber(car.boost, nextCar.boost, blend),
    };
  });
}

function orderCarsForHud(cars = [], replay) {
  const roster = replay?.players || [];
  const rosterIndex = new Map(
    roster.map((player, index) => [
      String(player.platform_player_id || player.player_name || `${player.side}-${index}`),
      index,
    ])
  );
  return [...cars].sort((left, right) => {
    const leftKey = String(left.player_id || left.player_name || '');
    const rightKey = String(right.player_id || right.player_name || '');
    const leftRank = rosterIndex.get(leftKey);
    const rightRank = rosterIndex.get(rightKey);
    if (leftRank !== undefined || rightRank !== undefined) {
      return (leftRank ?? 999) - (rightRank ?? 999);
    }
    if ((left.team ?? 0) !== (right.team ?? 0)) return Number(left.team ?? 0) - Number(right.team ?? 0);
    return String(left.player_name || '').localeCompare(String(right.player_name || ''));
  });
}

function shouldSnapFrameTransition(frame, nextFrame) {
  if (!frame || !nextFrame) return false;
  if (isKickoffLikeFrame(nextFrame) && !isKickoffLikeFrame(frame)) {
    return true;
  }
  if (Math.abs(Number(frame?.ball?.vel?.[0] || 0) - Number(nextFrame?.ball?.vel?.[0] || 0)) > 1600) {
    return true;
  }
  if (rlDistance(frame.ball?.pos, nextFrame.ball?.pos) > 140) {
    return true;
  }
  const currentCars = frame.cars || [];
  const nextById = new Map((nextFrame.cars || []).map((car) => [car.player_id, car]));
  for (const car of currentCars) {
    const nextCar = nextById.get(car.player_id);
    if (!nextCar) continue;
    const boostReset = Math.abs(Number(car.boost ?? 33) - 33) > 2 && Math.abs(Number(nextCar.boost ?? 33) - 33) <= 1.5;
    if (boostReset && isKickoffLikeFrame(nextFrame)) {
      return true;
    }
    if (rlDistance(car.pos, nextCar.pos) > 120) {
      return true;
    }
    const currentQuat = carQuaternionFromTelemetry(car.euler || [0, 0, 0]);
    const nextQuat = carQuaternionFromTelemetry(nextCar.euler || car.euler || [0, 0, 0]);
    if (quaternionDistance(currentQuat, nextQuat) > 0.35 && rlDistance(car.pos, nextCar.pos) > 55) {
      return true;
    }
  }
  return false;
}

function clampNumber(value, min, max) {
  return Math.max(min, Math.min(max, Number(value)));
}


function horizontalToVerticalFov(horizontalFovDeg, aspect = 16 / 9) {
  const horizontal = THREE.MathUtils.degToRad(clampNumber(horizontalFovDeg || 100, 60, 125));
  const vertical = 2 * Math.atan(Math.tan(horizontal / 2) / Math.max(0.1, aspect || 16 / 9));
  return THREE.MathUtils.radToDeg(vertical);
}

function cameraSettingsForCar(car) {
  const settings = car?.camera_settings || car?.cameraSettings || {};
  const read = (...keys) => {
    for (const key of keys) {
      if (settings[key] !== undefined && settings[key] !== null) return Number(settings[key]);
      if (car?.[key] !== undefined && car?.[key] !== null) return Number(car[key]);
    }
    return undefined;
  };
  return {
    fieldOfView: read('fieldOfView', 'field_of_view', 'fov') ?? 110,
    distance: read('distance') ?? 270,
    height: read('height') ?? 110,
    pitch: read('pitch', 'angle') ?? -4,
    stiffness: read('stiffness') ?? 0.45,
    swivelSpeed: read('swivelSpeed', 'swivel_speed', 'swivel') ?? 4,
    transitionSpeed: read('transitionSpeed', 'transition_speed', 'transition') ?? 1,
  };
}

function carBasisFromOrientation(orientation) {
  const quat = orientation instanceof THREE.Quaternion ? orientation : new THREE.Quaternion();
  return {
    right: new THREE.Vector3(1, 0, 0).applyQuaternion(quat).normalize(),
    up: new THREE.Vector3(0, 1, 0).applyQuaternion(quat).normalize(),
    forward: new THREE.Vector3(0, 0, 1).applyQuaternion(quat).normalize(),
  };
}

function ensureBallTrail(runtime) {
  if (runtime.ballTrail) return runtime.ballTrail;
  const trail = [];
  const material = new THREE.MeshBasicMaterial({
    color: '#f8f0dc',
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
  });
  for (let index = 0; index < 18; index += 1) {
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.18, 10, 8), material.clone());
    dot.visible = false;
    dot.renderOrder = 4;
    runtime.scene.add(dot);
    trail.push(dot);
  }
  runtime.ballTrail = trail;
  runtime.ballTrailCursor = 0;
  return trail;
}

function updateBallTrail(runtime, ballPos, ballVel) {
  if (!runtime.effects?.ballTrail) return;
  const trail = ensureBallTrail(runtime);
  const speed = new THREE.Vector3(ballVel.x, ballVel.y, ballVel.z).length();
  if (speed < 0.18) return;
  runtime.ballTrailCursor = (runtime.ballTrailCursor + 1) % trail.length;
  const current = trail[runtime.ballTrailCursor];
  current.position.copy(ballPos);
  current.visible = true;
  trail.forEach((dot, index) => {
    const age = (runtime.ballTrailCursor - index + trail.length) % trail.length;
    const fade = Math.max(0, 1 - age / trail.length);
    dot.material.opacity = 0.34 * fade;
    dot.scale.setScalar(0.75 + fade * 1.1);
  });
}

function ensureBoostTrail(mesh, team = 0) {
  if (mesh.userData?.boostTrail) return mesh.userData.boostTrail;
  const group = new THREE.Group();
  group.name = 'native-boost-trail';
  const color = Number(team) === 1 ? '#ff9b52' : '#45c8ff';
  const material = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: 0.62,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  [-0.34, 0.34].forEach((x) => {
    const cone = new THREE.Mesh(new THREE.ConeGeometry(0.18, 1.35, 12, 1, true), material.clone());
    cone.rotation.x = -Math.PI / 2;
    cone.position.set(x, 0.32, -1.08);
    group.add(cone);
  });
  group.visible = false;
  mesh.add(group);
  mesh.userData.boostTrail = group;
  return group;
}

function updateCarBoostTrail(mesh, car) {
  const trail = ensureBoostTrail(mesh, car?.team);
  const active = Boolean(car?.boost_active || car?.boostActive || car?.boosting || car?.boost_source);
  trail.visible = active && mesh.visible;
  if (!trail.visible) return;
  const vel = sceneVectorFromRl(car?.vel || [0, 0, 0]).length();
  const scale = clampNumber(0.85 + vel * 0.08, 0.85, 1.9);
  trail.children.forEach((child, index) => {
    child.scale.set(0.8 + scale * 0.15, scale, 0.8 + scale * 0.15);
    child.material.opacity = index === 0 ? 0.52 : 0.42;
  });
}

function ensureDebugRig(runtime) {
  if (runtime.debugRig) return runtime.debugRig;
  const group = new THREE.Group();
  group.name = 'native-debug-camera-rig';
  const target = new THREE.Mesh(
    new THREE.SphereGeometry(0.28, 12, 8),
    new THREE.MeshBasicMaterial({ color: '#fff06a', depthTest: false })
  );
  target.name = 'debug-target';
  group.add(target);
  runtime.scene.add(group);
  runtime.debugRig = group;
  return group;
}

function scoreCameraCandidate(candidate, context) {
  const { lastPosition, ballPos, focusPos } = context;
  const motionPenalty = lastPosition ? candidate.position.distanceTo(lastPosition) * 0.22 : 0;
  const ballDistance = candidate.position.distanceTo(ballPos);
  const focusDistance = candidate.position.distanceTo(focusPos || ballPos);
  const heightPenalty = Math.abs(candidate.position.y - candidate.idealHeight) * 0.08;
  const wallPenalty = Math.max(0, Math.abs(candidate.position.x) - 38) * 2.5 + Math.max(0, Math.abs(candidate.position.z) - 55) * 2.5;
  return candidate.weight - motionPenalty - Math.abs(ballDistance - candidate.idealDistance) * 0.045 - Math.abs(focusDistance - candidate.idealDistance) * 0.015 - heightPenalty - wallPenalty;
}

function chooseBroadcastCamera(context) {
  const { ballPos, ballVel, cars, cameraState } = context;
  const speed = new THREE.Vector3(ballVel.x, 0, ballVel.z);
  const attackDir = speed.lengthSq() > 0.01 ? Math.sign(speed.z || cameraState.lastAttackDir || 1) : (cameraState.lastAttackDir || 1);
  cameraState.lastAttackDir = attackDir || cameraState.lastAttackDir || 1;
  const side = ballPos.x > 7 ? -1 : ballPos.x < -7 ? 1 : (cameraState.replaySide || 1);
  cameraState.replaySide = side;
  const nearest = [...cars].sort((a, b) => a.pos.distanceTo(ballPos) - b.pos.distanceTo(ballPos))[0];
  const focus = nearest ? ballPos.clone().lerp(nearest.pos, 0.22) : ballPos.clone();
  const highBall = ballPos.y > 6.2;
  const corner = Math.abs(ballPos.x) > 29 && Math.abs(ballPos.z) > 41;
  const goalThreat = Math.abs(ballPos.z) > 37 || Math.abs(ballVel.z) > 0.42;
  const candidates = [
    {
      name: 'midfield-wide',
      position: new THREE.Vector3(side * 17.5, highBall ? 20 : 15.5, ballPos.z - attackDir * 33),
      target: focus.clone().add(new THREE.Vector3(0, 1.8, 0)),
      fov: highBall ? 34 : 31,
      weight: corner ? 62 : goalThreat ? 72 : 86,
      idealDistance: 36,
      idealHeight: highBall ? 20 : 15,
    },
    {
      name: 'attacking-follow',
      position: ballPos.clone().add(new THREE.Vector3(side * 10.5, highBall ? 12.5 : 9.5, -attackDir * 23)),
      target: focus.clone().add(new THREE.Vector3(0, 1.2, attackDir * 4.2)),
      fov: 38,
      weight: goalThreat ? 92 : 78,
      idealDistance: 25,
      idealHeight: highBall ? 13 : 10,
    },
    {
      name: 'goal-box',
      position: new THREE.Vector3(side * 10, 8.8, Math.sign(ballPos.z || attackDir) * 52),
      target: ballPos.clone().add(new THREE.Vector3(0, 1.3, -Math.sign(ballPos.z || attackDir) * 5)),
      fov: 42,
      weight: goalThreat ? 101 : 48,
      idealDistance: 22,
      idealHeight: 9,
    },
    {
      name: 'corner-wall',
      position: new THREE.Vector3(Math.sign(ballPos.x || side) * 37, 11.8, Math.sign(ballPos.z || attackDir) * 42),
      target: ballPos.clone().add(new THREE.Vector3(0, 1.3, 0)),
      fov: 44,
      weight: corner ? 110 : 42,
      idealDistance: 22,
      idealHeight: 12,
    },
    {
      name: 'tactical-ceiling',
      position: new THREE.Vector3(0, 34, ballPos.z * 0.18),
      target: ballPos.clone().lerp(focus, 0.5),
      fov: 46,
      weight: highBall ? 85 : 54,
      idealDistance: 35,
      idealHeight: 34,
    },
  ];
  let best = candidates[0];
  let bestScore = -Infinity;
  for (const candidate of candidates) {
    const score = scoreCameraCandidate(candidate, { ...context, focusPos: focus });
    if (score > bestScore) {
      best = candidate;
      bestScore = score;
    }
  }
  cameraState.lastDirectorShot = best.name;
  return best;
}

function applyReplayFrameToScene(runtime) {
  const framesPayload = runtime.framesPayload;
  const frames = framesPayload?.frames || [];
  if (!runtime || !runtime.assetsReady || !frames.length) return;
  const playbackValue = Number(runtime.playbackFrameRef?.current || 0);
  const startFrame = Number(framesPayload?.start_frame || 0);
  const localFloatFrame = clampNumber(playbackValue - startFrame, 0, Math.max(0, frames.length - 1));
  const frameIndex = Math.floor(localFloatFrame);
  const nextIndex = Math.min(frameIndex + 1, frames.length - 1);
  const frame = frames[frameIndex];
  const next = frames[nextIndex] || frame;
  if (!frame) return;

  const transitionSnaps = shouldSnapFrameTransition(frame, next);
  const blend = transitionSnaps ? 0 : clampNumber(localFloatFrame - frameIndex, 0, 0.9999);
  const ballPos = interpolateRlVector(frame.ball?.pos || [0, 0, 0], next.ball?.pos || frame.ball?.pos || [0, 0, 0], blend);
  const ballVel = interpolateRlVector(frame.ball?.vel || [0, 0, 0], next.ball?.vel || frame.ball?.vel || [0, 0, 0], blend);
  const flatVelocity = new THREE.Vector3(ballVel.x, 0, ballVel.z);
  const cameraMode = runtime.cameraMode || 'director';
  const nextCarsById = new Map((next.cars || []).map((item) => [item.player_id, item]));
  const interpolatedCars = (frame.cars || []).map((car) => {
    const nextCar = nextCarsById.get(car.player_id) || car;
    const pos = interpolateRlVector(car.pos || [0, 0, 0], nextCar.pos || car.pos || [0, 0, 0], blend);
    const currentQuat = carEulerToQuaternion(car.euler || [0, 0, 0]);
    const nextQuat = carEulerToQuaternion(nextCar.euler || car.euler || [0, 0, 0]);
    const orientation = transitionSnaps ? currentQuat : currentQuat.clone().slerp(nextQuat, blend);
    return {
      ...car,
      pos,
      vel: nextCar.vel || car.vel || [0, 0, 0],
      boost: interpolateNumber(car.boost, nextCar.boost, blend),
      boost_active: Boolean(car.boost_active || nextCar.boost_active || Number(nextCar.boost ?? car.boost ?? 0) < Number(car.boost ?? 0) - 0.8),
      orientation,
    };
  });
  const interpolatedCarsById = new Map(interpolatedCars.map((car) => [car.player_id, car]));
  const trackedCar = interpolatedCarsById.get(runtime.selectedPlayerId) || interpolatedCars[0] || null;
  const cameraState = runtime.cameraState || (runtime.cameraState = {
    reviewHalf: 1,
    replaySide: 1,
    lastBallPos: ballPos.clone(),
    lastAttackDir: 1,
    lastCameraPos: runtime.camera.position.clone(),
    initialized: false,
    lastDirectorShot: 'none',
  });

  if (runtime.ball) {
    runtime.ball.position.copy(ballPos);
  }
  updateBallTrail(runtime, ballPos, ballVel);

  const padStates = frame.pad_states || next.pad_states || [];
  if (runtime.boostPads?.length) {
    runtime.boostPads.forEach((pad, index) => {
      const active = padStates[index] !== false;
      pad.activeMesh.visible = active;
      pad.inactiveMesh.visible = !active;
      if (pad.group) {
        pad.group.scale.setScalar(active ? 1 : 0.94);
      }
    });
  }

  const activeCars = new Set();
  interpolatedCars.forEach((car) => {
    activeCars.add(car.player_id);
    let mesh = runtime.cars.get(car.player_id);
    if (!mesh) {
      mesh = runtime.fallbackMode || !runtime.assets || !shouldUseAssetCarModel(car)
        ? createCarMesh(car, runtime.templateCache)
        : buildCarModel(runtime.assets, car);
      attachNameTag(mesh, car);
      runtime.scene.add(mesh);
      runtime.cars.set(car.player_id, mesh);
    }
    mesh.position.set(car.pos.x, car.pos.y + (runtime.fallbackMode ? 0.18 : 0), car.pos.z);
    mesh.quaternion.copy(car.orientation);
    const isSelectedPovCar = (cameraMode === 'pov' || cameraMode === 'pov-ball') && String(car.player_id) === String(runtime.selectedPlayerId);
    mesh.visible = !car.demo && !isSelectedPovCar;
    updateCarBoostTrail(mesh, car);
    const sprite = mesh.userData?.nameTag;
    if (sprite) {
      sprite.visible = cameraMode !== 'pov' && cameraMode !== 'pov-ball';
      const height = clampNumber(runtime.camera.position.distanceTo(mesh.position) * 0.035, 0.92, 1.85);
      sprite.scale.set(height * 4, height, 1);
    }
  });

  runtime.cars.forEach((mesh, playerId) => {
    if (!activeCars.has(playerId)) {
      mesh.visible = false;
    }
  });

  runtime.controls.enabled = cameraMode === 'free';
  const aspect = runtime.camera.aspect || 16 / 9;
  let desiredPosition = runtime.camera.position.clone();
  let desiredTarget = ballPos.clone().add(new THREE.Vector3(0, 1.25, 0));
  let desiredFov = cameraMode === 'free' ? 38 : 34;
  let positionAlpha = transitionSnaps ? 0.5 : 0.09;
  let targetAlpha = transitionSnaps ? 0.55 : 0.12;
  let hardCut = false;

  if (cameraMode === 'free') {
    desiredFov = 38;
  } else if ((cameraMode === 'pov' || cameraMode === 'pov-ball') && trackedCar) {
    const settings = cameraSettingsForCar(trackedCar);
    const { forward, up } = carBasisFromOrientation(trackedCar.orientation);
    const eyeOffset = up.clone().multiplyScalar(0.78).add(forward.clone().multiplyScalar(0.42));
    desiredPosition = trackedCar.pos.clone().add(eyeOffset);
    const useBall = cameraMode === 'pov-ball' || Boolean(trackedCar.ball_cam || trackedCar.ballCam);
    desiredTarget = useBall
      ? ballPos.clone().add(new THREE.Vector3(0, 0.55, 0))
      : desiredPosition.clone().add(forward.clone().multiplyScalar(24));
    desiredFov = horizontalToVerticalFov(settings.fieldOfView, aspect);
    positionAlpha = transitionSnaps ? 1 : 0.72;
    targetAlpha = transitionSnaps ? 1 : clampNumber(0.35 + settings.swivelSpeed * 0.04, 0.38, 0.78);
    hardCut = !cameraState.initialized || runtime.lastCameraMode !== cameraMode || String(runtime.lastSelectedPlayerId || '') !== String(runtime.selectedPlayerId || '');
  } else if (cameraMode === 'player' && trackedCar) {
    const settings = cameraSettingsForCar(trackedCar);
    const { forward, up } = carBasisFromOrientation(trackedCar.orientation);
    const distance = clampNumber(settings.distance * 0.01, 2.0, 4.2);
    const height = clampNumber(settings.height * 0.01, 0.75, 1.85);
    desiredPosition = trackedCar.pos.clone()
      .sub(forward.clone().multiplyScalar(distance))
      .add(up.clone().multiplyScalar(height));
    desiredTarget = ballPos.clone().lerp(trackedCar.pos, 0.12).add(new THREE.Vector3(0, 0.62, 0));
    desiredFov = horizontalToVerticalFov(settings.fieldOfView, aspect);
    positionAlpha = transitionSnaps ? 0.52 : 0.22;
    targetAlpha = transitionSnaps ? 0.6 : 0.28;
  } else if (cameraMode === 'ball') {
    if (flatVelocity.lengthSq() < 0.0001) {
      flatVelocity.subVectors(ballPos, cameraState.lastBallPos || ballPos);
      flatVelocity.y = 0;
    }
    if (flatVelocity.lengthSq() < 0.0001) flatVelocity.set(0, 0, cameraState.lastAttackDir || 1);
    flatVelocity.normalize();
    const sideVector = new THREE.Vector3(flatVelocity.z, 0, -flatVelocity.x).normalize();
    desiredPosition = ballPos.clone().add(flatVelocity.clone().multiplyScalar(-9.5)).add(sideVector.multiplyScalar(5.4)).add(new THREE.Vector3(0, 4.7, 0));
    desiredTarget = ballPos.clone().add(new THREE.Vector3(clampNumber(ballVel.x * 0.22, -4.5, 4.5), 1.1, clampNumber(ballVel.z * 0.18, -6, 6)));
    desiredFov = 50;
    positionAlpha = transitionSnaps ? 0.4 : 0.18;
    targetAlpha = transitionSnaps ? 0.45 : 0.2;
  } else {
    const cinematicGoal = Math.abs(ballPos.z) > 48 && ballPos.y < 5.0;
    const candidate = chooseBroadcastCamera({ ballPos, ballVel, cars: interpolatedCars, cameraState, lastPosition: cameraState.lastCameraPos });
    desiredPosition = candidate.position;
    desiredTarget = candidate.target;
    desiredFov = cinematicGoal ? Math.max(candidate.fov, 46) : candidate.fov;
    positionAlpha = cinematicGoal ? 0.12 : transitionSnaps ? 0.34 : 0.075;
    targetAlpha = cinematicGoal ? 0.16 : transitionSnaps ? 0.42 : 0.11;
    hardCut = !cameraState.initialized || (transitionSnaps && runtime.effects?.cameraCuts);
  }

  desiredPosition.x = clampNumber(desiredPosition.x, -45, 45);
  desiredPosition.y = clampNumber(desiredPosition.y, 0.35, 38);
  desiredPosition.z = clampNumber(desiredPosition.z, -62, 62);

  if (Math.abs(runtime.camera.fov - desiredFov) > 0.1) {
    runtime.camera.fov += (desiredFov - runtime.camera.fov) * (hardCut ? 1 : 0.14);
    runtime.camera.updateProjectionMatrix();
  }

  if (cameraMode !== 'free') {
    if (!cameraState.initialized || hardCut) {
      runtime.camera.position.copy(desiredPosition);
      runtime.controls.target.copy(desiredTarget);
      cameraState.initialized = true;
    } else {
      runtime.controls.target.lerp(desiredTarget, targetAlpha);
      runtime.camera.position.lerp(desiredPosition, positionAlpha);
    }
    runtime.camera.lookAt(runtime.controls.target);
  }

  if (runtime.debugMode) {
    const debugRig = ensureDebugRig(runtime);
    debugRig.visible = true;
    debugRig.getObjectByName('debug-target')?.position.copy(runtime.controls.target);
  } else if (runtime.debugRig) {
    runtime.debugRig.visible = false;
  }

  cameraState.lastBallPos.copy(ballPos);
  cameraState.lastCameraPos.copy(runtime.camera.position);
  runtime.lastCameraMode = cameraMode;
  runtime.lastSelectedPlayerId = runtime.selectedPlayerId;
  runtime.currentFrameInfo = {
    frameIndex: Math.round(playbackValue),
    localFrame: frameIndex,
    cameraMode,
    directorShot: cameraState.lastDirectorShot,
    selectedPlayerId: runtime.selectedPlayerId,
    padCount: runtime.boostPads?.length || 0,
    carCount: interpolatedCars.length,
  };
}


function disposeSceneResources(scene) {
  const geometries = new Set();
  const materials = new Set();
  const textures = new Set();
  scene.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    if (child.geometry) geometries.add(child.geometry);
    const stack = Array.isArray(child.material) ? child.material : [child.material];
    stack.filter(Boolean).forEach((material) => {
      materials.add(material);
      Object.values(material).forEach((value) => {
        if (value && typeof value === 'object' && value.isTexture) {
          textures.add(value);
        }
      });
    });
  });
  textures.forEach((texture) => texture.dispose());
  materials.forEach((material) => material.dispose());
  geometries.forEach((geometry) => geometry.dispose());
}

function ReplayScene({ framesPayload, playbackFrameRef, cameraMode = 'director', selectedPlayerId = null, mapCode = '', qualityMode = 'balanced', debugMode = false, children = null }) {
  const hostRef = useRef(null);
  const sceneRef = useRef(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !framesPayload?.frames?.length) return undefined;

    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(qualityMode === 'performance' ? 1 : Math.min(window.devicePixelRatio || 1, qualityMode === 'cinematic' ? 2 : 1.5));
    renderer.setSize(host.clientWidth, host.clientHeight);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    host.innerHTML = '';
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#06080d');
    scene.fog = new THREE.Fog('#06080d', 96, 210);

    const camera = new THREE.PerspectiveCamera(32, host.clientWidth / Math.max(host.clientHeight, 1), 0.1, 1000);
    camera.position.set(0, 12, 46);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.enablePan = false;
    controls.maxPolarAngle = Math.PI / 2.08;
    controls.minDistance = 10;
    controls.maxDistance = 78;
    controls.target.set(0, 1.25, 0);

    scene.add(new THREE.AmbientLight('#f7fbff', 0.18));
    scene.add(new THREE.HemisphereLight('#f2f7ff', '#0d1820', 0.72));
    const sun = new THREE.DirectionalLight('#f7fbff', 1.85);
    sun.position.set(0, 44, 0);
    sun.castShadow = true;
    sun.shadow.mapSize.width = qualityMode === 'performance' ? 1024 : qualityMode === 'cinematic' ? 3072 : 2048;
    sun.shadow.mapSize.height = qualityMode === 'performance' ? 1024 : qualityMode === 'cinematic' ? 3072 : 2048;
    sun.shadow.camera.left = -70;
    sun.shadow.camera.right = 70;
    sun.shadow.camera.top = 70;
    sun.shadow.camera.bottom = -70;
    sun.shadow.bias = -0.00008;
    scene.add(sun);
    const rimBlue = new THREE.PointLight('#5fb7ff', 0.8, 140);
    rimBlue.position.set(-32, 8, -44);
    scene.add(rimBlue);
    const rimOrange = new THREE.PointLight('#ff945d', 0.8, 140);
    rimOrange.position.set(32, 8, 44);
    scene.add(rimOrange);

    const cars = new Map();
    let disposed = false;

    const resize = () => {
      const width = host.clientWidth;
      const height = Math.max(host.clientHeight, 1);
      renderer.setSize(width, height);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    window.addEventListener('resize', resize);

    const templateCache = new Map();
    const runtime = {
      scene,
      renderer,
      camera,
      controls,
      cars,
        templateCache,
        assetsReady: false,
        fallbackMode: false,
        ball: null,
        boostPads: [],
        framesPayload,
        playbackFrameRef,
        cameraMode,
        mapCode,
        selectedPlayerId,
        qualityMode,
        debugMode,
        effects: {
          ballTrail: qualityMode !== 'performance',
          cameraCuts: true,
        },
        cameraState: null,
      };

    let animationId;
    const renderLoop = () => {
      applyReplayFrameToScene(runtime);
      controls.update();
      renderer.render(scene, camera);
      animationId = window.requestAnimationFrame(renderLoop);
    };
    renderLoop();

    const pitchTexture = createPitchTexture(renderer);
    const pitchShape = createRoundedRectShape(ARENA_DIMENSIONS.halfWidth, ARENA_DIMENSIONS.halfLength, ARENA_DIMENSIONS.cornerRadius);
    const pitch = new THREE.Mesh(
      new THREE.ShapeGeometry(pitchShape, 96),
      new THREE.MeshStandardMaterial({
        color: '#134f44',
        map: pitchTexture,
        roughness: 0.96,
        metalness: 0.02,
      })
    );
    pitch.rotation.x = -Math.PI / 2;
    pitch.position.y = 0.04;
    pitch.receiveShadow = true;

    loadViewerAssets()
      .then((assets) => {
        if (disposed) return;
        const arenaGroup = new THREE.Group();
        arenaGroup.add(buildArenaModel(assets, runtime.mapCode || ''));
        const boostPads = [];
        const padLayout = framesPayload?.boost_pad_layout?.length ? framesPayload.boost_pad_layout : DEFAULT_BOOST_PAD_LAYOUT;
        padLayout.forEach((pad) => {
          const group = new THREE.Group();
          const activeMesh = buildBoostPadModel(assets, !!pad.full_boost, true);
          const inactiveMesh = buildBoostPadModel(assets, !!pad.full_boost, false);
          inactiveMesh.visible = false;
          group.add(activeMesh);
          group.add(inactiveMesh);
          const pos = sceneVectorFromRl([pad.x, pad.y, 0]);
          group.position.set(pos.x, 0.03, pos.z);
          arenaGroup.add(group);
          boostPads.push({ group, activeMesh, inactiveMesh });
        });
        scene.add(arenaGroup);
        const ball = buildBallModel(assets);
        ball.castShadow = true;
        scene.add(ball);
        runtime.assetsReady = true;
        runtime.ball = ball;
        runtime.assets = assets;
        runtime.boostPads = boostPads;
      })
      .catch(() => {
        if (disposed) return;
        const fallbackArena = addArena(renderer);
        fallbackArena.add(pitch);
        scene.add(fallbackArena);
        const ball = new THREE.Mesh(
          new THREE.SphereGeometry(0.92, 20, 16),
          new THREE.MeshStandardMaterial({ color: '#f0efe9', emissive: '#3d4143', emissiveIntensity: 0.14, metalness: 0.28, roughness: 0.22 })
        );
        ball.castShadow = true;
        scene.add(ball);
        const boostPads = [];
        const padLayout = framesPayload?.boost_pad_layout?.length ? framesPayload.boost_pad_layout : DEFAULT_BOOST_PAD_LAYOUT;
        padLayout.forEach((pad) => {
          const group = new THREE.Group();
          const radius = pad.full_boost ? 0.48 : 0.32;
          const activeMesh = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, 0.08, 24),
            new THREE.MeshBasicMaterial({ color: pad.full_boost ? '#ffd06b' : '#e2b455', transparent: true, opacity: 0.82 })
          );
          const inactiveMesh = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, 0.06, 18),
            new THREE.MeshBasicMaterial({ color: '#5c4d2d', transparent: true, opacity: 0.24 })
          );
          inactiveMesh.visible = false;
          group.add(activeMesh);
          group.add(inactiveMesh);
          const pos = sceneVectorFromRl([pad.x, pad.y, 0]);
          group.position.set(pos.x, 0.08, pos.z);
          fallbackArena.add(group);
          boostPads.push({ group, activeMesh, inactiveMesh });
        });
        runtime.fallbackMode = true;
        runtime.assetsReady = true;
        runtime.ball = ball;
        runtime.boostPads = boostPads;
      });

    sceneRef.current = runtime;
    return () => {
      disposed = true;
      window.removeEventListener('resize', resize);
      if (animationId) window.cancelAnimationFrame(animationId);
      controls.dispose();
      templateCache.clear();
      disposeSceneResources(scene);
      renderer.dispose();
      host.innerHTML = '';
      sceneRef.current = null;
    };
  }, [framesPayload?.replay_id, framesPayload?.payload_version, framesPayload?.start_frame, qualityMode]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.framesPayload = framesPayload;
  }, [framesPayload]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.playbackFrameRef = playbackFrameRef;
  }, [playbackFrameRef]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.cameraMode = cameraMode;
    if (runtime.cameraState) {
      runtime.cameraState.initialized = false;
    }
  }, [cameraMode]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.selectedPlayerId = selectedPlayerId;
    if (runtime.cameraState && runtime.cameraMode === 'player') {
      runtime.cameraState.initialized = false;
    }
  }, [selectedPlayerId]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.mapCode = mapCode;
  }, [mapCode]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.debugMode = debugMode;
  }, [debugMode]);

  useEffect(() => {
    const runtime = sceneRef.current;
    if (!runtime) return;
    runtime.qualityMode = qualityMode;
    runtime.effects = {
      ballTrail: qualityMode !== 'performance',
      cameraCuts: true,
    };
  }, [qualityMode]);

  return (
    <div className="scene-shell">
      <div className="scene-canvas" ref={hostRef} />
      {children}
      {!framesPayload?.frames?.length ? <div className="scene-overlay">3D telemetry is not available for this replay yet.</div> : null}
    </div>
  );
}

function ViewerHud({ replay, cars, clockSeconds, totalFrameCount, cameraMode, selectedPlayerName = '' }) {
  const replaySeconds = Number(replay?.duration || 0);
  const elapsedSeconds = Number(clockSeconds || 0);
  const inferredDuration = totalFrameCount > 0 ? totalFrameCount / 60 : 0;
  const totalSeconds = Math.max(replaySeconds, inferredDuration);
  const remainingSeconds = Math.max(0, Math.round(totalSeconds - elapsedSeconds));
  const orderedCars = orderCarsForHud(cars || [], replay);
  const blueCars = orderedCars.filter((car) => Number(car.team) === 0);
  const orangeCars = orderedCars.filter((car) => Number(car.team) === 1);
  const cameraLabel = cameraMode === 'review'
    ? 'Replay cam'
    : cameraMode === 'player'
      ? `${shortText(selectedPlayerName || 'Player', 18)} cam`
      : cameraMode === 'ball'
        ? 'Ball cam'
        : 'Free cam';

  function BoostList({ cars: teamCars, tone }) {
    return (
      <div className={`viewer-hud-side ${tone}`}>
        {teamCars.map((car) => (
          <div className="viewer-hud-player" key={`${tone}-${car.player_id}`}>
            <div className="viewer-hud-player-copy">
              <strong>{shortText(car.player_name, 16)}</strong>
              <span>{carBodyLabel(car)}</span>
            </div>
            <div className="viewer-hud-boost">
              <div className="viewer-hud-boost-fill" style={{ width: `${Math.max(0, Math.min(100, Number(car.boost || 0)))}%` }} />
              <em>{number(car.boost, 0)}</em>
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="viewer-hud">
      <div className="viewer-scorebug">
        <div className="viewer-scorebug-team blue">
          <span>{shortText(replay?.blue_team_name || 'Blue', 24)}</span>
          <strong>{replay?.blue_goals ?? 0}</strong>
        </div>
        <div className="viewer-scorebug-center">
          <strong>{formatClock(remainingSeconds)}</strong>
          <span>{cameraLabel}</span>
        </div>
        <div className="viewer-scorebug-team orange">
          <span>{shortText(replay?.orange_team_name || 'Orange', 24)}</span>
          <strong>{replay?.orange_goals ?? 0}</strong>
        </div>
      </div>
      <div className="viewer-hud-rails">
        <BoostList cars={blueCars} tone="blue" />
        <BoostList cars={orangeCars} tone="orange" />
      </div>
    </div>
  );
}

function addWheelArch(group, material, x, z, y, width = 0.26, height = 0.28, depth = 0.72) {
  const arch = new THREE.Mesh(new RoundedBoxGeometry(width, height, depth, 3, 0.05), material);
  arch.position.set(x, y, z);
  group.add(arch);
}

function addSideSkirt(group, material, zScale = 1) {
  const skirt = new THREE.Mesh(new RoundedBoxGeometry(0.14, 0.18, 2.1 * zScale, 3, 0.04), material);
  skirt.position.set(0.95, 0.28, 0);
  group.add(skirt);
  const mirrored = skirt.clone();
  mirrored.position.x *= -1;
  group.add(mirrored);
}

function addCarDetailPackage(group, styleKey, style, accent, paint, glass) {
  if (styleKey === 'fennec' || styleKey === 'merc') {
    addWheelArch(group, accent, 0.95, style.wheelZ, 0.56, 0.3, 0.34, 0.68);
    addWheelArch(group, accent, 0.95, -style.wheelZ, 0.56, 0.3, 0.34, 0.68);
    addWheelArch(group, accent, -0.95, style.wheelZ, 0.56, 0.3, 0.34, 0.68);
    addWheelArch(group, accent, -0.95, -style.wheelZ, 0.56, 0.3, 0.34, 0.68);
    addSideSkirt(group, accent, 1.1);
    const grille = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.18, 0.06), new THREE.MeshBasicMaterial({ color: '#141a1d' }));
    grille.position.set(0, style.bodyY + 0.04, style.body[2] / 2 + 0.04);
    group.add(grille);
  }
  if (styleKey === 'fennec') {
    const hoodPlate = new THREE.Mesh(new RoundedBoxGeometry(1.24, 0.12, 0.92, 4, 0.04), accent);
    hoodPlate.position.set(0, style.bodyY + 0.18, 0.82);
    group.add(hoodPlate);

    const roofBand = new THREE.Mesh(new RoundedBoxGeometry(1.08, 0.08, 0.96, 4, 0.04), paint);
    roofBand.position.set(0, style.cabin[4] + 0.26, -0.08);
    group.add(roofBand);

    const bumperBlocks = new THREE.Mesh(new RoundedBoxGeometry(1.46, 0.16, 0.2, 4, 0.03), accent);
    bumperBlocks.position.set(0, style.bodyY - 0.06, style.body[2] / 2 + 0.12);
    group.add(bumperBlocks);
  }
  if (styleKey === 'dominus' || styleKey === 'breakout' || styleKey === 'plank') {
    const splitter = new THREE.Mesh(new RoundedBoxGeometry(1.46, 0.06, 0.44, 3, 0.03), paint);
    splitter.position.set(0, 0.16, style.body[2] / 2 + 0.16);
    group.add(splitter);

    const hood = new THREE.Mesh(new RoundedBoxGeometry(1.22, 0.1, 1.02, 4, 0.04), accent);
    hood.position.set(0, style.bodyY + 0.16, 0.78);
    hood.rotation.x = -0.12;
    group.add(hood);

    addSideSkirt(group, paint, 1.12);
  }
  if (styleKey === 'dominus') {
    const hoodVent = new THREE.Mesh(new RoundedBoxGeometry(0.86, 0.06, 1.02, 4, 0.03), accent);
    hoodVent.position.set(0, style.bodyY + 0.14, 0.66);
    group.add(hoodVent);

    const tailWing = new THREE.Mesh(new RoundedBoxGeometry(1.52, 0.08, 0.18, 4, 0.03), accent);
    tailWing.position.set(0, style.bodyY + 0.54, -style.wheelZ - 0.52);
    group.add(tailWing);
  }
  if (styleKey === 'octane' || styleKey === 'hybrid') {
    const fender = new THREE.Mesh(new RoundedBoxGeometry(1.5, 0.18, 0.62, 4, 0.05), accent);
    fender.position.set(0, style.bodyY + 0.06, 1.1);
    fender.rotation.x = -0.08;
    group.add(fender);

    const roofScoop = new THREE.Mesh(new RoundedBoxGeometry(0.56, 0.1, 0.42, 3, 0.04), glass);
    roofScoop.position.set(0, style.cabin[4] + 0.18, -0.08);
    group.add(roofScoop);
  }
}

function createCarMesh(car, templateCache) {
  const templateKey = `${car?.team ?? 0}:${carStyleKey(car)}`;
  const cachedTemplate = templateCache?.get(templateKey);
  if (cachedTemplate) {
    return cachedTemplate.clone(true);
  }

  const team = car?.team ?? 0;
  const styleKey = carStyleKey(car);
  const style = CAR_BODY_STYLES[styleKey] || CAR_BODY_STYLES.octane;
  const group = new THREE.Group();
  const bodyRoot = new THREE.Group();
  group.add(bodyRoot);
  const primaryColor = team === 0 ? '#20adff' : '#ff8a4d';
  const accentColor = team === 0 ? '#bfefff' : '#ffd2b9';
  const paint = new THREE.MeshStandardMaterial({
    color: primaryColor,
    emissive: primaryColor,
    emissiveIntensity: 0.12,
    metalness: 0.28,
    roughness: 0.3,
  });
  const accent = new THREE.MeshStandardMaterial({
    color: accentColor,
    metalness: 0.16,
    roughness: 0.34,
  });
  const glass = new THREE.MeshPhysicalMaterial({
    color: '#1d2a34',
    transparent: true,
    opacity: 0.9,
    transmission: 0.22,
    roughness: 0.06,
    metalness: 0.02,
  });

  const body = new THREE.Mesh(new RoundedBoxGeometry(...style.body, 6, 0.14), paint);
  body.position.y = style.bodyY;
  bodyRoot.add(body);

  const bumper = new THREE.Mesh(new RoundedBoxGeometry(style.bumper[0], style.bumper[1], style.bumper[2], 4, 0.08), accent);
  bumper.position.set(0, style.bodyY - 0.14, style.bumper[3]);
  bodyRoot.add(bumper);

  const rearDeck = new THREE.Mesh(new RoundedBoxGeometry(style.rearDeck[0], style.rearDeck[1], style.rearDeck[2], 4, 0.06), accent);
  rearDeck.position.set(0, style.rearDeck[4], style.rearDeck[3]);
  bodyRoot.add(rearDeck);

  const cabin = new THREE.Mesh(new RoundedBoxGeometry(style.cabin[0], style.cabin[1], style.cabin[2], 6, 0.14), accent);
  cabin.position.set(0, style.cabin[4], style.cabin[3]);
  bodyRoot.add(cabin);

  const windshield = new THREE.Mesh(new THREE.BoxGeometry(style.windshield[0], style.windshield[1], style.windshield[2]), glass);
  windshield.position.set(0, style.windshield[4], style.windshield[3]);
  windshield.rotation.x = style.windshield[5];
  bodyRoot.add(windshield);

  const rearGlass = new THREE.Mesh(new THREE.BoxGeometry(style.rearGlass[0], style.rearGlass[1], style.rearGlass[2]), glass);
  rearGlass.position.set(0, style.rearGlass[4], style.rearGlass[3]);
  rearGlass.rotation.x = style.rearGlass[5];
  bodyRoot.add(rearGlass);

  if (style.spoiler) {
    const spoilerPosts = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.26, 0.1), accent);
    spoilerPosts.position.set(-0.48, style.bodyY + 0.47, -style.wheelZ - 0.38);
    bodyRoot.add(spoilerPosts);

    const spoilerPostsRight = spoilerPosts.clone();
    spoilerPostsRight.position.x *= -1;
    bodyRoot.add(spoilerPostsRight);

    const spoiler = new THREE.Mesh(new RoundedBoxGeometry(style.spoilerWidth || 1.28, 0.1, 0.32, 4, 0.05), paint);
    spoiler.position.set(0, style.bodyY + 0.64, -style.wheelZ - 0.4);
    bodyRoot.add(spoiler);
  }

  const headlights = new THREE.Mesh(new THREE.BoxGeometry(1.24, 0.08, 0.06), new THREE.MeshBasicMaterial({ color: '#ffe9b1' }));
  headlights.position.set(0, style.bodyY + 0.16, style.body[2] / 2 + 0.02);
  bodyRoot.add(headlights);

  const taillights = new THREE.Mesh(new THREE.BoxGeometry(1.06, 0.08, 0.06), new THREE.MeshBasicMaterial({ color: '#ff6b55' }));
  taillights.position.set(0, style.bodyY + 0.1, -(style.body[2] / 2 + 0.02));
  bodyRoot.add(taillights);

  addCarDetailPackage(bodyRoot, styleKey, style, accent, paint, glass);

  [-style.wheelX, style.wheelX].forEach((x) => {
    [-style.wheelZ, style.wheelZ].forEach((z) => {
      const wheel = new THREE.Mesh(
        new THREE.CylinderGeometry(style.wheelRadius, style.wheelRadius, 0.24, 16),
        new THREE.MeshStandardMaterial({ color: '#151515' })
      );
      wheel.rotation.z = Math.PI / 2;
      wheel.position.set(x, 0.28, z);
      bodyRoot.add(wheel);

      const rim = new THREE.Mesh(
        new THREE.CylinderGeometry(style.wheelRadius * 0.5, style.wheelRadius * 0.5, 0.26, 14),
        new THREE.MeshStandardMaterial({ color: '#b9c3ca', metalness: 0.72, roughness: 0.26 })
      );
      rim.rotation.z = Math.PI / 2;
      rim.position.set(x, 0.28, z);
      bodyRoot.add(rim);
    });
  });

  group.traverse((child) => {
    if (child instanceof THREE.Mesh) {
      child.castShadow = true;
      child.receiveShadow = true;
    }
  });

  templateCache?.set(templateKey, group.clone(true));
  return group;
}

function Viewer({ viewer, loading = false, selectedReplayCard = null, onRefreshReplay, onError }) {
  const replay = viewer?.replay;
  const prediction = (viewer?.predictions || []).find((item) => item.prediction_type === 'blue_win_probability');
  const replayEval = viewer?.eval;
  const playerImpact = viewer?.player_impact || [];
  const featuredVideo = replay?.videos?.[0];
  const [videoBusy, setVideoBusy] = useState(false);
  const nativeFrameRef = useRef(null);
  const [nativeFrameHeight, setNativeFrameHeight] = useState(780);
  const nativeStatus = String(replay?.carball_status?.status || '').toLowerCase();
  const nativeViewerReady = nativeStatus === 'completed';
  const grouped = useMemo(() => groupPlayers(replay?.players || []), [replay?.players]);
  const nativeViewerSrc = useMemo(() => {
    if (!replay?.replay_id) return '';
    const query = new URLSearchParams({
      replayId: replay.replay_id,
      apiBase: API_BASE,
      embed: '1',
    });
    return `/native-viewer/index.html?${query.toString()}`;
  }, [replay?.replay_id]);
  const fullNativeViewerHref = useMemo(() => {
    if (!replay?.replay_id) return '';
    const query = new URLSearchParams({
      replayId: replay.replay_id,
      apiBase: API_BASE,
    });
    return `/native-viewer/index.html?${query.toString()}`;
  }, [replay?.replay_id]);
  const viewerRoster = useMemo(
    () => (replay?.players || []).slice().sort((left, right) => {
      const leftSide = String(left.side || '').toLowerCase();
      const rightSide = String(right.side || '').toLowerCase();
      if (leftSide !== rightSide) return leftSide.localeCompare(rightSide);
      return String(left.player_name || '').localeCompare(String(right.player_name || ''));
    }),
    [replay?.players]
  );

  async function syncReplayVideos() {
    if (!replay?.replay_id) return;
    setVideoBusy(true);
    try {
      await fetchJson('/sources/youtube/sync', {
        method: 'POST',
        body: JSON.stringify({ replay_id: replay.replay_id, limit: 1 }),
      });
      await onRefreshReplay(replay.replay_id, { force: true });
    } catch (error) {
      onError(error.message);
    } finally {
      setVideoBusy(false);
    }
  }

  if (!replay) {
    if (loading && selectedReplayCard?.replay_id) {
      return (
        <div className="viewer-shell empty-state">
          Loading {selectedReplayCard.title || `${selectedReplayCard.blue_team_name || 'Blue'} vs ${selectedReplayCard.orange_team_name || 'Orange'}`}...
        </div>
      );
    }
    return <div className="viewer-shell empty-state">Pick a replay to load the viewer.</div>;
  }

  const nativeViewerMessage = nativeStatus === 'failed'
    ? (replay?.carball_status?.error || 'This replay does not have a usable local native-viewer parse yet.')
    : nativeStatus === 'running'
      ? 'This replay is still being parsed into 60 Hz native-viewer telemetry.'
      : 'This replay is still metadata-only for the native 3D viewer. Pick a replay with a completed local parse to watch it here.';

  useEffect(() => {
    if (!nativeViewerReady || !nativeViewerSrc) return undefined;
    const frame = nativeFrameRef.current;
    if (!frame) return undefined;
    let timer = null;
    const syncHeight = () => {
      try {
        const doc = frame.contentDocument;
        if (!doc) return;
        const nextHeight = Math.max(
          doc.documentElement?.scrollHeight || 0,
          doc.body?.scrollHeight || 0,
          780,
        );
        const resolved = Math.min(nextHeight + 16, 1700);
        setNativeFrameHeight((current) => (Math.abs(current - resolved) > 8 ? resolved : current));
      } catch {
        // iframe can briefly be unavailable while the native viewer is loading
      }
    };
    const onLoad = () => {
      syncHeight();
      if (timer) window.clearInterval(timer);
      timer = window.setInterval(syncHeight, 900);
    };
    frame.addEventListener('load', onLoad);
    onLoad();
    return () => {
      frame.removeEventListener('load', onLoad);
      if (timer) window.clearInterval(timer);
    };
  }, [nativeViewerReady, nativeViewerSrc, replay?.replay_id]);

  return (
    <div className="viewer-shell">
      <div className="viewer-head">
        <div>
          <p className="kicker">Replay Viewer</p>
          <h2>{replay.title || replay.replay_id}</h2>
          <div className="tag-row">
            {(replay.series || []).slice(0, 3).map((group) => <span className="tag" key={group.group_id}>{group.group_name}</span>)}
            <span className="tag muted">{replay.playlist_id || 'local corpus'}</span>
            <span className="tag muted">{formatDate(replay.match_date || replay.ingested_at)}</span>
            <span className="tag muted">Native 60 Hz replay viewer</span>
          </div>
        </div>
        <div className="scoreline">
          <div>
            <span>{replay.blue_team_name || 'Blue Side'}</span>
            <strong>{replay.blue_goals ?? 0}</strong>
          </div>
          <div>
            <span>{replay.orange_team_name || 'Orange Side'}</span>
            <strong>{replay.orange_goals ?? 0}</strong>
          </div>
        </div>
      </div>

      <div className="viewer-actions">
        {replay.local_file_path ? <a href={`${API_BASE}/library/replays/${replay.replay_id}/file`} target="_blank" rel="noreferrer">Download replay</a> : null}
        <span>{replay.carball_status?.status === 'completed' ? 'Carball replay parse ready at 60 Hz' : replay.has_semantic_features ? 'Frame-derived semantics ready' : 'Metadata only until the local replay parse finishes'}</span>
        <span>{prediction ? `Blue win model ${percent(prediction.probability)}` : 'Model score pending'}</span>
        {loading && selectedReplayCard?.replay_id && selectedReplayCard.replay_id !== replay.replay_id ? (
          <span>Loading {shortText(selectedReplayCard.title || selectedReplayCard.replay_id, 38)}...</span>
        ) : null}
      </div>

      <WinEdgeBar edge={viewer.win_edge} />
      <div className="review-help">
        <span>Impact swings are shown in win-probability points for the credited player or team.</span>
        <ReviewTimelineLegend />
      </div>

      <div className="eval-summary">
        <div className="eval-card">
          <span>Blue win</span>
          <strong>{percent(replayEval?.final_blue_probability)}</strong>
          <em>Start {percent(replayEval?.base_blue_probability)}</em>
        </div>
        <div className="eval-card">
          <span>Orange win</span>
          <strong>{replayEval?.final_blue_probability === null || replayEval?.final_blue_probability === undefined ? 'n/a' : percent(1 - replayEval.final_blue_probability)}</strong>
          <em>Live edge from replay events</em>
        </div>
        <div className="eval-card">
          <span>Volatility</span>
          <strong>{swingPointsText(replayEval?.volatility_points, 1)}</strong>
          <em>{metricValue(replayEval?.swing_count)} major win-probability swings</em>
        </div>
        <div className="eval-card">
          <span>Swing count</span>
          <strong>{metricValue(replayEval?.swing_count)}</strong>
          <em>{replayEval?.largest_swing ? `${impactSwingText(replayEval.largest_swing)} at ${number(replayEval.largest_swing.t, 1)}s` : 'No swing registered'}</em>
        </div>
        <div className="eval-card">
          <span>Largest blunder</span>
          <strong>{replayEval?.blunders?.[0]?.player_name || replayEval?.blunders?.[0]?.team_color || 'None yet'}</strong>
          <em>{replayEval?.blunders?.[0] ? `${impactSwingText(replayEval.blunders[0])} at ${number(replayEval.blunders[0].t, 1)}s` : 'No major blunder flagged'}</em>
        </div>
        <div className="eval-card">
          <span>Clutch play</span>
          <strong>{replayEval?.clutch_plays?.[0]?.player_name || replayEval?.clutch_plays?.[0]?.team_color || 'None yet'}</strong>
          <em>{replayEval?.clutch_plays?.[0] ? `${impactSwingText(replayEval.clutch_plays[0])} at ${number(replayEval.clutch_plays[0].t, 1)}s` : 'No late-game dagger flagged'}</em>
        </div>
        <div className="eval-card">
          <span>Impact leader</span>
          <strong>{playerImpact?.[0]?.player_name || 'None yet'}</strong>
          <em>{playerImpactSummaryText(playerImpact?.[0])}</em>
        </div>
      </div>

      <section className="media-panel viewer-stage-panel">
        <div className="media-panel-head">
          <div>
            <p className="kicker">3D Replay</p>
            <h3>Native replay cameras and playback</h3>
          </div>
          {fullNativeViewerHref ? (
            <a className="ghost-button" href={fullNativeViewerHref} target="_blank" rel="noreferrer">
              Open full viewer
            </a>
          ) : null}
        </div>
        {nativeViewerSrc && nativeViewerReady ? (
          <iframe
            key={replay.replay_id}
            ref={nativeFrameRef}
            className="native-viewer-frame"
            src={nativeViewerSrc}
            style={{ height: `${nativeFrameHeight}px` }}
            title={`${replay.title || replay.replay_id} native viewer`}
            allow="fullscreen; autoplay; clipboard-write"
          />
        ) : (
          <div className="native-viewer-empty">
            <strong>Native 3D viewer not ready for this replay.</strong>
            <p>{nativeViewerMessage}</p>
            <span>{nativeStatus === 'failed' ? 'The rest of the replay page is still usable: roster, eval, box score, and video links.' : 'Use the Replays tab with the 3D-ready filter to jump into a fully parsed match.'}</span>
          </div>
        )}
        <div className="current-roster">
          {viewerRoster.slice(0, 8).map((player) => (
            <div className="roster-chip static" key={`${player.side}-${player.player_name}`}>
              <strong>{shortText(player.player_name, 24)}</strong>
              <span>{player.side === 'orange' ? (replay.orange_team_name || 'Orange') : (replay.blue_team_name || 'Blue')}</span>
              <em>{player.car_name || 'Octane'}</em>
            </div>
          ))}
        </div>
      </section>

      <div className="viewer-support-grid">
        <section className="media-panel">
          <div className="media-panel-head">
            <div>
              <p className="kicker">Replay Video</p>
              <h3>YouTube links for this replay</h3>
            </div>
            <button type="button" onClick={syncReplayVideos} disabled={videoBusy}>{videoBusy ? 'Syncing...' : 'Sync this replay video'}</button>
          </div>
          {featuredVideo ? (
            <div className="video-stack">
              <div className="video-frame-wrap">
                <iframe
                  className="video-frame"
                  src={featuredVideo.embed_url}
                  title={featuredVideo.title}
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                  allowFullScreen
                />
              </div>
              <div className="video-meta">
                <strong>{featuredVideo.title}</strong>
                {featuredVideo.segment_label ? <span>{featuredVideo.segment_label}</span> : null}
                <span>{videoKindLabel(featuredVideo)}</span>
                {formatVideoWindow(featuredVideo) ? <span>{formatVideoWindow(featuredVideo)}</span> : null}
                <span>{featuredVideo.channel_title}</span>
                <span>{formatDate(featuredVideo.published_at)}</span>
              </div>
              <div className="video-list">
                {(replay.videos || []).map((video) => (
                  <a className="video-link" key={video.video_id} href={video.watch_url} target="_blank" rel="noreferrer">
                    <img src={video.thumbnail_url} alt={video.title} />
                    <div>
                      <strong>{shortText(video.title, 58)}</strong>
                      <span>{videoKindLabel(video)}</span>
                      {video.segment_label ? <span>{video.segment_label}</span> : null}
                      {formatVideoWindow(video) ? <span>{formatVideoWindow(video)}</span> : null}
                      <span>{video.channel_title}</span>
                      <em>score {number(video.match_score, 2)}</em>
                    </div>
                  </a>
                ))}
              </div>
            </div>
          ) : (
            <div className="empty-state">No synced video yet. Use the replay-level or batch YouTube sync action after Ballchasing metadata is available, and ReplayOS will use exact chapter clips when it can or estimated game jumps inside the full series VOD when it cannot.</div>
          )}
        </section>
      </div>

      <div className="viewer-grid">
        <section className="viewer-column">
          <h3>Turning points</h3>
          <div className="stack-list">
            {(viewer.timeline?.turning_points || []).slice(0, 8).map((point, index) => (
              <div className="stack-row" key={`${point.t}-${index}`} title={point.label || point.event_type}>
                <span>{`${reviewMarkerMeta(point.event_type).short} ${number(point.t, 1)}s`}</span>
                <strong>{point.label}</strong>
                <em>{reviewMarkerMeta(point.event_type).label}</em>
              </div>
            ))}
          </div>
        </section>

        <section className="viewer-column">
          <h3>Blunders</h3>
          <div className="stack-list">
            {(replayEval?.blunders || []).slice(0, 8).map((item, index) => (
              <div className={`stack-row eval-row ${impactTone(item)}`} key={`${item.t}-${index}`} title={impactWindowText(item) || item.label}>
                <span>{number(item.t, 1)}s</span>
                <strong>{item.label}</strong>
                <em>{impactSwingText(item)}</em>
              </div>
            ))}
          </div>
        </section>

        <section className="viewer-column">
          <h3>Best plays</h3>
          <div className="stack-list">
            {(replayEval?.plays || []).slice(0, 8).map((item, index) => (
              <div className={`stack-row eval-row positive ${impactTone(item)}`} key={`${item.t}-${index}`} title={impactWindowText(item) || item.label}>
                <span>{number(item.t, 1)}s</span>
                <strong>{item.label}</strong>
                <em>{impactSwingText(item)}</em>
              </div>
            ))}
          </div>
        </section>

        <section className="viewer-column">
          <h3>Clutch plays</h3>
          <div className="stack-list">
            {(replayEval?.clutch_plays || []).slice(0, 8).map((item, index) => (
              <div className={`stack-row eval-row positive ${impactTone(item)}`} key={`${item.t}-${index}`} title={impactWindowText(item) || item.label}>
                <span>{number(item.t, 1)}s</span>
                <strong>{item.label}</strong>
                <em>{impactSwingText(item)}</em>
              </div>
            ))}
          </div>
        </section>

        <section className="viewer-column">
          <h3>Player impact</h3>
          <div className="stack-list">
            {playerImpact.slice(0, 8).map((item, index) => (
              <div className="stack-row" key={`${item.player_name}-${index}`}>
                <span>{swingPointsText(item.net_swing_points, 1)}</span>
                <strong>{item.player_name}</strong>
                <em>{`${item.goals || 0} G, ${item.touches || 0} touches, ${item.positive_swings || 0}/${item.negative_swings || 0} swings, ${swingPointsText(item.advantage_per_touch_points, 1)} per touch`}</em>
              </div>
            ))}
          </div>
        </section>

        <section className="viewer-column">
          <h3>Model reason codes</h3>
          <div className="stack-list">
            {(prediction?.reasons || []).slice(0, 6).map((reason, index) => (
              <div className="stack-row" key={`${reason.feature || reason.name}-${index}`}>
                <span>{reason.feature || reason.name}</span>
                <strong>{number(reason.contribution ?? reason.value_z ?? 0, 3)}</strong>
                <em>{number(reason.value_z ?? reason.value ?? 0, 3)}</em>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="boxscores">
        <div className="boxscore-side blue">
          <h3>{replay.blue_team_name || 'Blue Side'}</h3>
          {(grouped.blue.length ? grouped.blue : [{ player_name: 'No downloaded player box score yet' }]).map((player, index) => (
            <div className="boxscore-row" key={`${player.player_name}-${index}`}>
              <div className="boxscore-namecell">
                <strong>{player.player_name}</strong>
                <em>{carBodyLabel(player)}</em>
              </div>
              <span>{player.goals ?? 0} G</span>
              <span>{player.assists ?? 0} A</span>
              <span>{player.saves ?? 0} S</span>
              <span>{player.score ?? '-'} score</span>
            </div>
          ))}
        </div>
        <div className="boxscore-side orange">
          <h3>{replay.orange_team_name || 'Orange Side'}</h3>
          {(grouped.orange.length ? grouped.orange : [{ player_name: 'No downloaded player box score yet' }]).map((player, index) => (
            <div className="boxscore-row" key={`${player.player_name}-${index}`}>
              <div className="boxscore-namecell">
                <strong>{player.player_name}</strong>
                <em>{carBodyLabel(player)}</em>
              </div>
              <span>{player.goals ?? 0} G</span>
              <span>{player.assists ?? 0} A</span>
              <span>{player.saves ?? 0} S</span>
              <span>{player.score ?? '-'} score</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function AnalystResult({ answer }) {
  if (!answer) return null;
  if (answer.intent === 'player_impact') {
    return (
      <div className="analyst-answer">
        <strong>Player impact</strong>
        <p>{answer.answer}</p>
        <div className="stack-list">
          {(answer.data || []).slice(0, 8).map((row, index) => (
            <div className="stack-row analyst-row" key={`${row.player_id || row.player_name}-${row.replay_id || index}`}>
              <span>{row.team_color || 'team'}</span>
              <strong>{row.player_name}</strong>
              <em>{number(row.impact_score, 3)} impact, {row.goals || 0} goals, {row.touches || 0} touches</em>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (answer.intent === 'momentum') {
    return (
      <div className="analyst-answer">
        <strong>Momentum map</strong>
        <p>{answer.answer}</p>
        <div className="stack-list">
          {(answer.data || []).map((row, index) => (
            <div className="stack-row analyst-row" key={`${row.segment}-${row.team_color}-${index}`}>
              <span>{row.segment}</span>
              <strong>{row.team_name || row.team_color}</strong>
              <em>{row.goals || 0} goals, {row.touches || 0} touches, {row.demos || 0} demos, {row.starvation_windows || 0} starve windows</em>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (answer.intent === 'opponent_weakness') {
    return (
      <div className="analyst-answer">
        <strong>Pressure targets</strong>
        <p>{answer.answer}</p>
        <div className="stack-list">
          {(answer.data || []).slice(0, 8).map((row, index) => (
            <div className="stack-row analyst-row" key={`${row.team_name}-${index}`}>
              <span>{row.rlcs_matches ? `${row.rlcs_matches} RLCS` : `${row.matches} matches`}</span>
              <strong>{row.team_name}</strong>
              <em>
                {row.starvation_rate !== null && row.starvation_rate !== undefined
                  ? `starve ${number(row.starvation_rate, 2)}, overcommit ${number(row.overcommit_rate, 2)}, clutch ${number(row.clutch_boost_advantage, 2)}`
                  : `pressure leaks ${number(row.overcommit_rate, 1)}/match, pressure rate ${number(row.pressure_rate, 2)}`}
                {row.latest_event ? ` - ${shortText(row.latest_event, 28)}` : ''}
              </em>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (answer.intent === 'model_evaluation') {
    return (
      <div className="analyst-answer">
        <strong>Model runs</strong>
        <p>{answer.answer}</p>
        <div className="stack-list">
          {(answer.data || []).slice(0, 6).map((row) => (
            <div className="stack-row analyst-row" key={row.model_version_id}>
              <span>{row.model_type}</span>
              <strong>{row.name}</strong>
              <em>{row.metrics?.accuracy ? `accuracy ${number(row.metrics.accuracy, 3)}` : row.target}</em>
            </div>
          ))}
        </div>
      </div>
    );
  }
  const counts = answer.data?.counts;
  const coverage = answer.data?.coverage;
  return (
    <div className="analyst-answer">
      <strong>{labelFromSlug(answer.intent || 'summary')}</strong>
      <p>{answer.answer}</p>
      {counts ? (
        <div className="stat-grid compact-grid">
          <StatMini label="Indexed replays" value={metricValue(counts.replays)} note={`${coverage?.parsed_replays || 0} parsed locally`} />
          <StatMini label="Semantic events" value={metricValue(counts.events)} note={`${coverage?.remote_replays || 0} Ballchasing replays`} />
          <StatMini label="Team features" value={metricValue(counts.team_match_features)} note={`${counts.predictions || 0} predictions`} />
          <StatMini label="Live boards" value={metricValue(coverage?.leaderboard_rows)} note="public standings cache" />
        </div>
      ) : (
        <pre>{JSON.stringify(answer.data, null, 2)}</pre>
      )}
    </div>
  );
}

function AnalystDesk({ replayId, onError }) {
  const [question, setQuestion] = useState(ANALYST_PROMPTS[0]);
  const [answer, setAnswer] = useState(null);

  async function runQuery(nextQuestion = question) {
    try {
      const payload = await fetchJson('/analyst/query', {
        method: 'POST',
        body: JSON.stringify({ question: nextQuestion, replay_id: replayId || null }),
      });
      setAnswer(payload);
    } catch (error) {
      onError(error.message);
    }
  }

  async function submit(event) {
    event.preventDefault();
    await runQuery(question);
  }

  return (
    <div className="analyst-desk">
      <div>
        <p className="kicker">Analyst Desk</p>
        <h3>Ask the match database something useful.</h3>
      </div>
      <div className="tag-row compact">
        {ANALYST_PROMPTS.map((prompt) => (
          <button key={prompt} type="button" className="secondary-button prompt-chip" onClick={() => { setQuestion(prompt); setAnswer(null); void runQuery(prompt); }}>
            {prompt}
          </button>
        ))}
      </div>
      <form className="analyst-form" onSubmit={submit}>
        <input value={question} onChange={(event) => setQuestion(event.target.value)} />
        <button type="submit">Run query</button>
      </form>
      {answer ? <AnalystResult answer={answer} /> : null}
    </div>
  );
}

const TAB_DEFS = [
  { id: 'overview', label: 'Overview', kicker: 'Overview', title: 'A calmer front page.', note: 'Quick pulse checks, featured replays, and the stuff worth opening first.' },
  { id: 'live', label: 'Live', kicker: 'Live Desk', title: 'Streams, slates, and standings in one place.', note: 'RLCS and pro-play watch coverage, with minute-level refreshes and leaderboard context.' },
  { id: 'replays', label: 'Replays', kicker: 'Replay Library', title: 'Browse the shelf without fighting the rest of the site.', note: 'Search, filter, and pick a replay, then hop straight into the viewer.' },
  { id: 'viewer', label: 'Viewer', kicker: 'Replay Viewer', title: '3D replay, eval swings, and video sync.', note: 'Open one match and stay with it instead of losing it in a mega-page.' },
  { id: 'rankings', label: 'Rankings', kicker: 'Rankings', title: 'Power board and player tables.', note: 'Standings-aware ratings, top scorers, and the signals driving the table.' },
  { id: 'records', label: 'Records', kicker: 'Records', title: 'All-time team and player history.', note: 'Profiles, rivalries, head-to-heads, and frequency boards from the named replay corpus.' },
  { id: 'analyst', label: 'Analyst', kicker: 'Analyst Desk', title: 'Ask the data something useful.', note: 'Query the match database and inspect the event mix the parser is actually seeing.' },
  { id: 'ops', label: 'Ops', kicker: 'Pipeline Ops', title: 'Syncs, upkeep, and data health.', note: 'All the machinery that keeps the site alive, now moved out of the front door.' },
];

const TAB_ALIAS = {
  home: 'overview',
  overview: 'overview',
  live: 'live',
  matches: 'replays',
  replays: 'replays',
  library: 'replays',
  viewer: 'viewer',
  rankings: 'rankings',
  records: 'records',
  analyst: 'analyst',
  ops: 'ops',
};

function normalizeTabId(value) {
  const normalized = String(value || '').replace(/^#/, '').trim().toLowerCase();
  return TAB_ALIAS[normalized] || null;
}

function getInitialTab() {
  if (typeof window === 'undefined') return 'overview';
  return normalizeTabId(window.location.hash) || 'overview';
}


function isNativeViewerRoute() {
  if (typeof window === 'undefined') return false;
  const query = new URLSearchParams(window.location.search);
  return Boolean(query.get('replayId')) && /native-viewer/i.test(window.location.pathname);
}

function useNativeViewerQuery() {
  return useMemo(() => {
    const query = new URLSearchParams(window.location.search);
    return {
      replayId: query.get('replayId') || '',
      title: query.get('title') || '',
      startFrame: Math.max(0, Number(query.get('startFrame') || 0)),
    };
  }, []);
}

function NativeReplayTimeline({ frame, totalFrames, events = [], onSeek }) {
  const pct = totalFrames ? Math.max(0, Math.min(100, (frame / totalFrames) * 100)) : 0;
  return (
    <div className="native-direct-timeline">
      <div className="native-direct-track" onClick={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const ratio = (event.clientX - rect.left) / Math.max(1, rect.width);
        onSeek(Math.round(ratio * Math.max(0, totalFrames - 1)));
      }}>
        <div className="native-direct-progress" style={{ width: `${pct}%` }} />
        {(events || []).slice(0, 80).map((item, index) => {
          const markerFrame = Number(item.frame ?? item.frame_number ?? item.start_frame ?? 0);
          const left = totalFrames ? Math.max(0, Math.min(100, (markerFrame / totalFrames) * 100)) : 0;
          return <button key={`${markerFrame}-${index}`} type="button" className={`native-direct-marker ${item.type || item.event_type || 'event'}`} style={{ left: `${left}%` }} title={`${item.type || item.event_type || 'event'} @ ${markerFrame}`} onClick={(event) => { event.stopPropagation(); onSeek(markerFrame); }} />;
        })}
      </div>
    </div>
  );
}

function NativeReplayMiniMap({ cars = [], ball }) {
  const points = cars.map((car) => {
    const pos = car.pos || [0, 0, 0];
    return {
      id: car.player_id,
      team: Number(car.team || 0),
      x: 50 + (Number(pos[0] || 0) / 8200) * 100,
      y: 50 + (Number(pos[1] || 0) / 10400) * 100,
    };
  });
  const ballPoint = ball?.pos ? {
    x: 50 + (Number(ball.pos[0] || 0) / 8200) * 100,
    y: 50 + (Number(ball.pos[1] || 0) / 10400) * 100,
  } : null;
  return (
    <div className="native-direct-radar">
      {ballPoint ? <span className="native-direct-radar-ball" style={{ left: `${ballPoint.x}%`, top: `${ballPoint.y}%` }} /> : null}
      {points.map((point) => <span key={point.id} className={`native-direct-radar-car ${point.team === 1 ? 'orange' : 'blue'}`} style={{ left: `${point.x}%`, top: `${point.y}%` }} />)}
    </div>
  );
}

function NativeReplayViewerApp() {
  const { replayId, startFrame: initialStartFrame } = useNativeViewerQuery();
  const [chunkStartFrame, setChunkStartFrame] = useState(initialStartFrame);
  const { framesPayload, frameError, frameLoading } = useReplayFrames(replayId, chunkStartFrame);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(1);
  const [cameraMode, setCameraMode] = useState('director');
  const [selectedPlayerId, setSelectedPlayerId] = useState('');
  const [qualityMode, setQualityMode] = useState('balanced');
  const [debugMode, setDebugMode] = useState(false);
  const [displayFrame, setDisplayFrame] = useState(initialStartFrame);
  const playbackFrameRef = useRef(initialStartFrame);
  const lastTickRef = useRef(0);

  const firstFrame = framesPayload?.frames?.[0] || null;
  const players = useMemo(() => framesPayload?.players || firstFrame?.cars || [], [framesPayload, firstFrame]);
  const selectedPlayerName = players.find((player) => String(player.player_id) === String(selectedPlayerId))?.player_name || '';
  const totalFrames = Number(framesPayload?.total_frame_count || framesPayload?.frame_count || 0);
  const currentLocal = Math.max(0, Math.min((framesPayload?.frames || []).length - 1, Math.floor(displayFrame - Number(framesPayload?.start_frame || 0))));
  const currentFrame = framesPayload?.frames?.[currentLocal] || firstFrame;

  useEffect(() => {
    if (!selectedPlayerId && players.length) {
      setSelectedPlayerId(players[0].player_id);
    }
  }, [players, selectedPlayerId]);

  useEffect(() => {
    playbackFrameRef.current = chunkStartFrame;
    setDisplayFrame(chunkStartFrame);
  }, [replayId, chunkStartFrame]);

  useEffect(() => {
    let raf = 0;
    const tick = (now) => {
      if (!lastTickRef.current) lastTickRef.current = now;
      const deltaMs = Math.min(80, Math.max(0, now - lastTickRef.current));
      lastTickRef.current = now;
      if (playing && framesPayload?.frames?.length) {
        const nextFrame = playbackFrameRef.current + (deltaMs / 1000) * VIEWER_PLAYBACK_HZ * speed;
        const maxFrame = Math.max(0, totalFrames - 1);
        playbackFrameRef.current = Math.min(maxFrame, nextFrame);
        setDisplayFrame(playbackFrameRef.current);
        const chunkEnd = Number(framesPayload.start_frame || 0) + Number(framesPayload.frame_count || framesPayload.frames.length || 0) - 90;
        if (playbackFrameRef.current >= chunkEnd && chunkEnd > 0 && playbackFrameRef.current < maxFrame) {
          const nextStart = Number(framesPayload.start_frame || 0) + Number(framesPayload.frame_count || framesPayload.frames.length || 0);
          setChunkStartFrame(nextStart);
        }
      }
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(raf);
  }, [playing, speed, framesPayload, totalFrames]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.target && ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) return;
      if (event.code === 'Space') { event.preventDefault(); setPlaying((value) => !value); }
      if (event.key === 'ArrowRight') { playbackFrameRef.current += event.shiftKey ? 60 : 1; setDisplayFrame(playbackFrameRef.current); }
      if (event.key === 'ArrowLeft') { playbackFrameRef.current = Math.max(0, playbackFrameRef.current - (event.shiftKey ? 60 : 1)); setDisplayFrame(playbackFrameRef.current); }
      if (event.key === 'b') setCameraMode('director');
      if (event.key === 'p') setCameraMode('pov');
      if (event.key === 'o') setCameraMode('pov-ball');
      if (event.key === 'c') setCameraMode('player');
      if (event.key === 'f') setCameraMode('free');
      if (/^[1-6]$/.test(event.key)) {
        const player = players[Number(event.key) - 1];
        if (player) setSelectedPlayerId(player.player_id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [players]);

  function seek(frame) {
    const nextFrame = Math.max(0, Math.min(Math.max(0, totalFrames - 1), Number(frame) || 0));
    const chunk = Math.floor(nextFrame / VIEWER_CHUNK_FRAMES) * VIEWER_CHUNK_FRAMES;
    setChunkStartFrame(chunk);
    playbackFrameRef.current = nextFrame;
    setDisplayFrame(nextFrame);
  }

  const replay = {
    blue_team_name: framesPayload?.teams?.blue || 'Blue',
    orange_team_name: framesPayload?.teams?.orange || 'Orange',
    blue_goals: framesPayload?.score?.blue ?? 0,
    orange_goals: framesPayload?.score?.orange ?? 0,
    duration: totalFrames / VIEWER_PLAYBACK_HZ,
  };

  if (!replayId) {
    return <div className="native-direct-root"><div className="native-direct-status">Missing replay id.</div></div>;
  }

  return (
    <div className="native-direct-root">
      <div className="native-direct-stage">
        <ReplayScene
          key={`${replayId}-${framesPayload?.start_frame || 0}-${qualityMode}`}
          framesPayload={framesPayload}
          playbackFrameRef={playbackFrameRef}
          cameraMode={cameraMode}
          selectedPlayerId={selectedPlayerId}
          mapCode={framesPayload?.map_code || framesPayload?.mapCode || ''}
          qualityMode={qualityMode}
          debugMode={debugMode}
        >
          <ViewerHud replay={replay} cars={currentFrame?.cars || players} clockSeconds={displayFrame / VIEWER_PLAYBACK_HZ} totalFrameCount={totalFrames} cameraMode={cameraMode} selectedPlayerName={selectedPlayerName} />
          <NativeReplayMiniMap cars={currentFrame?.cars || []} ball={currentFrame?.ball} />
          {debugMode ? <div className="native-direct-debug">{JSON.stringify({ replayId, frame: Math.round(displayFrame), chunk: framesPayload?.start_frame, cameraMode, selectedPlayerId, loading: frameLoading }, null, 2)}</div> : null}
        </ReplayScene>
        {(frameLoading && !framesPayload) || frameError ? <div className="native-direct-status">{frameError || 'Loading 60 Hz replay telemetry...'}</div> : null}
      </div>
      <div className="native-direct-controls">
        <div className="native-direct-row">
          <button type="button" onClick={() => setPlaying((value) => !value)}>{playing ? 'Pause' : 'Play'}</button>
          {[0.25, 0.5, 1, 1.5, 2].map((value) => <button key={value} type="button" className={speed === value ? 'active' : ''} onClick={() => setSpeed(value)}>{value}x</button>)}
          <button type="button" onClick={() => seek(Math.max(0, displayFrame - 60))}>-1s</button>
          <button type="button" onClick={() => seek(displayFrame + 60)}>+1s</button>
        </div>
        <NativeReplayTimeline frame={displayFrame} totalFrames={totalFrames} events={framesPayload?.director_hints || framesPayload?.events || []} onSeek={seek} />
        <div className="native-direct-row wrap">
          {[
            ['director', 'Broadcast'],
            ['ball', 'Ball'],
            ['player', 'Chase'],
            ['pov', 'POV'],
            ['pov-ball', 'POV Ball'],
            ['free', 'Free'],
          ].map(([id, label]) => <button key={id} type="button" className={cameraMode === id ? 'active' : ''} onClick={() => setCameraMode(id)}>{label}</button>)}
          <select value={selectedPlayerId} onChange={(event) => setSelectedPlayerId(event.target.value)}>
            {players.map((player) => <option key={player.player_id} value={player.player_id}>{player.player_name || player.player_id}</option>)}
          </select>
          <select value={qualityMode} onChange={(event) => setQualityMode(event.target.value)}>
            <option value="performance">Performance</option>
            <option value="balanced">Balanced</option>
            <option value="cinematic">Cinematic</option>
          </select>
          <button type="button" className={debugMode ? 'active' : ''} onClick={() => setDebugMode((value) => !value)}>Debug</button>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [home, setHome] = useState(null);
  const [records, setRecords] = useState(null);
  const [ballStatus, setBallStatus] = useState(null);
  const [carballStatus, setCarballStatus] = useState(null);
  const [youtubeStatus, setYouTubeStatus] = useState(null);
  const [maintenanceStatus, setMaintenanceStatus] = useState(null);
  const [live, setLive] = useState(null);
  const [liveStatus, setLiveStatus] = useState(null);
  const [replays, setReplays] = useState([]);
  const [viewer, setViewer] = useState(null);
  const [viewerLoading, setViewerLoading] = useState(false);
  const [teamRecordProfile, setTeamRecordProfile] = useState(null);
  const [playerRecordProfileState, setPlayerRecordProfileState] = useState(null);
  const [teamHeadToHead, setTeamHeadToHead] = useState(null);
  const [playerHeadToHead, setPlayerHeadToHead] = useState(null);
  const [selectedReplayId, setSelectedReplayId] = useState('');
  const [selectedTeam, setSelectedTeam] = useState('');
  const [selectedPlayer, setSelectedPlayer] = useState('');
  const [selectedTeamLeft, setSelectedTeamLeft] = useState('');
  const [selectedTeamRight, setSelectedTeamRight] = useState('');
  const [selectedPlayerLeft, setSelectedPlayerLeft] = useState('');
  const [selectedPlayerRight, setSelectedPlayerRight] = useState('');
  const [replaySearch, setReplaySearch] = useState('');
  const [parsedOnly, setParsedOnly] = useState(false);
  const [reviewReady, setReviewReady] = useState(false);
  const [replaySort, setReplaySort] = useState('recent');
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryTotal, setLibraryTotal] = useState(0);
  const [libraryHasMore, setLibraryHasMore] = useState(false);
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState(getInitialTab);
  const selectedReplayRequestRef = useRef(0);
  const viewerCacheRef = useRef(new Map());

  async function loadSelectedReplay(replayId, options = {}) {
    if (!replayId) return null;
    const force = Boolean(options.force);
    const activate = options.activate !== false;
    const replayKey = String(replayId);
    if (!force) {
      const cached = viewerCacheRef.current.get(replayKey);
      if (cached) {
        if (activate) {
          setViewer(cached);
          setViewerLoading(false);
          setError('');
        }
        return cached;
      }
    }
    const requestId = selectedReplayRequestRef.current + 1;
    if (activate) {
      selectedReplayRequestRef.current = requestId;
      setViewerLoading(true);
    }
    try {
      const payload = await fetchJson(`/library/replays/${encodeURIComponent(replayId)}/viewer`, {
        headers: { 'X-ReplayOS-Selected-Replay': replayId },
      });
      const returnedId = String(payload?.replay?.replay_id || payload?.replay_id || '');
      if (returnedId && returnedId !== replayKey) {
        throw new Error(`Viewer replay mismatch: requested ${replayId}, got ${returnedId}`);
      }
      viewerCacheRef.current.set(replayKey, payload);
      pruneViewerCache(viewerCacheRef.current);
      if (activate) {
        if (selectedReplayRequestRef.current !== requestId) return null;
        setViewer(payload);
        setViewerLoading(false);
        setError('');
      }
      return payload;
    } catch (error) {
      if (activate && selectedReplayRequestRef.current === requestId) {
        setViewerLoading(false);
      }
      throw error;
    }
  }

  async function loadLive(force = false, syncFirst = false) {
    const [livePayload, liveStatusPayload] = await Promise.all([
      fetchJson('/site/live?refresh_if_stale=true'),
      fetchJson('/sources/live/status'),
    ]);
    setLive(livePayload);
    setLiveStatus(liveStatusPayload);
    setError('');
    return livePayload;
  }

  async function loadRecordsOverview() {
    const payload = await fetchJson('/site/records');
    setRecords(payload);
    setError('');
    return payload;
  }

  async function loadTeamRecordProfile(name) {
    if (!name) return null;
    const payload = await fetchJson(`/site/records/team?name=${encodeURIComponent(name)}`);
    setTeamRecordProfile(payload);
    setError('');
    return payload;
  }

  async function loadPlayerRecordProfile(name) {
    if (!name) return null;
    const payload = await fetchJson(`/site/records/player?name=${encodeURIComponent(name)}`);
    setPlayerRecordProfileState(payload);
    setError('');
    return payload;
  }

  async function loadTeamHeadToHead(left, right) {
    if (!left || !right || left === right) {
      setTeamHeadToHead(null);
      return null;
    }
    const payload = await fetchJson(`/site/records/head-to-head?kind=team&left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
    setTeamHeadToHead(payload);
    setError('');
    return payload;
  }

  async function loadPlayerHeadToHead(left, right) {
    if (!left || !right || left === right) {
      setPlayerHeadToHead(null);
      return null;
    }
    const payload = await fetchJson(`/site/records/head-to-head?kind=player&left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
    setPlayerHeadToHead(payload);
    setError('');
    return payload;
  }

  async function loadReplayLibrary(options = {}) {
    const limit = Number(options.limit || REPLAY_LIBRARY_PAGE_SIZE);
    const offset = Math.max(0, Number(options.offset || 0));
    const append = Boolean(options.append && offset > 0);
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    const activeSearch = options.search ?? replaySearch;
    const activeParsed = options.parsedOnly ?? parsedOnly;
    const activeReview = options.reviewReady ?? reviewReady;
    const activeSort = options.sortMode ?? replaySort;
    if (activeSearch) params.set('search', activeSearch);
    if (activeParsed) params.set('parsed_only', 'true');
    if (activeReview) params.set('review_ready', 'true');
    if (activeSort) params.set('sort', activeSort);
    setLibraryLoading(true);
    try {
      const payload = await fetchJson(`/library/replays?${params.toString()}`);
      const items = payload.items || [];
      const nextItems = append
        ? Array.from(
          new Map(
            [...replays, ...items].map((item) => [item.replay_id, item])
          ).values()
        )
        : items;
      setReplays(nextItems);
      setLibraryTotal(Number(payload.total || nextItems.length));
      setLibraryHasMore(Boolean(payload.has_more));
      setError('');
      const preferredReplay = options.preserveReplayId ?? selectedReplayId;
      const defaultReplay = nextItems.find((item) => String(item?.carball_status?.status || '').toLowerCase() === 'completed') || nextItems[0];
      const targetReplay = preferredReplay && nextItems.some((item) => item.replay_id === preferredReplay)
        ? preferredReplay
        : defaultReplay?.replay_id || '';
      setSelectedReplayId(targetReplay);
      return targetReplay;
    } finally {
      setLibraryLoading(false);
    }
  }

  async function loadHome(preserveReplayId) {
    const [homePayload, ballPayload, carballPayload, youtubePayload, maintenancePayload] = await Promise.all([
      fetchJson('/site/home'),
      fetchJson('/sources/ballchasing/status'),
      fetchJson('/sources/carball/status'),
      fetchJson('/sources/youtube/status'),
      fetchJson('/sources/maintenance/status'),
    ]);
    setHome(homePayload);
    setBallStatus(ballPayload);
    setCarballStatus(carballPayload);
    setYouTubeStatus(youtubePayload);
    setMaintenanceStatus(maintenancePayload);
    setError('');
    const targetReplay = await loadReplayLibrary({ preserveReplayId });
    return targetReplay;
  }

  useEffect(() => {
    Promise.all([loadHome(), loadLive(false, false), loadRecordsOverview()])
      .then(([targetReplay]) => (activeTab === 'viewer' && targetReplay ? loadSelectedReplay(targetReplay) : null))
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const syncFromHash = () => {
      const nextTab = normalizeTabId(window.location.hash);
      if (nextTab) setActiveTab(nextTab);
    };
    window.addEventListener('hashchange', syncFromHash);
    return () => window.removeEventListener('hashchange', syncFromHash);
  }, []);

  useEffect(() => {
    if (!selectedReplayId || activeTab !== 'viewer') return;
    loadSelectedReplay(selectedReplayId).catch((err) => setError(err.message));
  }, [selectedReplayId, activeTab]);

  useEffect(() => {
    if (!selectedReplayId || activeTab === 'viewer' || viewerCacheRef.current.has(String(selectedReplayId))) return undefined;
    const timer = window.setTimeout(() => {
      loadSelectedReplay(selectedReplayId, { activate: false }).catch(() => {});
    }, 650);
    return () => window.clearTimeout(timer);
  }, [selectedReplayId, activeTab]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      loadReplayLibrary({ preserveReplayId: selectedReplayId }).catch((err) => setError(err.message));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [replaySearch, parsedOnly, reviewReady, replaySort]);

  useEffect(() => {
    const warm = window.setTimeout(() => {
      loadLive(false, true).catch((err) => setError(err.message));
    }, 1200);
    const timer = window.setInterval(() => {
      loadLive(false, true).catch((err) => setError(err.message));
    }, 60_000);
    const statusTimer = window.setInterval(() => {
      fetchJson('/sources/maintenance/status')
        .then(setMaintenanceStatus)
        .catch((err) => setError(err.message));
    }, 90_000);
    return () => {
      window.clearTimeout(warm);
      window.clearInterval(timer);
      window.clearInterval(statusTimer);
    };
  }, []);

  useEffect(() => {
    const homeTimer = window.setInterval(() => {
      loadHome(selectedReplayId)
        .then((targetReplay) => {
          if (activeTab !== 'viewer' || !targetReplay) return null;
          if (viewer?.replay?.replay_id === targetReplay && viewerCacheRef.current.has(String(targetReplay))) {
            return null;
          }
          return loadSelectedReplay(targetReplay);
        })
        .catch((err) => setError(err.message));
    }, 90_000);
    const recordsTimer = window.setInterval(() => {
      loadRecordsOverview().catch((err) => setError(err.message));
    }, 180_000);
    return () => {
      window.clearInterval(homeTimer);
      window.clearInterval(recordsTimer);
    };
  }, [activeTab, selectedReplayId, viewer?.replay?.replay_id]);

  useEffect(() => {
    const teamOptions = records?.team_options || [];
    const playerOptions = records?.player_options || [];
    const topTeam = records?.team_leaders?.most_wins?.[0]?.name || teamOptions[0];
    const topPlayer = records?.player_leaders?.most_goals?.[0]?.name || playerOptions[0];
    const defaultTeamMatchup = records?.matchup_leaders?.teams?.[0];
    const defaultPlayerMatchup = records?.matchup_leaders?.players?.[0];
    if (!selectedTeam && topTeam) setSelectedTeam(topTeam);
    if (!selectedPlayer && topPlayer) setSelectedPlayer(topPlayer);
    if (!selectedTeamLeft && (defaultTeamMatchup?.team_a || teamOptions[0])) setSelectedTeamLeft(defaultTeamMatchup?.team_a || teamOptions[0]);
    if (!selectedTeamRight && (defaultTeamMatchup?.team_b || teamOptions[1])) setSelectedTeamRight(defaultTeamMatchup?.team_b || teamOptions[1]);
    if (!selectedPlayerLeft && (defaultPlayerMatchup?.player_a || playerOptions[0])) setSelectedPlayerLeft(defaultPlayerMatchup?.player_a || playerOptions[0]);
    if (!selectedPlayerRight && (defaultPlayerMatchup?.player_b || playerOptions[1])) setSelectedPlayerRight(defaultPlayerMatchup?.player_b || playerOptions[1]);
  }, [records, selectedPlayer, selectedPlayerLeft, selectedPlayerRight, selectedTeam, selectedTeamLeft, selectedTeamRight]);

  useEffect(() => {
    if (!selectedTeam) return;
    loadTeamRecordProfile(selectedTeam).catch((err) => setError(err.message));
  }, [selectedTeam]);

  useEffect(() => {
    if (!selectedPlayer) return;
    loadPlayerRecordProfile(selectedPlayer).catch((err) => setError(err.message));
  }, [selectedPlayer]);

  useEffect(() => {
    if (!selectedTeamLeft || !selectedTeamRight) return;
    loadTeamHeadToHead(selectedTeamLeft, selectedTeamRight).catch((err) => setError(err.message));
  }, [selectedTeamLeft, selectedTeamRight]);

  useEffect(() => {
    if (!selectedPlayerLeft || !selectedPlayerRight) return;
    loadPlayerHeadToHead(selectedPlayerLeft, selectedPlayerRight).catch((err) => setError(err.message));
  }, [selectedPlayerLeft, selectedPlayerRight]);

  async function refreshRecords() {
    await loadRecordsOverview();
    if (selectedTeam) await loadTeamRecordProfile(selectedTeam);
    if (selectedPlayer) await loadPlayerRecordProfile(selectedPlayer);
    if (selectedTeamLeft && selectedTeamRight && selectedTeamLeft !== selectedTeamRight) {
      await loadTeamHeadToHead(selectedTeamLeft, selectedTeamRight);
    }
    if (selectedPlayerLeft && selectedPlayerRight && selectedPlayerLeft !== selectedPlayerRight) {
      await loadPlayerHeadToHead(selectedPlayerLeft, selectedPlayerRight);
    }
  }

  function switchTab(tabId) {
    setActiveTab(tabId);
    if (typeof window !== 'undefined') {
      const nextHash = `#${tabId}`;
      if (window.location.hash !== nextHash) {
        window.history.replaceState(null, '', nextHash);
      }
    }
  }

  function handleReplaySelect(replayId, options = {}) {
    setSelectedReplayId(replayId);
    if (options.openViewer) {
      switchTab('viewer');
    }
  }

  const spotlight = replays.find((item) => item?.blue_goals !== null && item?.blue_goals !== undefined && item?.orange_goals !== null && item?.orange_goals !== undefined) || replays[0];
  const selectedReplayCard = replays.find((item) => item.replay_id === selectedReplayId) || spotlight;
  const activeTabMeta = TAB_DEFS.find((tab) => tab.id === activeTab) || TAB_DEFS[0];
  const overviewMatches = replays.slice(0, 6);
  const featuredTournament = live?.featured_tournament;

  return (
    <main className="site-shell">
      <header className="masthead">
        <div className="brand-lockup">
          <div>
            <span className="brand-mark">ReplayOS</span>
            <p className="brand-tagline">Rocket League replay intelligence, finally broken into actual rooms.</p>
          </div>
        </div>
        <span className="api-pill">{API_BASE}</span>
      </header>

      <div className="tab-strip" role="tablist" aria-label="ReplayOS sections">
        {TAB_DEFS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            className={`tab-button ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => switchTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {error ? <div className="notice">Backend note: {error}</div> : null}

      <section className="tab-summary">
        <div>
          <p className="kicker">{activeTabMeta.kicker}</p>
          <h1>{activeTabMeta.title}</h1>
        </div>
        <p className="section-note">{activeTabMeta.note}</p>
      </section>

      <div className="tab-panel">
        {activeTab === 'overview' ? (
          <>
            <section className="spotlight-band">
              <img
                className="hero-image"
                src="/site-images/overview-hero.png"
                alt="Rocket League stadium and replay scene"
              />
              <div className="spotlight-copy">
                <p className="kicker">Rocket League Match Hub</p>
                <h2>3D replays, synced match videos, RLCS live radar, and swing tracking.</h2>
                <p className="lede">Pull replays from Ballchasing, cache 60 Hz telemetry, then layer in VOD matching, replay eval, and live RLCS watch coverage.</p>
              </div>
              <div className="spotlight-score">
                <span>{spotlight?.blue_team_name || 'Blue Side'}</span>
                <strong>{spotlight?.blue_goals ?? '-'}</strong>
                <b>:</b>
                <strong>{spotlight?.orange_goals ?? '-'}</strong>
                <span>{spotlight?.orange_team_name || 'Orange Side'}</span>
                <small>{spotlight ? formatDate(spotlight.match_date) : 'Load replays to start the board'}</small>
              </div>
            </section>

            <section className="content-band">
              <div className="metric-strip">
                <div>
                  <span>Local replays</span>
                  <strong>{home?.counts?.local_replays ?? '...'}</strong>
                </div>
                <div>
                  <span>Replay files indexed</span>
                  <strong>{home?.counts?.indexed_local_replays ?? '...'}</strong>
                </div>
                <div>
                  <span>Parsed 3D replays</span>
                  <strong>{home?.counts?.parsed_replays ?? '0'}</strong>
                </div>
                <div>
                  <span>Named team replays</span>
                  <strong>{home?.counts?.named_team_replays ?? '0'}</strong>
                </div>
                <div>
                  <span>Name coverage</span>
                  <strong>{percent(home?.counts?.name_coverage_rate ?? 0)}</strong>
                </div>
                <div>
                  <span>Series indexed</span>
                  <strong>{home?.counts?.series ?? '0'}</strong>
                </div>
                <div>
                  <span>Replay reviews</span>
                  <strong>{home?.counts?.eval_ready_replays ?? '0'}</strong>
                </div>
                <div>
                  <span>YouTube links</span>
                  <strong>{home?.counts?.youtube_videos ?? '0'}</strong>
                </div>
                <div>
                  <span>Live streams</span>
                  <strong>{liveStatus?.streams ?? '0'}</strong>
                </div>
              </div>
            </section>

            <section className="content-band overview-grid">
              <div>
                <div className="section-head">
                  <div>
                    <p className="kicker">Featured Matches</p>
                    <h2>Open a replay and go straight to the viewer.</h2>
                  </div>
                  <div className="button-row">
                    <button type="button" className="ghost-button" onClick={() => switchTab('replays')}>Open replay shelf</button>
                  </div>
                </div>
                <MatchTicker matches={overviewMatches} onSelect={(replayId) => handleReplaySelect(replayId, { openViewer: true })} selectedReplayId={selectedReplayId} />
              </div>

              <div className="overview-stack">
                <div className="control-panel">
                  <div className="section-head">
                    <div>
                      <p className="kicker">Live Snapshot</p>
                      <h3>{featuredTournament?.name || 'Live sync is warming up'}</h3>
                    </div>
                    <button type="button" className="ghost-button" onClick={() => switchTab('live')}>Open live desk</button>
                  </div>
                  <div className="maintenance-grid">
                    <div>
                      <span>Status</span>
                      <strong>{featuredTournament?.status || 'idle'}</strong>
                    </div>
                    <div>
                      <span>Matches</span>
                      <strong>{featuredTournament?.matches?.length || 0}</strong>
                    </div>
                    <div>
                      <span>Streams</span>
                      <strong>{live?.streams?.length || 0}</strong>
                    </div>
                    <div>
                      <span>Boards</span>
                      <strong>{live?.leaderboards?.length || 0}</strong>
                    </div>
                  </div>
                  <small className="control-note">{featuredTournament?.location_name ? `${featuredTournament.location_name}. ` : ''}Last sync {formatAgo(live?.refreshed_at || liveStatus?.last_run?.completed_at)}.</small>
                </div>

                <div className="control-panel">
                  <div className="section-head">
                    <div>
                      <p className="kicker">Power Board</p>
                      <h3>Top teams right now</h3>
                    </div>
                    <button type="button" className="ghost-button" onClick={() => switchTab('rankings')}>Open rankings</button>
                  </div>
                  <Ladder items={(home?.team_elo || []).slice(0, 5)} />
                </div>
              </div>
            </section>
          </>
        ) : null}

        {activeTab === 'live' ? (
          <LiveRadar
            live={live}
            liveStatus={liveStatus}
            onRefresh={() => loadLive(false, false)}
            onError={setError}
          />
        ) : null}

        {activeTab === 'replays' ? (
          <>
            <section className="content-band ops-band">
              <ReplayFilters
                search={replaySearch}
                onChangeSearch={setReplaySearch}
                parsedOnly={parsedOnly}
                onChangeParsedOnly={setParsedOnly}
                reviewReady={reviewReady}
                onChangeReviewReady={setReviewReady}
                sortMode={replaySort}
                onChangeSortMode={setReplaySort}
                resultCount={replays.length}
                totalCount={libraryTotal}
                loading={libraryLoading}
                onRefresh={() => loadReplayLibrary({ preserveReplayId: selectedReplayId })}
              />
              <div className="control-panel">
                <div className="panel-filter-head">
                  <div>
                    <p className="kicker">Selected Replay</p>
                    <h3>{selectedReplayCard?.title || 'Pick a replay from the board'}</h3>
                  </div>
                  <button type="button" onClick={() => switchTab('viewer')} disabled={!selectedReplayId}>Open viewer</button>
                </div>
                <div className="maintenance-grid">
                  <div>
                    <span>Series</span>
                    <strong>{selectedReplayCard?.series?.length || 0}</strong>
                  </div>
                  <div>
                    <span>3D status</span>
                    <strong>{selectedReplayCard?.carball_status?.status || 'pending'}</strong>
                  </div>
                  <div>
                    <span>Review</span>
                    <strong>{selectedReplayCard?.review ? 'ready' : 'pending'}</strong>
                  </div>
                  <div>
                    <span>Match date</span>
                    <strong>{selectedReplayCard?.match_date ? formatDate(selectedReplayCard.match_date) : 'n/a'}</strong>
                  </div>
                </div>
                <small className="control-note">{selectedReplayCard?.review ? reviewNote(selectedReplayCard.review) : 'Choose a replay here, then open the Viewer tab when you want the full 3D replay and eval stack.'}</small>
              </div>
            </section>

            <section className="content-band">
              <div className="section-head">
                <div>
                  <p className="kicker">Replay Board</p>
                  <h2>Current library</h2>
                </div>
                <div className="button-row section-actions">
                  <span className="section-note">
                    {libraryTotal ? `${replays.length} of ${libraryTotal.toLocaleString()} loaded.` : 'Pick the match here, keep browsing if you want, then open the Viewer tab once you are ready to watch it.'}
                  </span>
                  {libraryHasMore ? (
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={() => loadReplayLibrary({ preserveReplayId: selectedReplayId, offset: replays.length, append: true })}
                      disabled={libraryLoading}
                    >
                      {libraryLoading ? 'Loading...' : 'Load more'}
                    </button>
                  ) : null}
                </div>
              </div>
              <MatchTicker matches={replays} onSelect={(replayId) => handleReplaySelect(replayId, { openViewer: false })} selectedReplayId={selectedReplayId} />
            </section>

            <section className="content-band split-band">
              <div>
                <div className="section-head">
                  <div>
                    <p className="kicker">Series Index</p>
                    <h2>Rounds and matchup bundles</h2>
                  </div>
                </div>
                <SeriesGrid series={home?.series || []} />
              </div>

              <div className="image-rail">
                <img
                  src="/site-images/library-rail.png"
                  alt="Rocket League replay viewer scene"
                />
                <div className="image-rail-copy">
                  <p className="kicker">Replay Library</p>
                  <h3>Fresh syncs can add replays, player box scores, series groupings, replay files, video links, and frame-based 3D views where telemetry exists.</h3>
                </div>
              </div>
            </section>
          </>
        ) : null}

        {activeTab === 'viewer' ? (
          <section className="content-band">
            <Viewer viewer={viewer} loading={viewerLoading} selectedReplayCard={selectedReplayCard} onRefreshReplay={loadSelectedReplay} onError={setError} />
          </section>
        ) : null}

        {activeTab === 'rankings' ? (
          <section className="content-band">
            <div className="overview-stack">
              <div className="section-head">
                <div>
                  <p className="kicker">Rankings</p>
                  <h2>Overall power and current RLCS context</h2>
                </div>
                <span className="section-note">Overall power blends results, opponent quality, recency, goal margin, public boards, and replay telemetry. Current RLCS boards stay separate so regional standings are visible instead of getting mashed into one global list.</span>
              </div>
              <LiveLeaderboardBoards boards={live?.leaderboards || []} />
            </div>

            <div className="rankings-band">
              <div>
              <div className="section-head">
                <div>
                  <p className="kicker">Rankings</p>
                  <h2>Standings-weighted power board</h2>
                </div>
                <span className="section-note">Power score folds together match strength, opponent quality, recency, goal margin, public standings, and replay telemetry.</span>
              </div>
              <Ladder items={home?.team_elo || []} />
              </div>

              <div className="overview-stack">
                <div className="section-head">
                  <div>
                    <p className="kicker">Rosters</p>
                    <h2>Best lineups</h2>
                  </div>
                  <span className="section-note">These are roster-by-roster records from the named replay corpus, not just org branding.</span>
                </div>
                <RecordShelf kicker="Rosters" title="Most wins" items={records?.roster_leaders?.most_wins || []} />
                <RecordShelf kicker="Rosters" title="Best win rate" items={records?.roster_leaders?.best_win_rate || []} mode="percent" />
                <RecordShelf kicker="This RLCS" title="Best current RLCS lineups" items={records?.roster_leaders?.rlcs_most_wins || []} />
                <RecordShelf kicker="This RLCS" title="Best current RLCS teams" items={records?.team_leaders?.rlcs_most_wins || []} />
                <div className="section-head">
                  <div>
                    <p className="kicker">Players</p>
                    <h2>Scoring table</h2>
                  </div>
                </div>
                <PlayersBoard players={home?.top_players || []} />
              </div>
            </div>
          </section>
        ) : null}

        {activeTab === 'records' ? (
          <section className="content-band records-band">
            <div className="section-head">
              <div>
                <p className="kicker">Records</p>
                <h2>All-time team and player record book</h2>
              </div>
              <span className="section-note">
                {records?.summary ? `${records.summary.tracked_matches} tracked matches. ${records.summary.tracked_teams} teams and ${records.summary.tracked_players} players already have match history, with ${records.summary.known_teams || records.summary.tracked_teams} known teams and ${records.summary.known_players || records.summary.tracked_players} known players in the pool. Public boards now require at least ${records.summary.minimums?.team || 1} team matches, ${records.summary.minimums?.player || 1} player matches, and ${records.summary.minimums?.roster || 1} lineup matches.` : 'Building the record book from named replay data.'}
              </span>
            </div>

            <div className="records-grid">
              <RecordShelf kicker="Teams" title="Most wins" items={records?.team_leaders?.most_wins || []} />
              <RecordShelf kicker="Teams" title="Best win rate" items={records?.team_leaders?.best_win_rate || []} mode="percent" />
              <RecordShelf kicker="Teams" title="Best scoring rate" items={records?.team_leaders?.best_scoring_rate || []} mode="decimal" />
              <RecordShelf kicker="This RLCS" title="Best RLCS win rate" items={records?.team_leaders?.rlcs_best_win_rate || []} mode="percent" />
              <RecordShelf kicker="Players" title="Most wins" items={records?.player_leaders?.most_wins || []} />
              <RecordShelf kicker="Players" title="Most goals" items={records?.player_leaders?.most_goals || []} />
              <RecordShelf kicker="Players" title="Best average score" items={records?.player_leaders?.best_avg_score || []} />
              <RecordShelf kicker="This RLCS" title="Best RLCS goal rate" items={records?.player_leaders?.rlcs_best_goal_rate || []} mode="decimal" />
            </div>

            <div className="records-grid matchup-grid">
              <RecordShelf kicker="Rivalries" title="Most played team matchups" items={(records?.matchup_leaders?.teams || []).map((row) => ({ name: `${row.team_a} vs ${row.team_b}`, value: row.games, games: row.games }))} />
              <RecordShelf kicker="Rivalries" title="Most played player matchups" items={(records?.matchup_leaders?.players || []).map((row) => ({ name: `${row.player_a} vs ${row.player_b}`, value: row.games, games: row.games }))} />
            </div>

            <div className="records-grid matchup-grid">
              <RecordShelf kicker="Rosters" title="Best lineups by wins" items={records?.roster_leaders?.most_wins || []} />
              <RecordShelf kicker="Rosters" title="Best lineups by win rate" items={records?.roster_leaders?.best_win_rate || []} mode="percent" />
              <RecordShelf kicker="This RLCS" title="Best RLCS lineups" items={records?.roster_leaders?.rlcs_best_win_rate || []} mode="percent" />
              <RecordShelf kicker="Rosters" title="Best goal diff lineups" items={records?.roster_leaders?.best_goal_diff || []} />
            </div>

            <div className="records-grid matchup-grid">
              <RecordShelf kicker="Events" title="Best tournament runs" items={records?.event_leaders?.most_wins || []} />
              <RecordShelf kicker="This RLCS" title="Best current RLCS runs" items={records?.event_leaders?.rlcs_most_wins || []} />
              <RecordShelf kicker="This RLCS" title="Best RLCS event win rate" items={records?.event_leaders?.rlcs_best_win_rate || []} mode="percent" />
            </div>

            <div className="records-workbench">
              <TeamRecordPanel
                options={records?.team_options || []}
                value={selectedTeam}
                onChange={setSelectedTeam}
                profile={teamRecordProfile}
              />
              <PlayerRecordPanel
                options={records?.player_options || []}
                value={selectedPlayer}
                onChange={setSelectedPlayer}
                profile={playerRecordProfileState}
              />
              <TeamHeadToHeadPanel
                options={records?.team_options || []}
                left={selectedTeamLeft}
                right={selectedTeamRight}
                onChangeLeft={setSelectedTeamLeft}
                onChangeRight={setSelectedTeamRight}
                payload={teamHeadToHead}
              />
              <PlayerHeadToHeadPanel
                options={records?.player_options || []}
                left={selectedPlayerLeft}
                right={selectedPlayerRight}
                onChangeLeft={setSelectedPlayerLeft}
                onChangeRight={setSelectedPlayerRight}
                payload={playerHeadToHead}
              />
            </div>
          </section>
        ) : null}

        {activeTab === 'analyst' ? (
          <section className="content-band analyst-band">
            <AnalystDesk replayId={selectedReplayId} onError={setError} />
            <div className="event-meta">
              <div className="section-head">
                <div>
                  <p className="kicker">Event Mix</p>
                  <h2>What the parser is seeing</h2>
                </div>
              </div>
              <div className="event-list">
                {(home?.event_counts || []).map((event) => (
                  <div className="event-pill" key={event.event_type}>
                    <span>{labelFromSlug(event.event_type)}</span>
                    <strong>{event.n}</strong>
                  </div>
                ))}
              </div>
            </div>
          </section>
        ) : null}

        {activeTab === 'ops' ? (
          <>
            <section className="content-band ops-band">
              <MaintenanceWidget
                status={maintenanceStatus}
                onRefresh={async () => {
                  const targetReplay = await loadHome(selectedReplayId);
                  if (targetReplay) await loadSelectedReplay(targetReplay);
                  await loadLive(false, false);
                  await refreshRecords();
                }}
                onError={setError}
              />
              <div className="control-panel">
                <div className="section-head">
                  <div>
                    <p className="kicker">Worker Status</p>
                    <h3>{maintenanceStatus?.worker?.running ? 'Background upkeep is running' : 'Background upkeep is idle'}</h3>
                  </div>
                </div>
                <div className="maintenance-grid">
                  <div>
                    <span>Thread</span>
                    <strong>{maintenanceStatus?.worker?.thread_alive ? 'alive' : 'down'}</strong>
                  </div>
                  <div>
                    <span>Trigger</span>
                    <strong>{maintenanceStatus?.worker?.current_trigger || 'idle'}</strong>
                  </div>
                  <div>
                    <span>Parse backlog</span>
                    <strong>{maintenanceStatus?.health?.parse_backlog ?? '...'}</strong>
                  </div>
                  <div>
                    <span>Review backlog</span>
                    <strong>{maintenanceStatus?.health?.review_backlog ?? '...'}</strong>
                  </div>
                </div>
                <small className="control-note">Background upkeep now handles live sync, replay sync, Carball parsing, and review backfill.</small>
              </div>
            </section>

            <section className="utility-band triple">
              <BallchasingSyncWidget
                status={ballStatus}
                onRefresh={async () => {
                  const targetReplay = await loadHome(selectedReplayId);
                  if (targetReplay) await loadSelectedReplay(targetReplay);
                  await loadLive();
                  await refreshRecords();
                }}
                onError={setError}
              />
              <CarballBackfillWidget
                status={carballStatus || home?.coverage}
                onRefresh={async () => {
                  const targetReplay = await loadHome(selectedReplayId);
                  if (targetReplay) await loadSelectedReplay(targetReplay);
                  await refreshRecords();
                }}
                onError={setError}
              />
              <YouTubeSyncWidget
                status={youtubeStatus}
                selectedReplayId={selectedReplayId}
                onRefresh={async () => {
                  const targetReplay = await loadHome(selectedReplayId);
                  if (targetReplay) await loadSelectedReplay(targetReplay);
                  await refreshRecords();
                }}
                onError={setError}
              />
            </section>
          </>
        ) : null}
      </div>
    </main>
  );
}

createRoot(document.getElementById('root')).render(isNativeViewerRoute() ? <NativeReplayViewerApp /> : <App />);
