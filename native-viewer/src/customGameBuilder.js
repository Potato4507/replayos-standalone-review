import CameraManager from 'replay-viewer/managers/CameraManager';
import DataManager from 'replay-viewer/managers/DataManager';
import { GameManager } from 'replay-viewer/managers/GameManager';
import KeyManager from 'replay-viewer/managers/KeyManager';
import AnimationManager from 'replay-viewer/managers/AnimationManager';
import { addFrameListener } from 'replay-viewer/eventbus/events/frame';
import defaultSceneBuilder from 'replay-viewer/builders/SceneBuilder';
import customAnimationBuilder from './customAnimationBuilder';

function syncAnimationToClock(clock) {
  let animationManager;
  try {
    animationManager = AnimationManager.getInstance();
  } catch {
    return;
  }

  const elapsedMs = Number(clock?.getElapsedTime?.() ?? clock?.elapsedTime ?? 0);
  const absoluteSeconds = Math.max(0, elapsedMs / 1000);
  const playerMixers = animationManager?.mixers?.players || [];
  playerMixers.forEach((mixer) => mixer?.setTime?.(absoluteSeconds));
  animationManager?.mixers?.ball?.setTime?.(absoluteSeconds);
}

export default async function customGameBuilder({
  clock,
  replayData,
  replayMetadata,
  loadingManager,
  useBallRotation = true,
}) {
  const players = replayMetadata?.players || [];
  const sceneManager = await defaultSceneBuilder(players, loadingManager);
  customAnimationBuilder(replayData, sceneManager.players, sceneManager.ball, useBallRotation);
  DataManager.init({ replayData, replayMetadata });
  CameraManager.init();
  KeyManager.init();

  // The stock replay-viewer clock only advances mixers by relative delta.
  // On seeks/jumps we need to scrub animation to the absolute replay time
  // before the rest of the frame listeners read scene state.
  addFrameListener(({ reason, delta }) => {
    if (reason === 'playback' && Number(delta || 0) > 0) return;
    syncAnimationToClock(clock);
  });

  return GameManager.init({ clock });
}
