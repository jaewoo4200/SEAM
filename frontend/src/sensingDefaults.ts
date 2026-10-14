/**
 * Frontend mirror of backend schemas/sensing.py: the TR 38.901 sensing-target
 * tables (sionna-rt 2.2 sionna/rt/rcs/tr38901/parameters.py) and the
 * kind -> object-type defaults the backend applies when sensing.object_type is
 * null. The inspector shows the derived values as "auto (...)" placeholders;
 * nothing here is written back into the stored spec. Also the sensing
 * waveform presets of services/numerology.py and a preview of the numbers the
 * backend derives from them (the backend stays the authority).
 */

import type {
  Actor,
  ActorKind,
  CyclicPrefix,
  SensingTargetSpec,
  TR38901ObjectType,
  WaveformOptions,
} from "./types/api";

export const TR38901_OBJECT_TYPES: TR38901ObjectType[] = [
  "uav-small-size",
  "uav-large-size",
  "human",
  "vehicle-single-sp",
  "vehicle-multi-sp",
  "agv-single-sp",
  "agv-multi-sp",
];

/** Model types TR 38.901 clause 7.9.2.1 defines per object type. */
export const TR38901_MODEL_TYPES: Record<TR38901ObjectType, (1 | 2)[]> = {
  "uav-small-size": [1],
  "uav-large-size": [2],
  human: [1, 2],
  "vehicle-single-sp": [2],
  "vehicle-multi-sp": [2],
  "agv-single-sp": [2],
  "agv-multi-sp": [2],
};

/** 10lg(σ_M): the spec's mean monostatic RCS [dBsm]. */
export const TR38901_NOMINAL_RCS_DBSM: Record<TR38901ObjectType, number> = {
  "uav-small-size": -12.81,
  "uav-large-size": -5.85,
  human: -1.37,
  "vehicle-single-sp": 11.25,
  "vehicle-multi-sp": 11.25,
  "agv-single-sp": -4.25,
  "agv-multi-sp": -4.25,
};

export const DEFAULT_OBJECT_TYPE_BY_KIND: Record<ActorKind, TR38901ObjectType | null> = {
  car: "vehicle-multi-sp",
  human: "human",
  uav: "uav-small-size",
  custom: null,
};

export const OBJECT_TYPE_LABELS: Record<TR38901ObjectType, string> = {
  "uav-small-size": "UAV, small — 1 scattering point",
  "uav-large-size": "UAV, large — 1 scattering point",
  human: "Human — 1 scattering point",
  "vehicle-single-sp": "Vehicle — 1 scattering point",
  "vehicle-multi-sp": "Vehicle — 5 scattering points",
  "agv-single-sp": "AGV — 1 scattering point",
  "agv-multi-sp": "AGV — 5 scattering points",
};

/** TR 38.901 object type the backend will use (null: constant / unresolved). */
export function resolveObjectType(actor: Actor): TR38901ObjectType | null {
  const spec = actor.sensing;
  if (!spec || spec.model !== "tr38901") return null;
  return spec.object_type ?? DEFAULT_OBJECT_TYPE_BY_KIND[actor.kind];
}

export function resolveModelType(ot: TR38901ObjectType, spec: SensingTargetSpec): 1 | 2 {
  if (spec.model_type !== null) return spec.model_type;
  return Math.max(...TR38901_MODEL_TYPES[ot]) as 1 | 2;
}

/** Fresh binding for a model switch. Fields of the other model stay null —
 *  the backend rejects them (schemas/sensing.py rules R2/R3). */
export function defaultSensingSpec(
  model: SensingTargetSpec["model"],
  kind: ActorKind,
): SensingTargetSpec {
  return {
    model,
    object_type: model === "tr38901" && kind === "custom" ? "vehicle-single-sp" : null,
    model_type: null,
    rcs_dbsm: model === "constant" ? 0 : null,
    xpr_db: null,
    random_components: false,
    size_m: null,
    velocity_m_s: null,
    enabled: true,
  };
}

// ------------------------------------------------ sensing waveform (#23)

export type WaveformPresetId = "none" | "nr_30k_51prb" | "grid_s";

/** services/numerology.py NR_30K_51PRB and GRID_S, sent verbatim as
 *  sensing.waveform. Grid S: 15.625 kHz x 1280 SC, pilots on every 5th
 *  subcarrier of every 9th 1/14400 s symbol -> K 256 x N 16 = 4096 REs / 10 ms. */
export const WAVEFORM_PRESETS: Record<
  Exclude<WaveformPresetId, "none">,
  { label: string; title: string; waveform: WaveformOptions }
> = {
  nr_30k_51prb: {
    label: "NR 30k · 51 PRB",
    title: "612 subcarriers x 30 kHz (B_occ 18.36 MHz), normal CP, every RE a pilot",
    waveform: { scs_hz: 30e3, n_fft: 1024, n_active_sc: 612, cp: "normal" },
  },
  grid_s: {
    label: "Grid S (4096)",
    title:
      "1280 subcarriers x 15.625 kHz (B_occ 20 MHz), comb-5 pilots on every 9th symbol: " +
      "256 x 16 = 4096 pilot REs per 10 ms",
    waveform: {
      scs_hz: 15_625,
      n_fft: 2048,
      n_active_sc: 1280,
      cp: "normal",
      symbol_duration_s: 1 / 14_400,
      pilot: { pattern: "comb", subcarrier_step: 5, symbol_step: 9 },
    },
  },
};

const SPEED_OF_LIGHT = 299_792_458;
const CP_RATIO: Record<CyclicPrefix, number> = { normal: 144 / 2048, extended: 512 / 2048 };
const NR_SYMBOLS_PER_SLOT: Record<CyclicPrefix, number> = { normal: 14, extended: 12 };

/** Symbol duration incl. CP: explicit, the NR slot average (15·2^mu kHz,
 *  mu 0..6), else (1 / scs)(1 + CP ratio). */
export function waveformSymbolDurationS(w: WaveformOptions): number {
  if (w.symbol_duration_s != null) return w.symbol_duration_s;
  const scs = w.scs_hz ?? 30e3;
  const cp = w.cp ?? "normal";
  const mu = Math.log2(scs / 15e3);
  const r = Math.round(mu);
  if (Math.abs(mu - r) < 1e-9 && r >= 0 && r <= 6) return 1e-3 / 2 ** r / NR_SYMBOLS_PER_SLOT[cp];
  return (1 / scs) * (1 + CP_RATIO[cp]);
}

export interface NumerologyPreview {
  symbols: number;
  gridK: number;
  gridN: number;
  /** Pilot REs per CPI: the effective cpi_pulses. */
  nRe: number;
  gainDb: number;
  bOccHz: number;
  /** c / B_occ (bistatic range-sum cell). */
  rangeSumCellM: number;
  dopplerResolutionHz: number;
}

/** What resolve_numerology(w, cpi_s) will derive; null when the CPI is
 *  shorter than one symbol (the backend answers 400). */
export function previewNumerology(w: WaveformOptions, cpiS: number): NumerologyPreview | null {
  const tSym = waveformSymbolDurationS(w);
  const symbols = w.symbols_per_cpi ?? Math.floor(cpiS / tSym + 1e-9);
  if (symbols < 1) return null;
  const nActive = w.n_active_sc ?? 612;
  const scStep = w.pilot?.subcarrier_step ?? 1;
  const symStep = w.pilot?.symbol_step ?? 1;
  const gridK = Math.ceil(nActive / scStep);
  const gridN = Math.ceil(symbols / symStep);
  const nRe = gridK * gridN;
  const bOccHz = nActive * (w.scs_hz ?? 30e3);
  return {
    symbols,
    gridK,
    gridN,
    nRe,
    gainDb: 10 * Math.log10(nRe),
    bOccHz,
    rangeSumCellM: SPEED_OF_LIGHT / bOccHz,
    dopplerResolutionHz: 1 / (gridN * symStep * tSym),
  };
}
