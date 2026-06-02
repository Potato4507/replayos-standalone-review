import { dispatchFrameEvent } from 'replay-viewer/eventbus/events/frame';

function clampFrame(frame, maxFrame) {
  if (!Number.isFinite(frame)) return 0;
  return Math.max(0, Math.min(maxFrame, Math.round(frame)));
}

function binarySearchFrameForElapsed(frameToDuration, elapsedTime) {
  const maxFrame = frameToDuration.length - 1;
  if (maxFrame <= 0) return 0;
  const boundedElapsed = Math.max(0, Math.min(frameToDuration[maxFrame], elapsedTime));
  let low = 0;
  let high = maxFrame;
  let best = 0;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if ((frameToDuration[mid] || 0) <= boundedElapsed) {
      best = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return best;
}

function frameFloatForElapsed(frameToDuration, elapsedTime, currentFrame, snapFrames = null) {
  const maxFrame = frameToDuration.length - 1;
  if (maxFrame <= 0) return { frameFloat: 0, frameAlpha: 0 };
  const frame = clampFrame(currentFrame, maxFrame);
  if (frame >= maxFrame) return { frameFloat: maxFrame, frameAlpha: 0 };
  if (snapFrames?.has(frame) || snapFrames?.has(frame + 1)) return { frameFloat: frame, frameAlpha: 0 };
  const start = frameToDuration[frame] || 0;
  const next = frameToDuration[frame + 1] || start;
  const span = Math.max(1, next - start);
  const alpha = Math.max(0, Math.min(1, (elapsedTime - start) / span));
  return { frameFloat: frame + alpha, frameAlpha: alpha };
}

function normalizePlaybackRate(rate) {
  if (!Number.isFinite(Number(rate))) return 1;
  return Math.max(0.1, Math.min(4, Number(rate)));
}

class SmoothReplayClock {
  constructor(frameToDuration, snapFrames = []) {
    this.frameToDuration = frameToDuration;
    this.snapFrames = new Set(Array.isArray(snapFrames) ? snapFrames.map((frame) => clampFrame(Number(frame) || 0, frameToDuration.length - 1)) : []);
    this.paused = true;
    this.elapsedTime = 0;
    this.currentFrame = 0;
    this.lastFrameTime = 0;
    this.rafHandle = 0;
    this.lastReportedDelta = 0;
    this.playbackRate = 1;
    this.currentFrameFloat = 0;
    this.currentFrameAlpha = 0;
    this.tick = this.tick.bind(this);
  }

  reset() {
    this.pause();
    this.setFrame(0, { reason: 'reset', silentDelta: true });
  }

  setPlaybackRate(rate) {
    this.playbackRate = normalizePlaybackRate(rate);
    this.lastFrameTime = performance.now();
    return this.playbackRate;
  }

  getPlaybackRate() {
    return this.playbackRate;
  }

  setFrame(frame, options = {}) {
    const maxFrame = this.frameToDuration.length - 1;
    const boundedFrame = clampFrame(frame, maxFrame);
    const nextElapsed = this.frameToDuration[boundedFrame] || 0;
    const deltaMs = options.silentDelta ? 0 : nextElapsed - this.elapsedTime;
    this.elapsedTime = nextElapsed;
    this.currentFrame = boundedFrame;
    const floatState = frameFloatForElapsed(this.frameToDuration, this.elapsedTime, this.currentFrame, this.snapFrames);
    this.currentFrameFloat = floatState.frameFloat;
    this.currentFrameAlpha = floatState.frameAlpha;
    this.lastFrameTime = performance.now();
    this.dispatch(deltaMs / 1000, options.reason || 'seek');
  }

  seekSeconds(seconds, options = {}) {
    const maxElapsed = this.getDuration();
    const nextElapsed = Math.max(0, Math.min(maxElapsed, this.elapsedTime + Number(seconds || 0) * 1000));
    const previousElapsed = this.elapsedTime;
    this.elapsedTime = nextElapsed;
    this.currentFrame = binarySearchFrameForElapsed(this.frameToDuration, nextElapsed);
    const floatState = frameFloatForElapsed(this.frameToDuration, this.elapsedTime, this.currentFrame, this.snapFrames);
    this.currentFrameFloat = floatState.frameFloat;
    this.currentFrameAlpha = floatState.frameAlpha;
    this.lastFrameTime = performance.now();
    this.dispatch(options.silentDelta ? 0 : (nextElapsed - previousElapsed) / 1000, options.reason || 'seek');
  }

  step(frames = 1) {
    this.setFrame(this.currentFrame + Number(frames || 0), { reason: 'step', silentDelta: true });
  }

  isPaused() {
    return this.paused;
  }

  play() {
    if (!this.paused) return;
    this.paused = false;
    this.lastFrameTime = performance.now();
    this.cancelTick();
    this.rafHandle = window.requestAnimationFrame(this.tick);
    this.dispatch(0, 'play');
  }

  pause() {
    if (this.paused && !this.rafHandle) return;
    this.paused = true;
    this.cancelTick();
    this.lastFrameTime = 0;
    this.dispatch(0, 'pause');
  }

  toggle() {
    if (this.isPaused()) this.play();
    else this.pause();
  }

  getElapsedTime() {
    return this.frameToDuration[this.currentFrame] || 0;
  }

  getDelta() {
    return this.lastReportedDelta;
  }

  getFrameFloat() {
    return this.currentFrameFloat;
  }

  getDuration() {
    return this.frameToDuration[this.frameToDuration.length - 1] || 0;
  }

  cancelTick() {
    if (this.rafHandle) {
      window.cancelAnimationFrame(this.rafHandle);
      this.rafHandle = 0;
    }
  }

  dispatch(deltaSeconds, reason = 'frame') {
    this.lastReportedDelta = Number.isFinite(deltaSeconds) ? deltaSeconds : 0;
    dispatchFrameEvent({
      delta: this.lastReportedDelta,
      frame: this.currentFrame,
      frameFloat: this.currentFrameFloat,
      frameAlpha: this.currentFrameAlpha,
      elapsedTime: this.getElapsedTime(),
      playbackRate: this.playbackRate,
      reason,
    });
  }

  tick(now) {
    if (this.paused) return;
    if (!this.lastFrameTime) this.lastFrameTime = now;
    const rawDeltaMs = now - this.lastFrameTime;
    const cappedRealDeltaMs = Math.max(0, Math.min(rawDeltaMs, 80));
    const scaledDeltaMs = cappedRealDeltaMs * this.playbackRate;
    this.lastFrameTime = now;
    const maxElapsed = this.getDuration();
    const previousElapsed = this.elapsedTime;
    const nextElapsed = Math.min(previousElapsed + scaledDeltaMs, maxElapsed);
    this.elapsedTime = nextElapsed;
    this.currentFrame = binarySearchFrameForElapsed(this.frameToDuration, nextElapsed);
    const floatState = frameFloatForElapsed(this.frameToDuration, this.elapsedTime, this.currentFrame, this.snapFrames);
    this.currentFrameFloat = floatState.frameFloat;
    this.currentFrameAlpha = floatState.frameAlpha;
    this.dispatch((nextElapsed - previousElapsed) / 1000, 'playback');
    if (nextElapsed >= maxElapsed) {
      this.pause();
      return;
    }
    this.rafHandle = window.requestAnimationFrame(this.tick);
  }

  static convertReplayToClock(data) {
    let elapsedTime = 0;
    const frames = (data?.frames || []).map((frameInfo) => {
      const current = elapsedTime;
      elapsedTime += Math.max(0, Number(frameInfo?.[0] || 0)) * 1000;
      return current;
    });
    if (!frames.length) frames.push(0);
    return new SmoothReplayClock(frames, data?.snapFrames || data?.snap_frames || []);
  }
}

export default SmoothReplayClock;
