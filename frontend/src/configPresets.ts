/**
 * Solver config presets for the SolverControls Preset dropdown.
 *
 * A preset bundles the common solver knobs (frequency, depth, mechanisms,
 * samples, bandwidth) plus the radio-map grid cell/height for a canonical
 * deployment scenario. Selecting a preset patches BOTH the paths config and the
 * radio-map config in the store, keeping the user's backend/tx/rx selections
 * (those are not part of a preset).
 *
 * "Custom" is the sentinel for "the user has hand-edited the config"; it carries
 * no patch and is auto-selected whenever the live config no longer matches any
 * named preset.
 *
 * Every patch is a `Partial<SimulationConfig>` (with an optional nested
 * radio_map grid patch), so presets never introduce keys outside the pinned
 * wire types.
 */

import type { RadioMapGridConfig, SimulationConfig } from "./types/api";

export type ConfigPresetId =
  | "indoor_lab_28"
  | "outdoor_campus_28"
  | "urban_macro_35"
  | "indoor_60"
  | "uav_a2g_28"
  | "custom";

export interface ConfigPreset {
  id: ConfigPresetId;
  label: string;
  /**
   * Solver-config fields the preset sets. Applied to both paths and radio-map
   * configs. `undefined` means "leave whatever the user had".
   */
  config: Partial<
    Pick<
      SimulationConfig,
      | "frequency_hz"
      | "max_depth"
      | "los"
      | "reflection"
      | "scattering"
      | "refraction"
      | "diffraction"
      | "edge_diffraction"
      | "num_samples"
      | "bandwidth_hz"
    >
  >;
  /** Radio-map grid fields (nested under SimulationConfig.radio_map). */
  radioMap: Partial<Pick<RadioMapGridConfig, "cell_size_m" | "height_m">>;
}

/**
 * Ordered, named presets. "Custom" is intentionally excluded here (it is the
 * fallback shown when nothing matches); PRESETS holds only the concrete ones.
 */
export const PRESETS: ConfigPreset[] = [
  {
    id: "indoor_lab_28",
    label: "28 GHz Indoor Lab",
    config: {
      frequency_hz: 28e9,
      max_depth: 5,
      reflection: true,
      refraction: true,
      scattering: true,
      num_samples: 1_000_000,
    },
    radioMap: { cell_size_m: 0.25, height_m: 1.2 },
  },
  {
    id: "outdoor_campus_28",
    label: "28 GHz Outdoor Campus",
    config: {
      frequency_hz: 28e9,
      max_depth: 3,
      reflection: true,
      scattering: true,
      refraction: false,
    },
    radioMap: { cell_size_m: 2.0 },
  },
  {
    id: "urban_macro_35",
    label: "3.5 GHz Urban Macro",
    config: {
      frequency_hz: 3.5e9,
      max_depth: 4,
      reflection: true,
      refraction: true,
      diffraction: true,
      bandwidth_hz: 100e6,
    },
    radioMap: { cell_size_m: 5.0 },
  },
  {
    id: "indoor_60",
    label: "60 GHz Indoor",
    config: {
      frequency_hz: 60e9,
      max_depth: 4,
      reflection: true,
      refraction: true,
      bandwidth_hz: 400e6,
    },
    radioMap: { cell_size_m: 0.25 },
  },
  {
    // UAV air-to-ground (TR 36.777 regime): mostly-LOS links with ground
    // reflections + building-edge diffraction dominating; shallow depth keeps
    // campus-scale solves fast, and the radio-map plane sits at a typical UAV
    // operating altitude so coverage maps answer "what does the drone see".
    id: "uav_a2g_28",
    label: "28 GHz UAV A2G",
    config: {
      frequency_hz: 28e9,
      max_depth: 3,
      los: true,
      reflection: true,
      diffraction: true,
      scattering: false,
      refraction: false,
      bandwidth_hz: 100e6,
    },
    radioMap: { cell_size_m: 2.0, height_m: 60 },
  },
];

export const CONFIG_PRESETS: Record<
  Exclude<ConfigPresetId, "custom">,
  ConfigPreset
> = {
  indoor_lab_28: PRESETS[0],
  outdoor_campus_28: PRESETS[1],
  urban_macro_35: PRESETS[2],
  indoor_60: PRESETS[3],
  uav_a2g_28: PRESETS[4],
};

/** Label for any preset id, including the "Custom" sentinel. */
export function presetLabel(id: ConfigPresetId): string {
  if (id === "custom") return "Custom";
  return CONFIG_PRESETS[id].label;
}

/**
 * Does a live config match a preset's declared fields exactly? Only the fields
 * the preset sets (plus its radio-map grid fields) are compared, so unrelated
 * knobs the user changed (seed, noise figure, backend) don't matter.
 */
export function configMatchesPreset(
  config: SimulationConfig,
  preset: ConfigPreset,
): boolean {
  const cfg = config as unknown as Record<string, unknown>;
  const grid = config.radio_map as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(preset.config)) {
    if (cfg[key] !== value) return false;
  }
  for (const [key, value] of Object.entries(preset.radioMap)) {
    if (grid[key] !== value) return false;
  }
  return true;
}

/** The preset id the current paths config matches, or "custom" if none. */
export function detectPreset(config: SimulationConfig): ConfigPresetId {
  for (const preset of PRESETS) {
    if (configMatchesPreset(config, preset)) return preset.id;
  }
  return "custom";
}
