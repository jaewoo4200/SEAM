/**
 * Environment presets (indoor / outdoor) and the "auto" inference used by the
 * Toolbar Environment select.
 *
 * A preset bundles solver knobs that are sensible defaults for a scale of
 * scene, plus a camera hint the viewer *could* consume. Applying a preset
 * patches the two solver configs in the store (pathsConfig / radioMapConfig);
 * it does NOT persist anything as a project default — the user still has to
 * explicitly Save (SolverControls) for that.
 *
 * Contract note: the shape here only ever produces `Partial<SimulationConfig>`
 * / `Partial<RadioMapGridConfig>` patches, so it never introduces keys outside
 * the pinned wire types.
 */

import type {
  Environment,
  RadioMapGridConfig,
  Scene,
  SimulationConfig,
  Vec3,
} from "./types/api";

/** A resolved environment is never "auto" — it is what "auto" infers to. */
export type ResolvedEnvironment = Exclude<Environment, "auto">;

export interface EnvPreset {
  /** Patch applied to the paths solver config. */
  paths: Partial<SimulationConfig>;
  /** Patch applied to the radio-map grid (nested under SimulationConfig.radio_map). */
  radioMap: Partial<RadioMapGridConfig>;
  /** Suggested camera position (Z-up ENU meters). Viewer3D may consume this. */
  cameraPos: Vec3;
  /** Free-form marker/scale hint for the viewer ("indoor" | "outdoor"). */
  markerHint: ResolvedEnvironment;
}

/**
 * The two concrete presets. `num_samples` stays at 1e6 for both (matching the
 * backend SimulationConfig default) so the ray budget is unchanged; the depth
 * and mechanism knobs are what actually differ.
 */
export const ENV_PRESETS: Record<ResolvedEnvironment, EnvPreset> = {
  indoor: {
    paths: {
      max_depth: 5,
      refraction: true,
      num_samples: 1_000_000,
    },
    radioMap: {
      cell_size_m: 0.25,
      height_m: 1.2,
    },
    cameraPos: [8, -8, 6],
    markerHint: "indoor",
  },
  outdoor: {
    paths: {
      max_depth: 3,
      refraction: false,
      num_samples: 1_000_000,
    },
    radioMap: {
      cell_size_m: 2.0,
      height_m: 1.5,
    },
    cameraPos: [60, -60, 45],
    markerHint: "outdoor",
  },
};

/**
 * Indoor radio maps also want a shallower ray budget than the outdoor sweep;
 * the preset carries a max_depth for the radio-map grid too (the store applies
 * it to radioMapConfig.max_depth). Kept as a companion map so ENV_PRESETS stays
 * a clean {paths, radioMap} shape.
 */
export const ENV_RADIOMAP_DEPTH: Record<ResolvedEnvironment, number> = {
  indoor: 4,
  outdoor: 3,
};

/** Below this bbox extent (meters, largest axis span) a scene reads as indoor. */
export const INDOOR_EXTENT_M = 25;

/**
 * Infer indoor/outdoor from the scene's spatial extent: the largest axis span
 * over device positions and prim translations. An empty scene defaults to
 * outdoor (the safer, wider-camera choice). This is the "auto" resolution.
 */
export function inferEnvironment(scene: Scene | null): ResolvedEnvironment {
  if (!scene) return "outdoor";
  const pts: Vec3[] = [];
  for (const d of scene.devices) pts.push(d.position);
  for (const p of scene.prims) {
    if (p.type === "mesh_primitive") pts.push(p.transform.translation);
  }
  if (pts.length < 2) return "outdoor";
  let maxSpan = 0;
  for (let axis = 0; axis < 3; axis++) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const pt of pts) {
      const v = pt[axis];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    maxSpan = Math.max(maxSpan, hi - lo);
  }
  return maxSpan < INDOOR_EXTENT_M ? "indoor" : "outdoor";
}

/**
 * Resolve a scene.environment value to a concrete indoor/outdoor: pass through
 * explicit choices, infer only when "auto". This is what the store exposes as
 * `resolvedEnvironment` for the viewer.
 */
export function resolveEnvironment(
  environment: Environment,
  scene: Scene | null,
): ResolvedEnvironment {
  return environment === "auto" ? inferEnvironment(scene) : environment;
}

/** The preset a given environment value should apply, resolving "auto" first. */
export function presetForEnvironment(
  environment: Environment,
  scene: Scene | null,
): EnvPreset {
  return ENV_PRESETS[resolveEnvironment(environment, scene)];
}
