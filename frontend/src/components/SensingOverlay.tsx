/**
 * Radar sensing overlay: a marker at every scattering point of every target
 * and the echo paths (target_id set) colored by Doppler on a symmetric
 * diverging scale — red approaching, blue receding, gray no radial motion.
 * Comm paths appended by include_comm_paths are NOT drawn here; the Rays
 * overlay owns those.
 */

import { useMemo } from "react";
import { Line } from "@react-three/drei";
import type { RayPath, SensingResultSet } from "../types/api";
import { PATH_COLORS } from "./common";
import { dopplerColor, dopplerGradientCss, sensingDopplerRange } from "../utils/dopplerColor";

/** Doppler-colored echo rays: one line per path with a target_id (comm
 *  paths in the same list are skipped). maxAbs is the ±Hz color scale. */
export function SensingEchoLines({
  paths,
  maxAbs,
  lineWidth = 2,
}: {
  paths: RayPath[];
  maxAbs: number;
  lineWidth?: number;
}) {
  return (
    <>
      {paths
        .filter((p) => p.target_id != null)
        .map((p) => (
          <Line
            key={p.path_id}
            points={p.vertices}
            color={dopplerColor(p.doppler_hz, maxAbs)}
            lineWidth={lineWidth}
          />
        ))}
    </>
  );
}

/** ``maxAbs`` overrides the result's own ±Hz scale, so these echoes match
 *  the one legend on screen (the scenario run's, while its layer shows too). */
export default function SensingOverlay({
  result,
  markerRadius,
  maxAbs: sharedMaxAbs,
}: {
  result: SensingResultSet;
  markerRadius: number;
  maxAbs?: number;
}) {
  const ownMaxAbs = useMemo(() => sensingDopplerRange(result.paths), [result]);
  const maxAbs = sharedMaxAbs ?? ownMaxAbs;
  return (
    <group userData={{ __noFit: true }}>
      <SensingEchoLines paths={result.paths} maxAbs={maxAbs} />
      {/* Single-point targets put their SP inside the actor's own
          (translucent) box: draw on top like the pick markers. */}
      {result.targets.flatMap((t) =>
        t.scattering_points.map((sp, i) => (
          <mesh key={`${t.actor_id}_sp${i}`} position={sp} renderOrder={999}>
            <sphereGeometry args={[markerRadius, 16, 12]} />
            <meshBasicMaterial
              color={PATH_COLORS.sensing}
              transparent
              opacity={0.95}
              depthTest={false}
            />
          </mesh>
        )),
      )}
    </group>
  );
}

/** Doppler colorbar (HTML, outside the Canvas); stacks under the radio-map
 *  legend when that one is visible. Renders nothing without echo paths. */
export function SensingDopplerLegend({
  paths,
  stacked = true,
}: {
  paths: RayPath[];
  stacked?: boolean;
}) {
  if (!paths.some((p) => p.target_id != null)) return null;
  const maxAbs = sensingDopplerRange(paths);
  return (
    <div className={"radiomap-legend sensing-legend" + (stacked ? "" : " solo")}>
      <div className="radiomap-legend-title">Doppler (Hz)</div>
      <div className="radiomap-legend-scale">
        <div className="radiomap-legend-bar" style={{ background: dopplerGradientCss() }} />
        <div className="radiomap-legend-ticks">
          <span>+{maxAbs.toFixed(0)}</span>
          <span>0</span>
          <span>−{maxAbs.toFixed(0)}</span>
        </div>
      </div>
      <div className="sensing-legend-caption">red approaching · blue receding</div>
    </div>
  );
}
