/**
 * Frontend mirror of backend ACTOR_DEFAULTS (backend/app/schemas/scene.py):
 * default RF material, box size (l, w, h meters) and color per actor kind.
 *
 * The backend applies these on the Actor model_validator when rf_material_id /
 * color are null and shape stays at the generic 1x1x1 default. We seed the same
 * values client-side so a freshly-added actor renders with its kind's physical
 * size/color immediately (before the round-trip) and the inspector shows the
 * concrete values rather than blanks.
 */

import type { ActorKind, Vec3 } from "./types/api";

export interface ActorKindDefaults {
  rf_material_id: string;
  size_m: Vec3;
  color: string;
}

export const ACTOR_DEFAULTS: Record<ActorKind, ActorKindDefaults> = {
  car: { rf_material_id: "metal", size_m: [4.5, 1.8, 1.5], color: "#ffd166" },
  human: { rf_material_id: "human_body", size_m: [0.5, 0.35, 1.7], color: "#06d6a0" },
  custom: { rf_material_id: "unknown_rf", size_m: [1.0, 1.0, 1.0], color: "#a78bfa" },
};
