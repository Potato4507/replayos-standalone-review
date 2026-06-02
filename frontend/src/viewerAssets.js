import * as THREE from 'three';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';

export const BOOST_PAD_LAYOUT = [
  { pad_id: 'small-0', full_boost: false, x: 1792, y: 4184 },
  { pad_id: 'small-1', full_boost: false, x: -1792, y: 4184 },
  { pad_id: 'small-2', full_boost: false, x: 1792, y: -4184 },
  { pad_id: 'small-3', full_boost: false, x: -1792, y: -4184 },
  { pad_id: 'small-4', full_boost: false, x: 940, y: 3308 },
  { pad_id: 'small-5', full_boost: false, x: -940, y: 3308 },
  { pad_id: 'small-6', full_boost: false, x: 940, y: -3308 },
  { pad_id: 'small-7', full_boost: false, x: -940, y: -3308 },
  { pad_id: 'small-8', full_boost: false, x: 1788, y: 2300 },
  { pad_id: 'small-9', full_boost: false, x: -1788, y: 2300 },
  { pad_id: 'small-10', full_boost: false, x: 1788, y: -2300 },
  { pad_id: 'small-11', full_boost: false, x: -1788, y: -2300 },
  { pad_id: 'small-12', full_boost: false, x: 2048, y: 1036 },
  { pad_id: 'small-13', full_boost: false, x: -2048, y: 1036 },
  { pad_id: 'small-14', full_boost: false, x: 2048, y: -1036 },
  { pad_id: 'small-15', full_boost: false, x: -2048, y: -1036 },
  { pad_id: 'small-16', full_boost: false, x: 3584, y: 2484 },
  { pad_id: 'small-17', full_boost: false, x: -3584, y: 2484 },
  { pad_id: 'small-18', full_boost: false, x: 3584, y: -2484 },
  { pad_id: 'small-19', full_boost: false, x: -3584, y: -2484 },
  { pad_id: 'small-20', full_boost: false, x: 0, y: 4240 },
  { pad_id: 'small-21', full_boost: false, x: 0, y: -4240 },
  { pad_id: 'small-22', full_boost: false, x: 0, y: 2816 },
  { pad_id: 'small-23', full_boost: false, x: 0, y: -2816 },
  { pad_id: 'small-24', full_boost: false, x: 0, y: 1024 },
  { pad_id: 'small-25', full_boost: false, x: 0, y: -1024 },
  { pad_id: 'small-26', full_boost: false, x: 1024, y: 0 },
  { pad_id: 'small-27', full_boost: false, x: -1024, y: 0 },
  { pad_id: 'large-0', full_boost: true, x: 3072, y: 4096 },
  { pad_id: 'large-1', full_boost: true, x: -3072, y: 4096 },
  { pad_id: 'large-2', full_boost: true, x: 3072, y: -4096 },
  { pad_id: 'large-3', full_boost: true, x: -3072, y: -4096 },
  { pad_id: 'large-4', full_boost: true, x: 3584, y: 0 },
  { pad_id: 'large-5', full_boost: true, x: -3584, y: 0 },
];

export const SMALL_BOOST_PAD_COORDS = BOOST_PAD_LAYOUT.filter((pad) => !pad.full_boost).map((pad) => [pad.x, pad.y]);
export const LARGE_BOOST_PAD_COORDS = BOOST_PAD_LAYOUT.filter((pad) => pad.full_boost).map((pad) => [pad.x, pad.y]);

export const RL_SCENE_SCALE = 0.01;

const ROCKETSIMVIS_ROOT = '/viewer-assets/rocketsimvis';
const WEBREPLAY_ROOT = '/viewer-assets/webreplayviewer';
const ROCKET_VIEWER_ROOT = '/viewer-assets/rocketviewer';
const DRACO_ROOT = '/viewer-assets/draco/gltf/';
const LEGACY_BOOST_PAD_SCALE = 2.5 * RL_SCENE_SCALE;
const FIELD_SCALE = 400 * RL_SCENE_SCALE;
const BALL_SCALE = 105 * RL_SCENE_SCALE;
const CAR_SCALE = RL_SCENE_SCALE;
const WHEEL_FORWARD_DISTANCE = 80;
const WHEEL_LEFT_DISTANCE = 55;
const WHEEL_VERTICAL_DISTANCE = 0;
const CAR_BODY_Y_OFFSET = 31;
const MAP_TRANSPARENT_MATERIALS = {
  standard: new Set(['goal_glass', 'wall_grate', 'center_grate_material']),
  hoops: new Set(['net_material', 'wall_material_0', 'backboard_material_1_orange', 'backboard_material_1_blue']),
};

const viewerAssetCache = {
  promise: null,
};

function loadObj(loader, name) {
  return new Promise((resolve, reject) => {
    loader.load(`${ROCKETSIMVIS_ROOT}/${name}`, resolve, undefined, reject);
  });
}

function loadTexture(loader, name) {
  return new Promise((resolve, reject) => {
    loader.load(
      `${ROCKETSIMVIS_ROOT}/${name}`,
      (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.wrapS = THREE.ClampToEdgeWrapping;
        texture.wrapT = THREE.ClampToEdgeWrapping;
        resolve(texture);
      },
      undefined,
      reject
    );
  });
}

function loadGlb(loader, name) {
  return new Promise((resolve, reject) => {
    loader.load(`${WEBREPLAY_ROOT}/${name}`, resolve, undefined, reject);
  });
}

function loadRocketViewerGlb(loader, name) {
  return new Promise((resolve, reject) => {
    loader.load(`${ROCKET_VIEWER_ROOT}/${name}`, resolve, undefined, reject);
  });
}

function loadRocketViewerTexture(loader, name) {
  return new Promise((resolve, reject) => {
    loader.load(
      `${ROCKET_VIEWER_ROOT}/${name}`,
      (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        resolve(texture);
      },
      undefined,
      reject
    );
  });
}

function findNamedObject(root, targetName) {
  let found = null;
  root.traverse((child) => {
    if (!found && child.name === targetName) {
      found = child;
    }
  });
  return found;
}

function markRenderable(root, transformMesh) {
  root.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    child.castShadow = true;
    child.receiveShadow = true;
    child.geometry = child.geometry.clone();
    child.geometry.computeVertexNormals();
    if (transformMesh) {
      transformMesh(child);
    }
  });
  return root;
}

function prepareObjTemplate(root, material) {
  const wrapper = new THREE.Group();
  const clone = root.clone(true);
  clone.rotation.x = -Math.PI / 2;
  clone.scale.set(1, 1, -1);
  markRenderable(clone, (child) => {
    child.material = material.clone();
  });
  wrapper.add(clone);
  return wrapper;
}

function prepareSceneTemplate(root) {
  const wrapper = new THREE.Group();
  const clone = root.clone(true);
  markRenderable(clone);
  wrapper.add(clone);
  return wrapper;
}

function prepareMapTemplate(root, transparentMaterials = new Set()) {
  const wrapper = prepareSceneTemplate(root);
  wrapper.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    child.renderOrder = 1;
    materials.forEach((material) => {
      if (!material) return;
      material.depthWrite = !transparentMaterials.has(material.name);
      if (transparentMaterials.has(material.name)) {
        material.transparent = true;
      }
    });
  });
  wrapper.userData.viewerScale = RL_SCENE_SCALE;
  return wrapper;
}

function tuneBoostMaterial(material, { active, fullBoost }) {
  const tuned = material.clone();
  tuned.depthWrite = !String(tuned.name || '').toLowerCase().includes('glow');
  if ('emissive' in tuned && tuned.emissive?.set) {
    tuned.emissive.set(active ? (fullBoost ? '#ffd885' : '#e2b455') : '#2b2418');
    tuned.emissiveIntensity = active ? (fullBoost ? 1.35 : 0.72) : 0.18;
  }
  if ('color' in tuned && tuned.color?.multiplyScalar && !active) {
    tuned.color = tuned.color.clone();
    tuned.color.multiplyScalar(0.78);
  }
  if ('roughness' in tuned) tuned.roughness = active ? 0.32 : 0.58;
  if ('metalness' in tuned) tuned.metalness = active ? 0.14 : 0.05;
  if ('transparent' in tuned && String(tuned.name || '').toLowerCase().includes('glow')) {
    tuned.opacity = active ? 1 : 0.18;
  }
  return tuned;
}

function prepareBoostTemplate(root, glowTexture, { active, fullBoost }) {
  const wrapper = prepareSceneTemplate(root);
  wrapper.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    child.material = Array.isArray(child.material)
      ? child.material.map((material) => tuneBoostMaterial(material, { active, fullBoost }))
      : tuneBoostMaterial(child.material, { active, fullBoost });
  });
  if (active && glowTexture) {
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture,
      color: fullBoost ? '#ffd886' : '#e8bd54',
      depthWrite: false,
      transparent: true,
      opacity: fullBoost ? 0.82 : 0.54,
    }));
    sprite.position.y = fullBoost ? 115 : 70;
    sprite.scale.setScalar(fullBoost ? 180 : 128);
    wrapper.add(sprite);
  }
  wrapper.userData.viewerScale = RL_SCENE_SCALE;
  return wrapper;
}

function instantiateTemplate(template, scale = template?.userData?.viewerScale ?? 1) {
  const instance = template.clone(true);
  if (Array.isArray(scale)) {
    instance.scale.set(...scale);
  } else {
    instance.scale.setScalar(scale);
  }
  return instance;
}

function buildWheelGroup(assets) {
  const wheelGroup = new THREE.Group();
  const placements = [
    { x: WHEEL_FORWARD_DISTANCE, z: -WHEEL_LEFT_DISTANCE, mirror: false },
    { x: WHEEL_FORWARD_DISTANCE, z: WHEEL_LEFT_DISTANCE, mirror: true },
    { x: -WHEEL_FORWARD_DISTANCE, z: -WHEEL_LEFT_DISTANCE, mirror: false },
    { x: -WHEEL_FORWARD_DISTANCE, z: WHEEL_LEFT_DISTANCE, mirror: true },
  ];
  placements.forEach(({ x, z, mirror }) => {
    const wheel = assets.wheelTemplate.clone(true);
    wheel.position.set(x, WHEEL_VERTICAL_DISTANCE, z);
    if (mirror) {
      wheel.scale.z *= -1;
    }
    wheelGroup.add(wheel);
  });
  return wheelGroup;
}

export function sceneVectorFromRl(vector = [0, 0, 0]) {
  return new THREE.Vector3(
    Number(vector?.[0] || 0) * RL_SCENE_SCALE,
    Number(vector?.[2] || 0) * RL_SCENE_SCALE,
    Number(vector?.[1] || 0) * RL_SCENE_SCALE
  );
}

function sceneAxisFromRl(vector = [0, 0, 0]) {
  const axis = new THREE.Vector3(
    Number(vector?.[0] || 0),
    Number(vector?.[2] || 0),
    Number(vector?.[1] || 0)
  );
  if (axis.lengthSq() <= 1e-9) {
    return axis.set(0, 1, 0);
  }
  return axis.normalize();
}

export function carQuaternionFromTelemetry(euler = [0, 0, 0]) {
  const pitch = Number(euler?.[0] || 0);
  const yaw = Number(euler?.[1] || 0);
  const roll = Number(euler?.[2] || 0);

  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cr = Math.cos(roll);
  const sr = Math.sin(roll);

  const forward = sceneAxisFromRl([
    cp * cy,
    cp * sy,
    sp,
  ]);
  const right = sceneAxisFromRl([
    cy * sp * sr - cr * sy,
    sy * sp * sr + cr * cy,
    -cp * sr,
  ]).multiplyScalar(-1);
  const up = sceneAxisFromRl([
    -cr * cy * sp - sr * sy,
    -cr * sy * sp + sr * cy,
    cp * cr,
  ]);

  return new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(right, up, forward));
}

export async function loadViewerAssets() {
  if (!viewerAssetCache.promise) {
    viewerAssetCache.promise = (async () => {
      const objLoader = new OBJLoader();
      const textureLoader = new THREE.TextureLoader();
      const dracoLoader = new DRACOLoader();
      dracoLoader.setDecoderPath(DRACO_ROOT);
      const gltfLoader = new GLTFLoader();
      gltfLoader.setDRACOLoader(dracoLoader);

      const [
        fieldGltf,
        ballGltf,
        blueCarGltf,
        orangeCarGltf,
        wheelGltf,
        standardMapGltf,
        hoopsMapGltf,
        rocketBoostSmallGltf,
        rocketBoostBigGltf,
        rocketBoostSprite,
        arenaShellObj,
        boostSmallInactiveObj,
        boostSmallActiveObj,
        boostBigInactiveObj,
        boostBigActiveObj,
        boostTexture,
      ] = await Promise.all([
        loadGlb(gltfLoader, 'Field.glb'),
        loadGlb(gltfLoader, 'Ball.glb'),
        loadGlb(gltfLoader, 'Octane_ZXR_Blue.glb'),
        loadGlb(gltfLoader, 'Octane_ZXR_Orange.glb'),
        loadGlb(gltfLoader, 'Wheel.glb'),
        loadRocketViewerGlb(gltfLoader, 'maps/TrainStation_Night_P.draco.glb'),
        loadRocketViewerGlb(gltfLoader, 'maps/HoopsStadium_P.draco.glb'),
        loadRocketViewerGlb(gltfLoader, 'boost_small.draco.glb'),
        loadRocketViewerGlb(gltfLoader, 'boost_big.draco.glb'),
        loadRocketViewerTexture(textureLoader, 'sprites/boost_ball.png'),
        loadObj(objLoader, 'ArenaMeshCustom.obj'),
        loadObj(objLoader, 'BoostPad_Small_0.obj'),
        loadObj(objLoader, 'BoostPad_Small_1.obj'),
        loadObj(objLoader, 'BoostPad_Big_0.obj'),
        loadObj(objLoader, 'BoostPad_Big_1.obj'),
        loadTexture(textureLoader, 'T_BoostPad.png'),
      ]);

      const fieldRoot = findNamedObject(fieldGltf.scene, 'Field') || fieldGltf.scene;
      const ballRoot = findNamedObject(ballGltf.scene, 'Ball') || ballGltf.scene;
      const blueCarRoot = findNamedObject(blueCarGltf.scene, 'Octane') || blueCarGltf.scene;
      const orangeCarRoot = findNamedObject(orangeCarGltf.scene, 'Octane') || orangeCarGltf.scene;
      const wheelRoot = findNamedObject(wheelGltf.scene, 'Wheel') || wheelGltf.scene;
      const standardMapRoot = standardMapGltf.scene || standardMapGltf;
      const hoopsMapRoot = hoopsMapGltf.scene || hoopsMapGltf;
      const rocketBoostSmallRoot = rocketBoostSmallGltf.scene || rocketBoostSmallGltf;
      const rocketBoostBigRoot = rocketBoostBigGltf.scene || rocketBoostBigGltf;

      return {
        arenaTemplate: prepareSceneTemplate(fieldRoot),
        stadiumTemplates: {
          standard: prepareMapTemplate(standardMapRoot, MAP_TRANSPARENT_MATERIALS.standard),
          hoops: prepareMapTemplate(hoopsMapRoot, MAP_TRANSPARENT_MATERIALS.hoops),
        },
        arenaShellTemplate: prepareObjTemplate(
          arenaShellObj,
          new THREE.MeshStandardMaterial({
            color: '#1b252d',
            roughness: 0.82,
            metalness: 0.08,
            emissive: '#102028',
            emissiveIntensity: 0.14,
            side: THREE.DoubleSide,
          })
        ),
        ballTemplate: prepareSceneTemplate(ballRoot),
        blueCarTemplate: prepareSceneTemplate(blueCarRoot),
        orangeCarTemplate: prepareSceneTemplate(orangeCarRoot),
        wheelTemplate: prepareSceneTemplate(wheelRoot),
        boostPadTemplates: {
          smallInactive: prepareBoostTemplate(rocketBoostSmallRoot, rocketBoostSprite, { active: false, fullBoost: false }),
          smallActive: prepareBoostTemplate(rocketBoostSmallRoot, rocketBoostSprite, { active: true, fullBoost: false }),
          bigInactive: prepareBoostTemplate(rocketBoostBigRoot, rocketBoostSprite, { active: false, fullBoost: true }),
          bigActive: prepareBoostTemplate(rocketBoostBigRoot, rocketBoostSprite, { active: true, fullBoost: true }),
          legacySmallInactive: prepareObjTemplate(
            boostSmallInactiveObj,
            new THREE.MeshStandardMaterial({
              map: boostTexture,
              transparent: true,
              alphaTest: 0.05,
              roughness: 0.38,
              metalness: 0.06,
              emissive: '#54492f',
              emissiveIntensity: 0.18,
              side: THREE.DoubleSide,
            })
          ),
          legacySmallActive: prepareObjTemplate(
            boostSmallActiveObj,
            new THREE.MeshStandardMaterial({
              map: boostTexture,
              transparent: true,
              alphaTest: 0.05,
              roughness: 0.26,
              metalness: 0.08,
              emissive: '#e0bc5a',
              emissiveIntensity: 0.56,
              side: THREE.DoubleSide,
            })
          ),
          legacyBigInactive: prepareObjTemplate(
            boostBigInactiveObj,
            new THREE.MeshStandardMaterial({
              map: boostTexture,
              transparent: true,
              alphaTest: 0.05,
              roughness: 0.38,
              metalness: 0.06,
              emissive: '#5f4a27',
              emissiveIntensity: 0.22,
              side: THREE.DoubleSide,
            })
          ),
          legacyBigActive: prepareObjTemplate(
            boostBigActiveObj,
            new THREE.MeshStandardMaterial({
              map: boostTexture,
              transparent: true,
              alphaTest: 0.05,
              roughness: 0.2,
              metalness: 0.08,
              emissive: '#ffd06b',
              emissiveIntensity: 0.7,
              side: THREE.DoubleSide,
            })
          ),
        },
      };
    })();
  }
  return viewerAssetCache.promise;
}

function arenaVariantFromMapCode(mapCode = '') {
  return /hoops/i.test(String(mapCode || '')) ? 'hoops' : 'standard';
}

export function buildArenaModel(assets, mapCode = '') {
  const group = new THREE.Group();
  const stadiumTemplate = assets.stadiumTemplates?.[arenaVariantFromMapCode(mapCode)];
  if (stadiumTemplate) {
    group.add(instantiateTemplate(stadiumTemplate));
  } else {
    group.add(instantiateTemplate(assets.arenaTemplate, FIELD_SCALE));
  }
  if (!stadiumTemplate && assets.arenaShellTemplate) {
    group.add(instantiateTemplate(assets.arenaShellTemplate, RL_SCENE_SCALE));
  }
  return group;
}

export function buildBallModel(assets) {
  return instantiateTemplate(assets.ballTemplate, BALL_SCALE);
}

export function buildBoostPadModel(assets, fullBoost, active = true) {
  const templates = assets.boostPadTemplates;
  const template = fullBoost
    ? (active ? templates.bigActive : templates.bigInactive)
    : (active ? templates.smallActive : templates.smallInactive);
  if (template) {
    return instantiateTemplate(template);
  }
  const legacyTemplate = fullBoost
    ? (active ? templates.legacyBigActive : templates.legacyBigInactive)
    : (active ? templates.legacySmallActive : templates.legacySmallInactive);
  return instantiateTemplate(legacyTemplate, LEGACY_BOOST_PAD_SCALE);
}

export function buildCarModel(assets, car) {
  const teamKey = Number(car?.team) === 1 ? 'orange' : 'blue';
  const shell = (teamKey === 'orange' ? assets.orangeCarTemplate : assets.blueCarTemplate).clone(true);
  shell.children.forEach((child) => {
    child.position.y += CAR_BODY_Y_OFFSET;
  });

  const group = new THREE.Group();
  group.add(shell);
  group.add(buildWheelGroup(assets));
  group.scale.setScalar(CAR_SCALE);
  group.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    child.castShadow = true;
    child.receiveShadow = true;
  });
  return group;
}
