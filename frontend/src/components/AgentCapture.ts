/**
 * Multi-view capture with triangle-id buffers for the SEAM-Agent.
 *
 * `captureAgentViews` renders one prim's mesh from 6 orthographic viewpoints
 * (front/back/left/right + 2 oblique 45°) into offscreen render targets and
 * produces, per view:
 *   - an RGB JPEG data URL (the mesh with its existing material, plainly lit),
 *   - a triangle-id PNG data URL whose RGB channels encode faceIndex as a
 *     uint24 (r=id>>16, g=id>>8&255, b=id&255), background white (0xFFFFFF).
 * The agent uses the tri-id buffer to map any pixel it reasons about back to a
 * triangle of the source mesh.
 *
 * The shared scene is never mutated: we clone the mesh into a throwaway Scene
 * for the RGB pass and build a fresh non-indexed geometry for the id pass, then
 * dispose every geometry / material / render target we created.
 *
 * Coordinate system: the app world is Z-up ENU meters (see Viewer3D). The
 * orthographic cameras therefore use up=[0,0,1] and are placed along world
 * axes framed to the mesh's world-space AABB.
 */
import * as THREE from "three";
import type { AgentView } from "../types/api";

/** Longest side (px) of each captured view. Keeps the payload small while
 *  leaving enough resolution for the agent to localize materials + read the
 *  tri-id buffer. */
const VIEW_LONGEST_SIDE = 768;

/** JPEG quality for the RGB pass (tri-id is always lossless PNG). */
const RGB_JPEG_QUALITY = 0.85;

/** The 6 capture directions, each a unit vector FROM the mesh center TOWARD the
 *  camera in world space (Z-up). 4 cardinal elevations kept level; 2 obliques
 *  look down at 45°. */
const VIEW_DIRS: { id: string; dir: THREE.Vector3 }[] = [
  { id: "front", dir: new THREE.Vector3(0, -1, 0) },
  { id: "back", dir: new THREE.Vector3(0, 1, 0) },
  { id: "left", dir: new THREE.Vector3(-1, 0, 0) },
  { id: "right", dir: new THREE.Vector3(1, 0, 0) },
  // Oblique 45° views (azimuth 45° / 225°, elevated) give the agent corner
  // geometry that the axis-aligned views foreshorten away.
  { id: "oblique_1", dir: new THREE.Vector3(1, -1, 1).normalize() },
  { id: "oblique_2", dir: new THREE.Vector3(-1, 1, 1).normalize() },
];

/** Pixel dims for a view fitting a box of world size (w,h) along the camera's
 *  right/up axes, capped so the longest side is VIEW_LONGEST_SIDE. */
function viewPixelSize(spanRight: number, spanUp: number): { width: number; height: number } {
  const aspect = spanRight > 0 && spanUp > 0 ? spanRight / spanUp : 1;
  let width: number;
  let height: number;
  if (aspect >= 1) {
    width = VIEW_LONGEST_SIDE;
    height = Math.max(1, Math.round(VIEW_LONGEST_SIDE / aspect));
  } else {
    height = VIEW_LONGEST_SIDE;
    width = Math.max(1, Math.round(VIEW_LONGEST_SIDE * aspect));
  }
  return { width, height };
}

/** Read a render target into a bottom-up RGBA buffer, blit it TOP-DOWN into a
 *  2D canvas (readRenderTargetPixels returns rows bottom-to-top; flip so the
 *  PNG/JPEG matches on-screen orientation), and return the canvas. */
function renderTargetToCanvas(
  renderer: THREE.WebGLRenderer,
  rt: THREE.WebGLRenderTarget,
  width: number,
  height: number,
): HTMLCanvasElement {
  const buffer = new Uint8Array(width * height * 4);
  renderer.readRenderTargetPixels(rt, 0, 0, width, height, buffer);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  const image = ctx.createImageData(width, height);
  // Flip rows vertically: source row (height-1-y) -> destination row y.
  for (let y = 0; y < height; y++) {
    const srcRow = (height - 1 - y) * width * 4;
    const dstRow = y * width * 4;
    image.data.set(buffer.subarray(srcRow, srcRow + width * 4), dstRow);
  }
  ctx.putImageData(image, 0, 0);
  return canvas;
}

/** Frame an orthographic camera to a world-space AABB from a given view
 *  direction (unit vector from center toward camera), Z-up. A small margin
 *  keeps the mesh off the frame edge. */
function frameOrthoCamera(
  box: THREE.Box3,
  dir: THREE.Vector3,
): { camera: THREE.OrthographicCamera; spanRight: number; spanUp: number } {
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(1e-3, size.length() / 2);

  const up = new THREE.Vector3(0, 0, 1);
  // Degenerate up (looking straight down/up) → use +Y as the up hint.
  const forward = dir.clone().normalize();
  if (Math.abs(forward.dot(up)) > 0.999) up.set(0, 1, 0);
  const right = new THREE.Vector3().crossVectors(up, forward).normalize();
  const trueUp = new THREE.Vector3().crossVectors(forward, right).normalize();

  // Project the 8 box corners onto right/up to get the exact on-screen extent
  // for THIS view (an oblique view of a long box needs a wider frustum than the
  // axis-aligned span would suggest).
  const min = box.min;
  const max = box.max;
  let rMin = Infinity;
  let rMax = -Infinity;
  let uMin = Infinity;
  let uMax = -Infinity;
  for (let i = 0; i < 8; i++) {
    const corner = new THREE.Vector3(
      i & 1 ? max.x : min.x,
      i & 2 ? max.y : min.y,
      i & 4 ? max.z : min.z,
    ).sub(center);
    const r = corner.dot(right);
    const u = corner.dot(trueUp);
    if (r < rMin) rMin = r;
    if (r > rMax) rMax = r;
    if (u < uMin) uMin = u;
    if (u > uMax) uMax = u;
  }
  const margin = 1.06;
  const spanRight = Math.max(1e-3, (rMax - rMin) * margin);
  const spanUp = Math.max(1e-3, (uMax - uMin) * margin);

  const dist = radius * 3 + 1;
  const camera = new THREE.OrthographicCamera(
    -spanRight / 2,
    spanRight / 2,
    spanUp / 2,
    -spanUp / 2,
    0.01,
    dist * 2 + radius * 2,
  );
  camera.up.copy(up);
  camera.position.copy(center).addScaledVector(forward, dist);
  camera.lookAt(center);
  camera.updateMatrixWorld(true);
  camera.updateProjectionMatrix();
  return { camera, spanRight, spanUp };
}

/** Build a non-indexed clone of `geom` with a per-vertex COLOR_0 attribute that
 *  encodes each face's index as a uint24 (all 3 vertices of face f share the
 *  color). Non-normalized 0..1 floats: r=(id>>16)/255, g=((id>>8)&255)/255,
 *  b=(id&255)/255. Returns the fresh geometry (caller disposes) and faceCount.
 */
function buildTriIdGeometry(geom: THREE.BufferGeometry): {
  idGeom: THREE.BufferGeometry;
  faceCount: number;
} {
  // toNonIndexed() returns a FRESH geometry for indexed input (never mutating
  // the source); for already-non-indexed input it returns `this`, so clone().
  const idGeom = geom.index ? geom.toNonIndexed() : geom.clone();
  const pos = idGeom.getAttribute("position");
  const faceCount = Math.floor(pos.count / 3);
  const colors = new Float32Array(pos.count * 3);
  for (let f = 0; f < faceCount; f++) {
    const r = (f >> 16) & 0xff;
    const g = (f >> 8) & 0xff;
    const b = f & 0xff;
    const rf = r / 255;
    const gf = g / 255;
    const bf = b / 255;
    const base = f * 9;
    for (let v = 0; v < 3; v++) {
      colors[base + v * 3] = rf;
      colors[base + v * 3 + 1] = gf;
      colors[base + v * 3 + 2] = bf;
    }
  }
  idGeom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  return { idGeom, faceCount };
}

/** Decode a uint24 face id from an 8-bit RGB triple. */
function decodeId(r: number, g: number, b: number): number {
  return (r << 16) | (g << 8) | b;
}

/**
 * Capture 6 multi-view RGB + triangle-id buffers of `mesh` (which must belong
 * to the live `renderer`'s scene graph, though it is NOT rendered in place —
 * we clone it into an offscreen scene). Returns AgentView[]; the shared scene
 * and the mesh are never mutated. All temporary GPU/CPU resources are disposed
 * before returning.
 */
export function captureAgentViews(
  renderer: THREE.WebGLRenderer,
  mesh: THREE.Mesh,
): AgentView[] {
  const srcGeom = mesh.geometry as THREE.BufferGeometry;
  if (!srcGeom || !srcGeom.getAttribute("position")) return [];

  // World-space AABB of the mesh (it may be nested under transformed parents).
  mesh.updateWorldMatrix(true, false);
  const worldMatrix = mesh.matrixWorld.clone();
  const box = new THREE.Box3().setFromObject(mesh);
  if (box.isEmpty()) return [];

  // Preserve the renderer's current state so the visible frame is untouched.
  const prevRT = renderer.getRenderTarget();
  const prevClear = renderer.getClearColor(new THREE.Color());
  const prevClearAlpha = renderer.getClearAlpha();

  // ---- RGB pass scene: a clone of the mesh (geometry reference reused — we do
  // not mutate it) with plain white ambient + directional lighting. ----
  // Materials are CLONED (not shared) so we can strip any active slice-clipping
  // planes / overlay state Viewer3D may have set on the live materials without
  // mutating them — the capture must show the mesh whole, as authored.
  const rgbScene = new THREE.Scene();
  rgbScene.background = new THREE.Color(0x202020);
  const rgbMaterialClones: THREE.Material[] = [];
  const cloneMat = (m: THREE.Material): THREE.Material => {
    const c = m.clone();
    c.clippingPlanes = null;
    rgbMaterialClones.push(c);
    return c;
  };
  const rgbMesh = new THREE.Mesh(
    srcGeom, // reference reuse is fine (never mutated)
    Array.isArray(mesh.material)
      ? mesh.material.map(cloneMat)
      : cloneMat(mesh.material),
  );
  worldMatrix.decompose(rgbMesh.position, rgbMesh.quaternion, rgbMesh.scale);
  rgbMesh.updateMatrixWorld(true);
  rgbScene.add(rgbMesh);
  const ambient = new THREE.AmbientLight(0xffffff, 0.9);
  const dirLight = new THREE.DirectionalLight(0xffffff, 0.9);
  dirLight.position.set(0.5, -0.7, 1).normalize();
  rgbScene.add(ambient, dirLight);

  // ---- Triangle-id pass: non-indexed clone with per-face vertex colors,
  // rendered unlit over a white (0xFFFFFF) background. ----
  const { idGeom, faceCount } = buildTriIdGeometry(srcGeom);
  const idScene = new THREE.Scene();
  idScene.background = new THREE.Color(0xffffff);
  const idMaterial = new THREE.MeshBasicMaterial({
    vertexColors: true,
    toneMapped: false,
    side: THREE.DoubleSide,
  });
  const idMesh = new THREE.Mesh(idGeom, idMaterial);
  worldMatrix.decompose(idMesh.position, idMesh.quaternion, idMesh.scale);
  idMesh.updateMatrixWorld(true);
  idScene.add(idMesh);

  const views: AgentView[] = [];
  const disposables: { dispose: () => void }[] = [];
  let probeWarned = false;

  try {
    for (const { id, dir } of VIEW_DIRS) {
      const { camera, spanRight, spanUp } = frameOrthoCamera(box, dir);
      const { width, height } = viewPixelSize(spanRight, spanUp);

      // Two targets: the RGB target's texture is sRGB so three applies the
      // linear→sRGB encode when rendering INTO it (a render-target pass skips
      // the canvas output transform, so without this the RGB view reads dark).
      // The id target stays LINEAR so the uint24 face colors survive as the raw
      // values we wrote — the whole point of the id buffer. Both use samples=0:
      // MSAA on the id pass would blend face-id colors across edges and corrupt
      // the uint24.
      const rgbRt = new THREE.WebGLRenderTarget(width, height, {
        minFilter: THREE.NearestFilter,
        magFilter: THREE.NearestFilter,
        depthBuffer: true,
        samples: 0,
        colorSpace: THREE.SRGBColorSpace,
      });
      const idRt = new THREE.WebGLRenderTarget(width, height, {
        minFilter: THREE.NearestFilter,
        magFilter: THREE.NearestFilter,
        depthBuffer: true,
        samples: 0,
        colorSpace: THREE.NoColorSpace,
      });

      // RGB pass.
      renderer.setRenderTarget(rgbRt);
      renderer.setClearColor(0x202020, 1);
      renderer.clear();
      renderer.render(rgbScene, camera);
      const rgbCanvas = renderTargetToCanvas(renderer, rgbRt, width, height);
      const rgb_data_url = rgbCanvas.toDataURL("image/jpeg", RGB_JPEG_QUALITY);

      // Triangle-id pass.
      renderer.setRenderTarget(idRt);
      renderer.setClearColor(0xffffff, 1);
      renderer.clear();
      renderer.render(idScene, camera);
      const idCanvas = renderTargetToCanvas(renderer, idRt, width, height);
      const tri_id_png_data_url = idCanvas.toDataURL("image/png");

      // Color-fidelity probe: render-target passes skip the canvas sRGB output
      // transform in three r150+, so raw id values survive. Decode a non-
      // background pixel and warn if it decodes to an id >= faceCount (which
      // would mean the values were mangled by a color transform / AA).
      if (!probeWarned) {
        const ctx = idCanvas.getContext("2d")!;
        const data = ctx.getImageData(0, 0, width, height).data;
        for (let p = 0; p < data.length; p += 4) {
          const r = data[p];
          const g = data[p + 1];
          const b = data[p + 2];
          if (r === 255 && g === 255 && b === 255) continue; // background
          const decoded = decodeId(r, g, b);
          if (decoded >= faceCount) {
            console.warn(
              `[AgentCapture] tri-id probe: decoded face id ${decoded} exceeds ` +
                `faceCount ${faceCount} in view "${id}" — color fidelity may be off.`,
            );
            probeWarned = true;
          }
          break; // one probe pixel per view is enough
        }
      }

      disposables.push(rgbRt, idRt);
      views.push({ view_id: id, rgb_data_url, tri_id_png_data_url, width, height });
    }
  } finally {
    // Restore renderer state (visible frame untouched).
    renderer.setRenderTarget(prevRT);
    renderer.setClearColor(prevClear, prevClearAlpha);
    // Dispose everything we created (NOT the shared srcGeom / mesh.material).
    for (const d of disposables) d.dispose();
    for (const m of rgbMaterialClones) m.dispose();
    idGeom.dispose();
    idMaterial.dispose();
    ambient.dispose?.();
    dirLight.dispose?.();
  }

  return views;
}
