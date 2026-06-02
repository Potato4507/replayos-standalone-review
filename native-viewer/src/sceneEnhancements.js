import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import SceneManager from 'replay-viewer/managers/SceneManager';
import { addFrameListener, removeFrameListener } from 'replay-viewer/eventbus/events/frame';
import { SPRITE } from 'replay-viewer/constants/gameObjectNames';

const DRACO_DECODER_PATH = '/viewer-assets/draco/gltf/';
const STANDARD_FIELD_PATH = '/viewer-assets/webreplayviewer/Field.glb';
const STANDARD_BALL_PATH = '/viewer-assets/webreplayviewer/Ball.glb';
const HOOPS_FIELD_PATH = '/viewer-assets/rocketviewer/maps/HoopsStadium_P.draco.glb';
const SMALL_PAD_PATH = '/viewer-assets/rocketviewer/boost_small.draco.glb';
const LARGE_PAD_PATH = '/viewer-assets/rocketviewer/boost_big.draco.glb';
const INACTIVE_PAD_OPACITY = 0.2;
const ACTIVE_PAD_OPACITY = 1;
const STANDARD_FIELD_SCALE = 400;
const STANDARD_BALL_SCALE = 105;

const shared = {
  gltfLoader: null,
  fieldTemplates: new Map(),
  ballTemplate: null,
  padTemplatesPromise: null,
};

const STANDARD_BIG_PADS = [
  [0, -4240], [0, 4240],
  [-3072, -4096], [3072, -4096],
  [-3072, 4096], [3072, 4096],
];

const STANDARD_SMALL_PADS = [
  [-3584, -2484], [3584, -2484], [-3584, 2484], [3584, 2484],
  [-2560, -3008], [2560, -3008], [-2560, 3008], [2560, 3008],
  [-1792, -4184], [1792, -4184], [-1792, 4184], [1792, 4184],
  [-940, -3308], [940, -3308], [-940, 3308], [940, 3308],
  [-3584, 0], [3584, 0], [-1024, -1024], [1024, -1024], [-1024, 1024], [1024, 1024],
  [-2048, 0], [2048, 0], [0, -2816], [0, 2816], [-1024, 0], [1024, 0],
];

const FAMILY_PROFILES = {
  octane: { bodyScale: [1, 1, 1], wheelTrack: 55, wheelbaseFront: 80, wheelbaseRear: -80, shellLift: 0, kits: [] },
  fennec: { bodyScale: [1.05, 0.94, 1.02], wheelTrack: 56, wheelbaseFront: 79, wheelbaseRear: -79, shellLift: -2, kits: [
    { size: [66, 17, 60], position: [-2, 66, 0], roughness: 0.38, metalness: 0.14 },
    { size: [28, 8, 62], position: [44, 39, 0], roughness: 0.42, metalness: 0.1 },
    { size: [24, 10, 60], position: [-44, 34, 0], roughness: 0.48, metalness: 0.1 },
  ] },
  dominus: { bodyScale: [1.13, 0.78, 1.06], wheelTrack: 58, wheelbaseFront: 89, wheelbaseRear: -88, shellLift: -7, kits: [
    { size: [88, 10, 54], position: [-3, 49, 0], roughness: 0.34, metalness: 0.16 },
    { size: [40, 6, 62], position: [54, 25, 0], roughness: 0.36, metalness: 0.12 },
    { size: [34, 5, 62], position: [-56, 24, 0], roughness: 0.36, metalness: 0.12 },
  ] },
  breakout: { bodyScale: [1.12, 0.84, 0.98], wheelTrack: 56, wheelbaseFront: 90, wheelbaseRear: -84, shellLift: -4, kits: [
    { size: [52, 11, 46], position: [-16, 55, 0], roughness: 0.34, metalness: 0.16 },
    { size: [34, 7, 54], position: [54, 30, 0], roughness: 0.38, metalness: 0.1 },
  ] },
  merc: { bodyScale: [0.98, 1.16, 1.06], wheelTrack: 58, wheelbaseFront: 78, wheelbaseRear: -78, shellLift: 2, kits: [
    { size: [58, 26, 58], position: [-7, 74, 0], roughness: 0.42, metalness: 0.1 },
    { size: [30, 8, 58], position: [48, 42, 0], roughness: 0.46, metalness: 0.1 },
  ] },
  plank: { bodyScale: [1.16, 0.72, 1.08], wheelTrack: 58, wheelbaseFront: 92, wheelbaseRear: -92, shellLift: -8, kits: [
    { size: [92, 9, 58], position: [-4, 46, 0], roughness: 0.32, metalness: 0.16 },
    { size: [28, 5, 64], position: [58, 22, 0], roughness: 0.36, metalness: 0.12 },
    { size: [24, 5, 64], position: [-60, 22, 0], roughness: 0.36, metalness: 0.12 },
  ] },
  hybrid: { bodyScale: [1.05, 0.88, 1.02], wheelTrack: 56, wheelbaseFront: 84, wheelbaseRear: -84, shellLift: -2, kits: [
    { size: [64, 15, 56], position: [-2, 58, 0], roughness: 0.36, metalness: 0.14 },
    { size: [24, 6, 60], position: [48, 31, 0], roughness: 0.4, metalness: 0.1 },
  ] },
};

function normalizeName(value) {
  return String(value || '').trim().toLowerCase();
}

function scenePositionFromRl(x = 0, y = 0, z = 0) {
  return new THREE.Vector3(Number(x) || 0, Number(z) || 0, Number(y) || 0);
}

function configureShadows(root, { cast = true, receive = true } = {}) {
  root.traverse((node) => {
    if (node?.isMesh) {
      node.castShadow = cast;
      node.receiveShadow = receive;
    }
  });
  return root;
}

function disposeObject(root) {
  root?.traverse?.((node) => {
    if (!node?.isMesh) return;
    node.geometry?.dispose?.();
    if (Array.isArray(node.material)) node.material.forEach((material) => material?.dispose?.());
    else node.material?.dispose?.();
  });
}

function captureBounds(object) {
  if (!object) return null;
  const bounds = new THREE.Box3().setFromObject(object);
  if (!Number.isFinite(bounds.min.x) || bounds.isEmpty()) return null;
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  return { bounds, center, size };
}

function cloneWithUniqueMaterials(root) {
  const clone = root.clone(true);
  const materialMap = new Map();
  clone.traverse((node) => {
    if (!node?.isMesh || !node.material) return;
    if (Array.isArray(node.material)) {
      node.material = node.material.map((material) => {
        if (!material) return material;
        if (!materialMap.has(material.uuid)) materialMap.set(material.uuid, material.clone());
        return materialMap.get(material.uuid);
      });
    } else {
      const material = node.material;
      if (!materialMap.has(material.uuid)) materialMap.set(material.uuid, material.clone());
      node.material = materialMap.get(material.uuid);
    }
  });
  return clone;
}

function transparentizePad(root) {
  root.traverse((node) => {
    const materialList = Array.isArray(node?.material) ? node.material : node?.material ? [node.material] : [];
    materialList.forEach((material) => {
      if (!material) return;
      material.transparent = true;
      material.depthWrite = !String(material.name || '').toLowerCase().includes('glow');
    });
  });
  return root;
}

function makeGlowMaterial(color, opacity) {
  return new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
}

function makePadMaterial(fullBoost, active = true) {
  return new THREE.MeshStandardMaterial({
    color: fullBoost ? '#ffb24a' : '#34d6ff',
    emissive: fullBoost ? '#803800' : '#005580',
    emissiveIntensity: active ? 0.85 : 0.12,
    roughness: 0.45,
    metalness: 0.15,
    transparent: true,
    opacity: active ? ACTIVE_PAD_OPACITY : INACTIVE_PAD_OPACITY,
  });
}

function makeProceduralPad(fullBoost) {
  const root = new THREE.Group();
  root.name = fullBoost ? 'native-procedural-big-pad' : 'native-procedural-small-pad';
  const radius = fullBoost ? 115 : 72;
  const height = fullBoost ? 18 : 12;
  const base = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, height, 36),
    makePadMaterial(fullBoost, true),
  );
  base.name = 'native-pad-base';
  base.position.y = height / 2;
  base.castShadow = false;
  base.receiveShadow = true;
  root.add(base);

  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(radius * 0.82, fullBoost ? 6 : 4, 8, 36),
    makeGlowMaterial(fullBoost ? '#ffc46f' : '#5be7ff', 0.58),
  );
  ring.name = 'native-pad-glow';
  ring.rotation.x = Math.PI / 2;
  ring.position.y = height + 5;
  root.add(ring);

  root.userData.nativePadMaterials = [base.material, ring.material];
  return root;
}

function getGltfLoader() {
  if (!shared.gltfLoader) {
    const dracoLoader = new DRACOLoader();
    dracoLoader.setDecoderPath(DRACO_DECODER_PATH);
    const gltfLoader = new GLTFLoader();
    gltfLoader.setDRACOLoader(dracoLoader);
    shared.gltfLoader = gltfLoader;
  }
  return shared.gltfLoader;
}

async function loadTemplate(url) {
  const gltf = await getGltfLoader().loadAsync(url);
  return gltf.scene;
}

async function loadFieldClone(mapCode) {
  const assetPath = mapCode === 'HoopsStadium_P' ? HOOPS_FIELD_PATH : STANDARD_FIELD_PATH;
  if (!shared.fieldTemplates.has(assetPath)) shared.fieldTemplates.set(assetPath, loadTemplate(assetPath));
  const template = await shared.fieldTemplates.get(assetPath);
  const clone = template.clone(true);
  if (assetPath === STANDARD_FIELD_PATH) clone.scale.setScalar(STANDARD_FIELD_SCALE);
  configureShadows(clone, { cast: false, receive: true });
  return clone;
}

async function loadBallClone() {
  if (!shared.ballTemplate) shared.ballTemplate = loadTemplate(STANDARD_BALL_PATH);
  const template = await shared.ballTemplate;
  const clone = template.clone(true);
  clone.scale.setScalar(STANDARD_BALL_SCALE);
  configureShadows(clone, { cast: true, receive: true });
  return clone;
}

async function loadPadTemplates() {
  if (!shared.padTemplatesPromise) {
    shared.padTemplatesPromise = Promise.allSettled([loadTemplate(SMALL_PAD_PATH), loadTemplate(LARGE_PAD_PATH)]).then(([small, large]) => ({
      small: small.status === 'fulfilled' ? transparentizePad(small.value) : makeProceduralPad(false),
      large: large.status === 'fulfilled' ? transparentizePad(large.value) : makeProceduralPad(true),
    }));
  }
  return shared.padTemplatesPromise;
}

function currentCarRoot(playerManager) {
  return playerManager?.carGroup?.children?.find((child) => String(child?.name || '').endsWith('-car')) || null;
}

function wheelNodes(root) {
  return {
    frontLeft: root.getObjectByName?.('Front Left'),
    frontRight: root.getObjectByName?.('Front Right'),
    backLeft: root.getObjectByName?.('Back Left'),
    backRight: root.getObjectByName?.('Back Right'),
  };
}

function shellNodes(root) {
  return (root.children || []).filter((child) => !child.getObjectByName?.('Front Left') && !String(child?.name || '').startsWith('native-kit-'));
}

function ensureBaseTransform(node) {
  if (node.userData?.nativeBaseTransform) return;
  node.userData = node.userData || {};
  node.userData.nativeBaseTransform = { position: node.position.clone(), rotation: node.rotation.clone(), scale: node.scale.clone() };
}

function restoreNodeTransform(node) {
  const base = node.userData?.nativeBaseTransform;
  if (!base) return;
  node.position.copy(base.position);
  node.rotation.copy(base.rotation);
  node.scale.copy(base.scale);
}

function clearNativeKits(root) {
  const stale = (root.children || []).filter((child) => String(child?.name || '').startsWith('native-kit-'));
  stale.forEach((child) => {
    child.removeFromParent();
    disposeObject(child);
  });
}

function familyKey(card) {
  const raw = normalizeName(card?.car_family || card?.car_name || 'octane');
  if (raw.includes('fennec')) return 'fennec';
  if (raw.includes('dominus')) return 'dominus';
  if (raw.includes('breakout')) return 'breakout';
  if (raw.includes('merc')) return 'merc';
  if (raw.includes('plank')) return 'plank';
  if (raw.includes('hybrid')) return 'hybrid';
  return 'octane';
}

function teamKitMaterial(isOrange) {
  return new THREE.MeshStandardMaterial({ color: isOrange ? '#ef8b45' : '#4a8dff', roughness: 0.4, metalness: 0.12 });
}

function addFamilyKit(root, profile, isOrange) {
  (profile.kits || []).forEach((kit, index) => {
    const mesh = new THREE.Mesh(new RoundedBoxGeometry(kit.size[0], kit.size[1], kit.size[2], 3, 3.5), teamKitMaterial(isOrange));
    mesh.name = `native-kit-${index}`;
    mesh.position.set(kit.position[0], kit.position[1], kit.position[2]);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.material.roughness = kit.roughness ?? mesh.material.roughness;
    mesh.material.metalness = kit.metalness ?? mesh.material.metalness;
    root.add(mesh);
  });
}

function applyFamilyProfile(root, card) {
  if (!root) return;
  clearNativeKits(root);
  const profile = FAMILY_PROFILES[familyKey(card)] || FAMILY_PROFILES.octane;
  const wheels = wheelNodes(root);
  Object.values(wheels).forEach((node) => node && ensureBaseTransform(node));
  shellNodes(root).forEach((node) => ensureBaseTransform(node));
  shellNodes(root).forEach((node) => {
    restoreNodeTransform(node);
    const base = node.userData.nativeBaseTransform;
    node.scale.copy(base.scale).multiply(new THREE.Vector3(...profile.bodyScale));
    node.position.copy(base.position);
    node.position.y += profile.shellLift || 0;
  });
  if (wheels.frontLeft) { restoreNodeTransform(wheels.frontLeft); wheels.frontLeft.position.set(profile.wheelbaseFront, 0, -profile.wheelTrack); }
  if (wheels.frontRight) { restoreNodeTransform(wheels.frontRight); wheels.frontRight.position.set(profile.wheelbaseFront, 0, profile.wheelTrack); }
  if (wheels.backLeft) { restoreNodeTransform(wheels.backLeft); wheels.backLeft.position.set(profile.wheelbaseRear, 0, -profile.wheelTrack); }
  if (wheels.backRight) { restoreNodeTransform(wheels.backRight); wheels.backRight.position.set(profile.wheelbaseRear, 0, profile.wheelTrack); }
  addFamilyKit(root, profile, Number(card?.team) === 1 || Boolean(card?.is_orange));
}

function enhanceCars(sceneManager, payload) {
  const cardsByName = new Map((payload?.hud?.player_cards || []).map((card) => [normalizeName(card.player_name), card]));
  (sceneManager.players || []).forEach((playerManager) => {
    const card = cardsByName.get(normalizeName(playerManager.playerName));
    if (!card) return;
    stylePlayerSprite(playerManager, card);
  });
}

function stylePlayerSprite(playerManager, card) {
  const sprite = playerManager?.carGroup?.getObjectByName?.(SPRITE) || playerManager?.carGroup?.children?.find((node) => node?.isSprite || node?.name === SPRITE);
  if (!sprite) return;
  sprite.removeFromParent();
  if (sprite.material?.map) sprite.material.map.dispose?.();
  sprite.material?.dispose?.();
}

function boostSeriesForPlayer(payload, card) {
  const playerId = card?.player_id || card?.id;
  return payload?.hud?.boost_by_player?.[playerId] || payload?.boost_by_player?.[playerId] || [];
}

function makeBoostTrail(playerManager, card, payload) {
  const root = new THREE.Group();
  root.name = `native-boost-trail-${card?.player_id || playerManager.playerName}`;
  const isOrange = Number(card?.team) === 1 || Boolean(card?.is_orange);
  const color = isOrange ? '#ffb15e' : '#44b8ff';

  const flame = new THREE.Mesh(
    new THREE.ConeGeometry(22, 115, 20, 1, true),
    makeGlowMaterial(color, 0.64),
  );
  flame.name = 'native-boost-flame';
  flame.rotation.z = Math.PI / 2;
  flame.position.set(-92, 38, 0);
  root.add(flame);

  const core = new THREE.Mesh(
    new THREE.CylinderGeometry(10, 22, 86, 16, 1, true),
    makeGlowMaterial('#ffffff', 0.32),
  );
  core.rotation.z = Math.PI / 2;
  core.position.set(-76, 38, 0);
  root.add(core);

  root.visible = false;
  playerManager.carGroup.add(root);

  const series = boostSeriesForPlayer(payload, card);
  let lastBoost = null;
  const onFrame = ({ frame }) => {
    if (!playerManager?.carGroup?.parent) return;
    const value = Array.isArray(series) && series.length ? Number(series[Math.max(0, Math.min(series.length - 1, frame))] || 0) : null;
    const usingBoost = lastBoost !== null && value !== null ? value < lastBoost - 0.15 : false;
    lastBoost = value;
    root.visible = usingBoost;
    if (usingBoost) {
      const pulse = 0.82 + Math.sin(frame * 0.62) * 0.18;
      flame.scale.set(1.0 + pulse * 0.25, pulse, 1.0 + pulse * 0.25);
      flame.material.opacity = 0.45 + pulse * 0.2;
      core.material.opacity = 0.22 + pulse * 0.18;
    }
  };
  addFrameListener(onFrame);

  return () => {
    removeFrameListener(onFrame);
    root.removeFromParent();
    disposeObject(root);
  };
}

function attachCarBoostTrails(sceneManager, payload) {
  const cardsByName = new Map((payload?.hud?.player_cards || []).map((card) => [normalizeName(card.player_name), card]));
  const cleanups = [];
  (sceneManager.players || []).forEach((playerManager) => {
    const card = cardsByName.get(normalizeName(playerManager.playerName));
    if (!card || !playerManager?.carGroup) return;
    cleanups.push(makeBoostTrail(playerManager, card, payload));
  });
  return () => cleanups.forEach((cleanup) => cleanup());
}

async function attachMap(sceneManager, payload) {
  const useCustomMap = String(payload?.replay?.map_code || '') === 'HoopsStadium_P';
  const stockField = sceneManager.field?.field || null;
  if (stockField) stockField.visible = true;
  const map = useCustomMap ? await loadFieldClone(payload?.replay?.map_code) : null;
  if (map) {
    map.name = 'native-enhanced-map';
  }

  const ambience = new THREE.Group();
  ambience.name = 'native-ambience';
  ambience.add(new THREE.HemisphereLight('#b9dcff', '#142a21', 1.5));

  const key = new THREE.DirectionalLight('#fff5dc', 1.45);
  key.position.set(1200, 3400, 1800);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 9500;
  ambience.add(key);

  const blueFill = new THREE.PointLight('#2f7bff', 0.6, 5200, 2);
  blueFill.position.set(-1600, 540, -2600);
  ambience.add(blueFill);

  const orangeFill = new THREE.PointLight('#ff9348', 0.6, 5200, 2);
  orangeFill.position.set(1600, 540, 2600);
  ambience.add(orangeFill);

  const sky = new THREE.Mesh(new THREE.SphereGeometry(14000, 28, 20), new THREE.MeshBasicMaterial({ color: '#060a12', side: THREE.BackSide }));
  sky.name = 'native-sky-dome';
  ambience.add(sky);

  const previousFog = sceneManager.scene.fog;
  sceneManager.scene.fog = new THREE.FogExp2('#071018', 0.000075);
  if (map) {
    if (stockField) stockField.visible = false;
    sceneManager.scene.add(map);
  }
  sceneManager.scene.add(ambience);

  return () => {
    sceneManager.scene.fog = previousFog;
    ambience.removeFromParent();
    map?.removeFromParent();
    disposeObject(ambience);
    if (map) disposeObject(map);
    if (sceneManager.field?.field) sceneManager.field.field.visible = true;
  };
}

function makeBallTrail(ballAnchor) {
  const trail = new THREE.Group();
  trail.name = 'native-ball-trail';
  const ghosts = Array.from({ length: 7 }, (_, index) => {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(48 - index * 3.8, 18, 12), makeGlowMaterial('#7befff', 0.18 - index * 0.018));
    mesh.visible = false;
    trail.add(mesh);
    return mesh;
  });
  const history = [];
  const onFrame = () => {
    if (!ballAnchor?.parent) return;
    history.unshift(ballAnchor.getWorldPosition(new THREE.Vector3()));
    if (history.length > ghosts.length) history.pop();
    ghosts.forEach((ghost, index) => {
      const point = history[index];
      ghost.visible = Boolean(point);
      if (!point) return;
      ghost.position.copy(point);
      ghost.scale.setScalar(1 + index * 0.08);
      ghost.material.opacity = Math.max(0.025, 0.16 - index * 0.018);
    });
  };
  addFrameListener(onFrame);
  return { object: trail, cleanup: () => { removeFrameListener(onFrame); trail.removeFromParent(); disposeObject(trail); } };
}

async function attachBall(sceneManager) {
  const ballAnchor = sceneManager.ball?.ball;
  if (!ballAnchor) return () => {};
  const cleanups = [];

  try {
    const replacement = await loadBallClone();
    replacement.name = 'native-enhanced-ball';
    const originalChildren = [...ballAnchor.children];
    const previousVisibility = originalChildren.map((child) => child.visible);
    originalChildren.forEach((child) => { child.visible = false; });

    const glow = new THREE.Mesh(new THREE.SphereGeometry(78, 24, 16), makeGlowMaterial('#8ff5ff', 0.16));
    glow.name = 'native-ball-glow';
    ballAnchor.add(replacement);
    ballAnchor.add(glow);
    cleanups.push(() => {
      replacement.removeFromParent();
      glow.removeFromParent();
      originalChildren.forEach((child, index) => { child.visible = previousVisibility[index]; });
      disposeObject(replacement);
      disposeObject(glow);
    });
  } catch {
    // Keep the original ball if the replacement asset is missing.
  }

  const trail = makeBallTrail(ballAnchor);
  sceneManager.scene.add(trail.object);
  cleanups.push(trail.cleanup);
  return () => cleanups.reverse().forEach((cleanup) => cleanup());
}

function normalizePadLayout(payload) {
  const explicit = payload?.hud?.boost_pad_layout || payload?.boost_pad_layout || payload?.pads || [];
  if (explicit.length) {
    return explicit.map((pad, index) => ({
      pad_id: pad.pad_id ?? pad.id ?? index,
      x: Number(pad.x ?? pad.location?.x ?? pad.position?.x ?? 0),
      y: Number(pad.y ?? pad.location?.y ?? pad.position?.y ?? 0),
      full_boost: Boolean(pad.full_boost ?? pad.is_full_boost ?? pad.big ?? pad.large),
    }));
  }

  const big = STANDARD_BIG_PADS.map(([x, y], index) => ({ pad_id: `fallback-big-${index}`, x, y, full_boost: true }));
  const small = STANDARD_SMALL_PADS.map(([x, y], index) => ({ pad_id: `fallback-small-${index}`, x, y, full_boost: false }));
  return [...big, ...small];
}

function parsePadState(value) {
  if (value === undefined || value === null) return true;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value > 0;
  if (typeof value === 'object') {
    if ('active' in value) return Boolean(value.active);
    if ('is_active' in value) return Boolean(value.is_active);
    if ('available' in value) return Boolean(value.available);
    if ('respawn' in value) return Number(value.respawn) <= 0;
    if ('timer' in value) return Number(value.timer) <= 0;
  }
  return true;
}

function getPadMaterials(root) {
  if (root.userData?.nativePadMaterials) return root.userData.nativePadMaterials;
  const materials = [];
  root.traverse((node) => {
    const materialList = Array.isArray(node?.material) ? node.material : node?.material ? [node.material] : [];
    materialList.forEach((material) => {
      if (!material || materials.includes(material)) return;
      materials.push(material);
    });
  });
  root.userData = root.userData || {};
  root.userData.nativePadMaterials = materials;
  return materials;
}

function setPadActive(root, active, frame = 0) {
  root.userData = root.userData || {};
  if (root.userData.nativePadActive === active) return;
  root.userData.nativePadActive = active;
  root.visible = true;
  getPadMaterials(root).forEach((material) => {
    if (!material) return;
    material.opacity = active ? ACTIVE_PAD_OPACITY : INACTIVE_PAD_OPACITY;
    if ('emissiveIntensity' in material) material.emissiveIntensity = active ? 0.66 : 0.04;
    material.needsUpdate = true;
  });
  const glow = root.getObjectByName('native-pad-glow') || root.getObjectByName('BoostPad_Large_Glow') || root.getObjectByName('BoostPad_Small_Glow');
  root.userData.nativePadGlow = glow || null;
  if (root.userData.nativePadGlow) {
    root.userData.nativePadGlow.visible = active;
    root.userData.nativePadGlow.scale.setScalar(1);
  }
}

function pulsePad(root, frame = 0) {
  if (!root?.userData?.nativePadActive) return;
  const pulse = 0.92 + Math.sin(frame * 0.08 + (root.userData.nativePadIndex || 0)) * 0.08;
  const boostScale = root.userData?.nativePadFullBoost ? 1.22 : 1;
  const glow = root.getObjectByName('native-pad-glow') || root.getObjectByName('BoostPad_Large_Glow') || root.getObjectByName('BoostPad_Small_Glow');
  if (glow) {
    glow.scale.setScalar(boostScale + pulse * (root.userData?.nativePadFullBoost ? 0.16 : 0.08));
  }
  getPadMaterials(root).forEach((material) => {
    if (!material || !('emissiveIntensity' in material)) return;
    material.emissiveIntensity = (root.userData?.nativePadFullBoost ? 0.74 : 0.55) + pulse * (root.userData?.nativePadFullBoost ? 0.34 : 0.28);
  });
}

function shouldPulsePad(frame, index) {
  return frame % 2 === index % 2;
}

function getPadStateValue(state, root, index) {
  if (typeof state === 'number' && Number.isFinite(state)) {
    return Math.floor(state / (2 ** index)) % 2 >= 1;
  }
  if (Array.isArray(state)) return state[index];
  return state?.[root.name] ?? state?.[index];
}

async function attachBoostPads(sceneManager, payload) {
  const layout = normalizePadLayout(payload);
  if (!layout.length) return () => {};

  const templates = await loadPadTemplates();
  const padRoots = layout.map((pad, index) => {
    const template = pad.full_boost ? templates.large : templates.small;
    const root = cloneWithUniqueMaterials(template);
    root.name = `native-pad-${pad.pad_id || index}`;
    root.userData.nativePadIndex = index;
    root.userData.nativePadFullBoost = Boolean(pad.full_boost);
    root.position.copy(scenePositionFromRl(pad.x, pad.y, 0));
    root.position.y += pad.full_boost ? 6 : 3;
    root.scale.setScalar(pad.full_boost ? 1.4 : 1.08);
    configureShadows(root, { cast: false, receive: false });
    sceneManager.scene.add(root);
    setPadActive(root, true, 0);
    return root;
  });

  const padStatesByFrame = payload?.hud?.pad_state_masks || payload?.hud?.pad_states || payload?.hud?.boost_pad_states || payload?.pad_state_masks || payload?.pad_states || [];
  const onFrame = ({ frame }) => {
    const state = Array.isArray(padStatesByFrame) && padStatesByFrame.length
      ? padStatesByFrame[Math.max(0, Math.min(padStatesByFrame.length - 1, frame))]
      : null;
    padRoots.forEach((root, index) => {
      const active = parsePadState(getPadStateValue(state, root, index));
      setPadActive(root, active, frame);
      if (active && shouldPulsePad(frame, index)) pulsePad(root, frame);
    });
  };
  onFrame({ frame: 0 });
  addFrameListener(onFrame);

  return () => {
    removeFrameListener(onFrame);
    padRoots.forEach((root) => { root.removeFromParent(); disposeObject(root); });
  };
}

async function settleEnhancement(label, installer, cleanups) {
  try {
    const cleanup = await installer();
    if (typeof cleanup === 'function') cleanups.push(cleanup);
  } catch (error) {
    console.warn(`[native-viewer] ${label} enhancement failed`, error);
  }
}

export async function installSceneEnhancements(payload) {
  const sceneManager = SceneManager.getInstance();
  const cleanups = [];

  await Promise.all([
    settleEnhancement('map', () => attachMap(sceneManager, payload), cleanups),
    settleEnhancement('ball', () => attachBall(sceneManager), cleanups),
    settleEnhancement('boost pads', () => attachBoostPads(sceneManager, payload), cleanups),
  ]);

  enhanceCars(sceneManager, payload);

  return () => {
    for (const cleanup of cleanups.reverse()) {
      try { cleanup(); } catch { /* ignore scene cleanup errors on unload */ }
    }
  };
}
