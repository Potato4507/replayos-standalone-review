import { useEffect, useMemo } from 'react';
import * as THREE from 'three';
import CameraManager from 'replay-viewer/managers/CameraManager';
import SceneManager from 'replay-viewer/managers/SceneManager';
import { addFrameListener, removeFrameListener } from 'replay-viewer/eventbus/events/frame';
import { dispatchCameraChange } from 'replay-viewer/eventbus/events/cameraChange';
import { dispatchCameraFrameUpdate } from 'replay-viewer/eventbus/events/cameraFrameUpdate';

export const CAMERA_MODES = [
  { id: 'director', label: 'Auto Director' },
  { id: 'replay', label: 'Review Cam' },
  { id: 'player', label: 'True POV' },
  { id: 'driver', label: 'Forward POV' },
  { id: 'tactical', label: 'Tactical' },
  { id: 'blue-goal', label: 'Blue End' },
  { id: 'orange-goal', label: 'Orange End' },
];

const FIELD = {
  x: 4096,
  z: 5120,
  goalZ: 5120,
};

const DEFAULT_RL_CAMERA = {
  distance: 270,
  height: 110,
  pitch: -4,
  stiffness: 0.45,
  swivelSpeed: 4,
  transitionSpeed: 1.2,
  fieldOfView: 108,
};

const CAMERA_SETTINGS_KEYS = {
  fieldOfView: ['fieldOfView', 'field_of_view', 'fov', 'field_of_view_degrees'],
  height: ['height', 'camera_height'],
  pitch: ['pitch', 'angle', 'camera_pitch'],
  distance: ['distance', 'camera_distance'],
  stiffness: ['stiffness', 'camera_stiffness'],
  swivelSpeed: ['swivelSpeed', 'swivel_speed', 'swivel', 'camera_swivel_speed'],
  transitionSpeed: ['transitionSpeed', 'transition_speed', 'transition', 'camera_transition_speed'],
};

function clamp(value, min, max) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return min;
  return Math.max(min, Math.min(max, numeric));
}

function clamp01(value) {
  return clamp(value, 0, 1);
}

function normalizeName(value) {
  return String(value || '').trim().toLowerCase();
}

function safeVector(vector) {
  if (!vector || !Number.isFinite(vector.x) || !Number.isFinite(vector.y) || !Number.isFinite(vector.z)) {
    return new THREE.Vector3();
  }
  return vector;
}

function cloneVec(vector, fallback = new THREE.Vector3()) {
  if (!vector || !Number.isFinite(vector.x) || !Number.isFinite(vector.y) || !Number.isFinite(vector.z)) {
    return fallback.clone();
  }
  return vector.clone();
}

function clampCameraPosition(position) {
  position.x = clamp(position.x, -3920, 3920);
  position.y = clamp(position.y, 40, 3100);
  position.z = clamp(position.z, -5480, 5480);
  return position;
}

function sanitizeSpectatorDesired(desired) {
  if (!desired?.position || !desired?.target) return { unsafe: false, shifted: 0 };
  const position = desired.position.clone();
  const target = desired.target.clone();
  const original = position.clone();
  const absX = Math.abs(position.x);
  const absZ = Math.abs(position.z);
  const cornerTrap = absX > 3050 && absZ > 3900;
  const sideWallTrap = absX > 3320 && position.y < 1080;
  const backWallTrap = absZ > 4720 && position.y < 1040;

  if (absX > 3180) {
    position.x = Math.sign(position.x || 1) * Math.min(absX, 3180);
    position.y = Math.max(position.y, 900 + Math.max(0, absX - 3180) * 0.18);
  }
  if (absZ > 4580) {
    position.z = Math.sign(position.z || 1) * Math.min(absZ, 4580);
    position.y = Math.max(position.y, 960 + Math.max(0, absZ - 4580) * 0.24);
  }
  if (cornerTrap) {
    position.y = Math.max(position.y, 1260);
    position.x *= 0.9;
    position.z *= 0.94;
  }

  const toTarget = target.clone().sub(position);
  const distanceToTarget = toTarget.length();
  if (distanceToTarget < 1350) {
    position.add(toTarget.clone().normalize().multiplyScalar(-(1350 - distanceToTarget)));
    position.y = Math.max(position.y, target.y + 260);
  }

  clampCameraPosition(position);
  desired.position.copy(position);
  desired.target.copy(target);

  return {
    unsafe: cornerTrap || sideWallTrap || backWallTrap,
    shifted: original.distanceTo(position),
  };
}


function getRendererAspect(gameManager) {
  const canvas = gameManager?.renderer?.domElement;
  const width = canvas?.clientWidth || canvas?.width || window.innerWidth || 16;
  const height = canvas?.clientHeight || canvas?.height || window.innerHeight || 9;
  return clamp(width / Math.max(1, height), 0.45, 3.2);
}

function horizontalFovToVerticalFov(horizontalFov, aspect) {
  const radians = THREE.MathUtils.degToRad(clamp(horizontalFov, 60, 130));
  return THREE.MathUtils.radToDeg(2 * Math.atan(Math.tan(radians / 2) / Math.max(0.45, aspect || 16 / 9)));
}

function syncCameraProjection(camera, gameManager, desired) {
  const aspect = getRendererAspect(gameManager);
  const desiredFov = desired?.fovIsHorizontal ? horizontalFovToVerticalFov(desired.fov, aspect) : desired.fov;
  let changed = false;

  if (Math.abs(camera.aspect - aspect) > 0.001) {
    camera.aspect = aspect;
    changed = true;
  }

  return {
    verticalFov: clamp(desiredFov, 34, 115),
    aspect,
    projectionChanged: changed,
  };
}

function readFirstNumber(source, keys) {
  for (const key of keys) {
    const value = source?.[key];
    if (value !== undefined && value !== null && Number.isFinite(Number(value))) {
      return Number(value);
    }
  }
  return undefined;
}

function normalizeCameraSettings(card, payload) {
  const candidates = [
    card?.camera_settings,
    card?.cameraSettings,
    card?.camera,
    card?.settings?.camera,
    findMetadataPlayer(payload, card)?.camera_settings,
    findMetadataPlayer(payload, card)?.cameraSettings,
  ].filter(Boolean);

  const result = { ...DEFAULT_RL_CAMERA };

  candidates.forEach((source) => {
    Object.entries(CAMERA_SETTINGS_KEYS).forEach(([outKey, aliases]) => {
      const value = readFirstNumber(source, aliases);
      if (value !== undefined) {
        result[outKey] = value;
      }
    });
  });

  result.distance = clamp(result.distance, 120, 420);
  result.height = clamp(result.height, 45, 220);
  result.pitch = clamp(result.pitch, -15, 8);
  result.stiffness = clamp(result.stiffness, 0, 1);
  result.swivelSpeed = clamp(result.swivelSpeed, 0.5, 10);
  result.transitionSpeed = clamp(result.transitionSpeed, 0.25, 2.5);
  result.fieldOfView = clamp(result.fieldOfView, 80, 115);

  return result;
}

function findMetadataPlayer(payload, card) {
  if (!payload || !card) return null;
  const players = payload?.replayMetadata?.players || payload?.players || payload?.metadata?.players || [];
  const cardName = normalizeName(card.player_name || card.name);
  const cardId = String(card.player_id || card.id || card.online_id || '');
  return players.find((player) => {
    const playerName = normalizeName(player?.name || player?.player_name);
    const playerId = String(player?.id?.id || player?.id?.online_id || player?.player_id || player?.online_id || '');
    return (cardName && playerName === cardName) || (cardId && playerId === cardId);
  }) || null;
}

function findCardById(payload, playerId) {
  const cards = payload?.hud?.player_cards || payload?.player_cards || [];
  return cards.find((card) => card.player_id === playerId || card.id === playerId) || cards[0] || null;
}

function cardMapByName(payload) {
  return new Map((payload?.hud?.player_cards || payload?.player_cards || []).map((card) => [normalizeName(card.player_name || card.name), card]));
}

function findPlayerManagerByName(players, normalizedName) {
  if (!players?.length) return null;
  if (!normalizedName) return players[0] || null;
  return players.find((player) => normalizeName(player.playerName) === normalizedName) || players[0] || null;
}

function averagePlayerPosition(players) {
  const center = new THREE.Vector3();
  let count = 0;
  players.forEach((player) => {
    if (player?.carGroup?.position) {
      center.add(player.carGroup.position);
      count += 1;
    }
  });
  return count ? center.multiplyScalar(1 / count) : center;
}

function estimateBallVelocity(ballPos, lastBallPos, delta) {
  if (!lastBallPos || !delta) return new THREE.Vector3();
  return ballPos.clone().sub(lastBallPos).multiplyScalar(1 / Math.max(delta, 1 / 120));
}

function frameGoalSet(payload) {
  return new Set((payload?.replayMetadata?.gameMetadata?.goals || []).map((goal) => Number(goal.frameNumber || 0)));
}

function trackForward(playerManager) {
  return new THREE.Vector3(1, 0, 0).applyQuaternion(playerManager.carGroup.quaternion).normalize();
}

function trackRight(playerManager) {
  return new THREE.Vector3(0, 0, 1).applyQuaternion(playerManager.carGroup.quaternion).normalize();
}

function trackUp(playerManager) {
  return new THREE.Vector3(0, 1, 0).applyQuaternion(playerManager.carGroup.quaternion).normalize();
}

function rotateAroundAxis(vector, axis, degrees) {
  return vector.clone().applyAxisAngle(axis, THREE.MathUtils.degToRad(Number(degrees) || 0));
}

function projectHorizontal(vector, fallback = new THREE.Vector3(1, 0, 0)) {
  const horizontal = new THREE.Vector3(vector.x, 0, vector.z);
  if (horizontal.lengthSq() < 0.0001) return fallback.clone().normalize();
  return horizontal.normalize();
}


function currentCarRoot(playerManager) {
  return playerManager?.carGroup?.children?.find((child) => String(child?.name || '').endsWith('-car')) || null;
}

function restoreHiddenVisuals(hiddenState) {
  if (!hiddenState?.records) return;
  hiddenState.records.forEach((visible, object) => {
    if (object) object.visible = visible;
  });
  hiddenState.records.clear();
  hiddenState.activeRoot = null;
}

function syncWatchedCarVisibility(playerManager, hide, hiddenState) {
  if (!hiddenState) return;
  const root = currentCarRoot(playerManager);

  if (!hide || !root) {
    restoreHiddenVisuals(hiddenState);
    return;
  }

  if (hiddenState.activeRoot && hiddenState.activeRoot !== root) {
    restoreHiddenVisuals(hiddenState);
  }

  hiddenState.activeRoot = root;
  if (!hiddenState.records.has(root)) {
    hiddenState.records.set(root, root.visible);
  }
  root.visible = false;
}

function povProfileForCard(card) {
  const raw = normalizeName(card?.car_family || card?.car_name || card?.loadout?.car || 'octane');
  if (raw.includes('dominus') || raw.includes('plank')) return { forward: 78, up: 58, side: 0 };
  if (raw.includes('merc')) return { forward: 66, up: 84, side: 0 };
  if (raw.includes('fennec')) return { forward: 70, up: 72, side: 0 };
  if (raw.includes('breakout')) return { forward: 82, up: 62, side: 0 };
  if (raw.includes('hybrid')) return { forward: 74, up: 66, side: 0 };
  return { forward: 72, up: 68, side: 0 };
}

function classifyZone(ballPos) {
  if (Math.abs(ballPos.z) > 4550) return 'goal-mouth';
  if (Math.abs(ballPos.z) > 3900 && Math.abs(ballPos.x) < 1400) return 'goal-box';
  if (Math.abs(ballPos.x) > 3000 && Math.abs(ballPos.z) > 3000) return 'corner';
  if (Math.abs(ballPos.x) > 3350) return 'wall';
  if (Math.abs(ballPos.z) < 1150) return 'midfield';
  return 'half-field';
}

function computePlayMetrics(players, cardsByName, ballPos) {
  let nearest = null;
  let nearestDistance = Infinity;
  const teamCenters = [new THREE.Vector3(), new THREE.Vector3()];
  const teamCounts = [0, 0];
  const allPositions = [];

  players.forEach((player) => {
    const position = player?.carGroup?.position;
    if (!position) return;
    const card = cardsByName.get(normalizeName(player.playerName));
    const team = Number(card?.team || card?.is_orange || 0) === 1 ? 1 : 0;
    teamCenters[team].add(position);
    teamCounts[team] += 1;
    allPositions.push(position);

    const distance = position.distanceTo(ballPos);
    if (distance < nearestDistance) {
      nearest = { player, card, team, distance };
      nearestDistance = distance;
    }
  });

  teamCenters.forEach((center, index) => {
    if (teamCounts[index]) center.multiplyScalar(1 / teamCounts[index]);
  });

  const playCenter = averagePlayerPosition(players);
  const spread = allPositions.reduce((max, position) => Math.max(max, position.distanceTo(playCenter)), 0);
  const pressure = clamp(1 - nearestDistance / 1750, 0, 1);
  const teamAdvantageZ = teamCenters[1].z - teamCenters[0].z;

  return {
    playCenter,
    teamCenters,
    nearest,
    nearestDistance,
    spread,
    pressure,
    teamAdvantageZ,
    zone: classifyZone(ballPos),
  };
}

function readSeriesFrame(series, frame) {
  if (!series) return undefined;
  if (Array.isArray(series)) {
    const index = clamp(frame, 0, series.length - 1);
    return series[index];
  }
  return series[frame] ?? series[String(frame)];
}

function parseBallCamValue(value) {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value > 0;
  if (typeof value === 'string') {
    const lower = value.toLowerCase();
    if (lower === 'true' || lower === '1' || lower === 'ball') return true;
    if (lower === 'false' || lower === '0' || lower === 'car') return false;
  }
  if (typeof value === 'object') {
    if ('ball_cam' in value) return parseBallCamValue(value.ball_cam);
    if ('ballCam' in value) return parseBallCamValue(value.ballCam);
    if ('bUsingSecondaryCamera' in value) return parseBallCamValue(value.bUsingSecondaryCamera);
  }
  return undefined;
}

function getFrameBallCam(payload, playerCard, frame, forcedMode) {
  if (forcedMode === 'always-ball') return true;
  if (forcedMode === 'car') return false;
  if (forcedMode === 'driver') return false;

  const playerId = playerCard?.player_id || playerCard?.id;
  const candidates = [
    payload?.hud?.ball_cam_by_player?.[playerId],
    payload?.hud?.ballCamByPlayer?.[playerId],
    payload?.replayData?.ball_cam_by_player?.[playerId],
    payload?.replayData?.ballCamByPlayer?.[playerId],
    payload?.inputs?.ball_cam_by_player?.[playerId],
    payload?.inputs?.ballCamByPlayer?.[playerId],
  ];

  for (const series of candidates) {
    const parsed = parseBallCamValue(readSeriesFrame(series, frame));
    if (parsed !== undefined) return parsed;
  }

  // Some parsers store per-frame objects in player-index order.
  const order = payload?.hud?.player_order || [];
  const playerIndex = Math.max(0, order.indexOf(playerId));
  const frameInputs = payload?.replayData?.player_inputs?.[playerIndex] || payload?.replayData?.inputs?.[playerIndex];
  const parsed = parseBallCamValue(readSeriesFrame(frameInputs, frame));
  if (parsed !== undefined) return parsed;

  // Rocket League POV is normally ball cam in replays unless player input says otherwise.
  return true;
}

function makeDesired(position, target, fov, label, options = {}) {
  return {
    position: clampCameraPosition(position),
    target,
    fov: clamp(fov, 34, 130),
    label,
    fovIsHorizontal: Boolean(options.fovIsHorizontal),
    hideSelectedCar: Boolean(options.hideSelectedCar),
    phaseId: options.phaseId || '',
    phaseLabel: options.phaseLabel || '',
    followPreset: options.followPreset || null,
  };
}

function scoreCandidate(candidate, points, previousPosition) {
  const forward = candidate.target.clone().sub(candidate.position).normalize();
  let maxAngle = 0;
  let weightedAngle = 0;
  points.forEach(({ point, weight }) => {
    const toPoint = point.clone().sub(candidate.position).normalize();
    const angle = forward.angleTo(toPoint);
    maxAngle = Math.max(maxAngle, angle);
    weightedAngle += angle * weight;
  });

  const fovRadians = THREE.MathUtils.degToRad(candidate.fov * 0.72);
  const fitPenalty = Math.max(0, maxAngle - fovRadians) * 2500;
  const movementPenalty = previousPosition ? candidate.position.distanceTo(previousPosition) * 0.08 : 0;
  const wallPenalty = Math.max(0, Math.abs(candidate.position.x) - 3600) * 0.4 + Math.max(0, Math.abs(candidate.position.z) - 5200) * 0.4;
  const heightPenalty = candidate.position.y < 420 ? 420 - candidate.position.y : 0;
  return weightedAngle * 450 + fitPenalty + movementPenalty + wallPenalty + heightPenalty + (candidate.penalty || 0);
}

const PHASE_HOLD_FRAMES = {
  kickoff: 126,
  aerial: 34,
  wall: 32,
  corner: 34,
  'box-pressure': 38,
  attack: 32,
  defense: 32,
  transition: 28,
  neutral: 26,
};

const FOLLOW_PRESETS = {
  kickoff: { posRate: 3.4, targetRate: 3.9, fovRate: 3.4 },
  aerial: { posRate: 4.8, targetRate: 5.6, fovRate: 4.2 },
  wall: { posRate: 4.4, targetRate: 5.1, fovRate: 4.0 },
  corner: { posRate: 4.2, targetRate: 4.9, fovRate: 3.9 },
  'box-pressure': { posRate: 4.8, targetRate: 5.4, fovRate: 4.1 },
  attack: { posRate: 4.0, targetRate: 4.8, fovRate: 3.8 },
  defense: { posRate: 3.9, targetRate: 4.6, fovRate: 3.7 },
  transition: { posRate: 4.2, targetRate: 5.0, fovRate: 3.9 },
  neutral: { posRate: 3.6, targetRate: 4.2, fovRate: 3.4 },
  goal: { posRate: 3.8, targetRate: 4.6, fovRate: 3.7 },
};

function inferAttackSign(ballPos, ballVelocity, metrics, cameraState) {
  if (Math.abs(ballVelocity.z) > 540) return Math.sign(ballVelocity.z) || cameraState?.attackSign || 1;
  if (Math.abs(metrics.teamAdvantageZ) > 320) return metrics.teamAdvantageZ > 0 ? 1 : -1;
  if (Math.abs(ballPos.z) > 1800) return ballPos.z > 0 ? 1 : -1;
  return cameraState?.attackSign || 1;
}

function buildReplayPhaseCandidates(ballPos, ballVelocity, metrics, cameraState) {
  const speed = ballVelocity.length();
  const horizontalSpeed = Math.hypot(ballVelocity.x, ballVelocity.z);
  const attackSign = inferAttackSign(ballPos, ballVelocity, metrics, cameraState);
  const railSide = cameraState?.railSide || (ballPos.x >= 0 ? -1 : 1);
  const absX = Math.abs(ballPos.x);
  const absZ = Math.abs(ballPos.z);
  const centerDistance = Math.hypot(ballPos.x * 0.82, ballPos.z);
  const depth = attackSign * ballPos.z;
  const pressure = metrics.pressure;
  const wallScore = clamp01((absX - 2620) / 820);
  const cornerScore = clamp01(Math.min(absX - 2500, absZ - 3000) / 720);
  const boxScore = clamp01((absZ - 3720) / 780);
  const attackScore = clamp01((depth - 1500) / 1900);
  const defenseScore = clamp01((-depth - 1500) / 1900);
  const midfieldScore = 1 - clamp01(absZ / 2500);
  const heightScore = clamp01((ballPos.y - 460) / 900);
  const kickoffLike = centerDistance < 420 && speed < 920 && metrics.spread > 1400;
  const transitionScore = clamp01(horizontalSpeed / 3000) * 0.4 + midfieldScore * 0.18;

  return [
    {
      id: 'kickoff',
      label: 'kickoff reset',
      score: kickoffLike ? 1.45 : 0,
      forced: kickoffLike,
      attackSign,
      railSide,
    },
    {
      id: 'aerial',
      label: 'aerial contest',
      score: heightScore * 0.95 + clamp01(ballPos.y / 1700) * 0.18 + (metrics.zone === 'wall' ? 0.06 : 0),
      attackSign,
      railSide,
    },
    {
      id: 'wall',
      label: 'wall play',
      score: wallScore * 0.82 + (metrics.zone === 'wall' ? 0.22 : 0) + clamp01(ballPos.y / 780) * 0.08,
      attackSign,
      railSide: ballPos.x >= 0 ? -1 : 1,
    },
    {
      id: 'corner',
      label: 'corner pressure',
      score: cornerScore * 0.9 + (metrics.zone === 'corner' ? 0.22 : 0) + pressure * 0.14,
      attackSign,
      railSide: ballPos.x >= 0 ? -1 : 1,
    },
    {
      id: 'box-pressure',
      label: 'box pressure',
      score: boxScore * 0.84 + pressure * 0.24 + (metrics.zone === 'goal-mouth' || metrics.zone === 'goal-box' ? 0.24 : 0) + attackScore * 0.14,
      attackSign,
      railSide,
    },
    {
      id: 'attack',
      label: 'attacking third',
      score: attackScore * 0.74 + pressure * 0.12 + clamp01(horizontalSpeed / 2600) * 0.1,
      attackSign,
      railSide,
    },
    {
      id: 'defense',
      label: 'defensive third',
      score: defenseScore * 0.74 + pressure * 0.08,
      attackSign,
      railSide,
    },
    {
      id: 'transition',
      label: 'transition',
      score: transitionScore,
      attackSign,
      railSide,
    },
    {
      id: 'neutral',
      label: 'midfield shape',
      score: 0.34 + midfieldScore * 0.5 + (1 - pressure) * 0.1,
      attackSign,
      railSide,
    },
  ];
}

function selectReplayPhase(ballPos, ballVelocity, metrics, cameraState, frame) {
  const candidates = buildReplayPhaseCandidates(ballPos, ballVelocity, metrics, cameraState);
  const best = candidates.reduce((winner, candidate) => (!winner || candidate.score > winner.score ? candidate : winner), null);
  const current = cameraState.phaseInfo;

  if (!current) {
    const initial = { ...best, sinceFrame: frame };
    cameraState.phaseInfo = initial;
    return initial;
  }

  const currentCandidate = candidates.find((candidate) => candidate.id === current.id) || current;
  const holdFrames = PHASE_HOLD_FRAMES[current.id] || 12;
  const age = frame - (current.sinceFrame ?? frame);
  const switchMargin = best.forced
    ? 0
    : current.id === 'kickoff'
      ? 0.42
      : current.id === best.id
        ? 0
        : 0.34;
  const shouldSwitch = best.forced || current.forced !== best.forced || age >= holdFrames || best.score > (currentCandidate.score ?? current.score ?? 0) + switchMargin;

  if (!shouldSwitch) {
    const stable = { ...current, ...currentCandidate, sinceFrame: current.sinceFrame ?? frame };
    cameraState.phaseInfo = stable;
    return stable;
  }

  const next = { ...best, sinceFrame: frame };
  cameraState.phaseInfo = next;
  return next;
}

function buildFocusTarget(ballPos, ballVelocity, metrics, directorMode, aggression) {
  const nearestPos = metrics.nearest?.player?.carGroup?.position || metrics.playCenter;
  const focus = ballPos.clone()
    .lerp(nearestPos, directorMode ? 0.16 + metrics.pressure * 0.14 : 0.1 + metrics.pressure * 0.08)
    .lerp(metrics.playCenter, directorMode ? 0.08 : 0.16);

  const lead = ballVelocity.clone().multiplyScalar(directorMode ? 0.11 + aggression * 0.04 : 0.08 + aggression * 0.02);
  lead.x = clamp(lead.x, -520, 520);
  lead.y = clamp(lead.y, -120, 240);
  lead.z = clamp(lead.z, -760, 760);

  const target = focus.clone().add(lead);
  target.x = clamp(target.x, -FIELD.x + 380, FIELD.x - 380);
  target.z = clamp(target.z, -FIELD.z + 420, FIELD.z - 420);
  target.y = clamp(target.y + 95 + metrics.pressure * 80, 70, 900);

  return { focus, target, nearestPos };
}

function chooseCandidate(candidates, points, cameraState, phase) {
  let best = candidates[0];
  let bestScore = Infinity;
  const previousPosition = cameraState?.lastScorePosition || cameraState?.lastDesiredPosition || null;
  candidates.forEach((candidate) => {
    candidate.position = clampCameraPosition(candidate.position);
    const score = scoreCandidate(candidate, points, previousPosition);
    if (score < bestScore) {
      best = candidate;
      bestScore = score;
    }
  });
  cameraState.lastScorePosition = best.position.clone();
  return makeDesired(best.position, best.target, best.fov, best.label, {
    phaseId: phase.id,
    phaseLabel: phase.label,
    followPreset: FOLLOW_PRESETS[phase.id] || FOLLOW_PRESETS.neutral,
  });
}

function usesPhaseCandidateCamera(phaseId) {
  return ['kickoff', 'aerial', 'box-pressure'].includes(String(phaseId || ''));
}

function usesSafetySpectatorCamera(phaseId) {
  return ['wall', 'corner'].includes(String(phaseId || ''));
}

function buildSafetySpectatorCamera(ballPos, ballVelocity, metrics, cameraState, phase, directorMode) {
  const attackSign = phase.attackSign || cameraState.attackSign || inferAttackSign(ballPos, ballVelocity, metrics, cameraState);
  const sideBias = ballPos.x >= 0 ? 1 : -1;
  const focus = metrics.playCenter.clone().lerp(ballPos, 0.74);
  const target = ballPos.clone().lerp(metrics.playCenter, 0.16);
  target.x = clamp(target.x, -2000, 2000);
  target.y = clamp(ballPos.y + 118, 95, 860);
  target.z = clamp(target.z, -FIELD.z + 240, FIELD.z - 240);

  const position = new THREE.Vector3(
    clamp(focus.x * 0.26 - sideBias * 520, -2100, 2100),
    phase.id === 'corner' ? 1740 : 1560,
    clamp(focus.z - attackSign * (980 + (directorMode ? 140 : 60)), -3300, 3300),
  );

  return makeDesired(position, target, directorMode ? 54 : 56, directorMode ? 'director safety' : 'review safety', {
    phaseId: phase.id,
    phaseLabel: `${phase.label} safety`,
    followPreset: {
      posRate: directorMode ? 6.8 : 7.4,
      targetRate: directorMode ? 7.6 : 8.2,
      fovRate: 4.8,
    },
  });
}

function buildPhaseCamera(ballPos, ballVelocity, metrics, cameraState, settings, phase, directorMode) {
  const aggression = clamp(settings?.directorAggression ?? 0.55, 0, 1);
  const speed = ballVelocity.length();
  const speedFactor = clamp01(speed / 3300);
  const attackSign = phase.attackSign || cameraState?.attackSign || 1;
  const railSide = phase.railSide || cameraState?.railSide || 1;
  const oppositeRail = railSide * -1;
  const { focus, target, nearestPos } = buildFocusTarget(ballPos, ballVelocity, metrics, directorMode, aggression);
  const points = [
    { point: ballPos, weight: 1.8 },
    { point: nearestPos, weight: 1.0 },
    { point: metrics.playCenter, weight: 0.8 },
    { point: metrics.teamCenters[0], weight: 0.35 },
    { point: metrics.teamCenters[1], weight: 0.35 },
  ];

  const candidates = [];

  if (phase.id === 'kickoff') {
    candidates.push(
      {
        label: 'kickoff broadcast',
        position: new THREE.Vector3(railSide * 1760, 1020, -attackSign * 3240),
        target: new THREE.Vector3(0, 145, 0),
        fov: 66,
        penalty: 0,
      },
      {
        label: 'kickoff tactical',
        position: new THREE.Vector3(0, 1760, -attackSign * 2640),
        target: new THREE.Vector3(0, 135, 0),
        fov: 62,
        penalty: 20,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'aerial') {
    candidates.push(
      {
        label: 'aerial glide',
        position: new THREE.Vector3(focus.x + railSide * 520, clamp(ballPos.y + 860, 980, 2100), focus.z - attackSign * (1220 + speedFactor * 240)),
        target,
        fov: 58 + speedFactor * 4,
        penalty: 0,
      },
      {
        label: 'aerial wide',
        position: new THREE.Vector3(focus.x + oppositeRail * 420, clamp(ballPos.y + 1180, 1240, 2400), focus.z - attackSign * (1680 + speedFactor * 280)),
        target,
        fov: 60,
        penalty: 60,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'wall') {
    const wallRail = ballPos.x >= 0 ? -1 : 1;
    candidates.push(
      {
        label: 'wall rail',
        position: new THREE.Vector3(focus.x + wallRail * 1080, 980 + speedFactor * 140, focus.z - attackSign * 1450),
        target,
        fov: 57,
        penalty: 0,
      },
      {
        label: 'wall high cut',
        position: new THREE.Vector3(focus.x + wallRail * 540, 1480 + speedFactor * 180, focus.z - attackSign * 1860),
        target,
        fov: 59,
        penalty: 55,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'corner') {
    const cornerSide = ballPos.x >= 0 ? -1 : 1;
    candidates.push(
      {
        label: 'corner shoulder',
        position: new THREE.Vector3(focus.x + cornerSide * 1020, 940 + speedFactor * 160, focus.z - attackSign * 1180),
        target,
        fov: 57,
        penalty: 0,
      },
      {
        label: 'corner loft',
        position: new THREE.Vector3(focus.x + cornerSide * 620, 1520 + speedFactor * 180, focus.z - attackSign * 1720),
        target,
        fov: 59,
        penalty: 30,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'box-pressure') {
    const goalZ = attackSign > 0 ? -4980 : 4980;
    candidates.push(
      {
        label: 'pressure endline',
        position: new THREE.Vector3(clamp(focus.x * 0.45 + railSide * 320, -1700, 1700), 910 + metrics.pressure * 180, goalZ),
        target,
        fov: 58 + metrics.pressure * 2,
        penalty: 0,
      },
      {
        label: 'pressure rail',
        position: new THREE.Vector3(focus.x + railSide * 760, 840 + metrics.pressure * 120, focus.z - attackSign * 1680),
        target,
        fov: 56,
        penalty: 35,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'attack') {
    candidates.push(
      {
        label: directorMode ? 'attack director rail' : 'attack rail',
        position: new THREE.Vector3(focus.x + railSide * (760 + speedFactor * 170), 760 + speedFactor * 120, focus.z - attackSign * (1480 + aggression * 120)),
        target,
        fov: 55 + speedFactor * 3,
        penalty: 0,
      },
      {
        label: 'attack wide',
        position: new THREE.Vector3(focus.x + oppositeRail * (920 + speedFactor * 140), 890 + speedFactor * 120, focus.z - attackSign * 1780),
        target,
        fov: 57,
        penalty: 60,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'defense') {
    candidates.push(
      {
        label: 'defense rail',
        position: new THREE.Vector3(focus.x + railSide * 700, 780 + speedFactor * 110, focus.z - attackSign * 1760),
        target,
        fov: 56,
        penalty: 0,
      },
      {
        label: 'defense deep read',
        position: new THREE.Vector3(focus.x * 0.52 + railSide * 300, 980, attackSign > 0 ? 5050 : -5050),
        target,
        fov: 58,
        penalty: 25,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  if (phase.id === 'transition') {
    candidates.push(
      {
        label: 'transition rail',
        position: new THREE.Vector3(focus.x + railSide * 820, 820 + speedFactor * 150, focus.z - attackSign * 1680),
        target,
        fov: 56,
        penalty: 0,
      },
      {
        label: 'transition high',
        position: new THREE.Vector3(focus.x + railSide * 420, 1320 + speedFactor * 180, focus.z - attackSign * 2060),
        target,
        fov: 58,
        penalty: 40,
      },
    );
    return chooseCandidate(candidates, points, cameraState, phase);
  }

  const baseHeight = 700 + speedFactor * 120 + metrics.spread * 0.02;
  candidates.push(
    {
      label: directorMode ? 'director rail' : 'review rail',
      position: new THREE.Vector3(focus.x + railSide * (760 + speedFactor * 180), baseHeight, focus.z - attackSign * (1620 + aggression * 120)),
      target,
      fov: 55 + speedFactor * 3,
      penalty: 0,
    },
    {
      label: 'review high',
      position: new THREE.Vector3(focus.x + oppositeRail * 460, baseHeight + 420, focus.z - attackSign * 1980),
      target,
      fov: 57,
      penalty: 65,
    },
  );
  return chooseCandidate(candidates, points, cameraState, phase);
}

function buildStableReviewCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame, directorMode) {
  const phase = selectReplayPhase(ballPos, ballVelocity, metrics, cameraState, frame);
  if (usesSafetySpectatorCamera(phase.id)) {
    return buildSafetySpectatorCamera(ballPos, ballVelocity, metrics, cameraState, phase, directorMode);
  }
  if (usesPhaseCandidateCamera(phase.id)) {
    return buildPhaseCamera(ballPos, ballVelocity, metrics, cameraState, settings, phase, directorMode);
  }
  const attackSign = phase.attackSign || cameraState.attackSign || inferAttackSign(ballPos, ballVelocity, metrics, cameraState);
  const lateralBias = ballPos.x * 0.32 + metrics.playCenter.x * 0.68;
  if (Math.abs(lateralBias) > 220) {
    cameraState.railSide = lateralBias >= 0 ? -1 : 1;
  }
  const railSide = cameraState.railSide || (lateralBias >= 0 ? -1 : 1);
  const speed = ballVelocity.length();
  const spread = clamp(metrics.spread, 650, 2600);
  const spreadFactor = clamp01((spread - 650) / 1750);
  const speedFactor = clamp01(speed / 3200);
  const pressure = clamp01(metrics.pressure);
  const aerialLift = phase.id === 'aerial' ? 220 : phase.id === 'wall' || phase.id === 'corner' ? 120 : 0;

  const focus = metrics.playCenter.clone().lerp(ballPos, 0.62);
  const lookAhead = ballVelocity.clone().multiplyScalar(0.12 + speedFactor * 0.06);
  lookAhead.y *= 0.16;
  const target = focus.clone().add(lookAhead);
  target.x = clamp(target.x, -2400, 2400);
  target.y = clamp(84 + ballPos.y * 0.28 + pressure * 36 + aerialLift * 0.15, 78, 820);
  target.z = clamp(target.z, -FIELD.z + 220, FIELD.z - 220);

  const sideOffset = 1320 + spreadFactor * 380 + speedFactor * 160;
  const backOffset = 1160 + spreadFactor * 440 + speedFactor * 180 + (directorMode ? 120 : 0);
  const height = 440 + spreadFactor * 240 + pressure * 40 + clamp(ballPos.y * 0.18, 0, 190) + aerialLift + (directorMode ? 40 : 0);
  const position = new THREE.Vector3(
    clamp(focus.x + railSide * sideOffset, -3600, 3600),
    height,
    clamp(focus.z - attackSign * backOffset, -5300, 5300),
  );

  return makeDesired(
    position,
    target,
    47 + spreadFactor * 7 + speedFactor * 2 + (directorMode ? 1.2 : 0),
    directorMode ? 'broadcast director' : 'review broadcast',
    {
      phaseId: phase.id,
      phaseLabel: phase.label,
      followPreset: {
        posRate: directorMode ? 5.8 : 6.8,
        targetRate: directorMode ? 6.9 : 8.1,
        fovRate: 5.8,
      },
    },
  );
}

function buildSidelineReviewCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame) {
  const phase = selectReplayPhase(ballPos, ballVelocity, metrics, cameraState, frame);
  if (usesSafetySpectatorCamera(phase.id)) {
    return buildSafetySpectatorCamera(ballPos, ballVelocity, metrics, cameraState, phase, false);
  }
  if (usesPhaseCandidateCamera(phase.id)) {
    return buildPhaseCamera(ballPos, ballVelocity, metrics, cameraState, settings, phase, false);
  }
  const attackSign = phase.attackSign || cameraState.attackSign || inferAttackSign(ballPos, ballVelocity, metrics, cameraState);
  const xBias = ballPos.x * 0.7 + metrics.playCenter.x * 0.3;
  if (xBias > 1250) cameraState.railSide = -1;
  else if (xBias < -1250) cameraState.railSide = 1;
  const railSide = cameraState.railSide || (xBias >= 0 ? -1 : 1);
  const speed = ballVelocity.length();
  const spread = clamp(metrics.spread, 700, 2550);
  const speedFactor = clamp01(speed / 3200);
  const spreadFactor = clamp01((spread - 700) / 1850);
  const pressure = clamp01(metrics.pressure);
  const lift = phase.id === 'aerial' ? 140 : phase.id === 'wall' || phase.id === 'corner' ? 72 : 0;

  const anchor = metrics.playCenter.clone().lerp(ballPos, 0.72);
  const lead = ballVelocity.clone().multiplyScalar(0.085 + speedFactor * 0.035);
  lead.y *= 0.1;

  const target = anchor.clone().add(lead);
  target.x = clamp(target.x, -2100, 2100);
  target.y = clamp(96 + ballPos.y * 0.18 + pressure * 32 + lift * 0.1, 86, 620);
  target.z = clamp(target.z, -FIELD.z + 200, FIELD.z - 200);

  const lateralOffset = 1840 + spreadFactor * 320 + speedFactor * 120;
  const trailOffset = 1080 + speedFactor * 150 + pressure * 50;
  const height = 430 + spreadFactor * 110 + pressure * 32 + clamp(ballPos.y * 0.14, 0, 140) + lift;
  const position = new THREE.Vector3(
    clamp(anchor.x + railSide * lateralOffset, -3520, 3520),
    clamp(height, 340, 1120),
    clamp(anchor.z - attackSign * trailOffset, -5300, 5300),
  );

  return makeDesired(position, target, 51 + spreadFactor * 4.5 + speedFactor * 2.2, 'review sideline', {
    phaseId: phase.id,
    phaseLabel: phase.label,
    followPreset: {
      posRate: 4.2,
      targetRate: 5.1,
      fovRate: 3.9,
    },
  });
}

function buildReplayCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame) {
  return buildSidelineReviewCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame);
}

function buildDirectorCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame) {
  return buildStableReviewCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame, true);
}



function goalWindow(goalFrames, frame) {
  if (!goalFrames || !goalFrames.size) return null;
  for (const goalFrame of goalFrames) {
    const offset = frame - Number(goalFrame || 0);
    if (offset >= 10 && offset <= 150) {
      return { goalFrame: Number(goalFrame || 0), offset };
    }
  }
  return null;
}

function buildGoalReplayCamera(ballPos, ballVelocity, metrics, cameraState, goalInfo) {
  const attackSign = ballPos.z >= 0 ? 1 : -1;
  const side = ballPos.x >= 0 ? -1 : 1;
  const t = clamp((goalInfo?.offset || 0) / 150, 0, 1);
  const orbit = Math.sin(t * Math.PI * 1.1) * 220;
  const focus = ballPos.clone().lerp(metrics.playCenter, 0.12);
  const goalZ = attackSign > 0 ? FIELD.goalZ : -FIELD.goalZ;
  const target = new THREE.Vector3(
    clamp(focus.x * 0.48, -1350, 1350),
    clamp(ballPos.y + 135, 110, 760),
    clamp(goalZ - attackSign * 420, -FIELD.z, FIELD.z),
  );
  const position = new THREE.Vector3(
    clamp(focus.x + side * (860 + orbit), -3400, 3400),
    760 + t * 180 + clamp(ballPos.y * 0.06, 0, 120),
    clamp(goalZ - attackSign * (1780 + t * 180), -5400, 5400),
  );
  return makeDesired(position, target, 54 + t * 2, 'goal replay', {
    phaseId: 'goal',
    phaseLabel: 'goal replay',
    followPreset: FOLLOW_PRESETS.goal,
  });
}

function buildEndlineCamera(ballPos, metrics, orangeSide) {
  const focus = ballPos.clone().lerp(metrics.playCenter, 0.35);
  const position = focus.clone().add(new THREE.Vector3(0, 520, orangeSide ? 2250 : -2250));
  const target = ballPos.clone().add(new THREE.Vector3(0, 70, 0));
  return makeDesired(position, target, 60, orangeSide ? 'orange endline' : 'blue endline');
}

function buildTacticalCamera(ballPos, metrics) {
  const focus = ballPos.clone().lerp(metrics.playCenter, 0.28);
  const height = metrics.zone === 'corner' || metrics.zone === 'goal-box' || metrics.zone === 'goal-mouth' ? 2600 : 2250;
  const position = focus.clone().add(new THREE.Vector3(0, height, 220));
  return makeDesired(position, focus.clone(), 52, 'tactical overhead');
}

function buildRecoveryCamera(ballPos, ballVelocity, metrics, cameraState) {
  const attackSign = cameraState.attackSign || inferAttackSign(ballPos, ballVelocity, metrics, cameraState);
  const railSide = cameraState.railSide || (ballPos.x >= 0 ? -1 : 1);
  const anchor = metrics.playCenter.clone().lerp(ballPos, 0.7);
  const target = ballPos.clone().lerp(metrics.playCenter, 0.2);
  target.x = clamp(target.x, -2200, 2200);
  target.y = clamp(ballPos.y + 108, 90, 780);
  target.z = clamp(target.z, -FIELD.z + 220, FIELD.z - 220);

  const position = new THREE.Vector3(
    clamp(anchor.x + railSide * 1520, -3380, 3380),
    clamp(780 + ballPos.y * 0.1, 720, 1200),
    clamp(anchor.z - attackSign * 1320, -4700, 4700),
  );

  return makeDesired(position, target, 56, 'camera recovery', {
    phaseId: 'recovery',
    phaseLabel: 'camera recovery',
    followPreset: {
      posRate: 7.6,
      targetRate: 8.4,
      fovRate: 4.8,
    },
  });
}

function buildForwardPovCamera(playerManager, playerCard, payload) {
  const cameraSettings = normalizeCameraSettings(playerCard, payload);
  const forward = trackForward(playerManager);
  const right = trackRight(playerManager);
  const up = trackUp(playerManager);
  const carPos = playerManager.carGroup.position.clone();
  const profile = povProfileForCard(playerCard);

  const eye = carPos.clone()
    .add(up.clone().multiplyScalar(profile.up))
    .add(forward.clone().multiplyScalar(profile.forward))
    .add(right.clone().multiplyScalar(profile.side));

  const lookDir = rotateAroundAxis(forward, right, cameraSettings.pitch * 0.35).normalize();
  const target = eye.clone().add(lookDir.multiplyScalar(1800));

  return makeDesired(eye, target, cameraSettings.fieldOfView, 'forward first-person POV', {
    fovIsHorizontal: true,
    hideSelectedCar: true,
  });
}

function buildTruePlayerPovCamera(ballPos, playerManager, playerCard, payload, frame, settings) {
  const cameraSettings = normalizeCameraSettings(playerCard, payload);
  const forcedLookMode = settings?.playerLookMode || 'replay';
  const useBallCam = getFrameBallCam(payload, playerCard, frame, forcedLookMode);

  const forward = trackForward(playerManager);
  const right = trackRight(playerManager);
  const up = trackUp(playerManager);
  const carPos = playerManager.carGroup.position.clone();
  const profile = povProfileForCard(playerCard);

  // True POV: put the camera inside/just above the windshield area, not behind the car.
  // We still use the replay camera setting FOV, and we use pitch/ball-cam state to decide what the player was looking at.
  const eye = carPos.clone()
    .add(up.clone().multiplyScalar(profile.up))
    .add(forward.clone().multiplyScalar(profile.forward))
    .add(right.clone().multiplyScalar(profile.side));

  const carLookDir = rotateAroundAxis(forward, right, cameraSettings.pitch * 0.35).normalize();
  const carTarget = eye.clone().add(carLookDir.multiplyScalar(1800));
  const ballTarget = ballPos.clone().add(new THREE.Vector3(0, 76, 0));

  let target;
  let label;

  if (useBallCam) {
    // Ball-cam POV: from the player's eye point, look where their camera would be trying to look.
    // Transition/swivel tune how hard we bias toward the ball instead of snapping unrealistically.
    const transitionWeight = clamp(0.78 + (cameraSettings.transitionSpeed / 2.5) * 0.18, 0.78, 0.96);
    const swivelAssist = clamp((cameraSettings.swivelSpeed / 10) * 0.08, 0.02, 0.08);
    target = carTarget.lerp(ballTarget, clamp(transitionWeight + swivelAssist, 0.82, 0.98));
    label = 'true POV ball-cam';
  } else {
    target = carTarget;
    label = 'true POV car-cam';
  }

  target.y = clamp(target.y, 20, 1450);

  return makeDesired(eye, target, cameraSettings.fieldOfView, label, {
    fovIsHorizontal: true,
    hideSelectedCar: true,
  });
}

function buildPlayerChaseCamera(ballPos, playerManager, playerCard, payload, frame, settings) {
  const cameraSettings = normalizeCameraSettings(playerCard, payload);
  const forcedLookMode = settings?.playerLookMode || 'replay';
  const useBallCam = getFrameBallCam(payload, playerCard, frame, forcedLookMode);

  const forward = trackForward(playerManager);
  const right = trackRight(playerManager);
  const up = trackUp(playerManager);
  const carPos = playerManager.carGroup.position.clone();

  const followAnchor = carPos.clone().add(up.clone().multiplyScalar(cameraSettings.height));
  const boomDir = projectHorizontal(forward);
  const stiffnessDistanceScale = 1 + (1 - cameraSettings.stiffness) * 0.10;
  const position = followAnchor.clone().sub(boomDir.clone().multiplyScalar(cameraSettings.distance * stiffnessDistanceScale));

  const pitchDir = rotateAroundAxis(forward, right, cameraSettings.pitch).normalize();
  const carTarget = followAnchor.clone().add(pitchDir.multiplyScalar(1400));
  const ballTarget = ballPos.clone().add(new THREE.Vector3(0, 68, 0));

  let target;
  let label;
  if (useBallCam) {
    const ballAim = ballTarget.clone().sub(followAnchor).normalize();
    const angleToBall = forward.angleTo(ballAim);
    const behindFactor = clamp(angleToBall / Math.PI, 0, 1);
    const swivelWeight = clamp((cameraSettings.swivelSpeed / 10) * 0.45 + (cameraSettings.transitionSpeed / 2.5) * 0.16, 0.18, 0.65);
    const ballWeight = clamp(0.46 + swivelWeight - behindFactor * 0.18, 0.36, 0.88);
    target = carTarget.lerp(ballTarget, ballWeight);
    label = 'player chase ball-cam';
  } else {
    target = carTarget;
    label = 'player chase car-cam';
  }

  target.y = clamp(target.y, 35, 980);
  return makeDesired(position, target, cameraSettings.fieldOfView, label, { fovIsHorizontal: true });
}

function buildAutoPlayerPovCamera(ballPos, metrics, payload, frame, settings, cameraState) {
  if (settings?.autoPlayerPov === false) return null;
  const nearest = metrics?.nearest;
  const active = Number(cameraState?.autoPovUntil || 0) > frame;

  if (!active) {
    if (frame < Number(cameraState?.autoPovCooldownUntil || 0)) return null;
    const centerDistance = Math.hypot(ballPos.x, ballPos.z);
    const inPlayableView = Math.abs(ballPos.x) < 3300 && Math.abs(ballPos.z) < 4700 && ballPos.y < 1250;
    const closeControl = nearest?.player && Number(metrics?.nearestDistance ?? Infinity) < 620 && Number(metrics?.pressure ?? 0) > 0.42;
    if (!closeControl || !inPlayableView || centerDistance < 720) return null;
    cameraState.autoPovUntil = frame + (Number(metrics.pressure ?? 0) > 0.72 ? 96 : 72);
    cameraState.autoPovCooldownUntil = frame + 300;
    cameraState.autoPovPlayerName = normalizeName(nearest.card?.player_name || nearest.player?.playerName);
  }

  if (!nearest?.player || Number(metrics?.nearestDistance ?? Infinity) > 1050) {
    cameraState.autoPovUntil = 0;
    return null;
  }

  const desired = buildPlayerChaseCamera(ballPos, nearest.player, nearest.card, payload, frame, settings);
  desired.label = 'auto player POV';
  desired.phaseId = 'player-pov';
  desired.phaseLabel = nearest.card?.player_name || nearest.player?.playerName || 'player POV';
  desired.followPreset = { posRate: 9.2, targetRate: 11.2, fovRate: 6.0 };
  return desired;
}

function resetState(ballPos, trackedPos, lastBallPos, lastTrackedPos, frame, goalFrames) {
  if (!lastBallPos || !lastTrackedPos) return { resetLike: true, hardReset: true };
  const ballJump = ballPos.distanceTo(lastBallPos);
  const trackedJump = trackedPos.distanceTo(lastTrackedPos);
  const aroundGoal = goalFrames.has(frame) || goalFrames.has(frame - 1) || goalFrames.has(frame - 2) || goalFrames.has(frame - 3);
  const centerReset = Math.hypot(ballPos.x, ballPos.z) < 240 && Math.abs(ballPos.y - 92.75) < 220;
  const kickoffTeleport = centerReset && (ballJump > 900 || trackedJump > 1100);
  const hardReset = aroundGoal || kickoffTeleport || ballJump > 3200 || trackedJump > 2400;
  const resetLike = hardReset || ballJump > 1700 || trackedJump > 1250 || (centerReset && (ballJump > 500 || trackedJump > 700));
  return { resetLike, hardReset };
}

function computeDesiredCamera({ cameraMode, ballPos, ballVelocity, metrics, playerManager, playerCard, payload, frame, goalFrames, cameraState, settings }) {
  if ((cameraMode === 'director' || cameraMode === 'replay') && Number(cameraState?.rescueFrames || 0) > 0) {
    cameraState.rescueFrames = Math.max(0, Number(cameraState.rescueFrames || 0) - 1);
    const rescue = buildRecoveryCamera(ballPos, ballVelocity, metrics, cameraState);
    return rescue;
  }
  const goalInfo = goalWindow(goalFrames, frame);
  if (cameraMode === 'director' && goalInfo) {
    return buildGoalReplayCamera(ballPos, ballVelocity, metrics, cameraState, goalInfo);
  }
  if (cameraMode === 'driver' && playerManager) {
    return buildForwardPovCamera(playerManager, playerCard, payload);
  }
  if (cameraMode === 'player' && playerManager) {
    return buildTruePlayerPovCamera(ballPos, playerManager, playerCard, payload, frame, settings);
  }
  if (cameraMode === 'tactical') return buildTacticalCamera(ballPos, metrics);
  if (cameraMode === 'blue-goal') return buildEndlineCamera(ballPos, metrics, false);
  if (cameraMode === 'orange-goal') return buildEndlineCamera(ballPos, metrics, true);
  if (cameraMode === 'director' || cameraMode === 'replay') {
    const autoPov = buildAutoPlayerPovCamera(ballPos, metrics, payload, frame, settings, cameraState);
    if (autoPov) return autoPov;
  }
  if (cameraMode === 'director') return buildDirectorCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame);
  return buildReplayCamera(ballPos, ballVelocity, metrics, cameraState, settings, frame);
}

export function findPreferredStartFrame(payload) {
  const replayData = payload?.replayData;
  if (!replayData?.frames?.length) return 0;
  const ballFrames = replayData.ball || [];
  const playerSeries = replayData.players || [];
  const hints = payload?.director_hints || payload?.nativeTelemetry?.director_hints || [];
  const limit = Math.min(replayData.frames.length, 1500);
  const minimumOpenFrame = Math.min(limit - 1, 540);
  const firstBall = ballFrames[0] || [0, 0, 92.75];
  let movementFrame = null;
  let releaseFrame = null;

  const resolveOpenFrame = (candidateFrame) => {
    const start = clamp(candidateFrame, 0, limit - 1);
    for (let frame = start; frame < Math.min(limit, start + 420); frame += 1) {
      const ball = ballFrames[frame] || firstBall;
      const centerDistance = Math.hypot(Number(ball[0] || 0), Number(ball[1] || 0));
      const lifted = Number(ball[2] || 0) > 150;
      if (centerDistance > 650 || lifted) {
        return frame;
      }
    }
    return start;
  };

  for (const hint of hints) {
    const type = String(hint?.type || '').toLowerCase();
    const hintFrame = Number.isFinite(Number(hint?.frame))
      ? Number(hint.frame)
      : Number.isFinite(Number(hint?.frameNumber))
        ? Number(hint.frameNumber)
        : Number.isFinite(Number(hint?.t))
          ? Math.round(Number(hint.t) * 60)
          : null;
    if (!Number.isFinite(hintFrame) || hintFrame < 480) continue;
    if (type.includes('kickoff')) continue;
    if (type.includes('pressure') || type.includes('turnover') || type.includes('shot') || type.includes('save') || type.includes('demo') || type.includes('clutch') || type.includes('swing')) {
      return resolveOpenFrame(Math.max(minimumOpenFrame, hintFrame - 96));
    }
  }

  for (let frame = 6; frame < limit; frame += 1) {
    const ball = ballFrames[frame] || firstBall;
    const centerDistance = Math.hypot(Number(ball[0] || 0), Number(ball[1] || 0));
    const ballDistance = Math.hypot(
      Number(ball[0] || 0) - Number(firstBall[0] || 0),
      Number(ball[1] || 0) - Number(firstBall[1] || 0),
      Number(ball[2] || 0) - Number(firstBall[2] || 0),
    );
    const movingPlayers = playerSeries.filter((series) => {
      const start = series?.[0] || [0, 0, 0];
      const current = series?.[frame] || start;
      return Math.hypot(
        Number(current[0] || 0) - Number(start[0] || 0),
        Number(current[1] || 0) - Number(start[1] || 0),
        Number(current[2] || 0) - Number(start[2] || 0),
      ) > 220;
    }).length;

    if (movementFrame == null && (ballDistance > 170 || movingPlayers >= 2)) {
      movementFrame = frame;
    }

    const kickoffCleared = centerDistance > 320 || ballDistance > 260 || Number(ball[2] || 0) > 160 || movingPlayers >= 4;
    const trulyReleased = centerDistance > 820 || ballDistance > 720 || Number(ball[2] || 0) > 240 || movingPlayers >= 5;
    if (kickoffCleared && releaseFrame == null) {
      releaseFrame = frame;
    }
    if (movementFrame != null && trulyReleased) {
      return resolveOpenFrame(Math.max(minimumOpenFrame, frame - 24));
    }
  }
  if (releaseFrame != null) {
    return resolveOpenFrame(Math.max(minimumOpenFrame, releaseFrame + 150));
  }
  return movementFrame != null ? resolveOpenFrame(Math.max(minimumOpenFrame, movementFrame + 240)) : 0;
}

export function useReplayCamera({
  gameManager,
  payload,
  cameraMode,
  selectedPlayerId,
  ready,
  cameraSettings = {},
  onCameraTelemetry,
}) {
  const goalFrames = useMemo(() => frameGoalSet(payload), [payload]);
  const cardsByName = useMemo(() => cardMapByName(payload), [payload]);
  const selectedPlayerCard = useMemo(() => findCardById(payload, selectedPlayerId), [payload, selectedPlayerId]);
  const selectedPlayerName = useMemo(() => normalizeName(selectedPlayerCard?.player_name || selectedPlayerCard?.name), [selectedPlayerCard]);

  useEffect(() => {
    if (!ready || !gameManager || !payload) return undefined;

    const sceneManager = SceneManager.getInstance();
    const cameraManager = CameraManager.getInstance();
    const stockUpdate = cameraManager.update;
    const previousCamera = cameraManager.activeCamera || cameraManager.camera || sceneManager.camera || null;

    if (stockUpdate) removeFrameListener(stockUpdate);

    const camera = new THREE.PerspectiveCamera(62, 16 / 9, 1, 42000);
    camera.name = `native-${cameraMode}-camera`;
    sceneManager.scene.add(camera);
    cameraManager.setActiveCamera(camera);
    dispatchCameraChange({ camera });

    const smoothedPosition = new THREE.Vector3();
    const smoothedTarget = new THREE.Vector3();
    const cameraState = {
      railSide: 1,
      attackSign: 1,
      lastDesiredPosition: null,
      lastDesiredTarget: null,
      lastScorePosition: null,
      phaseInfo: null,
      lastAppliedPhaseId: '',
      resetCooldown: 0,
      rescueFrames: 0,
      stallFrames: 0,
      autoPovUntil: 0,
      autoPovCooldownUntil: 0,
      autoPovPlayerName: '',
    };

    const hiddenVisualState = {
      activeRoot: null,
      records: new Map(),
    };

    let initialized = false;
    let lastBallPos = null;
    let lastTrackedPos = null;
    let cachedPlayerManager = null;
    let lastTelemetryFrame = -999;

    const onFrame = ({ frame, delta }) => {
      const players = sceneManager.players || [];
      const ball = sceneManager.ball?.ball;
      if (!ball || !players.length) return;

      if (!cachedPlayerManager || !players.includes(cachedPlayerManager)) {
        cachedPlayerManager = findPlayerManagerByName(players, selectedPlayerName);
      }

      const playerManager = cachedPlayerManager;
      const ballPos = safeVector(cloneVec(ball.position));
      const ballVelocity = estimateBallVelocity(ballPos, lastBallPos, delta);
      const metrics = computePlayMetrics(players, cardsByName, ballPos);
      const trackedPosition = playerManager?.carGroup?.position?.clone() || metrics.playCenter.clone();

    const xBias = ballPos.x * 0.62 + metrics.playCenter.x * 0.38;
      const railThreshold = cameraMode === 'director' ? 2150 : 1600;
      if (xBias > railThreshold) cameraState.railSide = -1;
      else if (xBias < -railThreshold) cameraState.railSide = 1;

      const velocityThreshold = cameraMode === 'director' ? 1120 : 920;
      const zoneThreshold = cameraMode === 'director' ? 3200 : 2750;
      if (ballVelocity.z > velocityThreshold) cameraState.attackSign = 1;
      else if (ballVelocity.z < -velocityThreshold) cameraState.attackSign = -1;
      else if (Math.abs(ballVelocity.z) < 170) {
        if (ballPos.z > zoneThreshold) cameraState.attackSign = 1;
        else if (ballPos.z < -zoneThreshold) cameraState.attackSign = -1;
      }

      const desired = computeDesiredCamera({
        cameraMode,
        ballPos,
        ballVelocity,
        metrics,
        playerManager,
        playerCard: selectedPlayerCard,
        payload,
        frame,
        goalFrames,
        cameraState,
        settings: cameraSettings,
      });
      const spectatorMode = cameraMode === 'director' || cameraMode === 'replay' || cameraMode === 'tactical' || cameraMode === 'blue-goal' || cameraMode === 'orange-goal';
      const clearance = spectatorMode ? sanitizeSpectatorDesired(desired) : { unsafe: false, shifted: 0 };
      if (spectatorMode && clearance.unsafe && clearance.shifted > 220 && Number(cameraState.rescueFrames || 0) === 0) {
        cameraState.rescueFrames = 10;
      }

      const invalidDesired = !Number.isFinite(desired.position.x) || !Number.isFinite(desired.position.y) || !Number.isFinite(desired.position.z) || !Number.isFinite(desired.target.x) || !Number.isFinite(desired.target.y) || !Number.isFinite(desired.target.z);
      if (invalidDesired) {
        desired.position.copy(new THREE.Vector3(0, 1450, cameraState.attackSign > 0 ? -2500 : 2500));
        desired.target.copy(ballPos.clone().add(new THREE.Vector3(0, 120, 0)));
        desired.label = 'safe reset camera';
      }
      const { resetLike, hardReset: physicsHardReset } = resetState(ballPos, trackedPosition, lastBallPos, lastTrackedPos, frame, goalFrames);
      const phaseChanged = Boolean(cameraState.lastAppliedPhaseId && desired.phaseId && cameraState.lastAppliedPhaseId !== desired.phaseId);
      const hardReset = physicsHardReset || invalidDesired || (initialized && smoothedPosition.distanceTo(desired.position) > 5200);
      cameraState.resetCooldown = hardReset ? 6 : Math.max(0, Number(cameraState.resetCooldown || 0) - 1);
      const frameDelta = delta || 1 / 60;
      const playerLike = cameraMode === 'player' || cameraMode === 'driver';
      const ballTravel = lastBallPos ? ballPos.distanceTo(lastBallPos) : 0;
      const trackedTravel = lastTrackedPos ? trackedPosition.distanceTo(lastTrackedPos) : 0;
      const desiredTravel = cameraState.lastDesiredPosition ? cameraState.lastDesiredPosition.distanceTo(desired.position) : Infinity;
      const targetTravel = cameraState.lastDesiredTarget ? cameraState.lastDesiredTarget.distanceTo(desired.target) : Infinity;
      const stalledView = !playerLike
        && !hardReset
        && Number(cameraState.rescueFrames || 0) === 0
        && ballTravel > 260
        && trackedTravel > 220
        && desiredTravel < 8
        && targetTravel < 12;
      cameraState.stallFrames = stalledView ? Number(cameraState.stallFrames || 0) + 1 : Math.max(0, Number(cameraState.stallFrames || 0) - 2);
      if (cameraState.stallFrames >= 18) {
        cameraState.stallFrames = 0;
        cameraState.rescueFrames = 10;
        cameraState.phaseInfo = null;
        cameraState.lastAppliedPhaseId = '';
        cameraState.lastDesiredPosition = null;
        cameraState.lastDesiredTarget = null;
        cameraState.lastScorePosition = null;
        cameraState.railSide = (cameraState.railSide || 1) * -1;
      }
      const followPreset = desired.followPreset || null;
      const basePosRate = followPreset?.posRate ?? (playerLike ? 24.0 : cameraMode === 'director' ? 3.5 : 4.5);
      const baseTargetRate = followPreset?.targetRate ?? (playerLike ? 22.0 : cameraMode === 'director' ? 4.3 : 5.4);
      const baseFovRate = followPreset?.fovRate ?? (playerLike ? 7.0 : cameraMode === 'director' ? 3.3 : 4.0);
      const positionAlphaBase = hardReset ? 1 : resetLike ? 0.42 : 1 - Math.exp(-frameDelta * basePosRate);
      const targetAlphaBase = hardReset ? 1 : resetLike ? 0.48 : 1 - Math.exp(-frameDelta * baseTargetRate);
      const phaseBoost = phaseChanged && !hardReset && !resetLike && cameraState.resetCooldown === 0 ? (cameraMode === 'director' ? 0.004 : 0.01) : 0;
      const positionAlpha = clamp(positionAlphaBase + phaseBoost, 0, 1);
      const targetAlpha = clamp(targetAlphaBase + phaseBoost * 1.25, 0, 1);
      const fovAlpha = hardReset ? 1 : clamp((1 - Math.exp(-frameDelta * baseFovRate)) + phaseBoost * 0.5, 0, 1);

      if (!initialized || hardReset) {
        smoothedPosition.copy(desired.position);
        smoothedTarget.copy(desired.target);
        initialized = true;
      } else {
        smoothedPosition.lerp(desired.position, positionAlpha);
        smoothedTarget.lerp(desired.target, targetAlpha);
      }

      syncWatchedCarVisibility(playerManager, desired.hideSelectedCar, hiddenVisualState);

      camera.position.copy(smoothedPosition);

      const projection = syncCameraProjection(camera, gameManager, desired);
      if (Math.abs(camera.fov - projection.verticalFov) > 0.08) {
        camera.fov += (projection.verticalFov - camera.fov) * fovAlpha;
        projection.projectionChanged = true;
      }
      if (projection.projectionChanged) {
        camera.updateProjectionMatrix();
      }

      camera.lookAt(smoothedTarget);
      cameraState.lastAppliedPhaseId = desired.phaseId || cameraState.lastAppliedPhaseId;
      cameraState.lastDesiredPosition = desired.position.clone();
      cameraState.lastDesiredTarget = desired.target.clone();

      lastBallPos = ballPos.clone();
      lastTrackedPos = trackedPosition.clone();

      const actualSettings = normalizeCameraSettings(selectedPlayerCard, payload);
      const ballCam = cameraMode !== 'player' ? true : getFrameBallCam(payload, selectedPlayerCard, frame, cameraSettings.playerLookMode);
      const telemetry = {
        mode: cameraMode,
        label: desired.label,
        zone: metrics.zone,
        pressure: metrics.pressure,
        nearestPlayer: metrics.nearest?.card?.player_name || metrics.nearest?.player?.playerName || '',
        nearestDistance: metrics.nearestDistance,
        phase: desired.phaseLabel || desired.phaseId || cameraState.phaseInfo?.label || '',
        fov: camera.fov,
        rawReplayFov: desired.fov,
        aspect: camera.aspect,
        ballCam,
        cameraSettings: actualSettings,
      };

      if (onCameraTelemetry && frame - lastTelemetryFrame >= 4) {
        lastTelemetryFrame = frame;
        onCameraTelemetry(telemetry);
      }

      dispatchCameraFrameUpdate({
        ballPosition: ballPos,
        ballCam,
        isUsingBoost: false,
        activeCamera: camera,
        telemetry,
      });
    };

    addFrameListener(onFrame);

    return () => {
      removeFrameListener(onFrame);
      restoreHiddenVisuals(hiddenVisualState);
      camera.removeFromParent();
      if (previousCamera && previousCamera !== camera) {
        cameraManager.setActiveCamera(previousCamera);
        dispatchCameraChange({ camera: previousCamera });
      }
      if (stockUpdate) addFrameListener(stockUpdate);
    };
  }, [
    cameraMode,
    gameManager,
    goalFrames,
    payload,
    ready,
    selectedPlayerCard,
    selectedPlayerName,
    cardsByName,
    cameraSettings,
    onCameraTelemetry,
  ]);
}
