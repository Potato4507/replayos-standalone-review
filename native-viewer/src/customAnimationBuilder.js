import {
  AnimationClip,
  AnimationMixer,
  Euler,
  Quaternion,
  QuaternionKeyframeTrack,
  Vector3,
  VectorKeyframeTrack,
} from 'three';
import { BALL } from 'replay-viewer/constants/gameObjectNames';
import AnimationManager from 'replay-viewer/managers/AnimationManager';
import {
  getActionClipName,
  getPositionName,
  getRotationName,
} from 'replay-viewer/builders/utils/animationNameGetters';
import { getCarName, getGroupName } from 'replay-viewer/builders/utils/playerNameGetters';

const POSITION_EPSILON_SQ = 0.35 * 0.35;
const QUATERNION_EPSILON = 0.00055;
const MAX_KEYFRAME_GAP = 1 / 8;

function dataToVector(data) {
  const x = Number(data?.[0] || 0);
  const y = Number(data?.[2] || 0);
  const z = Number(data?.[1] || 0);
  return new Vector3(x, y, z);
}

function dataToQuaternion(data) {
  const pitch = -Number(data?.[3] || 0);
  const yaw = -Number(data?.[5] || 0);
  const roll = -Number(data?.[4] || 0);
  return new Quaternion().setFromEuler(new Euler(yaw, roll, pitch, 'YZX')).normalize();
}

function quaternionChanged(left, right) {
  return Math.abs(left.dot(right)) < 1 - QUATERNION_EPSILON;
}

function frameDuration(replayData, index) {
  return Math.max(0, Number(replayData?.frames?.[index]?.[0] || 0));
}

function pushVector(values, vector) {
  vector.toArray(values, values.length);
}

function pushQuaternion(values, quaternion) {
  quaternion.toArray(values, values.length);
}

function generateKeyframeData(replayData, posRotData = [], includeRotation = true) {
  const positions = [];
  const rotations = [];
  const positionTimes = [];
  const rotationTimes = [];
  let totalDuration = 0;
  let prevVector = null;
  let prevQuat = null;
  let lastPositionTime = -Infinity;
  let lastRotationTime = -Infinity;

  const frameCount = Math.max(posRotData.length, replayData?.frames?.length || 0);

  for (let index = 0; index < frameCount; index += 1) {
    const data = posRotData[index] || posRotData[index - 1] || [0, 0, 0, 0, 0, 0];
    const nextVector = dataToVector(data);
    const shouldWritePosition = !prevVector || nextVector.distanceToSquared(prevVector) > POSITION_EPSILON_SQ || totalDuration - lastPositionTime >= MAX_KEYFRAME_GAP;
    if (shouldWritePosition) {
      pushVector(positions, nextVector);
      positionTimes.push(totalDuration);
      prevVector = nextVector;
      lastPositionTime = totalDuration;
    }

    if (includeRotation) {
      const nextQuat = dataToQuaternion(data);
      const shouldWriteRotation = !prevQuat || quaternionChanged(nextQuat, prevQuat) || totalDuration - lastRotationTime >= MAX_KEYFRAME_GAP;
      if (shouldWriteRotation) {
        pushQuaternion(rotations, nextQuat);
        rotationTimes.push(totalDuration);
        prevQuat = nextQuat;
        lastRotationTime = totalDuration;
      }
    }

    totalDuration += frameDuration(replayData, index);
  }

  if (!positionTimes.length) {
    positionTimes.push(0);
    pushVector(positions, new Vector3());
  }
  if (includeRotation && !rotationTimes.length) {
    rotationTimes.push(0);
    pushQuaternion(rotations, new Quaternion());
  }

  return {
    duration: Math.max(totalDuration, positionTimes[positionTimes.length - 1] || 0, rotationTimes[rotationTimes.length - 1] || 0),
    positionTimes,
    positionValues: positions,
    rotationTimes,
    rotationValues: rotations,
  };
}

export default function customAnimationBuilder(replayData, playerModels, ballModel, useBallRotation = true) {
  const playerClips = [];
  for (let playerIndex = 0; playerIndex < (replayData?.players?.length || 0); playerIndex += 1) {
    const playerData = replayData.players[playerIndex] || [];
    const playerName = `${replayData.names?.[playerIndex] || `Player ${playerIndex + 1}`}`;
    const keyframeData = generateKeyframeData(replayData, playerData, true);
    const playerPosKeyframes = new VectorKeyframeTrack(getPositionName(getGroupName(playerName)), keyframeData.positionTimes, keyframeData.positionValues);
    const playerRotKeyframes = new QuaternionKeyframeTrack(getRotationName(getCarName(playerName)), keyframeData.rotationTimes, keyframeData.rotationValues);
    playerClips.push(new AnimationClip(getActionClipName(playerName), keyframeData.duration, [playerPosKeyframes, playerRotKeyframes]));
  }

  const ballKeyframeData = generateKeyframeData(replayData, replayData?.ball || [], useBallRotation);
  const ballTracks = [new VectorKeyframeTrack(getPositionName(BALL), ballKeyframeData.positionTimes, ballKeyframeData.positionValues)];
  if (useBallRotation) {
    ballTracks.push(new QuaternionKeyframeTrack(getRotationName(BALL), ballKeyframeData.rotationTimes, ballKeyframeData.rotationValues));
  }
  const ballClip = new AnimationClip(getActionClipName(BALL), ballKeyframeData.duration, ballTracks);

  return AnimationManager.init({
    playerClips,
    ballClip,
    playerMixers: playerModels.map((model) => new AnimationMixer(model.carGroup)),
    ballMixer: new AnimationMixer(ballModel.ball),
  });
}
