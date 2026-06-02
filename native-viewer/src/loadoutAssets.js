import * as THREE from 'three';
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';

const DRACO_DECODER_PATH = '/viewer-assets/draco/gltf/';
const BODY_BLUE_PATH = '/viewer-assets/webreplayviewer/Octane_ZXR_Blue.glb';
const BODY_ORANGE_PATH = '/viewer-assets/webreplayviewer/Octane_ZXR_Orange.glb';
const WHEEL_PATH = '/viewer-assets/webreplayviewer/Wheel.glb';
const STL_ROTATION_X = -Math.PI / 2;

const EXACT_BODY_MESHES = {
  backfire: '/viewer-assets/printables-cars/backfire.stl',
  batmobile: '/viewer-assets/printables-cars/batmobile.stl',
  breakout: '/viewer-assets/printables-cars/breakout.stl',
  delorean: '/viewer-assets/printables-cars/delorean.stl',
  dominus: '/viewer-assets/printables-cars/dominus.stl',
  esper: '/viewer-assets/printables-cars/esper.stl',
  fennec: '/viewer-assets/printables-cars/fennec.stl',
  gizmo: '/viewer-assets/printables-cars/gizmo.stl',
  mantis: '/viewer-assets/printables-cars/mantis.stl',
  marauder: '/viewer-assets/printables-cars/marauder.stl',
  masamune: '/viewer-assets/printables-cars/masamune.stl',
  merc: '/viewer-assets/printables-cars/merc.stl',
  paladin: '/viewer-assets/printables-cars/paladin.stl',
  ripper: '/viewer-assets/printables-cars/ripper.stl',
  scarab: '/viewer-assets/printables-cars/scarab.stl',
  takumi: '/viewer-assets/printables-cars/takumi.stl',
  venom: '/viewer-assets/printables-cars/venom.stl',
  vulcan: '/viewer-assets/printables-cars/vulcan.stl',
};

const BODY_MESH_ALIASES = {
  breakout_type_s: 'breakout',
  dominus_gt: 'dominus',
  octane_zsr: 'octane',
  takumi_rxt: 'takumi',
};

const TEAM_COLORS = {
  blue: {
    body: '#4c92ff',
    accent: '#89c4ff',
    trim: '#101820',
    glass: '#9fd8ff',
  },
  orange: {
    body: '#f08c44',
    accent: '#ffd091',
    trim: '#1a120d',
    glass: '#ffd3ab',
  },
};

const shared = {
  gltfLoader: null,
  stlLoader: null,
  templatePromises: new Map(),
  modelCache: new Map(),
};

const BODY_VARIANT_BY_ID = {
  21: 'backfire',
  22: 'breakout',
  23: 'octane',
  24: 'paladin',
  25: 'roadhog',
  26: 'gizmo',
  28: 'xdevil',
  29: 'hotshot',
  30: 'merc',
  31: 'venom',
  402: 'takumi',
  403: 'dominus',
  404: 'scarab',
  597: 'delorean',
  600: 'ripper',
  607: 'marauder',
  803: 'batmobile',
  1018: 'dominus_gt',
  1159: 'xdevil_mk2',
  1171: 'masamune',
  1172: 'marauder',
  1286: 'dominus',
  1295: 'takumi_rxt',
  1300: 'roadhog_xl',
  1317: 'esper',
  1416: 'breakout_type_s',
  1475: 'hybrid',
  1478: 'hybrid',
  1533: 'dominus',
  1568: 'octane_zsr',
  1603: 'plank',
  1623: 'plank',
  1624: 'dominus',
  1675: 'ice_charger',
  1691: 'plank',
  1856: 'hybrid',
  1919: 'dominus',
  1932: 'dominus',
  2268: 'dominus',
  2269: 'skyline',
  4284: 'fennec',
};

const BODY_NAME_HINTS = [
  ['fennec', 'fennec'],
  ['octane zsr', 'octane_zsr'],
  ['octane', 'octane'],
  ['takumi rx-t', 'takumi_rxt'],
  ['takumi', 'takumi'],
  ['dominus gt', 'dominus_gt'],
  ['dominus', 'dominus'],
  ['breakout type-s', 'breakout_type_s'],
  ['breakout', 'breakout'],
  ['road hog xl', 'roadhog_xl'],
  ['road hog', 'roadhog'],
  ['merc', 'merc'],
  ['marauder', 'marauder'],
  ['batmobile', 'batmobile'],
  ['skyline', 'skyline'],
  ['delorean', 'delorean'],
  ['ice charger', 'ice_charger'],
  ['mantis', 'plank'],
  ['endo', 'dominus'],
  ['centio', 'dominus'],
  ['animus', 'dominus'],
  ['jager', 'hybrid'],
  ['jaeger', 'hybrid'],
  ['twin mill', 'plank'],
  ['bone shaker', 'plank'],
  ['aftershock', 'dominus'],
  ['x-devil mk2', 'xdevil_mk2'],
  ['x-devil', 'xdevil'],
  ['venom', 'venom'],
  ['masamune', 'masamune'],
  ['esper', 'esper'],
  ['ripper', 'ripper'],
  ['backfire', 'backfire'],
  ['hotshot', 'hotshot'],
  ['paladin', 'paladin'],
  ['proteus', 'hybrid'],
  ['triton', 'hybrid'],
  ['vulcan', 'dominus'],
  ['scarab', 'scarab'],
  ['charger', 'dominus'],
];

const VERIFIED_EXACT_BODY_MESHES = new Set([
  'octane',
]);

const BODY_VARIANTS = {
  octane: {
    source: 'octane_glb',
    wheel: { front: 80, rear: -80, track: 55, y: 0, scale: 1 },
    shellScale: [1, 1, 1],
    addOns: [],
  },
  octane_zsr: {
    source: 'octane_glb',
    wheel: { front: 81, rear: -79, track: 55, y: 0, scale: 1 },
    shellScale: [1.02, 0.96, 1.02],
    addOns: [
      { size: [58, 12, 60], position: [-4, 64, 0], slot: 'body' },
      { size: [26, 6, 56], position: [46, 42, 0], slot: 'accent' },
      { size: [18, 8, 74], position: [-72, 48, 0], slot: 'accent' },
    ],
  },
  takumi: {
    source: 'procedural',
    wheel: { front: 81, rear: -80, track: 54, y: 0, scale: 0.98 },
    parts: [
      { size: [144, 28, 80], position: [0, 38, 0], slot: 'body' },
      { size: [64, 18, 60], position: [-8, 58, 0], slot: 'body' },
      { size: [34, 10, 52], position: [42, 46, 0], slot: 'accent' },
      { size: [22, 10, 56], position: [-44, 43, 0], slot: 'accent' },
      { size: [48, 10, 52], position: [0, 66, 0], slot: 'glass', transparent: true, opacity: 0.42 },
      { size: [20, 8, 70], position: [-66, 46, 0], slot: 'trim' },
    ],
  },
  takumi_rxt: {
    source: 'procedural',
    wheel: { front: 82, rear: -80, track: 54, y: 0, scale: 0.98 },
    parts: [
      { size: [146, 28, 80], position: [0, 38, 0], slot: 'body' },
      { size: [66, 18, 62], position: [-8, 58, 0], slot: 'body' },
      { size: [36, 10, 56], position: [44, 45, 0], slot: 'accent' },
      { size: [18, 9, 72], position: [-72, 46, 0], slot: 'accent' },
      { size: [12, 14, 16], position: [8, 70, 0], slot: 'accent' },
      { size: [48, 10, 52], position: [0, 66, 0], slot: 'glass', transparent: true, opacity: 0.42 },
    ],
  },
  paladin: {
    source: 'procedural',
    wheel: { front: 79, rear: -78, track: 56, y: 0, scale: 1 },
    parts: [
      { size: [142, 30, 82], position: [0, 40, 0], slot: 'body' },
      { size: [74, 22, 68], position: [-4, 62, 0], slot: 'body' },
      { size: [28, 12, 72], position: [52, 49, 0], slot: 'body' },
      { size: [50, 12, 54], position: [0, 70, 0], slot: 'glass', transparent: true, opacity: 0.42 },
    ],
  },
  hotshot: {
    source: 'procedural',
    wheel: { front: 78, rear: -77, track: 56, y: 0, scale: 0.97 },
    parts: [
      { size: [140, 30, 80], position: [0, 39, 0], slot: 'body' },
      { size: [78, 24, 66], position: [0, 62, 0], slot: 'body' },
      { size: [42, 12, 50], position: [0, 74, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  fennec: {
    source: 'procedural',
    wheel: { front: 79, rear: -79, track: 56, y: 0, scale: 1 },
    parts: [
      { size: [146, 34, 84], position: [0, 42, 0], slot: 'body' },
      { size: [78, 24, 76], position: [-4, 65, 0], slot: 'body' },
      { size: [62, 12, 72], position: [-4, 81, 0], slot: 'body' },
      { size: [22, 14, 80], position: [64, 41, 0], slot: 'body' },
      { size: [18, 10, 76], position: [-66, 39, 0], slot: 'trim' },
      { size: [50, 14, 60], position: [2, 69, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  dominus: {
    source: 'procedural',
    wheel: { front: 89, rear: -88, track: 58, y: 0, scale: 0.98 },
    parts: [
      { size: [168, 22, 84], position: [0, 33, 0], slot: 'body' },
      { size: [78, 14, 64], position: [-10, 49, 0], slot: 'body' },
      { size: [36, 8, 78], position: [60, 39, 0], slot: 'accent' },
      { size: [44, 8, 82], position: [-58, 39, 0], slot: 'accent' },
      { size: [22, 6, 92], position: [-82, 47, 0], slot: 'trim' },
      { size: [46, 8, 50], position: [-4, 56, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  dominus_gt: {
    source: 'procedural',
    wheel: { front: 89, rear: -88, track: 58, y: 0, scale: 0.98 },
    parts: [
      { size: [168, 22, 84], position: [0, 33, 0], slot: 'body' },
      { size: [80, 14, 64], position: [-10, 49, 0], slot: 'body' },
      { size: [34, 8, 80], position: [62, 38, 0], slot: 'accent' },
      { size: [18, 10, 88], position: [-84, 45, 0], slot: 'accent' },
      { size: [10, 16, 16], position: [0, 59, 0], slot: 'accent' },
      { size: [46, 8, 50], position: [-4, 56, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  breakout: {
    source: 'procedural',
    wheel: { front: 90, rear: -84, track: 56, y: 0, scale: 0.97 },
    parts: [
      { size: [170, 22, 78], position: [0, 33, 0], slot: 'body' },
      { size: [72, 18, 58], position: [-14, 50, 0], slot: 'body' },
      { size: [38, 10, 54], position: [56, 41, 0], slot: 'accent' },
      { size: [18, 8, 84], position: [-84, 43, 0], slot: 'trim' },
      { size: [44, 10, 44], position: [-10, 59, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  breakout_type_s: {
    source: 'procedural',
    wheel: { front: 90, rear: -84, track: 56, y: 0, scale: 0.97 },
    parts: [
      { size: [170, 22, 78], position: [0, 33, 0], slot: 'body' },
      { size: [74, 18, 60], position: [-14, 50, 0], slot: 'body' },
      { size: [40, 10, 58], position: [56, 41, 0], slot: 'accent' },
      { size: [22, 8, 88], position: [-84, 44, 0], slot: 'accent' },
      { size: [44, 10, 44], position: [-10, 59, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  merc: {
    source: 'procedural',
    wheel: { front: 78, rear: -78, track: 58, y: 0, scale: 1.03 },
    parts: [
      { size: [142, 40, 88], position: [0, 45, 0], slot: 'body' },
      { size: [82, 30, 78], position: [-4, 74, 0], slot: 'body' },
      { size: [58, 18, 72], position: [18, 86, 0], slot: 'body' },
      { size: [42, 16, 80], position: [50, 62, 0], slot: 'body' },
      { size: [42, 14, 62], position: [10, 79, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  roadhog: {
    source: 'procedural',
    wheel: { front: 77, rear: -76, track: 59, y: 0, scale: 1.03 },
    parts: [
      { size: [146, 38, 90], position: [0, 44, 0], slot: 'body' },
      { size: [84, 28, 80], position: [-10, 70, 0], slot: 'body' },
      { size: [50, 16, 84], position: [48, 55, 0], slot: 'accent' },
      { size: [44, 12, 64], position: [4, 74, 0], slot: 'glass', transparent: true, opacity: 0.36 },
      { size: [18, 14, 90], position: [-72, 46, 0], slot: 'trim' },
    ],
  },
  roadhog_xl: {
    source: 'procedural',
    wheel: { front: 78, rear: -77, track: 59, y: 0, scale: 1.03 },
    parts: [
      { size: [152, 40, 92], position: [0, 45, 0], slot: 'body' },
      { size: [86, 30, 82], position: [-10, 72, 0], slot: 'body' },
      { size: [54, 16, 86], position: [50, 56, 0], slot: 'accent' },
      { size: [46, 12, 66], position: [4, 76, 0], slot: 'glass', transparent: true, opacity: 0.36 },
      { size: [22, 14, 94], position: [-76, 47, 0], slot: 'trim' },
    ],
  },
  marauder: {
    source: 'procedural',
    wheel: { front: 77, rear: -77, track: 59, y: 0, scale: 1.04 },
    parts: [
      { size: [146, 42, 90], position: [0, 45, 0], slot: 'body' },
      { size: [80, 32, 80], position: [-6, 76, 0], slot: 'body' },
      { size: [52, 16, 84], position: [48, 58, 0], slot: 'body' },
      { size: [44, 14, 62], position: [4, 80, 0], slot: 'glass', transparent: true, opacity: 0.34 },
    ],
  },
  plank: {
    source: 'procedural',
    wheel: { front: 92, rear: -92, track: 58, y: 0, scale: 0.96 },
    parts: [
      { size: [174, 18, 88], position: [0, 30, 0], slot: 'body' },
      { size: [64, 12, 64], position: [-8, 44, 0], slot: 'body' },
      { size: [44, 8, 90], position: [58, 35, 0], slot: 'accent' },
      { size: [26, 6, 94], position: [-84, 38, 0], slot: 'trim' },
      { size: [42, 8, 46], position: [-8, 49, 0], slot: 'glass', transparent: true, opacity: 0.34 },
    ],
  },
  batmobile: {
    source: 'procedural',
    wheel: { front: 93, rear: -93, track: 58, y: 0, scale: 0.95 },
    parts: [
      { size: [178, 18, 90], position: [0, 30, 0], slot: 'body' },
      { size: [58, 10, 60], position: [-16, 43, 0], slot: 'body' },
      { size: [28, 6, 100], position: [70, 35, 0], slot: 'accent' },
      { size: [24, 6, 100], position: [-88, 36, 0], slot: 'accent' },
      { size: [40, 8, 44], position: [-18, 48, 0], slot: 'glass', transparent: true, opacity: 0.34 },
    ],
  },
  skyline: {
    source: 'procedural',
    wheel: { front: 92, rear: -90, track: 58, y: 0, scale: 0.96 },
    parts: [
      { size: [172, 20, 88], position: [0, 31, 0], slot: 'body' },
      { size: [76, 16, 64], position: [-8, 48, 0], slot: 'body' },
      { size: [34, 8, 88], position: [60, 38, 0], slot: 'accent' },
      { size: [24, 8, 90], position: [-78, 41, 0], slot: 'trim' },
      { size: [48, 8, 46], position: [-4, 55, 0], slot: 'glass', transparent: true, opacity: 0.36 },
    ],
  },
  delorean: {
    source: 'procedural',
    wheel: { front: 90, rear: -90, track: 58, y: 0, scale: 0.96 },
    parts: [
      { size: [170, 20, 88], position: [0, 32, 0], slot: 'body' },
      { size: [72, 16, 68], position: [-10, 48, 0], slot: 'body' },
      { size: [24, 8, 86], position: [62, 38, 0], slot: 'accent' },
      { size: [24, 8, 86], position: [-76, 39, 0], slot: 'accent' },
      { size: [46, 8, 48], position: [-8, 55, 0], slot: 'glass', transparent: true, opacity: 0.34 },
    ],
  },
  ice_charger: {
    source: 'procedural',
    wheel: { front: 91, rear: -88, track: 57, y: 0, scale: 0.97 },
    parts: [
      { size: [170, 20, 86], position: [0, 32, 0], slot: 'body' },
      { size: [74, 16, 64], position: [-10, 49, 0], slot: 'body' },
      { size: [28, 8, 88], position: [60, 39, 0], slot: 'accent' },
      { size: [46, 8, 48], position: [-8, 56, 0], slot: 'glass', transparent: true, opacity: 0.34 },
    ],
  },
  hybrid: {
    source: 'procedural',
    wheel: { front: 84, rear: -84, track: 56, y: 0, scale: 0.99 },
    parts: [
      { size: [152, 28, 82], position: [0, 38, 0], slot: 'body' },
      { size: [72, 20, 68], position: [-6, 58, 0], slot: 'body' },
      { size: [30, 10, 72], position: [50, 44, 0], slot: 'accent' },
      { size: [24, 10, 74], position: [-50, 44, 0], slot: 'trim' },
      { size: [48, 10, 56], position: [-2, 66, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  xdevil: {
    source: 'procedural',
    wheel: { front: 84, rear: -83, track: 56, y: 0, scale: 0.99 },
    parts: [
      { size: [154, 28, 84], position: [0, 38, 0], slot: 'body' },
      { size: [74, 22, 68], position: [-2, 60, 0], slot: 'body' },
      { size: [30, 10, 74], position: [52, 44, 0], slot: 'accent' },
      { size: [28, 10, 76], position: [-50, 44, 0], slot: 'accent' },
      { size: [48, 10, 56], position: [0, 68, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  xdevil_mk2: {
    source: 'procedural',
    wheel: { front: 84, rear: -83, track: 56, y: 0, scale: 0.99 },
    parts: [
      { size: [154, 28, 84], position: [0, 38, 0], slot: 'body' },
      { size: [74, 22, 68], position: [-2, 60, 0], slot: 'body' },
      { size: [34, 10, 76], position: [54, 44, 0], slot: 'accent' },
      { size: [16, 12, 86], position: [-72, 46, 0], slot: 'accent' },
      { size: [48, 10, 56], position: [0, 68, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  venom: {
    source: 'procedural',
    wheel: { front: 84, rear: -83, track: 56, y: 0, scale: 0.99 },
    parts: [
      { size: [150, 28, 82], position: [0, 38, 0], slot: 'body' },
      { size: [70, 18, 62], position: [-6, 57, 0], slot: 'body' },
      { size: [34, 10, 72], position: [48, 43, 0], slot: 'accent' },
      { size: [48, 10, 52], position: [-4, 64, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  masamune: {
    source: 'procedural',
    wheel: { front: 84, rear: -84, track: 56, y: 0, scale: 0.98 },
    parts: [
      { size: [156, 26, 82], position: [0, 37, 0], slot: 'body' },
      { size: [72, 18, 62], position: [-8, 56, 0], slot: 'body' },
      { size: [26, 8, 82], position: [54, 42, 0], slot: 'accent' },
      { size: [44, 8, 46], position: [-8, 63, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  esper: {
    source: 'procedural',
    wheel: { front: 84, rear: -84, track: 56, y: 0, scale: 0.98 },
    parts: [
      { size: [154, 26, 82], position: [0, 37, 0], slot: 'body' },
      { size: [70, 18, 62], position: [-10, 55, 0], slot: 'body' },
      { size: [24, 8, 84], position: [54, 41, 0], slot: 'accent' },
      { size: [22, 12, 70], position: [-60, 45, 0], slot: 'trim' },
      { size: [44, 8, 46], position: [-10, 62, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  ripper: {
    source: 'procedural',
    wheel: { front: 84, rear: -84, track: 56, y: 0, scale: 1 },
    parts: [
      { size: [152, 30, 84], position: [0, 39, 0], slot: 'body' },
      { size: [72, 20, 66], position: [-6, 59, 0], slot: 'body' },
      { size: [34, 10, 76], position: [52, 45, 0], slot: 'accent' },
      { size: [18, 12, 80], position: [-70, 46, 0], slot: 'trim' },
      { size: [46, 10, 50], position: [-2, 66, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  gizmo: {
    source: 'procedural',
    wheel: { front: 80, rear: -79, track: 55, y: 0, scale: 0.98 },
    parts: [
      { size: [142, 34, 82], position: [0, 41, 0], slot: 'body' },
      { size: [70, 24, 72], position: [-4, 64, 0], slot: 'body' },
      { size: [46, 12, 52], position: [-4, 72, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  backfire: {
    source: 'procedural',
    wheel: { front: 79, rear: -78, track: 54, y: 0, scale: 0.97 },
    parts: [
      { size: [146, 26, 80], position: [0, 36, 0], slot: 'body' },
      { size: [70, 18, 58], position: [-8, 54, 0], slot: 'body' },
      { size: [34, 8, 72], position: [52, 42, 0], slot: 'accent' },
      { size: [42, 10, 46], position: [-8, 61, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
  scarab: {
    source: 'procedural',
    wheel: { front: 74, rear: -74, track: 54, y: 0, scale: 0.94 },
    parts: [
      { size: [118, 42, 86], position: [0, 46, 0], slot: 'body' },
      { size: [58, 24, 74], position: [0, 74, 0], slot: 'body' },
      { size: [36, 12, 46], position: [0, 83, 0], slot: 'glass', transparent: true, opacity: 0.38 },
    ],
  },
  default: {
    source: 'procedural',
    wheel: { front: 80, rear: -80, track: 55, y: 0, scale: 1 },
    parts: [
      { size: [148, 28, 82], position: [0, 38, 0], slot: 'body' },
      { size: [70, 18, 64], position: [-4, 58, 0], slot: 'body' },
      { size: [48, 10, 54], position: [-4, 66, 0], slot: 'glass', transparent: true, opacity: 0.4 },
    ],
  },
};

function normalizeName(value) {
  return String(value || '').trim().toLowerCase();
}

function teamKey(card) {
  return Number(card?.team || card?.is_orange || 0) === 1 ? 'orange' : 'blue';
}

function cacheKeyForCard(card) {
  return JSON.stringify({
    team: teamKey(card),
    carBodyId: Number(card?.car_body_id || card?.loadout?.car || 23),
    wheelId: Number(card?.loadout?.wheels || 376),
    carName: normalizeName(card?.car_name),
    carFamily: normalizeName(card?.car_family),
  });
}

function cloneWithUniqueMaterials(root) {
  const clone = root.clone(true);
  const materialCache = new Map();
  clone.traverse((node) => {
    if (!node?.isMesh || !node.material) return;
    if (Array.isArray(node.material)) {
      node.material = node.material.map((material) => {
        if (!material) return material;
        if (!materialCache.has(material.uuid)) materialCache.set(material.uuid, material.clone());
        return materialCache.get(material.uuid);
      });
      return;
    }
    if (!materialCache.has(node.material.uuid)) materialCache.set(node.material.uuid, node.material.clone());
    node.material = materialCache.get(node.material.uuid);
  });
  return clone;
}

function disposeGroup(root) {
  root?.traverse?.((node) => {
    if (!node?.isMesh) return;
    node.geometry?.dispose?.();
    if (Array.isArray(node.material)) node.material.forEach((material) => material?.dispose?.());
    else node.material?.dispose?.();
  });
}

function getGltfLoader() {
  if (shared.gltfLoader) return shared.gltfLoader;
  const dracoLoader = new DRACOLoader();
  dracoLoader.setDecoderPath(DRACO_DECODER_PATH);
  const gltfLoader = new GLTFLoader();
  gltfLoader.setDRACOLoader(dracoLoader);
  shared.gltfLoader = gltfLoader;
  return shared.gltfLoader;
}

function getStlLoader() {
  if (shared.stlLoader) return shared.stlLoader;
  shared.stlLoader = new STLLoader();
  return shared.stlLoader;
}

async function loadGltfTemplate(url) {
  if (!shared.templatePromises.has(url)) {
    shared.templatePromises.set(
      url,
      getGltfLoader().loadAsync(url).then((gltf) => gltf.scene),
    );
  }
  return shared.templatePromises.get(url);
}

function normalizeStlTemplate(geometry) {
  const normalized = geometry.clone();
  normalized.computeVertexNormals();
  normalized.rotateX(STL_ROTATION_X);
  normalized.computeBoundingBox();
  const bounds = normalized.boundingBox.clone();
  const center = bounds.getCenter(new THREE.Vector3());
  normalized.translate(-center.x, -bounds.min.y, -center.z);
  normalized.computeBoundingBox();
  normalized.computeVertexNormals();

  const mesh = new THREE.Mesh(
    normalized,
    new THREE.MeshStandardMaterial({
      color: '#ffffff',
      roughness: 0.36,
      metalness: 0.18,
    }),
  );
  mesh.name = 'native-body-core';
  mesh.castShadow = true;
  mesh.receiveShadow = true;

  const group = new THREE.Group();
  group.name = 'native-body-template';
  group.userData.sourceSize = normalized.boundingBox.getSize(new THREE.Vector3()).toArray();
  group.add(mesh);
  return group;
}

async function loadStlTemplate(url) {
  const key = `stl:${url}`;
  if (!shared.templatePromises.has(key)) {
    shared.templatePromises.set(
      key,
      getStlLoader().loadAsync(url).then((geometry) => normalizeStlTemplate(geometry)),
    );
  }
  return shared.templatePromises.get(key);
}

function findNamedChild(root, name) {
  return root?.getObjectByName?.(name) || null;
}

function variantMeshKey(variantKey) {
  return BODY_MESH_ALIASES[variantKey] || variantKey;
}

function resolveVariantKey(card) {
  const rawBodyId = Number(card?.car_body_id || card?.loadout?.car);
  if (Number.isFinite(rawBodyId) && BODY_VARIANT_BY_ID[rawBodyId]) {
    return BODY_VARIANT_BY_ID[rawBodyId];
  }

  const carName = normalizeName(card?.car_name);
  if (carName) {
    for (const [hint, variant] of BODY_NAME_HINTS) {
      if (carName.includes(hint)) return variant;
    }
  }

  const family = normalizeName(card?.car_family);
  if (family && BODY_VARIANTS[family]) return family;
  return 'default';
}

function shouldPreferStableOctaneFallback(variantKey, meshKey) {
  if (variantKey === 'octane' || meshKey === 'octane') return false;
  return !VERIFIED_EXACT_BODY_MESHES.has(meshKey);
}

function createMaterialSet(isOrange) {
  const palette = TEAM_COLORS[isOrange ? 'orange' : 'blue'];
  return {
    body: new THREE.MeshStandardMaterial({
      color: palette.body,
      roughness: 0.38,
      metalness: 0.16,
    }),
    accent: new THREE.MeshStandardMaterial({
      color: palette.accent,
      roughness: 0.24,
      metalness: 0.3,
    }),
    trim: new THREE.MeshStandardMaterial({
      color: palette.trim,
      roughness: 0.62,
      metalness: 0.08,
    }),
    glass: new THREE.MeshPhysicalMaterial({
      color: palette.glass,
      roughness: 0.08,
      metalness: 0.08,
      transmission: 0.18,
      transparent: true,
      opacity: 0.42,
      clearcoat: 0.2,
      thickness: 0.4,
    }),
  };
}

function createSolidBodyMaterial(isOrange) {
  const palette = TEAM_COLORS[isOrange ? 'orange' : 'blue'];
  return new THREE.MeshStandardMaterial({
    color: palette.body,
    roughness: 0.34,
    metalness: 0.2,
    emissive: new THREE.Color(palette.trim),
    emissiveIntensity: 0.06,
    side: THREE.DoubleSide,
  });
}

function createPartMesh(part, materials) {
  const geometry = new RoundedBoxGeometry(
    part.size[0],
    part.size[1],
    part.size[2],
    part.segments || 4,
    part.radius || 4,
  );
  const slot = materials[part.slot] ? part.slot : 'body';
  const material = materials[slot].clone();
  if (part.transparent) {
    material.transparent = true;
    material.opacity = part.opacity ?? 0.42;
    material.depthWrite = false;
  }
  if (part.roughness != null) material.roughness = part.roughness;
  if (part.metalness != null) material.metalness = part.metalness;
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(...part.position);
  if (part.rotation) {
    mesh.rotation.set(
      THREE.MathUtils.degToRad(part.rotation[0] || 0),
      THREE.MathUtils.degToRad(part.rotation[1] || 0),
      THREE.MathUtils.degToRad(part.rotation[2] || 0),
    );
  }
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function buildWheelSet(wheelTemplate, variant) {
  const wheel = variant.wheel || BODY_VARIANTS.default.wheel;
  const group = new THREE.Group();
  const placements = [
    { name: 'Front Left', position: [wheel.front, wheel.y || 0, -wheel.track], mirror: false },
    { name: 'Front Right', position: [wheel.front, wheel.y || 0, wheel.track], mirror: true },
    { name: 'Back Left', position: [wheel.rear, wheel.y || 0, -wheel.track], mirror: false },
    { name: 'Back Right', position: [wheel.rear, wheel.y || 0, wheel.track], mirror: true },
  ];

  placements.forEach((placement) => {
    const wheelMesh = cloneWithUniqueMaterials(wheelTemplate);
    wheelMesh.name = placement.name;
    wheelMesh.position.set(...placement.position);
    wheelMesh.scale.setScalar(wheel.scale || 1);
    if (placement.mirror) wheelMesh.scale.z *= -1;
    wheelMesh.traverse((node) => {
      if (!node?.isMesh) return;
      node.castShadow = true;
      node.receiveShadow = true;
    });
    group.add(wheelMesh);
  });

  return group;
}

function buildProceduralBody(variant, isOrange) {
  const root = new THREE.Group();
  root.name = 'native-body-shell';
  const materials = createMaterialSet(isOrange);
  (variant.parts || BODY_VARIANTS.default.parts).forEach((part, index) => {
    const mesh = createPartMesh(part, materials);
    mesh.name = `native-body-part-${index}`;
    root.add(mesh);
  });
  return root;
}

function variantTargetMetrics(variant) {
  const parts = variant.parts || BODY_VARIANTS.default.parts || [];
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  let minZ = Infinity;
  let maxZ = -Infinity;

  parts.forEach((part) => {
    const [sizeX, sizeY, sizeZ] = part.size || [0, 0, 0];
    const [posX, posY, posZ] = part.position || [0, 0, 0];
    minX = Math.min(minX, posX - sizeX / 2);
    maxX = Math.max(maxX, posX + sizeX / 2);
    minY = Math.min(minY, posY - sizeY / 2);
    maxY = Math.max(maxY, posY + sizeY / 2);
    minZ = Math.min(minZ, posZ - sizeZ / 2);
    maxZ = Math.max(maxZ, posZ + sizeZ / 2);
  });

  if (!Number.isFinite(minX)) {
    return { length: 148, height: 48, width: 82, lift: 22 };
  }

  return {
    length: maxX - minX,
    height: maxY - minY,
    width: maxZ - minZ,
    lift: Math.max(18, -minY),
  };
}

function computeStlScale(sourceSize, target) {
  const sourceLength = Number(sourceSize?.[0] || 0);
  const sourceHeight = Number(sourceSize?.[1] || 0);
  const sourceWidth = Number(sourceSize?.[2] || 0);
  if (sourceLength <= 0 || sourceHeight <= 0 || sourceWidth <= 0) return 1;
  const lengthRatio = target.length / sourceLength;
  const heightRatio = target.height / sourceHeight;
  const widthRatio = target.width / sourceWidth;
  const blended = (lengthRatio * 0.45) + (widthRatio * 0.35) + (heightRatio * 0.2);
  return THREE.MathUtils.clamp(blended, 0.88, 1.28);
}

function applyStlVariant(template, variant, isOrange) {
  const wrapper = new THREE.Group();
  wrapper.name = 'native-body-shell';
  const body = cloneWithUniqueMaterials(template);
  const sourceSize = body.userData.sourceSize || template.userData.sourceSize;
  const target = variantTargetMetrics(variant);
  const scale = computeStlScale(sourceSize, target);
  const material = createSolidBodyMaterial(isOrange);
  body.scale.setScalar(scale);
  body.position.y += target.lift;
  body.traverse((node) => {
    if (!node?.isMesh) return;
    if (Array.isArray(node.material)) node.material.forEach((part) => part?.dispose?.());
    else node.material?.dispose?.();
    node.material = material.clone();
    node.castShadow = true;
    node.receiveShadow = true;
    node.frustumCulled = false;
    node.geometry?.computeBoundingBox?.();
    node.geometry?.computeBoundingSphere?.();
  });
  wrapper.add(body);
  return wrapper;
}

function applyGlbVariant(body, variant, isOrange) {
  const wrapper = new THREE.Group();
  wrapper.name = 'native-body-shell';
  const shellScale = variant.shellScale || [1, 1, 1];
  body.name = 'native-body-core';
  body.scale.set(shellScale[0], shellScale[1], shellScale[2]);
  body.position.y += variant.shellLift || 0;
  body.traverse((node) => {
    if (!node?.isMesh) return;
    node.castShadow = true;
    node.receiveShadow = true;
    node.frustumCulled = false;
    if (Array.isArray(node.material)) {
      node.material.forEach((material) => {
        if (!material) return;
        material.side = THREE.DoubleSide;
      });
    } else if (node.material) {
      node.material.side = THREE.DoubleSide;
    }
  });
  wrapper.add(body);

  if (variant.addOns?.length) {
    const materials = createMaterialSet(isOrange);
    variant.addOns.forEach((part, index) => {
      const mesh = createPartMesh(part, materials);
      mesh.name = `native-addon-${index}`;
      wrapper.add(mesh);
    });
  }

  return wrapper;
}

async function loadModelForCard(card) {
  const isOrange = teamKey(card) === 'orange';
  const wheelScene = await loadGltfTemplate(WHEEL_PATH);
  const wheelSource = findNamedChild(wheelScene, 'Wheel') || wheelScene;
  const finalVariant = BODY_VARIANTS.octane;

  const modelRoot = new THREE.Group();
  modelRoot.name = 'native-loadout-car';
  modelRoot.userData.nativeVariantKey = 'octane';

  try {
    const bodyTemplate = await loadGltfTemplate(isOrange ? BODY_ORANGE_PATH : BODY_BLUE_PATH);
    const bodySource = findNamedChild(bodyTemplate, 'Octane') || bodyTemplate;
    const body = cloneWithUniqueMaterials(bodySource);
    modelRoot.add(applyGlbVariant(body, finalVariant, isOrange));
  } catch {
    const bodyTemplate = await loadGltfTemplate(isOrange ? BODY_ORANGE_PATH : BODY_BLUE_PATH);
    const bodySource = findNamedChild(bodyTemplate, 'Octane') || bodyTemplate;
    const body = cloneWithUniqueMaterials(bodySource);
    modelRoot.add(applyGlbVariant(body, finalVariant, isOrange));
    modelRoot.userData.nativeVariantKey = 'octane-fallback';
  }

  modelRoot.add(buildWheelSet(wheelSource, finalVariant));

  return {
    scene: modelRoot,
    dispose() {
      disposeGroup(modelRoot);
    },
  };
}

export async function cloneLoadoutCarModel(card) {
  const key = cacheKeyForCard(card);
  if (!shared.modelCache.has(key)) {
    shared.modelCache.set(key, loadModelForCard(card));
  }
  const model = await shared.modelCache.get(key);
  const clonedScene = cloneWithUniqueMaterials(model.scene);
  return {
    scene: clonedScene,
    dispose() {
      disposeGroup(clonedScene);
    },
  };
}
