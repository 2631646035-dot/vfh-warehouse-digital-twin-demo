import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  zone: 'A',
  lastWarehouseZone: 'A',
  activeScreen: 'warehouse',
  zoneSettings: {},
  vfhScenario: null,
  vfhAnalysis: null,
  skus: [],
  stocks: [],
  simulation: null,
  playing: false,
  simMinute: 0,
  lastFrame: performance.now(),
  interactive: [],
  forklifts: [],
  workers: [],
  networkAnimated: [],
  selected: null,
};

const integerFields = new Set([
  'shelf_levels', 'workers', 'forklifts', 'inbound_pallets_day', 'outbound_orders_day',
]);

const colors = {
  background: 0x07111b,
  floor: 0x142231,
  floorEdge: 0x32536c,
  rack: 0x507089,
  beam: 0x7f9caf,
  aisle: 0x183549,
  cyan: 0x19d3c5,
  amber: 0xffb648,
  red: 0xff6f75,
};

const categoryPalette = [
  0x2fc7bc, 0xf6b94d, 0x5f9df7, 0xa579ff, 0xff7b7b, 0x69d07f, 0xdd74bf, 0x8cc8e8,
];

let scene;
let camera;
let renderer;
let controls;
let raycaster;
let pointer;
let dynamicRoot;
let clock;
let toastTimer;
let rebuildTimer;
let palletPreview = null;

function formatNumber(value, maximumFractionDigits = 0) {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits }).format(Number(value || 0));
}

function formatMoney(value, currency = state.vfhScenario?.currency || 'EUR', compact = false) {
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency, notation: compact ? 'compact' : 'standard',
    maximumFractionDigits: compact ? 1 : 0,
  }).format(Number(value || 0));
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function showToast(message, kind = 'success') {
  const toast = $('#toast');
  toast.textContent = message;
  toast.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 2800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const error = await response.json();
      message = typeof error.detail === 'string' ? error.detail : message;
    } catch (_) {
      // The status text above is sufficient when the server has no JSON body.
    }
    throw new Error(message);
  }
  return response.json();
}

function currentSettings() {
  const result = {};
  $$('[data-field]').forEach((input) => {
    const key = input.dataset.field;
    const value = Number(input.value);
    result[key] = integerFields.has(key) ? Math.round(value) : value;
  });
  return result;
}

function validateSettings(settings) {
  if (settings.floor_percent + settings.aisle_percent > 82) {
    throw new Error('地堆与过道占比之和不能超过 82%');
  }
  return settings;
}

function setForm(settings) {
  $$('[data-field]').forEach((input) => {
    const value = settings[input.dataset.field];
    if (value !== undefined) input.value = value;
  });
  updateOutputs();
}

function updateOutputs() {
  $$('[data-output]').forEach((node) => {
    const input = $(`[data-field="${node.dataset.output}"]`);
    node.textContent = `${formatNumber(input?.value)}%`;
  });
  const settings = currentSettings();
  const floor = Math.max(0, settings.floor_percent || 0);
  const aisle = Math.max(0, settings.aisle_percent || 0);
  const rack = Math.max(0, 100 - floor - aisle);
  $('.rack-share').style.width = `${rack}%`;
  $('.floor-share').style.width = `${floor}%`;
  $('.aisle-share').style.width = `${aisle}%`;
}

function categoryColor(category = '') {
  let hash = 0;
  for (const char of category) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return categoryPalette[Math.abs(hash) % categoryPalette.length];
}

function zoneSkus() {
  const zoneCodes = new Set(state.stocks.filter((item) => item.zone === state.zone).map((item) => item.code));
  return state.skus.filter((item) => zoneCodes.has(item.code));
}

function skuWithStock(sku) {
  const stock = state.stocks.find((item) => item.zone === state.zone && item.code === sku.code);
  return { ...sku, qty: stock?.qty || 0 };
}

function createTextSprite(text, tone = '#93adc0') {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 96;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = '600 30px Inter, Arial, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = tone;
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, depthTest: false, fog: false }));
  sprite.renderOrder = 20;
  sprite.scale.set(12, 2.25, 1);
  return sprite;
}

function material(color, roughness = 0.62, metalness = 0.08) {
  return new THREE.MeshStandardMaterial({ color, roughness, metalness });
}

function box(width, height, depth, mat) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, height, depth), mat);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function addLineBox(width, height, depth, color = 0x4d718a) {
  const geometry = new THREE.EdgesGeometry(new THREE.BoxGeometry(width, height, depth));
  return new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.55 }));
}

function disposeObject(root) {
  root.traverse((object) => {
    if (object.geometry) object.geometry.dispose();
    if (object.material) {
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      materials.forEach((item) => {
        if (item.map) item.map.dispose();
        item.dispose();
      });
    }
  });
}

function initScene() {
  const container = $('#scene-container');
  scene = new THREE.Scene();
  scene.background = new THREE.Color(colors.background);
  scene.fog = new THREE.FogExp2(colors.background, 0.009);

  camera = new THREE.PerspectiveCamera(42, 1, 0.1, 500);
  camera.position.set(58, 47, 66);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  container.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.075;
  controls.maxPolarAngle = Math.PI * 0.48;
  controls.minDistance = 18;
  controls.maxDistance = 160;
  controls.target.set(0, 1.5, 0);

  const hemi = new THREE.HemisphereLight(0x9ed9ff, 0x101923, 1.9);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xeaf7ff, 3.2);
  sun.position.set(-28, 52, 26);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.camera.left = -75;
  sun.shadow.camera.right = 75;
  sun.shadow.camera.top = 75;
  sun.shadow.camera.bottom = -75;
  scene.add(sun);
  const fill = new THREE.PointLight(colors.cyan, 1.2, 95);
  fill.position.set(32, 16, -28);
  scene.add(fill);

  dynamicRoot = new THREE.Group();
  scene.add(dynamicRoot);
  raycaster = new THREE.Raycaster();
  pointer = new THREE.Vector2();
  clock = new THREE.Clock();

  let pointerDown = null;
  renderer.domElement.addEventListener('pointerdown', (event) => {
    pointerDown = { x: event.clientX, y: event.clientY };
  });
  renderer.domElement.addEventListener('pointerup', (event) => {
    if (!pointerDown || Math.hypot(event.clientX - pointerDown.x, event.clientY - pointerDown.y) > 5) return;
    selectFromPointer(event);
  });

  new ResizeObserver(() => resizeScene()).observe(container);
  resizeScene();
  animate();
}

function resizeScene() {
  const container = $('#scene-container');
  const width = Math.max(1, container.clientWidth);
  const height = Math.max(1, container.clientHeight);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function resetView() {
  if (state.zone === 'ALL') {
    camera.position.set(82, 68, 102);
    controls.target.set(0, 2, 0);
    controls.maxDistance = 240;
    controls.update();
    return;
  }
  const settings = currentSettings();
  const span = Math.max(settings.warehouse_width_m || 60, settings.warehouse_depth_m || 72);
  camera.position.set(span * 0.76, span * 0.62, span * 0.88);
  controls.target.set(0, 1.6, 0);
  controls.maxDistance = 160;
  controls.update();
}

function createBoundary(width, depth) {
  const boundary = new THREE.Group();
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x315064, roughness: 0.65, transparent: true, opacity: 0.42 });
  const longWall = box(width, 1.15, 0.16, wallMat);
  longWall.position.set(0, 0.58, -depth / 2);
  boundary.add(longWall);
  for (const side of [-1, 1]) {
    const wall = box(0.16, 1.15, depth, wallMat.clone());
    wall.position.set(side * width / 2, 0.58, 0);
    boundary.add(wall);
  }
  return boundary;
}

function createFloor(width, depth, settings) {
  const foundation = box(width + 1.2, 0.34, depth + 1.2, material(0x0d1924, 0.9, 0));
  foundation.position.y = -0.22;
  dynamicRoot.add(foundation);

  const plane = box(width, 0.14, depth, material(colors.floor, 0.82, 0.02));
  plane.position.y = 0;
  dynamicRoot.add(plane, createBoundary(width, depth));

  const grid = new THREE.GridHelper(Math.max(width, depth), Math.round(Math.max(width, depth) / 2), 0x29485e, 0x172d3e);
  grid.position.y = 0.09;
  grid.scale.x = width / Math.max(width, depth);
  grid.scale.z = depth / Math.max(width, depth);
  grid.material.opacity = 0.42;
  grid.material.transparent = true;
  dynamicRoot.add(grid);

  const aisleWidth = Math.max(3.2, width * settings.aisle_percent / 100 * 0.3);
  const aisle = box(aisleWidth, 0.04, depth - 3, material(colors.aisle, 0.9, 0));
  aisle.position.set(0, 0.1, 0);
  dynamicRoot.add(aisle);

  const dashMat = material(0xd5b76a, 0.7, 0);
  for (let z = -depth / 2 + 3; z < depth / 2 - 2; z += 4.2) {
    for (const side of [-1, 1]) {
      const dash = box(0.09, 0.045, 1.9, dashMat);
      dash.position.set(side * aisleWidth / 2, 0.13, z);
      dash.castShadow = false;
      dynamicRoot.add(dash);
    }
  }

  const dock = box(Math.min(width * 0.44, 28), 0.07, 4.3, material(0x273948, 0.8, 0));
  dock.position.set(0, 0.13, depth / 2 - 2.3);
  dock.userData = { type: 'dock', name: '收发货月台', status: '作业缓冲区' };
  state.interactive.push(dock);
  dynamicRoot.add(dock);
  for (let x = -dock.geometry.parameters.width / 2 + 1.5; x < dock.geometry.parameters.width / 2; x += 3) {
    const stripe = box(1.5, 0.04, 0.18, material(colors.amber, 0.7, 0));
    stripe.position.set(x, 0.18, depth / 2 - 2.3);
    dynamicRoot.add(stripe);
  }
}

function createRackBay(id, x, z, settings, sku, rotation = 0) {
  const group = new THREE.Group();
  const width = settings.shelf_width_m;
  const depth = settings.shelf_depth_m;
  const levelHeight = settings.shelf_height_m;
  const levels = settings.shelf_levels;
  const totalHeight = levels * levelHeight + 0.35;
  group.position.set(x, 0, z);
  group.rotation.y = rotation;
  const occupancySeed = [...String(id)].reduce((total, char) => total + char.charCodeAt(0), 0);
  group.userData = {
    type: 'rack', id, levels, width, depth, totalHeight: levels * levelHeight,
    sku: sku?.code || null, occupancy: sku ? 68 + (occupancySeed % 28) : 0,
  };

  const uprightMat = material(colors.rack, 0.34, 0.58);
  const beamMat = material(colors.beam, 0.4, 0.42);
  for (const px of [-width / 2, width / 2]) {
    for (const pz of [-depth / 2, depth / 2]) {
      const post = box(0.085, totalHeight, 0.085, uprightMat);
      post.position.set(px, totalHeight / 2, pz);
      post.userData = group.userData;
      group.add(post);
      state.interactive.push(post);
    }
  }

  for (let level = 0; level <= levels; level += 1) {
    const y = 0.16 + level * levelHeight;
    for (const pz of [-depth / 2, depth / 2]) {
      const beam = box(width + 0.12, 0.09, 0.09, beamMat);
      beam.position.set(0, y, pz);
      beam.userData = group.userData;
      group.add(beam);
      state.interactive.push(beam);
    }
    if (level < levels && sku && ((level + id) % 4 !== 0)) {
      const cargoMat = material(categoryColor(sku.category), 0.68, 0.02);
      const cargo = box(width * 0.77, Math.min(levelHeight * 0.58, 1.05), depth * 0.74, cargoMat);
      cargo.position.set(0, y + Math.min(levelHeight * 0.31, 0.58), 0);
      cargo.userData = { type: 'sku', ...skuWithStock(sku), location: `${id}-${level + 1}层` };
      cargo.add(addLineBox(width * 0.77, Math.min(levelHeight * 0.58, 1.05), depth * 0.74, 0xc5e7ee));
      group.add(cargo);
      state.interactive.push(cargo);
    }
  }
  dynamicRoot.add(group);
}

function createPallet(id, x, z, sku, rotation = 0) {
  const group = new THREE.Group();
  group.position.set(x, 0.1, z);
  group.rotation.y = rotation;
  const length = Math.min(1.7, Math.max(0.8, Number(sku?.pallet_length_cm || 120) / 100));
  const width = Math.min(1.35, Math.max(0.6, Number(sku?.pallet_width_cm || 80) / 100));
  const pallet = box(length, 0.13, width, material(0x956a3f, 0.85, 0));
  pallet.position.y = 0.08;
  pallet.userData = { type: 'pallet', id, sku: sku?.code || null, units: sku?.units_per_pallet || 0 };
  state.interactive.push(pallet);
  group.add(pallet);
  if (sku) {
    const stackHeight = Math.min(2.5, Math.max(0.45, Number(sku.max_stack_height_cm || 160) / 100));
    const cargo = box(length * 0.88, stackHeight, width * 0.86, material(categoryColor(sku.category), 0.72, 0.02));
    cargo.position.y = 0.19 + stackHeight / 2;
    cargo.userData = { type: 'sku', ...skuWithStock(sku), location: `地堆 ${id}` };
    cargo.add(addLineBox(length * 0.88, stackHeight, width * 0.86, 0xd5eff2));
    state.interactive.push(cargo);
    group.add(cargo);
  }
  dynamicRoot.add(group);
}

function createForklift(id, index, settings) {
  const group = new THREE.Group();
  const body = box(1.25, 0.85, 1.7, material(colors.amber, 0.38, 0.25));
  body.position.y = 0.62;
  const cabin = box(1.06, 1.25, 0.9, material(0x2b414f, 0.3, 0.22));
  cabin.position.set(0, 1.35, 0.16);
  const mastMat = material(0xa4b5bd, 0.28, 0.68);
  for (const x of [-0.47, 0.47]) {
    const mast = box(0.09, 2.45, 0.09, mastMat);
    mast.position.set(x, 1.25, -1.0);
    group.add(mast);
  }
  const fork = box(0.92, 0.07, 1.3, mastMat);
  fork.position.set(0, 0.18, -1.47);
  group.add(body, cabin, fork);
  group.userData = {
    type: 'forklift', id: `FL-${String(id).padStart(2, '0')}`,
    efficiency: settings.forklift_efficiency, status: '待命', index,
  };
  group.traverse((child) => {
    if (child.isMesh) {
      child.userData = group.userData;
      state.interactive.push(child);
    }
  });
  dynamicRoot.add(group);
  state.forklifts.push(group);
}

function createWorker(id, index, settings) {
  const group = new THREE.Group();
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.23, 0.72, 4, 8), material(0x52a9ef, 0.56, 0.05));
  body.position.y = 0.75;
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.22, 14, 10), material(0xe6b691, 0.8, 0));
  head.position.y = 1.45;
  group.add(body, head);
  group.userData = {
    type: 'worker', id: `WK-${String(id).padStart(2, '0')}`,
    efficiency: settings.labor_efficiency, status: '巡检', index,
  };
  group.traverse((child) => {
    if (child.isMesh) {
      child.castShadow = true;
      child.userData = group.userData;
      state.interactive.push(child);
    }
  });
  dynamicRoot.add(group);
  state.workers.push(group);
}

function clearDynamicScene() {
  if (!dynamicRoot) return;
  disposeObject(dynamicRoot);
  dynamicRoot.clear();
  state.interactive = [];
  state.forklifts = [];
  state.workers = [];
  state.networkAnimated = [];
  state.selected = null;
  showObjectDetails(null);
}

function rebuildWarehouse(settings = currentSettings()) {
  if (!dynamicRoot) return;
  clearDynamicScene();
  scene.fog.density = 0.009;

  const width = settings.warehouse_width_m;
  const depth = settings.warehouse_depth_m;
  createFloor(width, depth, settings);

  const safetyMargin = 1.35;
  const separationGap = 0.9;
  const centralAisle = Math.max(3.2, width * settings.aisle_percent / 100 * 0.3);
  const floorZoneWidth = settings.floor_percent > 0 ? Math.min(width * 0.34, Math.max(5, width * settings.floor_percent / 100)) : 0;
  const floorMinX = width / 2 - safetyMargin - floorZoneWidth;
  const rightRackMaxX = floorZoneWidth > 0 ? floorMinX - separationGap : width / 2 - safetyMargin;
  const rackPitchX = settings.shelf_depth_m * 2 + 2.25;
  const rackPitchZ = settings.shelf_width_m + 0.55;
  const rackStartZ = -depth / 2 + 2.5 + settings.shelf_width_m / 2;
  const rackEndZ = depth / 2 - 5.3 - settings.shelf_width_m / 2;
  const bays = Math.max(1, Math.min(14, Math.floor((rackEndZ - rackStartZ) / rackPitchZ) + 1));
  const realSkus = zoneSkus();
  let rackIndex = 0;

  for (const side of [-1, 1]) {
    const minX = side < 0 ? -width / 2 + safetyMargin : centralAisle / 2 + separationGap;
    const maxX = side < 0 ? -centralAisle / 2 - separationGap : rightRackMaxX;
    const usableWidth = Math.max(0, maxX - minX);
    const columns = Math.max(0, Math.min(4, Math.floor((usableWidth - settings.shelf_depth_m) / rackPitchX) + 1));
    for (let col = 0; col < columns; col += 1) {
      const x = side < 0
        ? maxX - settings.shelf_depth_m / 2 - col * rackPitchX
        : minX + settings.shelf_depth_m / 2 + col * rackPitchX;
      for (let row = 0; row < bays; row += 1) {
        const z = rackStartZ + row * rackPitchZ;
        const sku = realSkus.length ? realSkus[rackIndex % realSkus.length] : null;
        createRackBay(`R${side < 0 ? 'L' : 'R'}-${col + 1}-${row + 1}`, x, z, settings, sku, Math.PI / 2);
        rackIndex += 1;
      }
    }
  }

  if (floorZoneWidth > 0) {
    const zoneCenterX = (floorMinX + width / 2 - safetyMargin) / 2;
    const zonePlate = box(Math.max(1, floorZoneWidth), 0.035, depth - 9.4, material(0x29322f, 0.9, 0));
    zonePlate.position.set(zoneCenterX, 0.105, -0.4);
    dynamicRoot.add(zonePlate);
    const maxPalletFootprint = 1.72;
    const palletPitchX = maxPalletFootprint + 0.38;
    const palletPitchZ = maxPalletFootprint + 0.62;
    const usableFloorWidth = Math.max(0, floorZoneWidth - 0.7);
    const palletCols = Math.max(1, Math.min(8, Math.floor((usableFloorWidth + 0.38) / palletPitchX)));
    const floorStartX = floorMinX + 0.35 + maxPalletFootprint / 2;
    const floorStartZ = -depth / 2 + 4.5 + maxPalletFootprint / 2;
    const floorEndZ = depth / 2 - 5.1 - maxPalletFootprint / 2;
    const palletRows = Math.max(1, Math.min(14, Math.floor((floorEndZ - floorStartZ) / palletPitchZ) + 1));
    let palletIndex = 0;
    for (let row = 0; row < palletRows; row += 1) {
      for (let col = 0; col < palletCols; col += 1) {
        const sku = realSkus.length ? realSkus[(palletIndex * 3) % realSkus.length] : null;
        const x = floorStartX + col * palletPitchX;
        const z = floorStartZ + row * palletPitchZ;
        createPallet(`P-${palletIndex + 1}`, x, z, sku, (row % 2) * Math.PI / 2);
        palletIndex += 1;
      }
    }
  }

  const zoneLabel = createTextSprite(state.zone === 'A' ? 'A区 / OWNER' : 'B区 / FORWARD HUB', '#6ce5db');
  zoneLabel.position.set(-width / 2 + 7, 0.7, -depth / 2 + 1.25);
  zoneLabel.rotation.x = -Math.PI / 2;
  dynamicRoot.add(zoneLabel);

  const shownForklifts = Math.min(settings.forklifts, 8);
  for (let index = 0; index < shownForklifts; index += 1) createForklift(index + 1, index, settings);
  const shownWorkers = Math.min(settings.workers, 18);
  for (let index = 0; index < shownWorkers; index += 1) createWorker(index + 1, index, settings);
  positionResources(0, settings);
}

function makeInteractive(root, data) {
  root.traverse((child) => {
    if (child.isMesh) {
      child.userData = data;
      state.interactive.push(child);
    }
  });
  root.userData = data;
  return root;
}

function createContainerStack(parent, x, z, columns = 3, levels = 2, scale = 1, baseY = 0) {
  const containerColors = [0x2aa99f, 0xc76a43, 0x416f9b, 0xd2a13d, 0x6a7180];
  for (let level = 0; level < levels; level += 1) {
    for (let column = 0; column < columns; column += 1) {
      const container = box(3.15 * scale, 1.2 * scale, 1.22 * scale, material(containerColors[(column + level * 2) % containerColors.length], 0.48, 0.22));
      container.position.set(x + (column - (columns - 1) / 2) * 3.38 * scale, baseY + 0.68 * scale + level * 1.25 * scale, z);
      const ridges = new THREE.LineSegments(
        new THREE.EdgesGeometry(container.geometry),
        new THREE.LineBasicMaterial({ color: 0xdce9ee, transparent: true, opacity: 0.22 }),
      );
      container.add(ridges);
      parent.add(container);
    }
  }
}

function createWarehouseNode(label, x, z, width, depth, accent, nodeType, subtitle) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  const base = box(width, 0.35, depth, material(0x244052, 0.82, 0.03));
  base.position.y = 0.02;
  group.add(base);
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x3c7188, roughness: 0.58, transparent: true, opacity: 0.64, side: THREE.DoubleSide });
  const backWall = box(width, 4.6, 0.18, wallMat);
  backWall.position.set(0, 2.3, -depth / 2);
  group.add(backWall);
  for (const side of [-1, 1]) {
    const wall = box(0.18, 4.6, depth, wallMat.clone());
    wall.position.set(side * width / 2, 2.3, 0);
    group.add(wall);
  }
  const header = box(width - 1, 0.25, 0.45, material(accent, 0.42, 0.34));
  header.position.set(0, 4.45, -depth / 2 + 0.24);
  group.add(header);
  const rackMat = material(0x6f9ab2, 0.36, 0.46);
  const cargoMat = material(accent, 0.68, 0.04);
  for (let col = -1; col <= 1; col += 1) {
    for (let row = -2; row <= 2; row += 1) {
      const rack = box(1.1, 3.2, 3.2, rackMat);
      rack.position.set(col * 3.7, 1.8, row * 4.0);
      const cargo = box(0.86, 1.0, 2.65, cargoMat);
      cargo.position.set(0, 0.05, 0);
      rack.add(cargo);
      group.add(rack);
    }
  }
  const labelSprite = createTextSprite(label, `#${new THREE.Color(accent).getHexString()}`);
  labelSprite.scale.set(14, 2.6, 1);
  labelSprite.position.set(0, 6.3, 0);
  group.add(labelSprite);
  makeInteractive(group, { type: 'zone-node', name: label, subtitle, nodeType, width, depth });
  dynamicRoot.add(group);
  return group;
}

function createCustomsHub(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  const ground = box(28, 0.32, 30, material(0x4b402d, 0.78, 0.03));
  ground.position.y = 0.02;
  group.add(ground);
  const inspection = box(10, 3.8, 6, material(0x405464, 0.55, 0.12));
  inspection.position.set(5.5, 2.05, -7);
  group.add(inspection);
  const roof = box(11.5, 0.26, 7.2, material(colors.amber, 0.42, 0.25));
  roof.position.set(5.5, 4.05, -7);
  group.add(roof);
  createContainerStack(group, -5, 6.5, 3, 3, 0.82);
  for (let offset = -10; offset <= 10; offset += 4) {
    const barrier = box(0.16, 1.05, 2.6, material(0xffd56a, 0.45, 0.2));
    barrier.position.set(offset, 0.68, -13.4);
    group.add(barrier);
  }
  const gateLeft = box(0.45, 4.8, 0.45, material(0xd2a13d, 0.38, 0.35));
  const gateRight = gateLeft.clone();
  gateLeft.position.set(-4.5, 2.4, 13.4);
  gateRight.position.set(4.5, 2.4, 13.4);
  const gateTop = box(9.4, 0.45, 0.45, material(0xd2a13d, 0.38, 0.35));
  gateTop.position.set(0, 4.55, 13.4);
  group.add(gateLeft, gateRight, gateTop);
  const label = createTextSprite('虚拟海关 / CUSTOMS', '#ffd470');
  label.scale.set(18, 3.2, 1);
  label.position.set(0, 6.2, 0);
  group.add(label);
  makeInteractive(group, { type: 'customs', name: '虚拟海关区', status: '在途库存清关与放行', capacity: '批次级在途缓冲' });
  dynamicRoot.add(group);
  return group;
}

function createOceanShip(x, z) {
  const ship = new THREE.Group();
  ship.position.set(x, 1.25, z);
  const hull = box(25, 2.5, 6.2, material(0x327695, 0.3, 0.58));
  hull.position.y = 0;
  ship.add(hull);
  const lowerHull = box(20, 1.15, 5.2, material(0xd45748, 0.5, 0.25));
  lowerHull.position.y = -1.55;
  ship.add(lowerHull);
  const bow = new THREE.Mesh(new THREE.ConeGeometry(3.1, 5.2, 4), material(0x327695, 0.3, 0.58));
  bow.rotation.z = -Math.PI / 2;
  bow.rotation.y = Math.PI / 4;
  bow.position.set(14.3, 0, 0);
  bow.castShadow = true;
  ship.add(bow);
  const deck = box(20.8, 0.28, 5.6, material(0x8d9aa0, 0.6, 0.1));
  deck.position.y = 1.35;
  ship.add(deck);
  for (let lane = -1; lane <= 1; lane += 1) createContainerStack(ship, -2.5, lane * 1.58, 5, 2, 0.7, 1.35);
  const bridge = box(3.1, 4.2, 5.1, material(0xe3eaeb, 0.46, 0.12));
  bridge.position.set(-10.1, 3.25, 0);
  ship.add(bridge);
  const bridgeGlass = box(3.18, 0.72, 5.18, material(0x63a9c5, 0.24, 0.45));
  bridgeGlass.position.set(-10.1, 4.0, 0);
  ship.add(bridgeGlass);
  const label = createTextSprite('跨海运输 / OCEAN', '#83dfff');
  label.scale.set(17, 3, 1);
  label.position.set(0, 8.2, 0);
  ship.add(label);
  makeInteractive(ship, { type: 'ship', name: '跨海运输船', status: '供应商 → 目的国港口', cargo: '整柜与SKU批次在途库存' });
  dynamicRoot.add(ship);
  state.networkAnimated.push({ object: ship, baseY: ship.position.y, phase: 0 });
  return ship;
}

function createRoute(points, color = colors.cyan) {
  const curve = new THREE.CatmullRomCurve3(points.map(([x, y, z]) => new THREE.Vector3(x, y, z)));
  const tube = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 48, 0.13, 7, false),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.75 }),
  );
  dynamicRoot.add(tube);
  const end = points.at(-1);
  const arrow = new THREE.Mesh(new THREE.ConeGeometry(0.75, 2.1, 10), new THREE.MeshBasicMaterial({ color }));
  arrow.rotation.z = -Math.PI / 2;
  arrow.position.set(end[0], end[1], end[2]);
  dynamicRoot.add(arrow);
}

function updateNetworkKpis() {
  const current = state.vfhAnalysis?.current_inventory || {};
  const aUnits = current.a_units || state.stocks.filter((item) => item.zone === 'A').reduce((total, item) => total + Number(item.qty || 0), 0);
  const bUnits = current.b_units || state.stocks.filter((item) => item.zone === 'B').reduce((total, item) => total + Number(item.qty || 0), 0);
  const labels = $$('.kpi-row article > span');
  ['链路节点', '在途 / 清关', 'A区库存', 'B区库存'].forEach((label, index) => { if (labels[index]) labels[index].textContent = label; });
  $('#kpi-capacity').textContent = '4 节点';
  $('#kpi-capacity-sub').textContent = '跨海运输 · 海关 · A区 · B区';
  $('#kpi-space').textContent = formatNumber((current.sea_units || 0) + (current.customs_units || 0));
  $('#kpi-space-sub').textContent = '在途 + 清关库存（件）';
  $('#kpi-cycle').textContent = formatNumber(aUnits);
  $('#kpi-cycle-sub').textContent = 'A区货主库存（件）';
  $('#kpi-completion').textContent = formatNumber(bUnits);
  $('#kpi-risk').textContent = 'B区平台货权库存（件）';
}

function rebuildNetworkOverview() {
  if (!dynamicRoot) return;
  clearDynamicScene();
  scene.fog.density = 0.0035;
  const landMaterial = new THREE.MeshStandardMaterial({ color: 0x203744, roughness: 0.88, metalness: 0.02, emissive: 0x08131a, emissiveIntensity: 0.65 });
  const land = box(92, 0.4, 58, landMaterial);
  land.position.set(23, -0.25, 0);
  dynamicRoot.add(land);
  const sea = box(48, 0.38, 58, new THREE.MeshStandardMaterial({ color: 0x167596, roughness: 0.24, metalness: 0.16, emissive: 0x07334a, emissiveIntensity: 0.72, transparent: true, opacity: 0.97 }));
  sea.position.set(-47, -0.28, 0);
  dynamicRoot.add(sea);
  for (let row = -5; row <= 5; row += 1) {
    const wave = box(39, 0.025, 0.08, new THREE.MeshBasicMaterial({ color: 0x54bdd3, transparent: true, opacity: 0.24 }));
    wave.position.set(-49, 0.02, row * 4.8);
    dynamicRoot.add(wave);
  }
  createOceanShip(-49, -3);
  createCustomsHub(-15, 0);
  createWarehouseNode('A区 / OWNER', 20, -8, 24, 29, colors.cyan, 'A', '货主货权 · 按批次库龄计费');
  createWarehouseNode('B区 / FORWARD HUB', 53, 8, 22, 26, colors.amber, 'B', '平台货权 · 固定面积计租');
  [
    [-49, 17, -3, 0x79dcff, 58], [-15, 18, 0, 0xffcf70, 48],
    [20, 19, -8, 0x59eee2, 48], [53, 19, 8, 0xffc869, 46],
  ].forEach(([x, y, z, color, distance]) => {
    const light = new THREE.PointLight(color, 42, distance, 1.7);
    light.position.set(x, y, z);
    dynamicRoot.add(light);
  });
  createRoute([[-35, 0.45, -3], [-29, 0.45, -1], [-27, 0.45, 0]], 0x52d4e9);
  createRoute([[-1, 0.45, 0], [5, 0.45, -4], [7, 0.45, -6]], colors.cyan);
  createRoute([[33, 0.45, -5], [38, 0.45, -1], [40, 0.45, 3]], colors.amber);
  const title = createTextSprite('VFH 全链路数字孪生', '#d9f8ff');
  title.scale.set(24, 4.4, 1);
  title.position.set(4, 12, -27);
  dynamicRoot.add(title);
  updateNetworkKpis();
  resetView();
}

function positionResources(elapsed, settings = currentSettings()) {
  const width = settings.warehouse_width_m || 60;
  const depth = settings.warehouse_depth_m || 72;
  const aisleWidth = Math.max(3.2, width * settings.aisle_percent / 100 * 0.3);
  const operationMultiplier = state.playing ? 1 : 0.12;
  state.forklifts.forEach((forklift, index) => {
    const phase = elapsed * (0.42 + index * 0.035) * operationMultiplier + index * 2.3;
    const lane = ((index % 2) * 2 - 1) * aisleWidth * 0.23;
    forklift.position.set(lane, 0.1, Math.sin(phase) * (depth / 2 - 7));
    forklift.rotation.y = Math.cos(phase) >= 0 ? 0 : Math.PI;
    const event = activeEventForResource(forklift.userData.id);
    forklift.userData.status = event ? `${event.kind === 'inbound' ? '入库' : '出库'} · ${event.sku}` : (state.playing ? '行驶中' : '待命');
  });
  state.workers.forEach((worker, index) => {
    const phase = elapsed * (0.19 + (index % 4) * 0.018) * operationMultiplier + index * 1.4;
    const side = index % 2 ? 1 : -1;
    worker.position.set(side * (aisleWidth / 2 + 1.1 + (index % 3) * 1.6), 0.1, Math.sin(phase) * (depth / 2 - 7));
    worker.rotation.y = Math.cos(phase) >= 0 ? 0 : Math.PI;
    const event = activeEventForResource(worker.userData.id);
    worker.userData.status = event ? `${event.kind === 'inbound' ? '入库' : '拣选'} · ${event.sku}` : (state.playing ? '作业中' : '巡检');
  });
}

function activeEventForResource(resource) {
  if (!state.simulation?.events) return null;
  return state.simulation.events.find((event) => event.resource === resource && event.time <= state.simMinute && event.finish >= state.simMinute);
}

function selectFromPointer(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(state.interactive, false)[0];
  state.selected = hit?.object || null;
  showObjectDetails(state.selected?.userData || null);
}

function detailRows(rows) {
  return rows.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
}

function showObjectDetails(data) {
  const card = $('#object-card');
  if (!data?.type) {
    card.innerHTML = '<div class="object-empty"><span class="focus-glyph">⌁</span><strong>选择一个对象</strong><p>点击货架、货物、托盘、船舶、海关或作业车辆后显示详细信息。</p></div>';
    return;
  }

  let title = '对象详情';
  let badge = data.type.toUpperCase();
  let rows = [];
  if (data.type === 'sku') {
    title = data.name || data.code;
    badge = 'SKU';
    rows = [
      ['SKU编码', data.code], ['库位', data.location], ['品类', data.category || '未分类'],
      ['单件尺寸', `${data.length_cm} × ${data.width_cm} × ${data.height_cm} cm`],
      ['单件重量', `${data.weight_kg} kg`], ['当前库存', `${formatNumber(data.qty)} 件`],
      ['打托规格', `${data.pallet_length_cm} × ${data.pallet_width_cm} cm`],
      ['码托能力', `${formatNumber(data.units_per_pallet)} 件 / 托`],
    ];
  } else if (data.type === 'rack') {
    title = `货架 ${data.id}`;
    badge = 'RACK';
    rows = [
      ['层数', `${data.levels} 层`], ['规格', `${data.width} × ${data.depth} × ${Number(data.totalHeight).toFixed(1)} m`],
      ['视觉占用率', `${data.occupancy}%`], ['当前SKU', data.sku || '空库位'],
    ];
  } else if (data.type === 'pallet') {
    title = `托盘 ${data.id}`;
    badge = 'PALLET';
    rows = [['当前SKU', data.sku || '空托盘'], ['标准装载', `${formatNumber(data.units)} 件 / 托`]];
  } else if (data.type === 'forklift') {
    title = `叉车 ${data.id}`;
    badge = 'VEHICLE';
    rows = [['实时状态', data.status], ['效率系数', `${data.efficiency}%`], ['仿真时刻', $('#sim-clock').textContent]];
  } else if (data.type === 'worker') {
    title = `作业员 ${data.id}`;
    badge = 'LABOR';
    rows = [['实时状态', data.status], ['人效系数', `${data.efficiency}%`], ['仿真时刻', $('#sim-clock').textContent]];
  } else if (data.type === 'dock') {
    title = data.name;
    badge = 'DOCK';
    rows = [['区域用途', data.status], ['作业窗口', '08:00 — 16:00']];
  } else if (data.type === 'ship') {
    title = data.name;
    badge = 'OCEAN';
    rows = [['运输链路', data.status], ['承载对象', data.cargo], ['库存节点', '海运在途']];
  } else if (data.type === 'customs') {
    title = data.name;
    badge = 'CUSTOMS';
    rows = [['节点作用', data.status], ['规划能力', data.capacity], ['货权状态', '清关完成前保持供应商货权']];
  } else if (data.type === 'zone-node') {
    title = data.name;
    badge = data.nodeType === 'A' ? 'OWNER' : 'FORWARD HUB';
    rows = [['业务边界', data.subtitle], ['模型尺寸', `${data.width} × ${data.depth} m`], ['点击提示', '切换到对应仓区可查看内部布局']];
  }
  const palletSku = data.type === 'sku' ? data.code : (data.type === 'pallet' ? data.sku : null);
  card.innerHTML = `<div class="object-detail"><div class="detail-head"><div><span class="detail-badge">${escapeHtml(badge)}</span><h3>${escapeHtml(title)}</h3></div><button class="icon-button" aria-label="关闭详情">×</button></div><div class="detail-grid">${detailRows(rows)}</div>${palletSku ? `<button class="secondary-button detail-action" data-open-pallet="${escapeHtml(palletSku)}">查看3D打托</button>` : ''}</div>`;
  $('[aria-label="关闭详情"]', card)?.addEventListener('click', () => {
    state.selected = null;
    showObjectDetails(null);
  });
  $('[data-open-pallet]', card)?.addEventListener('click', (event) => openSkuDialog(event.currentTarget.dataset.openPallet));
}

function updateKpis(result) {
  if (!result) return;
  const capacity = result.capacity;
  const operations = result.operations;
  const labels = $$('.kpi-row article > span');
  ['库容托位', '空间利用率', '平均周期', '完成率'].forEach((label, index) => { if (labels[index]) labels[index].textContent = label; });
  $('#kpi-capacity').textContent = formatNumber(capacity.capacity_pallets);
  $('#kpi-capacity-sub').textContent = `货架 ${formatNumber(capacity.rack_positions)} + 地堆 ${formatNumber(capacity.floor_positions)}`;
  $('#kpi-space').textContent = `${formatNumber(capacity.space_utilization, 1)}%`;
  $('#kpi-space-sub').textContent = `${formatNumber(capacity.stock_pallets)} 托 / ${formatNumber(capacity.capacity_pallets)} 托`;
  $('#kpi-cycle').textContent = `${formatNumber(operations.average_cycle_min, 1)} min`;
  $('#kpi-cycle-sub').textContent = `P95 ${formatNumber(operations.p95_cycle_min, 1)} · 等待 ${formatNumber(operations.average_wait_min, 1)}`;
  $('#kpi-completion').textContent = `${formatNumber(operations.completion_rate, 1)}%`;
  $('#kpi-risk').textContent = `${operations.risk} · 积压 ${formatNumber(operations.backlog_tasks)} 任务`;
  $('#kpi-risk').dataset.risk = operations.risk;
}

async function runScenario({ quiet = false } = {}) {
  if (state.zone === 'ALL') {
    rebuildNetworkOverview();
    if (!quiet) showToast('全链路视角已刷新');
    return;
  }
  try {
    const settings = validateSettings(currentSettings());
    const button = $('#run-simulation');
    button.disabled = true;
    button.textContent = '计算中…';
    rebuildWarehouse(settings);
    const result = await api('/api/simulation/run', {
      method: 'POST',
      body: JSON.stringify({ zone: state.zone, seed: Number($('#seed').value || 42), settings }),
    });
    state.simulation = result;
    state.simMinute = 0;
    $('#sim-progress').value = 0;
    updateClock();
    updateKpis(result);
    state.playing = true;
    $('#playback-toggle').textContent = 'Ⅱ';
    if (!quiet) showToast(`仿真完成：${formatNumber(result.operations.generated_tasks)} 个任务已生成`);
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    const button = $('#run-simulation');
    button.disabled = false;
    button.textContent = state.activeScreen === 'decision' ? '重新分析' : (state.zone === 'ALL' ? '刷新全景' : '运行仿真');
  }
}

async function saveSettings() {
  if (state.activeScreen === 'decision') {
    try {
      await saveVfhScenario();
    } catch (error) {
      showToast(error.message, 'error');
    }
    return;
  }
  if (state.zone === 'ALL') {
    showToast('全链路是汇总视角，请切换到A区或B区保存参数');
    return;
  }
  try {
    const settings = validateSettings(currentSettings());
    const saved = await api(`/api/model-settings/${state.zone}`, {
      method: 'PUT', body: JSON.stringify(settings),
    });
    state.zoneSettings[state.zone] = saved;
    showToast(`${state.zone}区方案已保存`);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function switchZone(zone) {
  if (zone !== 'ALL' && !state.zoneSettings[zone]) return;
  state.zone = zone;
  if (zone !== 'ALL') state.lastWarehouseZone = zone;
  $$('.zone-button').forEach((button) => button.classList.toggle('active', button.dataset.zone === zone));
  document.body.classList.toggle('network-view', state.activeScreen === 'warehouse' && zone === 'ALL');
  if (state.activeScreen === 'warehouse') {
    $('#zone-code').textContent = zone;
    $('#zone-title').textContent = zone === 'ALL' ? '全链路 · 数字孪生' : (zone === 'A' ? 'A区 · 货主自营仓' : 'B区 · 平台前置仓');
    $('.panel-intro > p:last-child').textContent = zone === 'ALL'
      ? '同时查看跨海运输、虚拟海关、A区和VFH-HUB四个节点。'
      : '调整参数后可即时重建3D布局，并运行8小时作业仿真。';
  }
  if (zone === 'ALL') {
    $('#run-simulation').textContent = '刷新全景';
    rebuildNetworkOverview();
    return;
  }
  setForm(state.zoneSettings[zone]);
  $('#run-simulation').textContent = '运行仿真';
  rebuildWarehouse(currentSettings());
  resetView();
  if (state.activeScreen === 'warehouse') runScenario({ quiet: true });
}

function updateClock() {
  const total = 8 * 60 + Math.round(state.simMinute);
  const hour = Math.floor(total / 60);
  const minute = total % 60;
  $('#sim-clock').textContent = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  $('#sim-progress').value = Math.round(state.simMinute);
  if (state.selected?.userData?.type === 'forklift' || state.selected?.userData?.type === 'worker') {
    showObjectDetails(state.selected.userData);
  }
}

function animate(now = performance.now()) {
  requestAnimationFrame(animate);
  const delta = Math.min(0.1, clock.getDelta());
  const elapsed = now / 1000;
  if (state.playing && state.simulation) {
    const speed = Number($('#playback-speed').value || 1);
    state.simMinute += delta * speed * 2.2;
    if (state.simMinute >= 480) {
      state.simMinute = 480;
      state.playing = false;
      $('#playback-toggle').textContent = '▶';
    }
    updateClock();
  }
  positionResources(elapsed);
  state.networkAnimated.forEach((entry, index) => {
    entry.object.position.y = entry.baseY + Math.sin(elapsed * 0.75 + entry.phase + index) * 0.16;
    entry.object.rotation.z = Math.sin(elapsed * 0.42 + index) * 0.008;
  });
  controls.update();
  renderer.render(scene, camera);
  if (palletPreview && $('#sku-dialog').open) {
    palletPreview.controls.update();
    palletPreview.renderer.render(palletPreview.scene, palletPreview.camera);
  }
}

function openSkuDrawer() {
  $('#sku-drawer').classList.add('open');
  $('#drawer-scrim').classList.add('open');
  $('#sku-drawer').setAttribute('aria-hidden', 'false');
  $('#sku-search').focus();
}

function closeSkuDrawer() {
  $('#sku-drawer').classList.remove('open');
  $('#drawer-scrim').classList.remove('open');
  $('#sku-drawer').setAttribute('aria-hidden', 'true');
}

function renderSkuList(filter = '') {
  const query = filter.trim().toLocaleLowerCase('zh-CN');
  const items = state.skus.filter((sku) => {
    if (!query) return true;
    return [sku.code, sku.name, sku.category].some((value) => String(value || '').toLocaleLowerCase('zh-CN').includes(query));
  });
  $('#sku-count').textContent = `${items.length} / ${state.skus.length} 个真实SKU`;
  $('#sku-list').innerHTML = items.map((sku) => {
    const dimensionsComplete = Number(sku.length_cm) > 0 && Number(sku.width_cm) > 0 && Number(sku.height_cm) > 0;
    const dimensions = dimensionsComplete
      ? `${formatNumber(sku.length_cm, 1)} × ${formatNumber(sku.width_cm, 1)} × ${formatNumber(sku.height_cm, 1)}`
      : '待补全';
    return `
    <button class="sku-row" data-code="${escapeHtml(sku.code)}">
      <span><strong>${escapeHtml(sku.code)}</strong><small>${escapeHtml(sku.name || '未命名')} · ${escapeHtml(sku.category || '未分类')}</small></span>
      <span>${dimensions}</span>
      <span><strong>${formatNumber(sku.units_per_pallet)}</strong><small>件 / 托</small></span>
    </button>`;
  }).join('') || '<div class="sku-empty">没有匹配的SKU</div>';
}

function readPalletPreviewSpec() {
  const form = $('#sku-form');
  const number = (name) => Math.max(0, Number(form.elements[name]?.value || 0));
  return {
    code: form.elements.code.value,
    category: form.elements.category.value,
    boxLength: number('length_cm') / 100,
    boxWidth: number('width_cm') / 100,
    boxHeight: number('height_cm') / 100,
    palletLength: number('pallet_length_cm') / 100,
    palletWidth: number('pallet_width_cm') / 100,
    maxStackHeight: number('max_stack_height_cm') / 100,
    unitsPerLayer: Math.max(1, Math.round(number('units_per_layer'))),
    layers: Math.max(1, Math.round(number('layers_per_pallet'))),
    totalUnits: Math.max(1, Math.round(number('units_per_pallet'))),
    rotationAllowed: form.elements.rotation_allowed.checked,
  };
}

function calculatePalletPacking(spec) {
  const orientations = [{ length: spec.boxLength, width: spec.boxWidth, rotated: false }];
  if (spec.rotationAllowed && Math.abs(spec.boxLength - spec.boxWidth) > 0.0001) {
    orientations.push({ length: spec.boxWidth, width: spec.boxLength, rotated: true });
  }
  const candidates = orientations.map((item) => {
    const columns = item.length > 0 ? Math.floor((spec.palletLength + 0.0001) / item.length) : 0;
    const rows = item.width > 0 ? Math.floor((spec.palletWidth + 0.0001) / item.width) : 0;
    return { ...item, columns, rows, capacity: columns * rows };
  });
  const best = candidates.sort((a, b) => b.capacity - a.capacity)[0] || { length: 0, width: 0, columns: 0, rows: 0, capacity: 0, rotated: false };
  const palletHeight = 0.15;
  const physicalLayers = spec.boxHeight > 0 ? Math.max(0, Math.floor((spec.maxStackHeight - palletHeight + 0.0001) / spec.boxHeight)) : 0;
  return { ...best, palletHeight, physicalLayers, physicalTotal: best.capacity * physicalLayers };
}

function initPalletPreview() {
  if (palletPreview) return palletPreview;
  const container = $('#pallet-preview-canvas');
  const previewScene = new THREE.Scene();
  const previewCamera = new THREE.PerspectiveCamera(38, 1, 0.01, 100);
  const previewRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  previewRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  previewRenderer.shadowMap.enabled = true;
  previewRenderer.shadowMap.type = THREE.PCFSoftShadowMap;
  previewRenderer.outputColorSpace = THREE.SRGBColorSpace;
  previewRenderer.toneMapping = THREE.ACESFilmicToneMapping;
  previewRenderer.toneMappingExposure = 1.12;
  container.appendChild(previewRenderer.domElement);
  const previewControls = new OrbitControls(previewCamera, previewRenderer.domElement);
  previewControls.enableDamping = true;
  previewControls.dampingFactor = 0.07;
  previewControls.autoRotate = true;
  previewControls.autoRotateSpeed = 0.7;
  previewControls.maxPolarAngle = Math.PI * 0.49;
  previewControls.minDistance = 1.2;
  previewControls.maxDistance = 15;
  previewScene.add(new THREE.HemisphereLight(0xd8f3ff, 0x111822, 2.1));
  const keyLight = new THREE.DirectionalLight(0xffffff, 3.8);
  keyLight.position.set(-4, 7, 5);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(1024, 1024);
  previewScene.add(keyLight);
  const rim = new THREE.PointLight(colors.cyan, 1.6, 15);
  rim.position.set(4, 3, -3);
  previewScene.add(rim);
  const root = new THREE.Group();
  previewScene.add(root);
  palletPreview = { scene: previewScene, camera: previewCamera, renderer: previewRenderer, controls: previewControls, root };
  const resize = () => {
    const width = Math.max(1, container.clientWidth);
    const height = Math.max(1, container.clientHeight);
    previewRenderer.setSize(width, height, false);
    previewCamera.aspect = width / height;
    previewCamera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(container);
  resize();
  return palletPreview;
}

function buildPalletPreview() {
  const preview = initPalletPreview();
  const spec = readPalletPreviewSpec();
  const packing = calculatePalletPacking(spec);
  disposeObject(preview.root);
  preview.root.clear();
  if (![spec.boxLength, spec.boxWidth, spec.boxHeight, spec.palletLength, spec.palletWidth].every((value) => value > 0)) {
    $('#pallet-preview-stats').innerHTML = '<p class="preview-warning">请先补全SKU与托盘尺寸。</p>';
    return;
  }

  const floorSize = Math.max(spec.palletLength, spec.palletWidth) * 2.7;
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(floorSize, floorSize), new THREE.ShadowMaterial({ color: 0x000000, opacity: 0.32 }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.012;
  floor.receiveShadow = true;
  preview.root.add(floor);
  const slatMaterial = material(0xa87643, 0.78, 0.02);
  for (let index = 0; index < 7; index += 1) {
    const slat = box(spec.palletLength, 0.055, Math.max(0.035, spec.palletWidth / 8.3), slatMaterial);
    slat.position.set(0, 0.12, -spec.palletWidth / 2 + spec.palletWidth * (index + 1) / 8);
    preview.root.add(slat);
  }
  for (const z of [-spec.palletWidth * 0.36, 0, spec.palletWidth * 0.36]) {
    const runner = box(spec.palletLength * 0.96, 0.1, Math.max(0.06, spec.palletWidth * 0.12), material(0x7d522e, 0.82, 0.02));
    runner.position.set(0, 0.055, z);
    preview.root.add(runner);
  }
  const palletOutline = addLineBox(spec.palletLength, packing.palletHeight, spec.palletWidth, 0xffd391);
  palletOutline.position.y = packing.palletHeight / 2;
  preview.root.add(palletOutline);

  const shownPerLayer = Math.min(spec.unitsPerLayer, Math.max(1, packing.capacity));
  const shownLayers = Math.min(spec.layers, Math.max(1, packing.physicalLayers));
  const instanceCount = Math.max(0, shownPerLayer * shownLayers);
  if (instanceCount > 0) {
    const geometry = new THREE.BoxGeometry(packing.length * 0.975, spec.boxHeight * 0.985, packing.width * 0.975);
    const cargoMaterial = material(categoryColor(spec.category), 0.62, 0.035);
    const instances = new THREE.InstancedMesh(geometry, cargoMaterial, instanceCount);
    instances.castShadow = true;
    instances.receiveShadow = true;
    const matrix = new THREE.Matrix4();
    let cursor = 0;
    for (let layer = 0; layer < shownLayers; layer += 1) {
      for (let item = 0; item < shownPerLayer; item += 1) {
        const layoutColumns = Math.max(1, packing.columns);
        const layoutRows = Math.max(1, packing.rows);
        const column = item % layoutColumns;
        const row = Math.floor(item / layoutColumns);
        const x = -layoutColumns * packing.length / 2 + packing.length / 2 + column * packing.length;
        const z = -layoutRows * packing.width / 2 + packing.width / 2 + row * packing.width;
        const y = packing.palletHeight + spec.boxHeight / 2 + layer * spec.boxHeight;
        matrix.makeTranslation(x, y, z);
        instances.setMatrixAt(cursor, matrix);
        instances.setColorAt(cursor, new THREE.Color(categoryColor(spec.category)).offsetHSL(0, 0, (layer % 2) * 0.035));
        cursor += 1;
      }
    }
    instances.instanceMatrix.needsUpdate = true;
    if (instances.instanceColor) instances.instanceColor.needsUpdate = true;
    preview.root.add(instances);
    const stackOutline = addLineBox(
      Math.max(packing.length, packing.columns * packing.length),
      shownLayers * spec.boxHeight,
      Math.max(packing.width, packing.rows * packing.width),
      0xd9f7ff,
    );
    stackOutline.position.y = packing.palletHeight + shownLayers * spec.boxHeight / 2;
    preview.root.add(stackOutline);
  }
  const stackHeight = packing.palletHeight + shownLayers * spec.boxHeight;
  const span = Math.max(spec.palletLength, spec.palletWidth, stackHeight, 1);
  preview.camera.position.set(span * 1.55, span * 1.18, span * 1.65);
  preview.controls.target.set(0, Math.max(0.25, stackHeight * 0.46), 0);
  preview.controls.update();

  const footprintUse = packing.capacity ? Math.min(100, spec.unitsPerLayer * spec.boxLength * spec.boxWidth / (spec.palletLength * spec.palletWidth) * 100) : 0;
  const warnings = [];
  if (spec.unitsPerLayer > packing.capacity) warnings.push(`每层填写 ${spec.unitsPerLayer} 件，但几何最多 ${packing.capacity} 件`);
  if (spec.layers > packing.physicalLayers) warnings.push(`填写 ${spec.layers} 层，但最大堆高允许 ${packing.physicalLayers} 层`);
  if (spec.totalUnits !== spec.unitsPerLayer * spec.layers) warnings.push(`每托件数应为 ${spec.unitsPerLayer * spec.layers}，当前填写 ${spec.totalUnits}`);
  $('#pallet-preview-stats').innerHTML = `
    <div><span>自动排布</span><strong>${packing.columns} × ${packing.rows} / 层${packing.rotated ? ' · 已旋转' : ''}</strong></div>
    <div><span>几何容量</span><strong>${formatNumber(packing.physicalTotal)} 件 / 托</strong></div>
    <div><span>当前模拟</span><strong>${formatNumber(instanceCount)} 件 · ${shownLayers} 层</strong></div>
    <div><span>外形高度</span><strong>${formatNumber(stackHeight * 100, 1)} cm</strong></div>
    <div><span>单层面积利用率</span><strong>${formatNumber(footprintUse, 1)}%</strong></div>
    ${warnings.length ? `<p class="preview-warning">${warnings.map(escapeHtml).join('<br>')}</p>` : '<p>规格在几何范围内；拖拽可旋转，滚轮可缩放。</p>'}`;
}

function autoPalletize() {
  const form = $('#sku-form');
  const spec = readPalletPreviewSpec();
  const packing = calculatePalletPacking(spec);
  if (!packing.capacity || !packing.physicalLayers) {
    showToast('当前尺寸无法完成自动打托，请检查外箱与最大堆高', 'error');
    return;
  }
  form.elements.units_per_layer.value = packing.capacity;
  form.elements.layers_per_pallet.value = packing.physicalLayers;
  form.elements.units_per_pallet.value = packing.physicalTotal;
  buildPalletPreview();
}

function openSkuDialog(code) {
  const sku = state.skus.find((item) => item.code === code);
  if (!sku) return;
  const form = $('#sku-form');
  $('#sku-dialog-title').textContent = `${sku.code} · ${sku.name || '未命名SKU'}`;
  const fields = [
    'code', 'name', 'category', 'storage_type', 'length_cm', 'width_cm', 'height_cm', 'weight_kg',
    'pallet_length_cm', 'pallet_width_cm', 'max_stack_height_cm', 'units_per_layer',
    'layers_per_pallet', 'units_per_pallet',
  ];
  fields.forEach((field) => { form.elements[field].value = sku[field] ?? ''; });
  form.elements.rotation_allowed.checked = Boolean(sku.rotation_allowed);
  $('#sku-dialog').showModal();
  requestAnimationFrame(() => buildPalletPreview());
}

async function saveSku(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const code = form.elements.code.value;
  const numberFields = [
    'length_cm', 'width_cm', 'height_cm', 'weight_kg', 'pallet_length_cm', 'pallet_width_cm',
    'max_stack_height_cm', 'units_per_layer', 'layers_per_pallet', 'units_per_pallet',
  ];
  const payload = {
    name: form.elements.name.value.trim(),
    category: form.elements.category.value.trim(),
    storage_type: form.elements.storage_type.value,
    rotation_allowed: form.elements.rotation_allowed.checked,
  };
  numberFields.forEach((field) => { payload[field] = Number(form.elements[field].value); });
  try {
    const saved = await api(`/api/skus/${encodeURIComponent(code)}`, { method: 'PUT', body: JSON.stringify(payload) });
    const index = state.skus.findIndex((item) => item.code === code);
    if (index >= 0) state.skus[index] = saved;
    renderSkuList($('#sku-search').value);
    if (state.zone === 'ALL') rebuildNetworkOverview(); else rebuildWarehouse(currentSettings());
    $('#sku-dialog').close();
    showToast(`${code} 的尺寸与打托规格已保存`);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function setVfhForm(scenario) {
  state.vfhScenario = { ...scenario };
  $$('[data-vfh-field]').forEach((input) => {
    const value = scenario[input.dataset.vfhField];
    if (value !== undefined && value !== null) input.value = value;
  });
}

function collectVfhScenario() {
  const scenario = { ...(state.vfhScenario || {}) };
  $$('[data-vfh-field]').forEach((input) => {
    const key = input.dataset.vfhField;
    scenario[key] = input.type === 'number' ? Number(input.value) : input.value.trim();
  });
  return scenario;
}

function validateVfhScenario(scenario) {
  const transport = scenario.parcel_percent + scenario.ltl_percent + scenario.ftl_percent;
  if (Math.abs(transport - 100) > 0.1) throw new Error('快递、散托和整车比例之和必须为 100%');
  if (scenario.office_percent + scenario.rest_percent >= 50) throw new Error('办公区与休息区合计占比必须小于 50%');
  return scenario;
}

function switchScreen(screen) {
  state.activeScreen = screen;
  $$('.screen-button').forEach((button) => button.classList.toggle('active', button.dataset.screen === screen));
  $('#warehouse-screen').classList.toggle('active', screen === 'warehouse');
  $('#decision-screen').classList.toggle('active', screen === 'decision');
  document.body.classList.toggle('decision-active', screen === 'decision');
  document.body.classList.toggle('network-view', screen === 'warehouse' && state.zone === 'ALL');
  $('#reset-view').style.display = screen === 'warehouse' ? '' : 'none';
  $('#apply-layout').style.display = screen === 'warehouse' ? '' : 'none';
  $('#run-simulation').textContent = screen === 'warehouse' ? (state.zone === 'ALL' ? '刷新全景' : '运行仿真') : '重新分析';
  if (screen === 'warehouse') {
    $('#zone-code').textContent = state.zone;
    $('#zone-title').textContent = state.zone === 'ALL' ? '全链路 · 数字孪生' : (state.zone === 'A' ? 'A区 · 货主自营仓' : 'B区 · 平台前置仓');
    $('.panel-intro > p:last-child').textContent = state.zone === 'ALL'
      ? '同时查看跨海运输、虚拟海关、A区和VFH-HUB四个节点。'
      : '调整参数后可即时重建3D布局，并运行8小时作业仿真。';
    if (state.zone === 'ALL') rebuildNetworkOverview();
    requestAnimationFrame(resizeScene);
  } else {
    $('#zone-code').textContent = 'VFH';
    $('#zone-title').textContent = 'VFH · 经营决策';
    $('.panel-intro > p:last-child').textContent = '评估是否建立VFH、面积与用工，并持续判断扩缩容、爆仓和成本风险。';
  }
}

function switchDecisionPage(page) {
  $$('[data-decision-page]').forEach((button) => button.classList.toggle('active', button.dataset.decisionPage === page));
  $$('.decision-page').forEach((view) => view.classList.toggle('active', view.dataset.page === page));
}

async function saveVfhScenario({ quiet = false } = {}) {
  const scenario = validateVfhScenario(collectVfhScenario());
  const saved = await api('/api/vfh/scenario', { method: 'PUT', body: JSON.stringify(scenario) });
  setVfhForm(saved);
  if (!quiet) showToast(`${saved.country} 的VFH经营情景已保存`);
  return saved;
}

async function analyzeVfh({ quiet = false } = {}) {
  const button = $('#analyze-vfh');
  const topButton = $('#run-simulation');
  try {
    const scenario = validateVfhScenario(collectVfhScenario());
    button.disabled = true;
    topButton.disabled = true;
    button.textContent = '分析中…';
    if (state.activeScreen === 'decision') topButton.textContent = '分析中…';
    const result = await api('/api/vfh/analyze', {
      method: 'POST', body: JSON.stringify({ scenario, seed: Number($('#seed').value || 42) }),
    });
    state.vfhScenario = scenario;
    state.vfhAnalysis = result;
    renderVfhAnalysis(result);
    if (!quiet) {
      switchScreen('decision');
      showToast('VFH经营决策分析已完成');
    }
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    button.disabled = false;
    topButton.disabled = false;
    button.textContent = '分析VFH';
    topButton.textContent = state.activeScreen === 'decision' ? '重新分析' : (state.zone === 'ALL' ? '刷新全景' : '运行仿真');
  }
}

function renderVfhAnalysis(result) {
  const decision = result.decision;
  const scenario = state.vfhScenario || collectVfhScenario();
  const currency = decision.currency;
  $('#decision-country').textContent = String(scenario.country || 'Demo-Europe').toUpperCase();
  $('#decision-title').textContent = decision.recommendation;
  const savingDirection = decision.savings >= 0 ? '节省' : '增加';
  $('#decision-subtitle').textContent = `${decision.horizon_days}天情景中，使用VFH预计${savingDirection} ${formatMoney(Math.abs(decision.savings), currency)}；观测数据与情景假设已分开标识。`;
  $('#decision-density').textContent = decision.data_density_label;
  $('#decision-band').textContent = `情景区间 ±${decision.scenario_band_pct}%`;
  $('#vfh-saving').textContent = formatMoney(decision.annualized_savings, currency, true);
  $('#vfh-saving-sub').textContent = decision.payback_months ? `预计回收期 ${decision.payback_months} 个月` : '当前参数下无法回收建设投入';
  $('#vfh-area').textContent = `${formatNumber(decision.recommended_area_m2)} m²`;
  $('#vfh-area-sub').textContent = `当前输入 ${formatNumber(scenario.b_area_m2)} m²`;
  $('#vfh-workers').textContent = `${formatNumber(decision.recommended_workers)} 人`;
  $('#vfh-workers-sub').textContent = `当前输入 ${formatNumber(scenario.b_workers)} 人`;
  $('#vfh-scale').textContent = `${formatNumber(decision.supported_monthly_units, 0)} 件`;
  $('#vfh-scale-sub').textContent = `业务余量 ${decision.business_headroom_units_month >= 0 ? '+' : ''}${formatNumber(decision.business_headroom_units_month)} 件/月`;
  $('#cost-period').textContent = `${decision.horizon_days}天 · ${currency}`;
  renderCostComparison(result);
  renderActions(result);
  renderForecast(result);
  renderOptions(result);
  renderCostBreakdown(result);
  renderInventory(result);
}

function renderCostComparison(result) {
  const currency = result.decision.currency;
  const items = [
    ['不使用VFH', result.without_vfh.total_cost, 'without'],
    ['使用VFH', result.with_vfh.total_cost, 'vfh'],
  ];
  const max = Math.max(...items.map((item) => item[1]), 1);
  $('#cost-comparison').innerHTML = items.map(([label, value, type]) => `
    <div class="compare-row ${type}"><span>${label}</span><div class="compare-track"><i style="width:${Math.max(1, value / max * 100)}%"></i></div><strong>${formatMoney(value, currency, true)}</strong></div>
  `).join('');
}

function renderActions(result) {
  $('#decision-actions').innerHTML = result.actions.map((item) => `<p>${escapeHtml(item)}</p>`).join('');
}

function renderForecast(result) {
  const rows = result.with_vfh.forecast;
  if (!rows.length) {
    $('#forecast-chart').innerHTML = '<p class="field-note">当前情景没有可绘制的预测数据。</p>';
    return;
  }
  const width = 920;
  const height = 225;
  const pad = { left: 42, right: 18, top: 16, bottom: 28 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const maxBacklog = Math.max(...rows.map((row) => row.backlog_units), 1);
  const maxUtil = Math.max(100, ...rows.map((row) => row.b_utilization));
  const point = (row, index, key, max) => {
    const x = pad.left + index / Math.max(1, rows.length - 1) * plotW;
    const y = pad.top + plotH - Number(row[key] || 0) / max * plotH;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  };
  const utilPath = rows.map((row, index) => point(row, index, 'b_utilization', maxUtil)).join(' ');
  const backlogPath = rows.map((row, index) => point(row, index, 'backlog_units', maxBacklog)).join(' ');
  const target = Number(state.vfhScenario?.target_utilization_pct || 85);
  const targetY = pad.top + plotH - target / maxUtil * plotH;
  const xTicks = [0, Math.floor((rows.length - 1) / 2), rows.length - 1];
  const grid = [0, 25, 50, 75, 100].map((value) => {
    const y = pad.top + plotH - value / maxUtil * plotH;
    return `<line class="grid" x1="${pad.left}" x2="${width - pad.right}" y1="${y}" y2="${y}"></line><text x="4" y="${y + 4}">${value}%</text>`;
  }).join('');
  const labels = xTicks.map((index) => {
    const x = pad.left + index / Math.max(1, rows.length - 1) * plotW;
    return `<text text-anchor="middle" x="${x}" y="${height - 5}">${escapeHtml(rows[index].date.slice(5))}</text>`;
  }).join('');
  $('#forecast-chart').innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="B区利用率和积压预测">
      ${grid}<line class="target-line" x1="${pad.left}" x2="${width - pad.right}" y1="${targetY}" y2="${targetY}"></line>
      <polyline class="util-line" points="${utilPath}"></polyline>
      <polyline class="backlog-line" points="${backlogPath}"></polyline>${labels}
    </svg>
    <div class="chart-legend"><span><i></i>B区利用率</span><span class="target"><i></i>目标 ${target}%</span><span class="backlog"><i></i>积压（独立归一化）</span></div>`;
}

function renderOptions(result) {
  const currency = result.decision.currency;
  const rows = result.shortlist;
  $('#scenario-options').innerHTML = `<table><thead><tr><th>面积</th><th>人员</th><th>托位</th><th>峰值利用率</th><th>服务水平</th><th>年度成本</th><th>状态</th></tr></thead><tbody>${rows.map((row) => `
    <tr><td>${formatNumber(row.area_m2)} m²</td><td>${row.workers} 人</td><td>${formatNumber(row.capacity_pallets)}</td><td>${formatNumber(row.peak_utilization, 1)}%</td><td>${formatNumber(row.service_level, 1)}%</td><td>${formatMoney(row.annual_incremental_cost, currency, true)}</td><td><span class="status-pill ${row.feasible ? '' : 'warn'}">${row.feasible ? '满足约束' : '存在风险'}</span></td></tr>`).join('')}</tbody></table>`;
}

function renderBreakdown(selector, costs, currency) {
  const entries = Object.entries(costs).filter(([, value]) => Number(value) > 0).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, value]) => value), 1);
  $(selector).innerHTML = entries.map(([label, value]) => `
    <div class="breakdown-row"><span>${escapeHtml(label)}</span><div class="breakdown-track"><i style="width:${Math.max(1, value / max * 100)}%"></i></div><strong>${formatMoney(value, currency, true)}</strong></div>
  `).join('');
}

function renderCostBreakdown(result) {
  const currency = result.decision.currency;
  $('#without-cost-total').textContent = formatMoney(result.without_vfh.total_cost, currency, true);
  $('#with-cost-total').textContent = formatMoney(result.with_vfh.total_cost, currency, true);
  renderBreakdown('#without-costs', result.without_vfh.costs, currency);
  renderBreakdown('#with-costs', result.with_vfh.costs, currency);
  $('#model-warnings').innerHTML = result.warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join('');
}

function parseCsv(text) {
  const matrix = [];
  let row = [];
  let field = '';
  let quoted = false;
  const source = String(text || '').replace(/^\uFEFF/, '');
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') { field += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ',') { row.push(field.trim()); field = ''; }
    else if (char === '\n') { row.push(field.trim()); if (row.some(Boolean)) matrix.push(row); row = []; field = ''; }
    else if (char !== '\r') field += char;
  }
  row.push(field.trim());
  if (row.some(Boolean)) matrix.push(row);
  if (matrix.length < 2) throw new Error('CSV至少需要表头和一行批次数据');
  const aliases = {
    batch_id: ['batch_id', '批次号', '批次'], sku_code: ['sku_code', 'sku', 'SKU', 'sku编码'],
    receipt_date: ['receipt_date', '入库日期', '日期'], qty: ['qty', '数量', '库存数量'],
    zone: ['zone', '区域', '节点'], status: ['status', '状态'],
    unit_volume_m3: ['unit_volume_m3', '单件体积m3', '单件体积'],
  };
  const header = matrix[0].map((item) => item.trim());
  const findColumn = (key) => header.findIndex((name) => aliases[key].some((alias) => alias.toLowerCase() === name.toLowerCase()));
  const columns = Object.fromEntries(Object.keys(aliases).map((key) => [key, findColumn(key)]));
  ['batch_id', 'sku_code', 'receipt_date', 'qty', 'zone'].forEach((key) => {
    if (columns[key] < 0) throw new Error(`CSV缺少必填列：${aliases[key][0]}`);
  });
  const zones = { A: 'A', 'A区': 'A', B: 'B', 'B区': 'B', SEA: 'SEA', '海运在途': 'SEA', CUSTOMS: 'CUSTOMS', '虚拟海关': 'CUSTOMS' };
  return matrix.slice(1).map((values, index) => {
    const cell = (key) => columns[key] >= 0 ? String(values[columns[key]] || '').trim() : '';
    const zone = zones[cell('zone').toUpperCase()] || zones[cell('zone')];
    const qty = Number(cell('qty').replaceAll(',', ''));
    const unitVolume = cell('unit_volume_m3');
    if (!cell('batch_id') || !cell('sku_code') || !cell('receipt_date') || !zone || !Number.isFinite(qty) || qty <= 0) {
      throw new Error(`CSV第 ${index + 2} 行存在空值、无效区域或无效数量`);
    }
    return {
      batch_id: cell('batch_id'), sku_code: cell('sku_code'),
      receipt_date: cell('receipt_date').replaceAll('/', '-'), qty: Math.round(qty), zone,
      status: cell('status').toLowerCase() || 'active',
      unit_volume_m3: unitVolume ? Number(unitVolume) : null,
    };
  });
}

function downloadBatchTemplate() {
  const csv = '\uFEFFbatch_id,sku_code,receipt_date,qty,zone,status,unit_volume_m3\nDEMO-2026-001,DEMO-MW-01,2026-08-01,480,A,active,0.0585\nDEMO-SEA-002,DEMO-TV-02,2026-09-18,1200,SEA,active,0.0478\n';
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'vfh_batch_import_template.csv';
  link.click();
  URL.revokeObjectURL(url);
}

async function importBatchFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const rows = parseCsv(await file.text());
    const country = collectVfhScenario().country || 'Demo-Europe';
    const saved = await api('/api/vfh/batches/import', {
      method: 'POST', body: JSON.stringify({ country, rows }),
    });
    const density = $('[data-vfh-field="data_density"]');
    density.value = 'detailed';
    const unknown = saved.unknown_skus?.length ? `；${saved.unknown_skus.length}个SKU不在主数据中` : '';
    showToast(`已导入 ${saved.count} 个批次${unknown}`);
    await analyzeVfh({ quiet: true });
    switchDecisionPage('inventory');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    event.target.value = '';
  }
}

function renderInventory(result) {
  const first = result.with_vfh.forecast[0] || {};
  const last = result.with_vfh.forecast.at(-1) || {};
  const current = result.current_inventory || {};
  const imported = Boolean(current.has_imported_batches);
  const pipeline = [
    ['海运在途', imported ? current.sea_units : first.at_sea_units, imported ? '导入批次当前值' : `${state.vfhScenario?.sea_lead_days || 0}天海运时效`],
    ['虚拟海关', imported ? current.customs_units : first.customs_units, imported ? '导入批次当前值' : `${state.vfhScenario?.customs_lead_days || 0}天清关时效`],
    ['A区库存', imported ? current.a_units : last.a_units, '货主货权 · 按批次库龄计费'],
    ['A→B转运', result.with_vfh.transferred_units, '同仓库位转移'],
    ['B区库存', imported ? current.b_units : last.b_units, imported ? '平台货权 · 导入批次当前值' : `平台货权 · 利用率 ${last.b_utilization || 0}%`],
  ];
  $('#inventory-pipeline').innerHTML = pipeline.map(([label, value, note]) => `<div class="pipeline-node"><span>${label}</span><strong>${formatNumber(value)} 件</strong><small>${escapeHtml(note)}</small></div>`).join('');
  $('#batch-source-label').textContent = imported ? `${current.batch_count}个导入批次 · 日期与数量为观测数据` : 'SKU数量真实 · 入库日期为情景假设';
  $('#batch-table').innerHTML = `<table><thead><tr><th>${imported ? '导入批次' : '情景批次'}</th><th>SKU</th><th>入库日期</th><th>库龄</th><th>数量</th><th>区域/货权</th><th>计费状态</th></tr></thead><tbody>${result.batch_preview.map((row) => `
    <tr><td>${escapeHtml(row.batch_id)}</td><td>${escapeHtml(row.sku)}</td><td>${escapeHtml(row.receipt_date)}</td><td>${row.age_days}天</td><td>${formatNumber(row.qty)}</td><td>${escapeHtml(row.zone)} / ${escapeHtml(row.title_owner)}</td><td><span class="status-pill ${row.fee_status.includes('计费') && !row.fee_status.includes('未触发') ? 'warn' : ''}">${escapeHtml(row.fee_status)}</span></td></tr>`).join('')}</tbody></table>`;
  $('#sku-policy-table').innerHTML = `<table><thead><tr><th>SKU</th><th>品类</th><th>库存</th><th>尺寸 cm</th><th>件/托</th><th>建议</th><th>原因</th></tr></thead><tbody>${result.sku_policy.map((row) => `
    <tr><td>${escapeHtml(row.code)}</td><td>${escapeHtml(row.category || '未分类')}</td><td>${formatNumber(row.qty)}</td><td>${row.dimensions_cm.map((value) => formatNumber(value, 1)).join(' × ')}</td><td>${formatNumber(row.units_per_pallet)}</td><td><span class="status-pill ${row.placement === '待补数据' ? 'warn' : ''}">${escapeHtml(row.placement)}</span></td><td>${escapeHtml(row.reason)}</td></tr>`).join('')}</tbody></table>`;
}

function bindUi() {
  $$('.screen-button').forEach((button) => button.addEventListener('click', () => switchScreen(button.dataset.screen)));
  $$('.zone-button').forEach((button) => button.addEventListener('click', () => switchZone(button.dataset.zone)));
  $('#reset-view').addEventListener('click', resetView);
  $('#open-sku').addEventListener('click', openSkuDrawer);
  $('#close-sku').addEventListener('click', closeSkuDrawer);
  $('#drawer-scrim').addEventListener('click', closeSkuDrawer);
  $('#run-simulation').addEventListener('click', () => {
    if (state.activeScreen === 'decision') analyzeVfh(); else runScenario();
  });
  $('#analyze-vfh').addEventListener('click', () => analyzeVfh());
  $('#save-settings').addEventListener('click', saveSettings);
  $('#apply-layout').addEventListener('click', () => {
    try {
      rebuildWarehouse(validateSettings(currentSettings()));
      showToast('3D布局已按当前参数重建');
    } catch (error) {
      showToast(error.message, 'error');
    }
  });
  $('#playback-toggle').addEventListener('click', () => {
    if (!state.simulation) return;
    if (state.simMinute >= 480) state.simMinute = 0;
    state.playing = !state.playing;
    $('#playback-toggle').textContent = state.playing ? 'Ⅱ' : '▶';
  });
  $('#sim-progress').addEventListener('input', (event) => {
    state.simMinute = Number(event.target.value);
    updateClock();
  });
  $('#sku-search').addEventListener('input', (event) => renderSkuList(event.target.value));
  $('#sku-list').addEventListener('click', (event) => {
    const row = event.target.closest('[data-code]');
    if (row) openSkuDialog(row.dataset.code);
  });
  $('#sku-form').addEventListener('submit', saveSku);
  $('#sku-form').addEventListener('input', () => {
    if ($('#sku-dialog').open) requestAnimationFrame(buildPalletPreview);
  });
  $('#auto-palletize').addEventListener('click', autoPalletize);
  $('#download-batch-template').addEventListener('click', downloadBatchTemplate);
  $('#import-batches').addEventListener('click', () => $('#batch-file').click());
  $('#batch-file').addEventListener('change', importBatchFile);
  $$('.modal-close').forEach((button) => button.addEventListener('click', () => $('#sku-dialog').close()));
  $$('[data-decision-page]').forEach((button) => button.addEventListener('click', () => switchDecisionPage(button.dataset.decisionPage)));

  $$('[data-field]').forEach((input) => input.addEventListener('input', () => {
    updateOutputs();
    clearTimeout(rebuildTimer);
    rebuildTimer = setTimeout(() => {
      try { rebuildWarehouse(validateSettings(currentSettings())); } catch (_) { /* show error on explicit action */ }
    }, 260);
  }));
  $$('.section-title').forEach((button) => button.addEventListener('click', () => {
    const expanded = button.getAttribute('aria-expanded') === 'true';
    button.setAttribute('aria-expanded', String(!expanded));
    button.parentElement.classList.toggle('collapsed', expanded);
    $('span', button).textContent = expanded ? '+' : '−';
  }));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeSkuDrawer();
  });
}

async function bootstrap() {
  initScene();
  bindUi();
  try {
    const [settings, catalog, stocks, vfhScenario] = await Promise.all([
      api('/api/model-settings'), api('/api/skus?limit=500'), api('/api/stocks'), api('/api/vfh/scenario'),
    ]);
    state.zoneSettings = settings;
    state.skus = catalog.items;
    state.stocks = stocks;
    setForm(settings[state.zone]);
    setVfhForm(vfhScenario);
    renderSkuList();
    rebuildWarehouse(currentSettings());
    resetView();
    await runScenario({ quiet: true });
    await analyzeVfh({ quiet: true });
    const initialView = new URLSearchParams(window.location.search).get('view');
    if (initialView === 'network') switchZone('ALL');
  } catch (error) {
    showToast(`初始化失败：${error.message}`, 'error');
  }
}

bootstrap();
