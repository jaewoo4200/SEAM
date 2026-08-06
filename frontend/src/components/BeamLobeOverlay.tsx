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
// Full-elevation revolution: rho(az, el) = r(az) * cos^VERT_POWER(el) over
// el in [-90, 90]. The radius vanishing at the poles closes every petal into
// a smooth balloon through the apex — no rims anywhere (the earlier +/-13 deg
// slab had hard top/bottom edges; user asked for an ellipsoid-like body).
// VERT_POWER sets the vertical beamwidth: 1 is literally a SPHERE (r=R*cos
// is a circle through the apex — that build looked like a giant ball), so a
// directed lobe needs a much faster falloff. 6 puts the vertical half-power
// point near +/-27 deg: an elongated teardrop instead of a ball.
const EL_STEPS = 13;
const VERT_POWER = 6;
// Azimuth subdivision between sweep samples (Catmull-Rom on the radius): a
// 10-deg codebook step otherwise renders every sample as a polygon corner.
// Interpolation only smooths BETWEEN measured points — it passes through
// every sample, so no lobe is invented.
const AZ_SUBDIV = 6;
// Silhouette lines darker than the body: a same-hue outline melts into the
// translucent fill; near-black reads on both the dark viewport and light maps.
const OUTLINE_COLOR = "#0b0f14";

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
    // Catmull-Rom resample of r over az: passes through every measured
    // sample, rounds the corners between them (clamped at 0 so an overshoot
    // below the floor cannot flip a petal inside out).
    if (az.length >= 2 && AZ_SUBDIV > 1) {
      const sAz: number[] = [];
      const sR: number[] = [];
      for (let i = 0; i < az.length - 1; i++) {
        const r0 = r[Math.max(0, i - 1)];
        const r1 = r[i];
        const r2 = r[i + 1];
        const r3 = r[Math.min(r.length - 1, i + 2)];
        for (let k = 0; k < AZ_SUBDIV; k++) {
          const t = k / AZ_SUBDIV;
          const t2 = t * t;
          const t3 = t2 * t;
          const rr =
            0.5 *
            (2 * r1 +
              (-r0 + r2) * t +
              (2 * r0 - 5 * r1 + 4 * r2 - r3) * t2 +
              (-r0 + 3 * r1 - 3 * r2 + r3) * t3);
          sAz.push(az[i] + ((az[i + 1] - az[i]) * k) / AZ_SUBDIV);
          sR.push(Math.max(0, rr));
        }
      }
      sAz.push(az[az.length - 1]);
      sR.push(r[r.length - 1]);
      return { az: sAz, r: sR };
    }
    return { az, r };
  }, [anglesDeg, powerDbm, axisDeg, radius]);

  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    const n = curve.r.length;
    if (n < 2) return g;
    const els: number[] = [];
    for (let j = 0; j < EL_STEPS; j++) {
      els.push((-90 + (180 * j) / (EL_STEPS - 1)) * DEG);
    }
    // One smooth shell per petal, closed through the apex at both poles (rho
    // -> 0 at el = +/-90), plus the two azimuth-end caps. No lids and no
    // interior membranes: earlier versions stacked apex fans inside the body
    // ("several overlapping beams") or cut the arc at +/-13 deg (hard rims).
    const pos: number[] = [];
    const push = (i: number, j: number) => {
      const a = curve.az[i];
      const e = els[j];
      const ce = Math.cos(e);
      const rho = curve.r[i] * Math.pow(Math.abs(ce), VERT_POWER);
      pos.push(rho * ce * Math.cos(a), rho * ce * Math.sin(a), rho * Math.sin(e));
    };
    const apex = () => pos.push(0, 0, 0);
    for (let i = 0; i < n - 1; i++) {
      for (let j = 0; j < EL_STEPS - 1; j++) {
        push(i, j);
        push(i + 1, j);
        push(i + 1, j + 1);
        push(i, j);
        push(i + 1, j + 1);
        push(i, j + 1);
      }
    }
    // End caps: flat fans closing the first/last azimuth edge (their edge
    // curve already starts and ends at the apex via the pole pinch).
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

  // Silhouette outline: the meridian curves closing the two azimuth ends
  // (they start and end at the apex via the pole pinch). The 0.35-alpha body
  // alone melts into busy scenes; full-opacity dark borders keep the lobe
  // readable as one object.
  const outline = useMemo(() => {
    const meridian = (i: number) => {
      const pts: Vec3[] = [];
      for (let j = 0; j < EL_STEPS; j++) {
        const e = (-90 + (180 * j) / (EL_STEPS - 1)) * DEG;
        const ce = Math.cos(e);
        const rho = curve.r[i] * Math.pow(Math.abs(ce), VERT_POWER);
        pts.push([
          rho * ce * Math.cos(curve.az[i]),
          rho * ce * Math.sin(curve.az[i]),
          rho * Math.sin(e),
        ]);
      }
      return pts;
    };
    return { edges: [meridian(0), meridian(curve.r.length - 1)] };
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
      <Line points={crest} color={OUTLINE_COLOR} lineWidth={1.5} />
      {outline.edges.map((pts, i) => (
        <Line key={i} points={pts} color={OUTLINE_COLOR} lineWidth={1.25} />
      ))}
    </group>
  );
}
