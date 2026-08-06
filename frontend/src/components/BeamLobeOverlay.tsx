/**
 * Dynamic beam lobe: the antenna lobe built DIRECTLY from a measured sweep
 * curve, never from a canned shape. r(az) is that azimuth's power normalized
 * over a fixed dB window, so a concentrated curve is a narrow spike and a
 * spread curve is a wide lobe BY CONSTRUCTION — the geometry IS the metric
 * display. Alpha stays fixed on purpose: a constant silhouette whose opacity
 * tracks the metric would not show beamwidth at all.
 *
 * Both consumers pass a dB-scale curve — the static beamforming result sends
 * codebook sweep GAIN (dB), playback frames send absolute beam power (dBm).
 * Normalization is peak-relative, so the two are interchangeable here.
 *
 * Z-up scene: azimuth is atan2(y, x) about +Z, the same convention as
 * RayPath.aod_deg (backend/seam_studio/schemas/results.py:47).
 */

import { useEffect, useMemo } from "react";
import * as THREE from "three";
import { Line } from "@react-three/drei";
import type { BeamformingResult, Vec3 } from "../types/api";

// Display window below the peak. Everything at or under peak-25 dB collapses
// to r=0, which is also the sidelobe floor the mock backend clamps to.
const DYNAMIC_RANGE_DB = 25;
// r = radius * t^SHARPEN. The exponent only sharpens the CONTRAST between main
// lobe and sidelobes; it does not invent shape (t is monotone in the data).
const SHARPEN = 1.5;
// Vertical extrusion of the horizontal curve: an elevation arc of +/-EL_MAX_DEG
// in EL_STEPS rings, so the lobe reads as a 3D body instead of a flat ribbon.
// Odd step count keeps one ring exactly at elevation 0 (the crest).
const EL_STEPS = 9;
const EL_MAX_DEG = 13;

const DEG = Math.PI / 180;

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

/** World azimuth (deg) that sweep angle 0 points along.
 *
 *  The two backends anchor the codebook differently and NOTHING in
 *  BeamformingResult records which one applied, so the convention lives here:
 *  - "mock": the analytic lobe is centered on the world TX->RX azimuth, i.e.
 *    its sweep angles are already world-anchored (mock_backend.py:560) — the
 *    axis is 0.
 *  - "sionna": the panels are added with look_at each other unless the request
 *    sets use_device_orientation (sionna_backend.py:1023-1032, "panels face
 *    each other ... use_device_orientation=True instead honors the devices'
 *    own orientation_deg"), and appStore.runBeamforming never sends that flag,
 *    so the codebook is LINK-RELATIVE and its axis is the TX->RX azimuth.
 *
 *  Playback frames do not go through here: the backend already reprojects them
 *  onto world azimuth (PlaybackFrame.beam_azimuth_deg), so callers pass 0.
 */
export function beamSweepAxisDeg(
  result: Pick<BeamformingResult, "backend">,
  txPos: Vec3,
  rxPos: Vec3,
): number {
  if (result.backend === "mock") return 0;
  return Math.atan2(rxPos[1] - txPos[1], rxPos[0] - txPos[0]) / DEG;
}

export default function BeamLobeOverlay({
  origin,
  axisDeg,
  anglesDeg,
  powerDbm,
  radius,
  tiltDeg = 0,
  color = "#4fc3f7",
}: {
  /** Lobe apex in world meters (the TX device position). */
  origin: Vec3;
  /** World azimuth of sweep angle 0 — see beamSweepAxisDeg. */
  axisDeg: number;
  anglesDeg: number[];
  /** Parallel to anglesDeg; null marks sweep cells the backend could not
   *  evaluate (drawn at r=0, so a gap reads as "no beam here"). */
  powerDbm: (number | null)[];
  /** Radius of the peak sample in meters (scene-scaled by the caller). */
  radius: number;
  /** Elevation pitch of the whole fan (deg, + up). look_at sweeps aim the
   *  panel at the RX in 3D, so codebook angle 0 carries the link's downtilt —
   *  a rooftop TX firing at a ground RX must NOT draw a horizontal lobe
   *  hovering over the void. Fixed-bearing (device-orientation) sweeps stay
   *  at 0: elevation steering is not modeled there. */
  tiltDeg?: number;
  color?: string;
}) {
  // Polar curve in LOCAL coordinates (the group carries the origin): world
  // azimuth per sample and the normalized radius that is the whole point of
  // this overlay.
  const curve = useMemo(() => {
    const n = Math.min(anglesDeg.length, powerDbm.length);
    const az: number[] = [];
    const r: number[] = [];
    let peak = -Infinity;
    for (let i = 0; i < n; i++) {
      const p = powerDbm[i];
      if (p !== null && Number.isFinite(p) && p > peak) peak = p;
    }
    // No finite sample anywhere: there is no lobe to draw (not a round blob).
    if (!Number.isFinite(peak)) return { az, r };
    const floor = peak - DYNAMIC_RANGE_DB;
    for (let i = 0; i < n; i++) {
      const p = powerDbm[i];
      const t =
        p === null || !Number.isFinite(p)
          ? 0
          : clamp01((p - floor) / DYNAMIC_RANGE_DB);
      az.push((axisDeg + anglesDeg[i]) * DEG);
      r.push(radius * Math.pow(t, SHARPEN));
    }
    return { az, r };
  }, [anglesDeg, powerDbm, axisDeg, radius]);

  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    const n = curve.r.length;
    if (n < 2) return g;
    const els: number[] = [];
    for (let j = 0; j < EL_STEPS; j++) {
      els.push((-EL_MAX_DEG + (2 * EL_MAX_DEG * j) / (EL_STEPS - 1)) * DEG);
    }
    // ONE closed rounded body per petal: outer shell + top/bottom lids + the
    // two side caps. An earlier version also drew an apex fan at EVERY
    // elevation ring, which stacked five translucent sheets inside the body
    // and read as "several overlapping beams" (user report) — interior
    // membranes are exactly what a closed surface must not have.
    const pos: number[] = [];
    const push = (i: number, j: number) => {
      const r = curve.r[i];
      const a = curve.az[i];
      const e = els[j];
      // Spherical sample: cos(elevation) tapers the horizontal radius, so the
      // vertical cross-section is a smooth arc rather than a box edge.
      pos.push(r * Math.cos(e) * Math.cos(a), r * Math.cos(e) * Math.sin(a), r * Math.sin(e));
    };
    const apex = () => pos.push(0, 0, 0);
    const top = EL_STEPS - 1;
    for (let i = 0; i < n - 1; i++) {
      // Outer shell between adjacent rings.
      for (let j = 0; j < EL_STEPS - 1; j++) {
        push(i, j);
        push(i + 1, j);
        push(i + 1, j + 1);
        push(i, j);
        push(i + 1, j + 1);
        push(i, j + 1);
      }
      // Lids: apex to the outermost rings only.
      apex();
      push(i, top);
      push(i + 1, top);
      apex();
      push(i + 1, 0);
      push(i, 0);
    }
    // Side caps close the first/last azimuth edge across the elevation arc.
    for (let j = 0; j < EL_STEPS - 1; j++) {
      apex();
      push(0, j + 1);
      push(0, j);
      apex();
      push(n - 1, j);
      push(n - 1, j + 1);
    }
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    return g;
  }, [curve]);

  // The memo mints a new BufferGeometry on every curve change (every playback
  // frame); without this the old GPU buffers leak for the whole session.
  useEffect(() => () => geometry.dispose(), [geometry]);

  // Crest polyline at elevation 0: the r(az) curve itself, drawn bright so the
  // beamwidth is readable through the translucent body.
  const crest = useMemo(
    () =>
      curve.r.map(
        (r, i) => [r * Math.cos(curve.az[i]), r * Math.sin(curve.az[i]), 0] as Vec3,
      ),
    [curve],
  );

  // Silhouette outline: top/bottom elevation rings plus the four apex edges.
  // The 0.35-alpha body alone melts into busy scenes; a full-opacity border
  // keeps the lobe readable as one object.
  const outline = useMemo(() => {
    const ring = (e: number) =>
      curve.r.map(
        (r, i) =>
          [
            r * Math.cos(e) * Math.cos(curve.az[i]),
            r * Math.cos(e) * Math.sin(curve.az[i]),
            r * Math.sin(e),
          ] as Vec3,
      );
    const top = ring(EL_MAX_DEG * DEG);
    const bottom = ring(-EL_MAX_DEG * DEG);
    const apex: Vec3 = [0, 0, 0];
    const edges: Vec3[][] = [
      [apex, top[0]],
      [apex, top[top.length - 1]],
      [apex, bottom[0]],
      [apex, bottom[bottom.length - 1]],
    ];
    return { top, bottom, edges };
  }, [curve]);

  // Pitch the whole fan about the horizontal axis perpendicular to the aim
  // azimuth: a point at azimuth a0 maps to (cos e * xy, sin e * z), i.e. the
  // fan tilts toward/away from the target instead of staying in the plane.
  const quaternion = useMemo(() => {
    const q = new THREE.Quaternion();
    if (tiltDeg) {
      const a0 = axisDeg * DEG;
      q.setFromAxisAngle(
        new THREE.Vector3(Math.sin(a0), -Math.cos(a0), 0).normalize(),
        tiltDeg * DEG,
      );
    }
    return q;
  }, [axisDeg, tiltDeg]);

  if (curve.r.length < 2) return null;

  return (
    // __noFit is MANDATORY: the lobe is UI, not scene geometry. Untagged, it
    // wins the surface-probe raycast (device AGL) and drags the camera fit,
    // exactly like the device markers documented in Viewer3D.tsx:624-628.
    <group position={origin} quaternion={quaternion} userData={{ __noFit: true }}>
      <mesh geometry={geometry} renderOrder={2}>
        <meshBasicMaterial
          color={color}
          transparent
          opacity={0.35}
          depthWrite={false}
          side={THREE.DoubleSide}
        />
      </mesh>
      <Line points={crest} color={color} lineWidth={2} />
      <Line points={outline.top} color={color} lineWidth={1.25} />
      <Line points={outline.bottom} color={color} lineWidth={1.25} />
      {outline.edges.map((pts, i) => (
        <Line key={i} points={pts} color={color} lineWidth={1.25} />
      ))}
    </group>
  );
}
