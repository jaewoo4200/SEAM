/**
 * Mesh radio map overlay: the metric value sampled per surface triangle,
 * drawn as small jet-colored discs draped on the geometry (each disc lies in
 * its triangle's plane via a quaternion from +Z to the face normal).
 *
 * One InstancedMesh for all discs across all surfaces keeps the draw cheap
 * even for thousands of triangles. Tagged __noFit like the other result
 * overlays so it never pulls the auto-fit camera.
 */

import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { MeshRadioMapResultSet } from "../types/api";

// Jet colormap (blue -> cyan -> green -> yellow -> red), same anchors as the
// radio-map plane so the mesh map reads consistently against it.
const JET: [number, number, number][] = [
  [0, 0, 131],
  [0, 60, 170],
  [5, 255, 255],
  [255, 255, 0],
  [250, 0, 0],
  [128, 0, 0],
];

function jetColor(t: number, out: THREE.Color): THREE.Color {
  const x = Math.min(1, Math.max(0, t)) * (JET.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = JET[i];
  const b = JET[Math.min(i + 1, JET.length - 1)];
  // Instance colors are read in the working (linear) space by three; setRGB
  // with SRGBColorSpace matches the CSS legend the panel renders.
  return out.setRGB(
    (a[0] + (b[0] - a[0]) * f) / 255,
    (a[1] + (b[1] - a[1]) * f) / 255,
    (a[2] + (b[2] - a[2]) * f) / 255,
    THREE.SRGBColorSpace,
  );
}

/** [min, max] over every non-null value across all surfaces. */
export function meshRadioMapRange(result: MeshRadioMapResultSet): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (const s of result.surfaces) {
    for (const v of s.values) {
      if (v === null || !Number.isFinite(v)) continue;
      if (v < min) min = v;
      if (v > max) max = v;
    }
  }
  if (!Number.isFinite(min)) return [0, 1];
  if (max - min < 1e-9) max = min + 1;
  return [min, max];
}

/** Rough disc radius: ~0.45·sqrt(mean triangle area). Falls back to 0.3 m when
 *  no area can be estimated (missing/degenerate geometry). Estimated from the
 *  mean nearest-neighbour spacing of centers per surface as a stand-in for the
 *  triangle size (the surface does not ship per-triangle areas). */
function discRadius(result: MeshRadioMapResultSet): number {
  // Estimate spacing from a bounded sample of center-to-center gaps.
  let sum = 0;
  let n = 0;
  for (const s of result.surfaces) {
    const c = s.centers;
    const step = Math.max(1, Math.floor(c.length / 64)); // cap the work
    for (let i = step; i < c.length; i += step) {
      const a = c[i - step];
      const b = c[i];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
      if (d > 1e-4 && Number.isFinite(d)) {
        sum += d;
        n += 1;
      }
    }
  }
  if (n === 0) return 0.3;
  // Half the mean spacing ≈ disc radius that tiles without heavy overlap.
  const r = (sum / n) * 0.5;
  return Number.isFinite(r) && r > 1e-3 ? Math.min(r, 2) : 0.3;
}

export default function MeshRadioMapOverlay({
  result,
}: {
  result: MeshRadioMapResultSet;
}) {
  const meshRef = useRef<THREE.InstancedMesh>(null);

  // Flat list of (center, normal, value) across every surface.
  const instances = useMemo(() => {
    const centers: [number, number, number][] = [];
    const normals: [number, number, number][] = [];
    const values: (number | null)[] = [];
    for (const s of result.surfaces) {
      const n = Math.min(s.centers.length, s.normals.length, s.values.length);
      for (let i = 0; i < n; i++) {
        centers.push(s.centers[i]);
        normals.push(s.normals[i]);
        values.push(s.values[i]);
      }
    }
    return { centers, normals, values };
  }, [result]);

  const [vmin, vmax] = useMemo(() => meshRadioMapRange(result), [result]);
  const radius = useMemo(() => discRadius(result), [result]);
  const count = instances.centers.length;

  // Rebuild instance matrices + colors whenever the data or radius changes.
  useEffect(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    const up = new THREE.Vector3(0, 0, 1);
    const normal = new THREE.Vector3();
    const quat = new THREE.Quaternion();
    const pos = new THREE.Vector3();
    const scl = new THREE.Vector3(1, 1, 1);
    const mat = new THREE.Matrix4();
    const color = new THREE.Color();
    const span = vmax - vmin || 1;
    for (let i = 0; i < count; i++) {
      const c = instances.centers[i];
      const nrm = instances.normals[i];
      normal.set(nrm[0], nrm[1], nrm[2]);
      if (normal.lengthSq() < 1e-9) normal.copy(up);
      else normal.normalize();
      quat.setFromUnitVectors(up, normal);
      pos.set(c[0], c[1], c[2]);
      mat.compose(pos, quat, scl);
      mesh.setMatrixAt(i, mat);
      const v = instances.values[i];
      if (v === null || !Number.isFinite(v)) {
        // No value here: paint it dark grey so gaps read as "no data", not blue.
        color.setRGB(0.16, 0.18, 0.22, THREE.SRGBColorSpace);
      } else {
        jetColor((v - vmin) / span, color);
      }
      mesh.setColorAt(i, color);
    }
    mesh.count = count;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }, [instances, count, vmin, vmax, radius]);

  if (count === 0) return null;

  return (
    <instancedMesh
      ref={meshRef}
      // args are only read on first mount; count drives how many draw.
      args={[undefined, undefined, count]}
      userData={{ __noFit: true }}
      renderOrder={2}
    >
      <circleGeometry args={[radius, 12]} />
      <meshBasicMaterial
        vertexColors
        side={THREE.DoubleSide}
        transparent
        opacity={0.9}
        depthWrite={false}
      />
    </instancedMesh>
  );
}
