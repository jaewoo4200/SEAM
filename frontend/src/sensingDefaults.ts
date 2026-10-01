/**
 * Frontend mirror of backend schemas/sensing.py: the TR 38.901 sensing-target
 * tables (sionna-rt 2.2 sionna/rt/rcs/tr38901/parameters.py) and the
 * kind -> object-type defaults the backend applies when sensing.object_type is
 * null. The inspector shows the derived values as "auto (...)" placeholders;
 * nothing here is written back into the stored spec.
 */

import type { Actor, ActorKind, SensingTargetSpec, TR38901ObjectType } from "./types/api";

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
