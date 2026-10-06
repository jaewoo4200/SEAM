/**
 * ISAC trade-off lobes: per TX, the comm beam (cyan) and the sensing beam
 * (magenta) of the stored ISAC result, drawn as codebook azimuth cuts on the
 * TX's fixed-bearing panel. The ISAC codebook is device-orientation anchored
 * (use_device_orientation is mandatory there), so codebook angle 0 is the
 * device yaw and the fan carries the panel pitch. With a 2-D (elevation)
 * codebook each lobe is the elevation slice holding its beam: the fan is
 * tilted by that beam's local elevation on top of the panel pitch.
 */

import { Fragment, useMemo } from "react";
import BeamLobeOverlay, { codebookBeamCurve } from "./BeamLobeOverlay";
import type { Device, ISACResultSet } from "../types/api";

export const ISAC_COMM_COLOR = "#4fc3f7";
export const ISAC_SENSING_COLOR = "#e040fb";

export function IsacBeamLobes({
  result,
  devices,
  radius,
}: {
  result: ISACResultSet;
  devices: Device[];
  radius: number;
}) {
  const lobes = useMemo(() => {
    const out: {
      key: string;
      tx: Device;
      rows: number;
      comm: ReturnType<typeof codebookBeamCurve> | null;
      sensing: ReturnType<typeof codebookBeamCurve> | null;
      commElDeg: number;
      sensingElDeg: number;
    }[] = [];
    for (const t of result.txs) {
      const tx = devices.find((d) => d.id === t.tx_id);
      if (!tx) continue;
      const cols = t.tx_array[1] ?? 1;
      const spacing = tx.antenna.horizontal_spacing ?? 0.5;
      out.push({
        key: t.tx_id,
        tx,
        rows: t.tx_array[0] ?? 1,
        comm:
          t.comm_beam_angle_deg !== null
            ? codebookBeamCurve(cols, spacing, t.comm_beam_angle_deg)
            : null,
        sensing:
          t.sensing_beam_angle_deg !== null
            ? codebookBeamCurve(cols, spacing, t.sensing_beam_angle_deg)
            : null,
        // 2-D codebook: the azimuth cut of the elevation slice that holds the
        // beam (azimuth-only lobes when the result has no elevation sweep).
        commElDeg: t.comm_beam_elevation_deg ?? 0,
        sensingElDeg: t.sensing_beam_elevation_deg ?? 0,
      });
    }
    return out;
  }, [result, devices]);

  return (
    <>
      {lobes.map(({ key, tx, rows, comm, sensing, commElDeg, sensingElDeg }) => (
        <Fragment key={key}>
          {comm && (
            <BeamLobeOverlay
              origin={tx.position}
              axisDeg={tx.orientation_deg[0]}
              // Sionna: +pitch tilts the boresight DOWN; BeamLobeOverlay tilts up.
              tiltDeg={-tx.orientation_deg[1] + commElDeg}
              txRows={rows}
              anglesDeg={comm.anglesDeg}
              powerDbm={comm.powerDb}
              color={ISAC_COMM_COLOR}
              radius={radius}
            />
          )}
          {sensing && (
            <BeamLobeOverlay
              origin={tx.position}
              axisDeg={tx.orientation_deg[0]}
              tiltDeg={-tx.orientation_deg[1] + sensingElDeg}
              txRows={rows}
              anglesDeg={sensing.anglesDeg}
              powerDbm={sensing.powerDb}
              color={ISAC_SENSING_COLOR}
              // Smaller so identical comm/sensing beams do not z-fight.
              radius={0.85 * radius}
            />
          )}
        </Fragment>
      ))}
    </>
  );
}
