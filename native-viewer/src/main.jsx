import React, { useEffect, useMemo, useRef, useState } from 'react';
import ReactDOM from 'react-dom';
import * as THREE from 'three';
import {
  GameManagerLoader,
  PlayControls,
  Slider,
} from 'replay-viewer';
import { addFrameListener, removeFrameListener } from 'replay-viewer/eventbus/events/frame';
import { addCameraFrameUpdateListener, removeCameraFrameUpdateListener } from 'replay-viewer/eventbus/events/cameraFrameUpdate';
import { dispatchCanvasResizeEvent } from 'replay-viewer/eventbus/events/canvasResize';
import { dispatchPlayPauseEvent } from 'replay-viewer/eventbus/events/playPause';
import { GameManager } from 'replay-viewer/managers/GameManager';
import { installSceneEnhancements } from './sceneEnhancements';
import customGameBuilder from './customGameBuilder';
import { CAMERA_MODES, useReplayCamera } from './replayCameraController';
import SmoothReplayClock from './smoothReplayClock';
import './styles.css';

GameManager.builder = customGameBuilder;

const DEFAULT_CAMERA_SETTINGS = {
  directorAggression: 0.58,
  playerLookMode: 'replay',
  autoPlayerPov: true,
  showHud: true,
  showBoostHud: true,
  showTelemetry: false,
  showStatPanel: true,
  compactBoost: false,
  showRadar: false,
  showDebug: false,
  qualityMode: 'balanced',
};

const SPEED_OPTIONS = [0.25, 0.5, 1, 1.5, 2, 3];
const LOOK_OPTIONS = [
  { id: 'replay', label: 'Replay Ball Cam' },
  { id: 'always-ball', label: 'Always Ball' },
  { id: 'car', label: 'Car Cam' },
];
const TIMELINE_MAJOR_TYPES = new Set(['demo', 'shot', 'save', 'pressure', 'pressure_phase', 'turnover', 'turnover_forced', 'clutch', 'swing']);

function timelineMarkerMeta(type) {
  const normalized = String(type || '').trim().toLowerCase();
  if (normalized === 'goal') return { tone: 'goal', short: 'G', label: 'Goal' };
  if (normalized === 'save') return { tone: 'save', short: 'SV', label: 'Save' };
  if (normalized === 'shot') return { tone: 'shot', short: 'S', label: 'Shot' };
  if (normalized === 'demo') return { tone: 'demo', short: 'D', label: 'Demo' };
  if (normalized === 'turnover' || normalized === 'turnover_forced') return { tone: 'turnover', short: 'TO', label: 'Turnover' };
  if (normalized === 'pressure' || normalized === 'pressure_phase') return { tone: 'pressure', short: 'PR', label: 'Pressure' };
  if (normalized === 'clutch') return { tone: 'clutch', short: 'CL', label: 'Clutch' };
  if (normalized === 'swing') return { tone: 'swing', short: 'SW', label: 'Swing' };
  if (normalized === 'touch') return { tone: 'touch', short: 'T', label: 'Touch' };
  return { tone: 'event', short: 'E', label: normalized.replace(/_/g, ' ') || 'Event' };
}

const TIMELINE_LEGEND_ITEMS = ['goal', 'shot', 'save', 'turnover', 'pressure_phase', 'demo'];

function currentQuery() {
  return new URLSearchParams(window.location.search);
}

function buildViewerUrl(apiBase, replayId, options = {}) {
  const normalizedBase = String(apiBase || '').replace(/\/$/, '');
  const url = new URL(`${normalizedBase}/library/replays/${encodeURIComponent(replayId)}/native-viewer`, window.location.origin);
  url.searchParams.set('hz', String(options.hz || 60));
  url.searchParams.set('max_frames', String(options.maxFrames || 24000));
  url.searchParams.set('start_frame', String(options.startFrame || 0));
  return url.toString();
}

function sortPlayerCards(cards = []) {
  return [...cards].sort((left, right) => {
    const teamDiff = Number(left.team || left.is_orange || 0) - Number(right.team || right.is_orange || 0);
    if (teamDiff) return teamDiff;
    return String(left.player_name || left.name || '').localeCompare(String(right.player_name || right.name || ''));
  });
}

function clampSeriesFrame(series, frameFloat) {
  const values = Array.isArray(series) ? series : [];
  const maxIndex = values.length - 1;
  if (maxIndex <= 0) return { low: 0, high: 0, alpha: 0 };
  const bounded = Math.max(0, Math.min(maxIndex, Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : 0));
  const low = Math.floor(bounded);
  const high = Math.min(maxIndex, low + 1);
  return { low, high, alpha: Math.max(0, Math.min(1, bounded - low)) };
}

function interpolatedSeriesNumber(series, frameFloat) {
  const values = Array.isArray(series) ? series : [];
  if (!values.length) return 0;
  const { low, high, alpha } = clampSeriesFrame(values, frameFloat);
  const start = Number(values[low] || 0);
  const end = Number(values[high] || start);
  return start + (end - start) * alpha;
}

function interpolatedFrameColumn(series, frameFloat, columnIndex, fallback = 0) {
  const values = Array.isArray(series) ? series : [];
  if (!values.length) return fallback;
  const { low, high, alpha } = clampSeriesFrame(values, frameFloat);
  const read = (frame) => Number(frame?.[columnIndex] ?? fallback);
  const start = read(values[low]);
  const end = read(values[high]);
  return start + (end - start) * alpha;
}

function playerSeriesForCard(payload, card) {
  const order = payload?.hud?.player_order || [];
  const playerId = card?.player_id || card?.id;
  const playerIndex = Math.max(0, order.indexOf(playerId));
  return payload?.replayData?.players?.[playerIndex] || [];
}

function boostAtFrame(payload, card, frameFloat) {
  const playerId = card?.player_id || card?.id;
  const direct = payload?.hud?.boost_by_player?.[playerId];
  if (Array.isArray(direct) && direct.length) return interpolatedSeriesNumber(direct, frameFloat);
  return interpolatedFrameColumn(playerSeriesForCard(payload, card), frameFloat, 7, 0);
}

function elapsedSecondsAtFrame(payload, frameFloat) {
  const values = payload?.replayData?.frames || payload?.nativeTelemetry?.frames || [];
  if (!values.length) return 0;
  const { low, high, alpha } = clampSeriesFrame(values, frameFloat);
  const readTime = (item) => {
    if (Array.isArray(item)) return Number(item[2] || 0);
    return Number(item?.t || 0);
  };
  const start = readTime(values[low]);
  const end = readTime(values[high]);
  return start + (end - start) * alpha;
}

function interpolatedPosition(series, frameFloat) {
  const values = Array.isArray(series) ? series : [];
  if (!values.length) return null;
  const { low, high, alpha } = clampSeriesFrame(values, frameFloat);
  const start = values[low] || values[0];
  const end = values[high] || start;
  return [
    Number(start?.[0] || 0) + (Number(end?.[0] || start?.[0] || 0) - Number(start?.[0] || 0)) * alpha,
    Number(start?.[1] || 0) + (Number(end?.[1] || start?.[1] || 0) - Number(start?.[1] || 0)) * alpha,
    Number(start?.[2] || 0) + (Number(end?.[2] || start?.[2] || 0) - Number(start?.[2] || 0)) * alpha,
  ];
}

function detectSnapFrames(replayData) {
  const ballSeries = replayData?.ball || [];
  if (!ballSeries.length) return [];
  const snapFrames = new Set();
  let last = ballSeries[0] || [0, 0, 0];
  for (let index = 1; index < ballSeries.length; index += 1) {
    const current = ballSeries[index] || last;
    const jump = Math.hypot(
      Number(current[0] || 0) - Number(last[0] || 0),
      Number(current[1] || 0) - Number(last[1] || 0),
      Number(current[2] || 0) - Number(last[2] || 0),
    );
    const centered = Math.hypot(Number(current[0] || 0), Number(current[1] || 0)) < 240
      && Math.abs(Number(current[2] || 0) - 92.75) < 220;
    if (jump > 1900 || (centered && jump > 700)) {
      snapFrames.add(index);
      if (index + 1 < ballSeries.length) snapFrames.add(index + 1);
      if (index + 2 < ballSeries.length) snapFrames.add(index + 2);
    }
    last = current;
  }
  return [...snapFrames].sort((left, right) => left - right);
}

function interpolatedSpeed(positionSeries, frameFloat, sampleHz = 60) {
  const current = interpolatedPosition(positionSeries, frameFloat);
  const previous = interpolatedPosition(positionSeries, Math.max(0, Number(frameFloat || 0) - 1));
  if (!current || !previous) return 0;
  return Math.hypot(
    Number(current[0] || 0) - Number(previous[0] || 0),
    Number(current[1] || 0) - Number(previous[1] || 0),
    Number(current[2] || 0) - Number(previous[2] || 0),
  ) * sampleHz;
}

function frameRatio(payload, frame) {
  const total = Math.max(1, payload?.replayData?.frames?.length || 1);
  return Math.max(0, Math.min(1, Number(frame || 0) / total));
}

function formatClockSeconds(totalSeconds) {
  const bounded = Math.max(0, Math.round(Number(totalSeconds || 0)));
  const minutes = Math.floor(bounded / 60);
  const seconds = bounded % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function mapLabelFromCode(mapCode) {
  const raw = String(mapCode || '').trim();
  if (!raw) return 'Map pending';
  const explicit = {
    'DFHStadium_P': 'DFH Stadium',
    'Mannfield_P': 'Mannfield',
    'ChampionsField_P': 'Champions Field',
    'UrbanCentral_P': 'Urban Central',
    'BeckwithPark_P': 'Beckwith Park',
    'NeoTokyo_P': 'Neo Tokyo',
    'UtopiaStadium_P': 'Utopia Coliseum',
    'TrainStation_P': 'Starbase ARC',
    'TrainStation_Night_P': 'Starbase ARC',
    'Wasteland_P': 'Wasteland',
    'AquaDome_P': 'AquaDome',
    'Farmstead_P': 'Farmstead',
    'SaltyShores_P': 'Salty Shores',
    'ForbiddenTemple_P': 'Forbidden Temple',
    'DeadeyeCanyon_P': 'Deadeye Canyon',
    'EstadioVida_P': 'Estadio Vida',
    'SovereignHeights_P': 'Sovereign Heights',
    'HoopsStadium_P': 'Dunk House',
  };
  if (explicit[raw]) return explicit[raw];
  return raw
    .replace(/_P$/i, '')
    .replace(/_/g, ' ')
    .replace(/\b([a-z])/g, (match) => match.toUpperCase());
}

function scoreStateAtFrame(payload, frameFloat) {
  const cardsById = new Map((payload?.hud?.player_cards || []).map((card) => [String(card.player_id), Number(card.team || card.is_orange || 0)]));
  const goals = [...(payload?.replayMetadata?.gameMetadata?.goals || [])]
    .map((goal) => ({
      frame: Number(goal?.frameNumber || 0),
      team: cardsById.get(String(goal?.playerId?.id || '')) ?? 0,
    }))
    .sort((left, right) => left.frame - right.frame);

  let blue = 0;
  let orange = 0;
  const currentFrame = Math.max(0, Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : 0);
  goals.forEach((goal) => {
    if (goal.frame > currentFrame + 0.001) return;
    if (goal.team === 1) orange += 1;
    else blue += 1;
  });
  return { blue, orange };
}

function displayClockState(payload, frameFloat) {
  const elapsed = elapsedSecondsAtFrame(payload, frameFloat);
  const overtimeEnabled = Boolean(payload?.replay?.overtime);
  if (overtimeEnabled && elapsed > 300) {
    const overtimeSeconds = Math.max(0, elapsed - 300);
    return {
      primary: `+${formatClockSeconds(overtimeSeconds)}`,
      secondary: 'Overtime',
    };
  }
  return {
    primary: formatClockSeconds(Math.max(0, 300 - elapsed)),
    secondary: mapLabelFromCode(payload?.replay?.map_code),
  };
}

function kickoffLikeAtFrame(payload, frameFloat) {
  const sampleFrame = Math.max(0, Math.round(Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : 0));
  const ballSeries = payload?.replayData?.ball || [];
  const playerSeries = payload?.replayData?.players || [];
  const currentBall = ballSeries[sampleFrame] || ballSeries[0] || [0, 0, 0];
  const centerDistance = Math.hypot(Number(currentBall[0] || 0), Number(currentBall[1] || 0));
  if (centerDistance > 240 || Math.abs(Number(currentBall[2] || 0)) > 180) return false;

  const positions = playerSeries
    .map((series) => series?.[sampleFrame] || series?.[0] || null)
    .filter(Boolean)
    .map((pos) => [Number(pos[0] || 0), Number(pos[1] || 0), Number(pos[2] || 0)]);

  if (positions.length < 4) return false;
  const center = positions.reduce((acc, pos) => {
    acc[0] += pos[0];
    acc[1] += pos[1];
    acc[2] += pos[2];
    return acc;
  }, [0, 0, 0]).map((value) => value / positions.length);
  const spread = positions.reduce((max, pos) => Math.max(
    max,
    Math.hypot(pos[0] - center[0], pos[1] - center[1], pos[2] - center[2]),
  ), 0);
  return spread > 1450;
}

function NativeBoostHud({ payload, compact }) {
  const cards = useMemo(() => sortPlayerCards(payload?.hud?.player_cards || []), [payload?.hud?.player_cards]);
  const elementsRef = useRef({});

  useEffect(() => {
    if (!payload || !cards.length) return undefined;
    const boostByPlayer = payload.hud?.boost_by_player || {};
    const update = ({ frame, frameFloat }) => {
      const sampleFrame = Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : Number(frame || 0);
      for (const card of cards) {
        const refs = elementsRef.current[card.player_id];
        if (!refs?.fill || !refs?.label) continue;
        const boost = boostAtFrame(payload, card, sampleFrame);
        if (refs.lastBoost !== undefined && Math.abs(refs.lastBoost - boost) < 0.1) continue;
        refs.lastBoost = boost;
        refs.fill.style.width = `${Math.max(0, Math.min(100, boost))}%`;
        refs.label.textContent = `${Math.round(boost)}`;
      }
    };
    update({ frame: 0 });
    addFrameListener(update);
    return () => removeFrameListener(update);
  }, [payload, cards]);

  const renderCard = (card, tone) => (
    <div className={`native-boost-card ${tone}`} key={card.player_id}>
      <div className={`native-boost-copy ${tone}`}>
        <strong>{card.player_name}</strong>
        {!compact && <span>{card.car_name || 'Octane'}</span>}
      </div>
      <div className={`native-boost-rail ${tone}`}>
        <div
          className="native-boost-fill"
          ref={(node) => {
            if (!elementsRef.current[card.player_id]) elementsRef.current[card.player_id] = {};
            elementsRef.current[card.player_id].fill = node;
          }}
        />
        <em
          ref={(node) => {
            if (!elementsRef.current[card.player_id]) elementsRef.current[card.player_id] = {};
            elementsRef.current[card.player_id].label = node;
          }}
        >
          0
        </em>
      </div>
    </div>
  );

  const blue = cards.filter((card) => Number(card.team || card.is_orange || 0) === 0);
  const orange = cards.filter((card) => Number(card.team || card.is_orange || 0) === 1);

  return (
    <div className={`native-boost-hud ${compact ? 'compact' : ''}`}>
      <div className="native-boost-column blue">{blue.map((card) => renderCard(card, 'blue'))}</div>
      <div className="native-boost-column orange">{orange.map((card) => renderCard(card, 'orange'))}</div>
    </div>
  );
}

function NativeScorebugOverlay({ payload }) {
  const rootRef = useRef(null);
  const blueScoreRef = useRef(null);
  const orangeScoreRef = useRef(null);
  const clockRef = useRef(null);
  const subtitleRef = useRef(null);

  useEffect(() => {
    if (!payload) return undefined;
    let lastBlue = -1;
    let lastOrange = -1;
    let lastClock = '';
    let lastSubtitle = '';
    const update = ({ frame, frameFloat }) => {
      const sampleFrame = Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : Number(frame || 0);
      const score = scoreStateAtFrame(payload, sampleFrame);
      const clock = displayClockState(payload, sampleFrame);
      if (blueScoreRef.current && score.blue !== lastBlue) {
        blueScoreRef.current.textContent = String(score.blue);
        lastBlue = score.blue;
      }
      if (orangeScoreRef.current && score.orange !== lastOrange) {
        orangeScoreRef.current.textContent = String(score.orange);
        lastOrange = score.orange;
      }
      if (clockRef.current && clock.primary !== lastClock) {
        clockRef.current.textContent = clock.primary;
        lastClock = clock.primary;
      }
      if (subtitleRef.current && clock.secondary !== lastSubtitle) {
        subtitleRef.current.textContent = clock.secondary;
        lastSubtitle = clock.secondary;
      }
    };
    update({ frame: 0, frameFloat: 0 });
    addFrameListener(update);
    return () => removeFrameListener(update);
  }, [payload]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    let obscured = false;
    const update = ({ ballPosition, activeCamera }) => {
      if (!root || !ballPosition || !activeCamera?.isCamera) return;
      const projected = ballPosition.clone().project(activeCamera);
      const nextObscured = projected.z > -1
        && projected.z < 1
        && Math.abs(projected.x) < 0.22
        && projected.y > 0.38;
      if (nextObscured === obscured) return;
      obscured = nextObscured;
      root.classList.toggle('obscured', obscured);
    };
    addCameraFrameUpdateListener(update);
    return () => removeCameraFrameUpdateListener(update);
  }, []);

  return (
    <div className="native-stage-scorebug" ref={rootRef}>
      <div className="native-stage-team blue">
        <span>{payload?.replay?.blue_team_name || 'Blue'}</span>
        <strong ref={blueScoreRef}>{payload?.replay?.blue_goals ?? 0}</strong>
      </div>
      <div className="native-stage-clock">
        <strong ref={clockRef}>5:00</strong>
        <span ref={subtitleRef}>{mapLabelFromCode(payload?.replay?.map_code)}</span>
      </div>
      <div className="native-stage-team orange">
        <span>{payload?.replay?.orange_team_name || 'Orange'}</span>
        <strong ref={orangeScoreRef}>{payload?.replay?.orange_goals ?? 0}</strong>
      </div>
    </div>
  );
}

function NativeKickoffCountdown({ payload }) {
  const rootRef = useRef(null);
  const labelRef = useRef(null);
  const kickoffStateRef = useRef({ active: false, waitingForExit: false, startSeconds: null, lastLabel: '' });

  useEffect(() => {
    if (!payload) return undefined;
    const root = rootRef.current;
    const label = labelRef.current;
    const state = kickoffStateRef.current;
    const update = ({ frame, frameFloat }) => {
      if (!root || !label) return;
      const sampleFrame = Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : Number(frame || 0);
      const elapsedSeconds = elapsedSecondsAtFrame(payload, sampleFrame);
      const kickoffLike = kickoffLikeAtFrame(payload, sampleFrame);

      if (!kickoffLike && state.waitingForExit) {
        state.waitingForExit = false;
      }

      if (kickoffLike && !state.active && !state.waitingForExit) {
        state.active = true;
        state.startSeconds = elapsedSeconds;
      } else if (!kickoffLike && state.active && elapsedSeconds - Number(state.startSeconds || 0) > 3.35) {
        state.active = false;
        state.startSeconds = null;
        state.lastLabel = '';
        root.classList.remove('visible');
        root.classList.remove('go');
        return;
      }

      if (!state.active || state.startSeconds == null) {
        root.classList.remove('visible');
        root.classList.remove('go');
        return;
      }

      const delta = Math.max(0, elapsedSeconds - state.startSeconds);
      let nextLabel = '';
      if (delta < 1) nextLabel = '3';
      else if (delta < 2) nextLabel = '2';
      else if (delta < 3) nextLabel = '1';
      else if (delta < 3.55) nextLabel = 'GO!';
      else {
        state.active = false;
        state.waitingForExit = true;
        state.startSeconds = null;
        state.lastLabel = '';
        root.classList.remove('visible');
        root.classList.remove('go');
        return;
      }

      if (nextLabel !== state.lastLabel) {
        label.textContent = nextLabel;
        state.lastLabel = nextLabel;
      }
      root.classList.add('visible');
      root.classList.toggle('go', nextLabel === 'GO!');
    };
    update({ frame: 0, frameFloat: 0 });
    addFrameListener(update);
    return () => removeFrameListener(update);
  }, [payload]);

  return (
    <div className="native-kickoff-countdown" ref={rootRef}>
      <span ref={labelRef}>3</span>
    </div>
  );
}

function NativeSelectedPanel({ payload, selectedPlayerId }) {
  const cards = payload?.hud?.player_cards || [];
  const card = cards.find((item) => item.player_id === selectedPlayerId) || cards[0];
  const boostRef = useRef(null);
  const speedRef = useRef(null);
  const frameRef = useRef(null);

  useEffect(() => {
    if (!payload || !card) return undefined;
    const playerOrder = payload?.hud?.player_order || [];
    const playerIndex = Math.max(0, playerOrder.indexOf(card.player_id));
    const positionSeries = payload?.replayData?.players?.[playerIndex] || [];
    const sampleHz = Number(payload?.nativeTelemetry?.sample_hz || payload?.nativeTelemetry?.base_hz || 60);
    let lastBoost = -999;
    let lastSpeed = -999;
    let lastFrameLabel = '';
    const update = ({ frame, frameFloat }) => {
      const sampleFrame = Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : Number(frame || 0);
      const boost = Math.round(boostAtFrame(payload, card, sampleFrame));
      const speed = Math.round(interpolatedSpeed(positionSeries, sampleFrame, sampleHz));
      const frameLabel = `${Math.round(sampleFrame)}`;
      if (boostRef.current && Math.abs(lastBoost - boost) >= 1) {
        boostRef.current.textContent = `${boost}`;
        lastBoost = boost;
      }
      if (speedRef.current && Math.abs(lastSpeed - speed) >= 4) {
        speedRef.current.textContent = `${speed}`;
        lastSpeed = speed;
      }
      if (frameRef.current && frameLabel !== lastFrameLabel) {
        frameRef.current.textContent = frameLabel;
        lastFrameLabel = frameLabel;
      }
    };
    update({ frame: 0 });
    addFrameListener(update);
    return () => removeFrameListener(update);
  }, [payload, card]);

  if (!card) return null;
  return (
    <div className="native-selected-panel">
      <h2>{card.player_name}</h2>
      <div className="native-stat-grid">
        <div className="native-stat"><span>Boost</span><strong ref={boostRef}>0</strong></div>
        <div className="native-stat"><span>Speed</span><strong ref={speedRef}>0</strong></div>
        <div className="native-stat"><span>Frame</span><strong ref={frameRef}>0</strong></div>
      </div>
      <div className="native-shortcuts">{card.car_name || 'Octane'} - {Number(card.team || card.is_orange || 0) === 1 ? 'Orange' : 'Blue'}</div>
    </div>
  );
}

function NativeCameraTelemetry() {
  const labelRef = useRef(null);
  const phaseRef = useRef(null);
  const focusRef = useRef(null);
  const pressureRef = useRef(null);
  const fovRef = useRef(null);

  useEffect(() => {
    const update = ({ telemetry }) => {
      if (!telemetry) return;
      const cam = telemetry.cameraSettings || {};
      if (labelRef.current) labelRef.current.textContent = `${telemetry.label || telemetry.mode || 'Camera'}`;
      if (phaseRef.current) phaseRef.current.textContent = `Phase: ${telemetry.phase || 'manual view'}`;
      if (focusRef.current) focusRef.current.textContent = `Focus: ${telemetry.nearestPlayer || telemetry.zone || 'ball'}`;
      if (pressureRef.current) pressureRef.current.textContent = `Pressure: ${Math.round((telemetry.pressure || 0) * 100)}%`;
      if (fovRef.current) fovRef.current.textContent = `FOV: ${Math.round(telemetry.fov || cam.fieldOfView || 0)}`;
    };
    addCameraFrameUpdateListener(update);
    return () => removeCameraFrameUpdateListener(update);
  }, []);

  return (
    <div className="native-camera-telemetry">
      <strong ref={labelRef}>Camera</strong>
      <span ref={phaseRef}>Phase: waiting</span>
      <span ref={focusRef}>Focus: ball</span>
      <span ref={pressureRef}>Pressure: 0%</span>
      <span ref={fovRef}>FOV: 0</span>
    </div>
  );
}

function eventFrame(payload, event) {
  if (Number.isFinite(Number(event?.frame))) return Number(event.frame);
  if (Number.isFinite(Number(event?.frameNumber))) return Number(event.frameNumber);
  const hz = Number(payload?.nativeTelemetry?.sample_hz || payload?.nativeTelemetry?.base_hz || 60);
  if (Number.isFinite(Number(event?.t))) return Math.round(Number(event.t) * hz);
  return 0;
}


function worldToRadarStyle(x, z) {
  const px = 50 + (Number(x || 0) / 8192) * 100;
  const py = 50 - (Number(z || 0) / 10240) * 100;
  return { left: `${Math.max(2, Math.min(98, px))}%`, top: `${Math.max(2, Math.min(98, py))}%` };
}

function NativeRadarOverlay({ payload }) {
  const cards = payload?.hud?.player_cards || [];
  const dotsRef = useRef({});
  useEffect(() => {
    if (!payload) return undefined;
    const order = payload?.hud?.player_order || [];
    const update = ({ frame }) => {
      const ball = payload?.replayData?.ball?.[frame] || [];
      const ballDot = dotsRef.current.ball;
      if (ballDot) Object.assign(ballDot.style, worldToRadarStyle(ball[0], ball[1]));
      order.forEach((playerId, index) => {
        const node = dotsRef.current[playerId];
        const point = payload?.replayData?.players?.[index]?.[frame];
        if (node && point) Object.assign(node.style, worldToRadarStyle(point[0], point[1]));
      });
    };
    update({ frame: 0 });
    addFrameListener(update);
    return () => removeFrameListener(update);
  }, [payload]);

  return (
    <div className="native-radar" aria-label="Replay radar">
      <span className="native-radar-midline" />
      <span className="native-radar-ball" ref={(node) => { dotsRef.current.ball = node; }} />
      {cards.map((card) => (
        <span
          key={card.player_id}
          className={`native-radar-dot ${Number(card.team || 0) === 1 ? 'orange' : 'blue'}`}
          title={card.player_name}
          ref={(node) => { dotsRef.current[card.player_id] = node; }}
        />
      ))}
    </div>
  );
}

function NativeDebugOverlay({ payload, replayId }) {
  const frameRef = useRef(null);
  const cameraRef = useRef(null);
  useEffect(() => {
    const onFrame = ({ frame, frameFloat }) => {
      if (frameRef.current) frameRef.current.textContent = `frame: ${Math.round(Number.isFinite(Number(frameFloat)) ? Number(frameFloat) : Number(frame || 0))}`;
    };
    const onCamera = ({ telemetry }) => {
      if (cameraRef.current) cameraRef.current.textContent = `camera: ${telemetry?.label || telemetry?.mode || 'none'}`;
    };
    addFrameListener(onFrame);
    addCameraFrameUpdateListener(onCamera);
    return () => {
      removeFrameListener(onFrame);
      removeCameraFrameUpdateListener(onCamera);
    };
  }, []);
  if (!payload) return null;
  const returnedId = String(payload?.replay?.replay_id || payload?.replay_id || payload?.request?.replay_id || '');
  return (
    <div className="native-debug-overlay">
      <strong>Native debug</strong>
      <span>requested: {replayId}</span>
      <span>returned: {returnedId || 'unknown'}</span>
      <span ref={frameRef}>frame: 0</span>
      <span>schema: {payload?.nativeTelemetry?.schema_version || payload?.payload_schema || 'legacy'}</span>
      <span ref={cameraRef}>camera: waiting</span>
      <span>pads: {(payload?.hud?.boost_pad_layout || []).length}</span>
    </div>
  );
}

function NativeTimelineMarkers({ payload, gameManager }) {
  const goals = payload?.replayMetadata?.gameMetadata?.goals || [];
  const hints = payload?.director_hints || payload?.nativeTelemetry?.director_hints || [];
  const seen = new Set();
  const totalFrames = Math.max(1, payload?.replayData?.frames?.length || 1);
  const markers = [
    ...goals.map((goal, index) => ({ type: 'goal', frame: Number(goal?.frameNumber || 0), label: `Goal ${index + 1}` })),
    ...hints
      .filter((hint) => TIMELINE_MAJOR_TYPES.has(String(hint?.type || '').toLowerCase()))
      .map((hint) => ({ type: String(hint.type || 'event'), frame: eventFrame(payload, hint), label: hint.label || hint.type })),
  ].filter((marker) => {
    const key = `${marker.type}-${marker.frame}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const spacing = Math.max(44, Math.round(totalFrames / 22));
  const thinned = [];
  let lastFrame = -spacing;
  for (const marker of markers.sort((left, right) => Number(left.frame || 0) - Number(right.frame || 0))) {
    const markerFrame = Number(marker.frame || 0);
    const keep = marker.type === 'goal' || markerFrame - lastFrame >= spacing;
    if (!keep) continue;
    thinned.push(marker);
    lastFrame = markerFrame;
    if (thinned.length >= 24) break;
  }
  if (!thinned.length || !gameManager) return null;
  return (
    <div className="native-timeline" aria-label="Replay timeline markers">
      {thinned.map((marker, index) => {
        const meta = timelineMarkerMeta(marker.type);
        return (
          <button
            key={`${marker.type}-${marker.frame}-${index}`}
            type="button"
            className={`native-timeline-marker ${meta.tone}`}
            style={{ left: `${frameRatio(payload, marker.frame) * 100}%` }}
            title={`Jump to ${meta.label}: ${marker.label}`}
            onClick={() => gameManager.clock.setFrame(Number(marker.frame || 0), { reason: `timeline-${marker.type}`, silentDelta: true })}
          >
            {meta.short}
          </button>
        );
      })}
    </div>
  );
}

function NativeTimelineLegend() {
  return (
    <div className="native-timeline-legend" aria-label="Replay timeline legend">
      {TIMELINE_LEGEND_ITEMS.map((type) => {
        const meta = timelineMarkerMeta(type);
        return (
          <span className={`native-timeline-key ${meta.tone}`} key={type}>
            <strong>{meta.short}</strong>
            <em>{meta.label}</em>
          </span>
        );
      })}
    </div>
  );
}

function GoalButtons({ payload, gameManager }) {
  const goals = payload?.replayMetadata?.gameMetadata?.goals || [];
  if (!goals.length || !gameManager) return null;
  const cardsById = new Map((payload?.hud?.player_cards || []).map((card) => [card.player_id, card]));
  return (
    <div className="native-goal-jumps">
      {goals.map((goal, index) => {
        const scorer = cardsById.get(goal?.playerId?.id);
        return (
          <button key={`${goal?.frameNumber || 0}-${index}`} type="button" className="native-chip compact" onClick={() => gameManager.clock.setFrame(Number(goal?.frameNumber || 0), { reason: 'goal-button' })}>
            {scorer?.player_name || `Goal ${index + 1}`}
          </button>
        );
      })}
    </div>
  );
}

function FullscreenControls({ fullscreenActive, onToggleFullscreen }) {
  return (
    <div className="native-toolbar-group">
      <button type="button" className={`native-chip compact ${fullscreenActive ? 'active' : ''}`} onClick={onToggleFullscreen}>
        {fullscreenActive ? 'Exit fullscreen' : 'Fullscreen'}
      </button>
    </div>
  );
}

function CameraControls({ cameraMode, onChangeCameraMode }) {
  return (
    <div className="native-camera-row">
      {CAMERA_MODES.map((mode) => (
        <button key={mode.id} type="button" className={`native-chip ${cameraMode === mode.id ? 'active' : ''}`} onClick={() => onChangeCameraMode(mode.id)}>
          {mode.label}
        </button>
      ))}
    </div>
  );
}

function SpeedControls({ gameManager, playbackRate, onChangePlaybackRate }) {
  useEffect(() => {
    if (gameManager?.clock?.setPlaybackRate) gameManager.clock.setPlaybackRate(playbackRate);
  }, [gameManager, playbackRate]);

  return (
    <div className="native-toolbar-group">
      {SPEED_OPTIONS.map((rate) => (
        <button key={rate} type="button" className={`native-chip compact ${playbackRate === rate ? 'active' : ''}`} onClick={() => onChangePlaybackRate(rate)}>
          {rate}x
        </button>
      ))}
    </div>
  );
}

function SettingsControls({ settings, onChange }) {
  const patch = (updates) => onChange((current) => ({ ...current, ...updates }));
  return (
    <div className="native-settings-panel">
      <div className="native-setting-line">
        <span>Player cam</span>
        <div className="native-toolbar-group wrap">
          {LOOK_OPTIONS.map((option) => (
            <button key={option.id} type="button" className={`native-chip compact ${settings.playerLookMode === option.id ? 'active' : ''}`} onClick={() => patch({ playerLookMode: option.id })}>
              {option.label}
            </button>
          ))}
        </div>
      </div>
      <label className="native-setting-line">
        <span>Director aggression</span>
        <input type="range" min="0" max="1" step="0.05" value={settings.directorAggression} onChange={(event) => patch({ directorAggression: Number(event.target.value) })} />
      </label>
      <div className="native-setting-line toggles">
        <button type="button" className={`native-chip compact ${settings.showHud ? 'active' : ''}`} onClick={() => patch({ showHud: !settings.showHud })}>Scorebug</button>
        <button type="button" className={`native-chip compact ${settings.showBoostHud ? 'active' : ''}`} onClick={() => patch({ showBoostHud: !settings.showBoostHud })}>Boost HUD</button>
        <button type="button" className={`native-chip compact ${settings.autoPlayerPov ? 'active' : ''}`} onClick={() => patch({ autoPlayerPov: !settings.autoPlayerPov })}>Auto POV</button>
        <button type="button" className={`native-chip compact ${settings.showTelemetry ? 'active' : ''}`} onClick={() => patch({ showTelemetry: !settings.showTelemetry })}>Telemetry</button>
        <button type="button" className={`native-chip compact ${settings.showStatPanel ? 'active' : ''}`} onClick={() => patch({ showStatPanel: !settings.showStatPanel })}>Stats</button>
        <button type="button" className={`native-chip compact ${settings.compactBoost ? 'active' : ''}`} onClick={() => patch({ compactBoost: !settings.compactBoost })}>Compact</button>
        <button type="button" className={`native-chip compact ${settings.showRadar ? 'active' : ''}`} onClick={() => patch({ showRadar: !settings.showRadar })}>Radar</button>
        <button type="button" className={`native-chip compact ${settings.showDebug ? 'active' : ''}`} onClick={() => patch({ showDebug: !settings.showDebug })}>Debug</button>
      </div>
      <div className="native-setting-line">
        <span>Quality</span>
        <div className="native-toolbar-group wrap">
          {['performance', 'balanced', 'cinematic'].map((mode) => (
            <button key={mode} type="button" className={`native-chip compact ${settings.qualityMode === mode ? 'active' : ''}`} onClick={() => patch({ qualityMode: mode })}>
              {mode}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function NativeViewerStage({ gameManager, payload, replayId, cameraMode, selectedPlayerId, onChangeCameraMode, onSelectPlayer, cameraSettings, setCameraSettings, playbackRate, setPlaybackRate, embedMode, fullscreenActive, onToggleFullscreen }) {
  const orderedCards = useMemo(() => sortPlayerCards(payload?.hud?.player_cards || []), [payload?.hud?.player_cards]);

  useReplayCamera({
    gameManager,
    payload,
    cameraMode,
    selectedPlayerId,
    ready: Boolean(gameManager && payload),
    cameraSettings,
  });

  useEffect(() => {
    const onKeyDown = (event) => {
      if (!gameManager?.clock) return;
      if (event.target?.tagName === 'INPUT') return;
      if (event.code === 'Space') { event.preventDefault(); gameManager.clock.toggle?.(); }
      if (event.key === 'ArrowRight') gameManager.clock.seekSeconds?.(event.shiftKey ? 10 : 2, { reason: 'keyboard' });
      if (event.key === 'ArrowLeft') gameManager.clock.seekSeconds?.(event.shiftKey ? -10 : -2, { reason: 'keyboard' });
      if (event.key === '.') gameManager.clock.step?.(1);
      if (event.key === ',') gameManager.clock.step?.(-1);
      if (event.key.toLowerCase() === 'f') {
        event.preventDefault();
        onToggleFullscreen?.();
      }
      const modeIndex = Number(event.key) - 1;
      if (Number.isInteger(modeIndex) && CAMERA_MODES[modeIndex]) onChangeCameraMode(CAMERA_MODES[modeIndex].id);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [gameManager, onChangeCameraMode, onToggleFullscreen]);

  return (
    <div className={`native-shell ${embedMode ? 'native-embed' : ''}`}>
      <div
        className="native-stage-wrap"
        onWheel={embedMode ? (event) => event.preventDefault() : undefined}
      >
        <NativeReplayViewport gameManager={gameManager} autoplay>
          {cameraSettings.showHud && <NativeScorebugOverlay payload={payload} />}
          {cameraSettings.showHud && <NativeKickoffCountdown payload={payload} />}
          {cameraSettings.showBoostHud && <NativeBoostHud payload={payload} compact={cameraSettings.compactBoost} />}
          {cameraSettings.showTelemetry && <NativeCameraTelemetry />}
          {cameraSettings.showStatPanel && <NativeSelectedPanel payload={payload} selectedPlayerId={selectedPlayerId} />}
          {cameraSettings.showRadar && <NativeRadarOverlay payload={payload} />}
          {cameraSettings.showDebug && <NativeDebugOverlay payload={payload} replayId={replayId} />}
        </NativeReplayViewport>
      </div>
      <div className={`native-controls ${embedMode ? 'native-embed' : ''}`}>
        <div className="native-controls-row">
          <PlayControls />
          <SpeedControls gameManager={gameManager} playbackRate={playbackRate} onChangePlaybackRate={setPlaybackRate} />
          <CameraControls cameraMode={cameraMode} onChangeCameraMode={onChangeCameraMode} />
          <FullscreenControls fullscreenActive={fullscreenActive} onToggleFullscreen={onToggleFullscreen} />
        </div>
        {!embedMode ? <SettingsControls settings={cameraSettings} onChange={setCameraSettings} /> : null}
        <div className="native-controls-row players">
          <div className="native-player-row">
            {orderedCards.map((card) => (
              <button key={card.player_id} type="button" className={`native-chip ${selectedPlayerId === card.player_id ? 'active' : ''} ${Number(card.team || card.is_orange || 0) === 1 ? 'orange' : 'blue'}`} onClick={() => onSelectPlayer(card.player_id)}>
                {card.player_name}
              </button>
            ))}
          </div>
          <GoalButtons payload={payload} gameManager={gameManager} />
        </div>
        <div className="native-slider-wrap">
          <NativeTimelineMarkers payload={payload} gameManager={gameManager} />
          <Slider />
          <NativeTimelineLegend />
        </div>
      </div>
    </div>
  );
}

function NativeReplayViewport({ gameManager, autoplay, children }) {
  const mountRef = useRef(null);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount || !gameManager?.getDOMNode) return undefined;

    const domNode = gameManager.getDOMNode();
    if (!domNode) return undefined;

    if (!mount.contains(domNode)) {
      mount.innerHTML = '';
      mount.appendChild(domNode);
    }

    const resize = () => {
      const { clientWidth, clientHeight } = mount;
      if (clientWidth > 0 && clientHeight > 0) {
        dispatchCanvasResizeEvent({ width: clientWidth, height: clientHeight });
      }
    };

    resize();
    dispatchPlayPauseEvent({ paused: !autoplay });

    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    observer?.observe(mount);
    window.addEventListener('resize', resize);

    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', resize);
    };
  }, [gameManager, autoplay]);

  return (
    <>
      <div className="native-canvas-host" ref={mountRef} />
      {children}
    </>
  );
}

function NativeViewerApp() {
  const query = useMemo(() => currentQuery(), []);
  const replayId = query.get('replayId') || '';
  const apiBase = query.get('apiBase') || window.location.origin;
  const embedMode = query.get('embed') === '1';
  const debugNative = query.get('debugNative') === '1' || query.get('debug') === '1';
  const requestHz = Number(query.get('hz') || 60);
  const requestMaxFrames = Number(query.get('maxFrames') || query.get('max_frames') || 24000);
  const preferredStartFrameHint = Number(query.get('preferredStartFrame') || query.get('preferred_start_frame') || '');
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState('');
  const [gameManager, setGameManager] = useState(null);
  const [sceneReady, setSceneReady] = useState(false);
  const [cameraMode, setCameraMode] = useState('replay');
  const [selectedPlayerId, setSelectedPlayerId] = useState('');
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [cameraSettings, setCameraSettings] = useState(() => ({
    ...DEFAULT_CAMERA_SETTINGS,
    compactBoost: embedMode ? true : DEFAULT_CAMERA_SETTINGS.compactBoost,
    showStatPanel: embedMode ? false : DEFAULT_CAMERA_SETTINGS.showStatPanel,
    showTelemetry: embedMode ? false : DEFAULT_CAMERA_SETTINGS.showTelemetry,
    showRadar: embedMode ? false : DEFAULT_CAMERA_SETTINGS.showRadar,
    qualityMode: embedMode ? 'performance' : DEFAULT_CAMERA_SETTINGS.qualityMode,
  }));
  const [playbackRate, setPlaybackRate] = useState(1);
  const preferredStartFrameRef = useRef(null);
  const requestSeqRef = useRef(0);

  useEffect(() => {
    if (debugNative) setCameraSettings((current) => ({ ...current, showDebug: true }));
  }, [debugNative]);

  useEffect(() => {
    const syncFullscreen = () => {
      setFullscreenActive(Boolean(document.fullscreenElement));
    };
    document.addEventListener('fullscreenchange', syncFullscreen);
    syncFullscreen();
    return () => document.removeEventListener('fullscreenchange', syncFullscreen);
  }, []);

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        return;
      }
      const target = document.documentElement;
      if (target?.requestFullscreen) {
        await target.requestFullscreen();
      }
    } catch {
      // Ignore fullscreen API errors; the viewer still works normally.
    }
  }

  useEffect(() => {
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    const controller = new AbortController();
    setPayload(null);
    setGameManager(null);
    setSceneReady(false);
    setSelectedPlayerId('');
    preferredStartFrameRef.current = null;
    setError('');
    async function load() {
      if (!replayId) { setError('Missing replay id.'); return; }
      try {
        const response = await fetch(buildViewerUrl(apiBase, replayId, { hz: requestHz, maxFrames: requestMaxFrames }), { signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json', 'X-Native-Viewer-Replay-Id': replayId } });
        if (!response.ok) {
          let detail = `Viewer payload failed: ${response.status}`;
          try {
            const errorPayload = await response.json();
            if (errorPayload?.detail) detail = String(errorPayload.detail);
          } catch { /* keep HTTP fallback */ }
          throw new Error(detail);
        }
        const nextPayload = await response.json();
        const returnedReplayId = String(nextPayload?.replay?.replay_id || nextPayload?.replay_id || nextPayload?.request?.replay_id || '');
        if (controller.signal.aborted || requestSeqRef.current !== requestSeq) return;
        if (returnedReplayId && returnedReplayId !== replayId) {
          throw new Error(`Native viewer returned ${returnedReplayId}, but requested ${replayId}.`);
        }
        if (!controller.signal.aborted) {
          setPayload(nextPayload);
          setSelectedPlayerId(nextPayload?.hud?.player_order?.[0] || nextPayload?.hud?.player_cards?.[0]?.player_id || '');
          preferredStartFrameRef.current = Number.isFinite(preferredStartFrameHint) && preferredStartFrameHint >= 0
            ? Math.max(0, Math.min((nextPayload?.replayData?.frames?.length || 1) - 1, preferredStartFrameHint))
            : 0;
          setError('');
        }
      } catch (loadError) {
        if (controller.signal.aborted || requestSeqRef.current !== requestSeq) return;
        setError(loadError.message || 'Viewer payload failed.');
      }
    }
    load();
    return () => { controller.abort(); };
  }, [apiBase, replayId, requestHz, requestMaxFrames, preferredStartFrameHint]);

  useEffect(() => {
    if (!gameManager || !payload) return undefined;
    let cancelled = false;
    let cleanup = null;
    setSceneReady(false);
    installSceneEnhancements(payload)
      .then((nextCleanup) => {
        if (cancelled) {
          nextCleanup?.();
          return;
        }
        cleanup = nextCleanup;
        setSceneReady(true);
      })
      .catch((sceneError) => {
        if (!cancelled) setError(sceneError?.message || 'Viewer scene enhancement failed.');
      });
    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, [gameManager, payload, replayId]);

  useEffect(() => {
    if (!gameManager?.renderer) return undefined;
    const renderer = gameManager.renderer;
    const syncRenderer = () => {
      const quality = cameraSettings.qualityMode || 'balanced';
      const pixelCap = quality === 'performance' ? 1.0 : quality === 'cinematic' ? 2.0 : 1.5;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, pixelCap));
      renderer.shadowMap.enabled = quality !== 'performance';
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      if ('outputColorSpace' in renderer) renderer.outputColorSpace = THREE.SRGBColorSpace;
      if ('toneMapping' in renderer) renderer.toneMapping = THREE.ACESFilmicToneMapping;
      if ('toneMappingExposure' in renderer) renderer.toneMappingExposure = quality === 'cinematic' ? 1.16 : 1.08;
    };
    syncRenderer();
    window.addEventListener('resize', syncRenderer);
    return () => window.removeEventListener('resize', syncRenderer);
  }, [gameManager, cameraSettings.qualityMode]);

  const options = useMemo(() => {
    if (!payload) return null;
    const replayData = {
      ...payload.replayData,
      snapFrames: payload?.nativeTelemetry?.snap_frames || payload?.nativeTelemetry?.snapFrames || detectSnapFrames(payload.replayData),
    };
    return {
      replayId,
      replayData,
      replayMetadata: payload.replayMetadata,
      clock: SmoothReplayClock.convertReplayToClock(replayData),
      defaultLoadouts: false,
      useBallRotation: false,
    };
  }, [payload, replayId]);

  useEffect(() => {
    if (!gameManager?.clock || typeof gameManager.render !== 'function') return undefined;
    const clock = gameManager.clock;
    const originalSetFrame = clock.setFrame?.bind(clock);
    const originalSeekSeconds = clock.seekSeconds?.bind(clock);
    if (!originalSetFrame) return undefined;

    clock.setFrame = (frame, options = {}) => {
      originalSetFrame(frame, { ...options, silentDelta: true });
      gameManager.render();
    };

    if (originalSeekSeconds) {
      clock.seekSeconds = (seconds, options = {}) => {
        originalSeekSeconds(seconds, { ...options, silentDelta: true });
        gameManager.render();
      };
    }

    return () => {
      clock.setFrame = originalSetFrame;
      if (originalSeekSeconds) clock.seekSeconds = originalSeekSeconds;
    };
  }, [gameManager]);

  useEffect(() => {
    if (!gameManager || preferredStartFrameRef.current == null) return;
    const targetFrame = Number(preferredStartFrameRef.current || 0);
    if (targetFrame > 0 && gameManager.clock.currentFrame === 0) gameManager.clock.setFrame(targetFrame, { reason: 'preferred-start', silentDelta: true });
  }, [gameManager]);

  const payloadReplayId = String(payload?.replay?.replay_id || payload?.replay_id || payload?.request?.replay_id || '');
  const viewerReadyForReplay = Boolean(options && (!payloadReplayId || payloadReplayId === replayId));

  if (error) return <div className={`native-root ${embedMode ? 'native-embed' : ''}`}><div className="native-status">{error}</div></div>;
  if (!viewerReadyForReplay) return <div className={`native-root ${embedMode ? 'native-embed' : ''}`}><div className="native-status">Loading native replay viewer...</div></div>;

  return (
    <div className={`native-root ${embedMode ? 'native-embed' : ''}`}>
      {!embedMode ? <div className="native-header">
        <div>
          <p className="native-kicker">Native Replay Viewer</p>
          <h1>{payload?.replay?.title || replayId}</h1>
          <p className="native-subtitle">Review camera, player POV, pads, boost trails, and replay telemetry.</p>
        </div>
        <div className="native-scoreline">
          <div><span>{payload?.replay?.blue_team_name || 'Blue'}</span><strong>{payload?.replay?.blue_goals ?? 0}</strong></div>
          <div><span>{payload?.replay?.orange_team_name || 'Orange'}</span><strong>{payload?.replay?.orange_goals ?? 0}</strong></div>
        </div>
      </div> : null}
      <GameManagerLoader key={`native-loader-${replayId}`} options={options} onLoad={setGameManager}>
        {gameManager && sceneReady ? (
          <NativeViewerStage
            key={`native-stage-${replayId}`}
            gameManager={gameManager}
            payload={payload}
            replayId={replayId}
            cameraMode={cameraMode}
            selectedPlayerId={selectedPlayerId}
            onChangeCameraMode={setCameraMode}
            onSelectPlayer={setSelectedPlayerId}
            cameraSettings={cameraSettings}
            setCameraSettings={setCameraSettings}
            playbackRate={playbackRate}
            setPlaybackRate={setPlaybackRate}
            embedMode={embedMode}
            fullscreenActive={fullscreenActive}
            onToggleFullscreen={toggleFullscreen}
          />
        ) : (
          <div className="native-status">Preparing stadium, cameras, pads, boost trails, and telemetry...</div>
        )}
      </GameManagerLoader>
    </div>
  );
}

ReactDOM.render(<NativeViewerApp />, document.getElementById('root'));
