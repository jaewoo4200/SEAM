/**
 * Radar sensing overlay: a marker at every scattering point of every target
 * and the echo paths (target_id set) colored by Doppler on a symmetric
 * diverging scale — red approaching, blue receding, gray no radial motion.
 * Comm paths appended by include_comm_paths are NOT drawn here; the Rays
 * overlay owns those.
 */

import { useMemo } from "react";
import { Line } from "@react-three/drei";
import type { SensingResultSet } from "../types/api";
import { PATH_COLORS } from "./common";
import { dopplerColor, dopplerGradientCss, sensingDopplerRange } from "../utils/dopplerColor";

export default function SensingOverlay({
  result,
  markerRadius,
}: {
  result: SensingResultSet;
  markerRadius: number;
}) {
  const echoes = useMemo(() => result.paths.filter((p) => p.target_id != null), [result]);
  const maxAbs = useMemo(() => sensingDopplerRange(result.paths), [result]);
  return (
    <group userData={{ __noFit: true }}>
      {echoes.map((p) => (
        <Line
          key={p.path_id}
          points={p.vertices}
          color={dopplerColor(p.doppler_hz, maxAbs)}
          lineWidth={2}
        />
      ))}
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
  result,
  stacked = true,
}: {
  result: SensingResultSet;
  stacked?: boolean;
}) {
  if (!result.paths.some((p) => p.target_id != null)) return null;
  const maxAbs = sensingDopplerRange(result.paths);
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
