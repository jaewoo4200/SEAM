/**
 * Helpers for the SimulationConfig solver surface.
 *
 * The scene's stored simulation_configs may predate newer fields (the backend
 * Pydantic model fills defaults on load, but an older scene.sionnatwin.json can
 * still round-trip a partial object into the frontend). `normalizeConfig` fills
 * every field so the object we send back on PUT/simulate exactly matches the
 * StrictModel contract (no missing keys, no extras).
 */

import type { RadioMapGridConfig, SimulationConfig } from "./types/api";

/** Backend RadioMapGridConfig defaults (schemas/simulation.py). */
export function defaultRadioMapGrid(): RadioMapGridConfig {
  return { cell_size_m: 2.0, height_m: 1.5, metric: "path_gain_db" };
}

/**
 * Backend SimulationConfig defaults (schemas/simulation.py). Single source of
 * truth for a fresh solver config when a scene has none.
 */
export function defaultSimConfig(): SimulationConfig {
  return {
    id: "default",
    name: "Default",
    backend: "auto",
    frequency_hz: 28e9,
    max_depth: 3,
    tx_ids: null,
    rx_ids: null,
    los: true,
    reflection: true,
    scattering: false,
    refraction: false,
    diffraction: false,
    edge_diffraction: false,
    diffraction_lit_region: false,
    engine: null,
    synthetic_array: true,
    seed: 42,
    num_samples: 1_000_000,
    bandwidth_hz: 100e6,
    noise_figure_db: 7.0,
    radio_map: defaultRadioMapGrid(),
  };
}

/**
 * Merge a possibly-partial stored config onto the full default set so every
 * field is present and typed. Unknown extra keys are dropped (we rebuild the
 * object field-by-field), keeping the PUT body StrictModel-clean.
 */
export function normalizeConfig(
  raw: Partial<SimulationConfig> | null | undefined,
): SimulationConfig {
  const d = defaultSimConfig();
  if (!raw) return d;
  const rm = raw.radio_map ?? undefined;
  return {
    id: raw.id ?? d.id,
    name: raw.name ?? d.name,
    backend: raw.backend ?? d.backend,
    frequency_hz: raw.frequency_hz ?? d.frequency_hz,
    max_depth: raw.max_depth ?? d.max_depth,
    tx_ids: raw.tx_ids ?? d.tx_ids,
    rx_ids: raw.rx_ids ?? d.rx_ids,
    los: raw.los ?? d.los,
    reflection: raw.reflection ?? d.reflection,
    scattering: raw.scattering ?? d.scattering,
    refraction: raw.refraction ?? d.refraction,
    diffraction: raw.diffraction ?? d.diffraction,
    edge_diffraction: raw.edge_diffraction ?? d.edge_diffraction,
    diffraction_lit_region: raw.diffraction_lit_region ?? d.diffraction_lit_region,
    engine: raw.engine ?? d.engine,
    synthetic_array: raw.synthetic_array ?? d.synthetic_array,
    seed: raw.seed ?? d.seed,
    num_samples: raw.num_samples ?? d.num_samples,
    bandwidth_hz: raw.bandwidth_hz ?? d.bandwidth_hz,
    noise_figure_db: raw.noise_figure_db ?? d.noise_figure_db,
    radio_map: {
      cell_size_m: rm?.cell_size_m ?? d.radio_map.cell_size_m,
      height_m: rm?.height_m ?? d.radio_map.height_m,
      metric: rm?.metric ?? d.radio_map.metric,
    },
  };
}
