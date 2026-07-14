import { Component, Fragment, Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import * as THREE from "three";
import { acceleratedRaycast, computeBoundsTree, disposeBoundsTree } from "three-mesh-bvh";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import type { ThreeEvent } from "@react-three/fiber";
import { Grid, Html, Line, OrbitControls, PerspectiveCamera, TransformControls, useGLTF } from "@react-three/drei";
import { useAppStore } from "../store/appStore";
import type { ResolvedEnvironment } from "../envPresets";
import type { Mode } from "../store/appStore";
import { SELECTED_PATH_COLOR } from "./common";
import { filterPaths, pathColor, powerRange, powerWidth } from "../pathFilter";
import { UE_COLORS, samplesForUe, trajectoryUeIds } from "../trajectoryUtils";
import { api } from "../api/client";
import { directionalPosition, renderQualityDpr } from "../viewportSettings";
import type { RadioMapColormap } from "../viewportSettings";
import ViewportPanel from "./ViewportPanel";
import MeshRadioMapOverlay from "./MeshRadioMapOverlay";
import { captureAgentViews } from "./AgentCapture";
import { segmentationClassColor } from "../types/api";
import type {
  Prim,
  RadioMapResultSet,
  RayPath,
  RFMaterialLibrary,
  Scene,
  TrajectoryResultSet,
  ValidationReport,
  Vec3,
} from "../types/api";

// All backend coordinates are Z-up ENU meters. The world is NOT rotated;
// instead the camera uses up=[0,0,1] and the grid is rotated into the XY plane.

// BVH-accelerated raycasting (three-mesh-bvh): imported city-scale meshes
// (e.g. a 106k-triangle terrain) make three.js' O(n) per-mesh raycast freeze
// pointer picking. Geometries above _BVH_MIN_FACES get a bounds tree after
// load; the accelerated raycast uses it when present and falls back otherwise.
// (three-mesh-bvh ships the matching "three" module augmentation, so the
// prototype assignments below type-check without local declarations.)
THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;
const _BVH_MIN_FACES = 5_000;

const ACCENT = "#4fc3f7";
const UNASSIGNED_COLOR = "#ff9800";
const UNMATCHED_COLOR = "#4b5563";

// AODT-style dark viewer (matches the FTC/lab-room GUI conventions in the
// reference bundle, not Sionna's white built-in preview). Path colors come
// from the shared palette in common.tsx (single source of truth with the
// results table). The viewer background is now a viewport setting; its default
// (#0d1420) is defined in viewportSettings.ts.
const PICKER_COLOR = SELECTED_PATH_COLOR; // bright highlight for selection

// Jet colormap (blue -> cyan -> green -> yellow -> red), the AODT-like radio
// map convention (guide section 10.5 / 17), not viridis.
const JET: [number, number, number][] = [
  [0, 0, 131],
  [0, 60, 170],
  [5, 255, 255],
  [255, 255, 0],
  [250, 0, 0],
  [128, 0, 0],
];

// Additional matplotlib-style colormaps (8 anchors each, linearly
// interpolated) for radio-map display parity with the Sionna RT GUI's
// colormap picker. Jet stays the default (AODT convention).
const VIRIDIS: [number, number, number][] = [
  [68, 1, 84], [70, 50, 127], [54, 92, 141], [39, 127, 142],
  [31, 161, 135], [74, 194, 109], [159, 218, 58], [253, 231, 37],
];
const PLASMA: [number, number, number][] = [
  [13, 8, 135], [84, 2, 163], [139, 10, 165], [185, 50, 137],
  [219, 92, 104], [244, 136, 73], [254, 188, 43], [240, 249, 33],
];
const TURBO: [number, number, number][] = [
  [48, 18, 59], [70, 107, 227], [40, 187, 236], [49, 242, 153],
  [162, 252, 60], [237, 208, 58], [251, 128, 34], [122, 4, 3],
];
const COLORMAPS: Record<RadioMapColormap, [number, number, number][]> = {
  jet: JET,
  viridis: VIRIDIS,
  plasma: PLASMA,
  turbo: TURBO,
};

function colormapRgb(name: RadioMapColormap, t: number): [number, number, number] {
  const stops = COLORMAPS[name] ?? JET;
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = stops[i];
  const b = stops[Math.min(i + 1, stops.length - 1)];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}

export function radioMapCss(t: number, cmap: RadioMapColormap = "jet"): string {
  const [r, g, b] = colormapRgb(cmap, t);
  return `rgb(${r}, ${g}, ${b})`;
}

type PrimSeverity = "error" | "warning";

function severityByPrim(validation: ValidationReport | null): Map<string, PrimSeverity> {
  const map = new Map<string, PrimSeverity>();
  if (!validation) return map;
  for (const issue of validation.issues) {
    if (!issue.prim_id) continue;
    if (issue.severity === "error") {
      map.set(issue.prim_id, "error");
    } else if (issue.severity === "warning" && map.get(issue.prim_id) !== "error") {
      map.set(issue.prim_id, "warning");
    }
  }
  return map;
}

/**
 * Overlay color for a prim per viewer mode; null means "use the original
 * visual/PBR material" (visual + results modes keep textures, so glass keeps
 * its transparency there and only there).
 */
function overlayColor(
  prim: Prim | null,
  mode: Mode,
  library: RFMaterialLibrary | null,
  sevMap: Map<string, PrimSeverity>,
): string | null {
  if (mode === "visual" || mode === "results") return null;
  if (mode === "validation") {
    if (!prim) return UNMATCHED_COLOR;
    const sev = sevMap.get(prim.id);
    if (sev === "error") return "#ef5350";
    if (sev === "warning") return "#ffb74d";
    return "#546e7a";
  }
  // rf + ai modes: RF material preview color, warning orange when unassigned.
  if (!prim) return UNMATCHED_COLOR;
  const matId = prim.rf.material_id;
  if (!matId) return UNASSIGNED_COLOR;
  const mat = library?.materials.find((m) => m.id === matId);
  return mat ? mat.preview_color : UNASSIGNED_COLOR;
}

function cloneWithEmissive(mat: THREE.Material): THREE.Material {
  const clone = mat.clone();
  const emissive = clone as THREE.MeshStandardMaterial;
  if (emissive.emissive !== undefined) {
    emissive.emissive = new THREE.Color(ACCENT);
    emissive.emissiveIntensity = 0.5;
  }
  return clone;
}

function isAdditive(e: ThreeEvent<PointerEvent>): boolean {
  return e.nativeEvent.ctrlKey || e.nativeEvent.metaKey;
}

// ------------------------------------------------------------------ GLB

function GLBScene({ url }: { url: string }) {
  const gltf = useGLTF(url);
  const scene = useAppStore((s) => s.scene);
  const materials = useAppStore((s) => s.materials);
  const mode = useAppStore((s) => s.mode);
  const selection = useAppStore((s) => s.selection);
  const validation = useAppStore((s) => s.validation);
  const selectPrim = useAppStore((s) => s.selectPrim);
  const hiddenPrims = useAppStore((s) => s.hiddenPrims);
  const showSlice = useAppStore((s) => s.viewport.showSlice);
  const sliceZ = useAppStore((s) => s.viewport.sliceZ);
  const unlitTextures = useAppStore((s) => s.viewport.unlitTextures);

  // Prims are matched to GLB nodes by mesh_ref.mesh_name; a named node's
  // descendants (multi-primitive meshes) inherit the match.
  const primByNodeName = useMemo(() => {
    const map = new Map<string, Prim>();
    for (const prim of scene?.prims ?? []) {
      if (prim.mesh_ref) map.set(prim.mesh_ref.mesh_name, prim);
    }
    return map;
  }, [scene]);

  const findPrim = useMemo(() => {
    return (obj: THREE.Object3D | null): Prim | null => {
      let cur: THREE.Object3D | null = obj;
      while (cur) {
        const prim = cur.name ? primByNodeName.get(cur.name) : undefined;
        if (prim) return prim;
        cur = cur.parent;
      }
      return null;
    };
  }, [primByNodeName]);

  useEffect(() => {
    const created: THREE.Material[] = [];
    const sevMap = severityByPrim(validation);
    gltf.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      // Cache the original visual material once so mode switches can restore it.
      if (mesh.userData.__origMat === undefined) mesh.userData.__origMat = mesh.material;
      // Imported GLBs (e.g. the FTC bundle terrain) can ship without a normal
      // attribute; lit materials then shade to solid black in Visual mode.
      if (!mesh.geometry.attributes.normal) mesh.geometry.computeVertexNormals();
      // Dense imported meshes get a BVH so picking stays interactive on
      // city-scale scenes (built once per geometry; cheap for small ones to
      // skip entirely).
      const faceCount = (mesh.geometry.index?.count ?? mesh.geometry.attributes.position.count) / 3;
      if (!mesh.geometry.boundsTree && faceCount >= _BVH_MIN_FACES) {
        mesh.geometry.computeBoundsTree?.();
      }
      const orig = mesh.userData.__origMat as THREE.Material | THREE.Material[];
      const prim = findPrim(mesh);
      // Scene-tree eye toggle (occlusion-blocker helpers seed hidden).
      mesh.visible = !(prim && hiddenPrims.includes(prim.id));
      const selected = prim !== null && selection.includes(prim.id);
      const color = overlayColor(prim, mode, materials, sevMap);
      if (color === null) {
        // Unlit photo textures (Blender "flat" shading for photogrammetry /
        // aerial-ortho city bundles): show the texture's own pixels instead
        // of texture x scene lighting, which reads dark and patchy on real
        // imagery. Only texture-carrying materials swap; flat-color meshes
        // in the same scene stay lit.
        if (unlitTextures) {
          const texMats = Array.isArray(orig) ? orig : [orig];
          if (texMats.some((m) => (m as THREE.MeshStandardMaterial).map)) {
            const toUnlit = (m: THREE.Material): THREE.Material => {
              const map = (m as THREE.MeshStandardMaterial).map;
              if (!map) return m;
              const basic = new THREE.MeshBasicMaterial({
                map,
                color: selected
                  ? new THREE.Color(ACCENT).lerp(new THREE.Color("#ffffff"), 0.45)
                  : new THREE.Color("#ffffff"),
                side: (m as THREE.MeshStandardMaterial).side,
              });
              created.push(basic);
              return basic;
            };
            mesh.material = Array.isArray(orig) ? orig.map(toUnlit) : toUnlit(orig);
            const planes = showSlice
              ? [new THREE.Plane(new THREE.Vector3(0, 0, -1), sliceZ)]
              : null;
            const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
            for (const m of mats) {
              m.clippingPlanes = planes;
              m.needsUpdate = true;
            }
            return;
          }
        }
        // Demo GLBs exported without materials render uniform default gray;
        // tint those (and only those) with a muted version of the prim's RF
        // preview color so visual mode stays legible.
        const src = Array.isArray(orig) ? orig[0] : orig;
        // NOT bare when the mesh carries vertex colors: the importer bakes the
        // Mitsuba bsdf color into COLOR_0 (orange metal, green terrain...), and
        // GLTFLoader exposes it as a white vertexColors material. Replacing it
        // discards the scene's real colors (the black/gray-FTC regression).
        const bare =
          src instanceof THREE.MeshStandardMaterial &&
          src.name === "" &&
          !src.map &&
          !src.vertexColors &&
          src.color.getHex() === 0xffffff;
        if (bare && prim) {
          const matDef = materials?.materials.find((mm) => mm.id === prim.rf.material_id);
          if (matDef) {
            const tinted = new THREE.MeshStandardMaterial({
              // Keep the RF preview color clearly recognizable: for untextured
              // scenes this tint is the ONLY material cue in Visual mode, and a
              // heavy gray lerp made brown ground and blue glass read as the
              // same gray. Only a light wash so lit shading still reads.
              color: new THREE.Color(matDef.preview_color).lerp(new THREE.Color("#9aa4ad"), 0.2),
              roughness: 0.85,
              // GLTFLoader flat-shades primitives that ship without normals;
              // the replacement must inherit that or those meshes go black.
              flatShading: src.flatShading === true,
            });
            created.push(tinted);
            mesh.material = tinted;
            if (selected) {
              tinted.emissive = new THREE.Color(ACCENT);
              tinted.emissiveIntensity = 0.5;
            }
            return;
          }
        }
        if (selected) {
          // Clone (never mutate) the shared visual material for the emissive boost.
          const boost = (m: THREE.Material) => {
            const c = cloneWithEmissive(m);
            created.push(c);
            return c;
          };
          mesh.material = Array.isArray(orig) ? orig.map(boost) : boost(orig);
        } else {
          mesh.material = orig;
        }
      } else {
        // RF/validation/AI overlays render UNLIT (MeshBasicMaterial):
        // material-ID colors are data, not shading, and an unlit flat view
        // (CAD convention) can never go black under any lighting, normals or
        // tone-mapping state (reported twice as "buildings turn black").
        const overlay = new THREE.MeshBasicMaterial({
          color: new THREE.Color(selected ? ACCENT : color),
          side: THREE.DoubleSide,
        });
        created.push(overlay);
        mesh.material = overlay;
      }
      // Horizontal slice plane (RT GUI parity): clips ONLY scene meshes,
      // keeping everything with z <= sliceZ. Devices/paths/radio map/overlay
      // are untouched. Applied to whatever material ended up active above.
      const planes = showSlice
        ? [new THREE.Plane(new THREE.Vector3(0, 0, -1), sliceZ)]
        : null;
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const m of mats) {
        m.clippingPlanes = planes;
        m.needsUpdate = true;
      }
    });
    return () => {
      gltf.scene.traverse((obj) => {
        const mesh = obj as THREE.Mesh;
        if (mesh.isMesh && mesh.userData.__origMat !== undefined) {
          mesh.material = mesh.userData.__origMat as THREE.Material | THREE.Material[];
          // The cached gltf outlives this mount; never leak clip planes into it.
          const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
          for (const m of mats) m.clippingPlanes = null;
        }
      });
      for (const m of created) m.dispose();
    };
  }, [gltf, findPrim, mode, selection, materials, validation, showSlice, sliceZ, hiddenPrims, unlitTextures]);

  return (
    <primitive
      object={gltf.scene}
      onPointerDown={(e: ThreeEvent<PointerEvent>) => {
        if (useAppStore.getState().pick) return; // pick owns clicks
        e.stopPropagation();
        const prim = findPrim(e.object);
        if (prim) selectPrim(prim.id, isAdditive(e));
      }}
    />
  );
}

// ------------------------------------------------- fallback placeholders

function FallbackPrims() {
  const scene = useAppStore((s) => s.scene);
  const materials = useAppStore((s) => s.materials);
  const mode = useAppStore((s) => s.mode);
  const selection = useAppStore((s) => s.selection);
  const validation = useAppStore((s) => s.validation);
  const selectPrim = useAppStore((s) => s.selectPrim);
  const showSlice = useAppStore((s) => s.viewport.showSlice);
  const sliceZ = useAppStore((s) => s.viewport.sliceZ);

  const sevMap = useMemo(() => severityByPrim(validation), [validation]);

  // Horizontal slice plane (RT GUI parity), mirroring GLBScene: clip placeholder
  // boxes to z <= sliceZ so slice-mode works even when the GLB failed to load.
  const clippingPlanes = useMemo(
    () => (showSlice ? [new THREE.Plane(new THREE.Vector3(0, 0, -1), sliceZ)] : null),
    [showSlice, sliceZ],
  );

  if (!scene) return null;
  // In visual/results mode the boxes still color by RF status so they stay
  // informative without textures.
  const colorMode: Mode = mode === "visual" || mode === "results" ? "rf" : mode;

  return (
    <group>
      {scene.prims
        .filter((p) => p.type === "mesh_primitive")
        .map((prim) => {
          const selected = selection.includes(prim.id);
          const color = overlayColor(prim, colorMode, materials, sevMap) ?? UNMATCHED_COLOR;
          return (
            <mesh
              key={prim.id}
              position={prim.transform.translation}
              onPointerDown={(e: ThreeEvent<PointerEvent>) => {
                e.stopPropagation();
                selectPrim(prim.id, isAdditive(e));
              }}
            >
              <boxGeometry args={[1.6, 1.6, 1.6]} />
              <meshStandardMaterial
                color={color}
                emissive={selected ? ACCENT : "#000000"}
                emissiveIntensity={selected ? 0.6 : 0}
                clippingPlanes={clippingPlanes}
              />
            </mesh>
          );
        })}
    </group>
  );
}

class AssetBoundary extends Component<
  { url: string; fallback: ReactNode; onFailed: () => void; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    // Evict the rejected entry from the drei/suspend-react loader cache:
    // it caches rejections, so without this every later mount of the same
    // URL re-throws instantly and the project is stuck on placeholders for
    // the whole browser session even after the asset/backend recovers.
    useGLTF.clear(this.props.url);
    this.props.onFailed();
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

// ---------------------------------------------------------------- devices

/** Environment scale multiplier: indoor scenes get compact markers and
 *  interaction dots, outdoor scenes larger ones (수정사항 #5/#9). */
export function envScale(env: ResolvedEnvironment): number {
  return env === "indoor" ? 0.35 : 1.0;
}

/** Marker radius from device spread, with the user's marker-size slider as
 *  the dominant multiplier. envScale is intentionally NOT applied here (audit
 *  M2: indoor 0.35x cancelled the 2x default and pinned lab-room markers to
 *  the 0.06 m floor; the outdoor 0.6 m cap kept campus markers tiny). The
 *  environment still scales path-interaction dots, which have no user knob. */
function deviceMarkerRadius(
  scene: Scene,
  _env: ResolvedEnvironment = "outdoor",
  markerScale = 1.0,
): number {
  const pos = scene.devices.map((d) => d.position);
  const base = (() => {
    if (pos.length < 1) return 0.5;
    let maxSpan = 0;
    for (let axis = 0; axis < 3; axis++) {
      const vals = pos.map((p) => p[axis]);
      maxSpan = Math.max(maxSpan, Math.max(...vals) - Math.min(...vals));
    }
    // Outdoor floor 1.0 m: two devices 40 m apart on a 350 m site otherwise
    // shrink to sub-meter dots invisible from the framed camera.
    const floor = _env === "indoor" ? 0.15 : 1.0;
    return Math.min(2.5, Math.max(floor, maxSpan * 0.02));
  })();
  return base * markerScale;
}

/** X/Y/Z translate gizmo WRAPPING the selected marker (deterministic - no
 *  conditional refs, whose detach ordering could null the target when
 *  selection moved to an earlier list entry). TransformControls mutates its
 *  internal group during the drag; mouse-up commits and force-restores orbit
 *  controls, as does the unmount cleanup, so input can never stay locked. */
function GizmoWrapped({
  position,
  rotation,
  onCommit,
  children,
}: {
  position: Vec3;
  rotation?: [number, number, number];
  onCommit: (pos: Vec3) => void;
  children: ReactElement;
}) {
  const controls = useThree((s) => s.controls) as { enabled?: boolean } | null;
  const setEvents = useThree((s) => s.setEvents);
  const tcRef = useRef<(THREE.Object3D & {
    addEventListener: (t: string, fn: (e: { value?: unknown }) => void) => void;
    removeEventListener: (t: string, fn: (e: { value?: unknown }) => void) => void;
  }) | null>(null);

  // ROOT CAUSE of "gizmo drag does nothing": r3f's own picking runs on the
  // same pointerdown as the gizmo grab. If the arrow overlays empty space,
  // onPointerMissed fires -> clearSelection -> the gizmo UNMOUNTS mid-click.
  // Standard fix: while the cursor hovers a gizmo axis ('axis-changed' with a
  // non-null value), suspend r3f events entirely; restore on leave/unmount.
  useEffect(() => {
    const tc = tcRef.current;
    if (!tc) return;
    const onAxis = (e: { value?: unknown }) => {
      gizmoBusy = e.value != null;
      // Mirror into the store so the live-sync poll pauses position merges
      // while an axis is dragged (audit: poll snapped markers back mid-drag).
      useAppStore.getState().setGizmoDragging(gizmoBusy);
      setEvents({ enabled: e.value == null });
    };
    tc.addEventListener("axis-changed", onAxis);
    // Debug handle for interaction tests (dev builds only).
    if (import.meta.env.DEV) {
      (window as unknown as { __stwGizmo?: unknown }).__stwGizmo = tc;
    }
    return () => {
      tc.removeEventListener("axis-changed", onAxis);
      gizmoBusy = false;
      useAppStore.getState().setGizmoDragging(false);
      setEvents({ enabled: true });
      if (controls) controls.enabled = true;
    };
  }, [setEvents, controls]);

  return (
    <TransformControls
      ref={tcRef as never}
      mode="translate"
      size={0.9}
      position={position}
      rotation={rotation ?? [0, 0, 0]}
      onMouseUp={(e) => {
        const obj = (e?.target as { object?: THREE.Object3D } | undefined)?.object;
        if (controls) controls.enabled = true;
        if (obj) onCommit([obj.position.x, obj.position.y, obj.position.z]);
      }}
    >
      {children}
    </TransformControls>
  );
}

function Devices() {
  const scene = useAppStore((s) => s.scene);
  const env = useAppStore((s) => s.resolvedEnvironment);
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);
  const selectDevice = useAppStore((s) => s.selectDevice);
  const updateDevice = useAppStore((s) => s.updateDevice);
  const markerScale = useAppStore((s) => s.viewport.markerScale);
  if (!scene) return null;
  const baseRadius = deviceMarkerRadius(scene, env, markerScale);
  // A device attached to an actor is carried BY that actor — the actor is the
  // visual anchor, so its antenna renders as a small dot instead of the full
  // sphere (the full-size marker completely swallowed the UAV drone model).
  const attachedIds = new Set(scene.actors.flatMap((a) => a.attached_device_ids));

  return (
    <group>
      {scene.devices.map((d) => {
        const selected = d.id === selectedDeviceId;
        const radius = attachedIds.has(d.id) ? baseRadius * 0.35 : baseRadius;
        const marker = (
          <>
            {/* Radio devices are spheres, colored by kind (AODT: TX red, UE blue). */}
            <mesh>
              <sphereGeometry args={[radius, 24, 16]} />
              <meshStandardMaterial
                color={d.color}
                emissive={selected ? PICKER_COLOR : "#000000"}
                emissiveIntensity={selected ? 0.9 : 0}
              />
            </mesh>
            <Html position={[0, 0, radius * 2.4]} center zIndexRange={[10, 0]}>
              <div className={"device-label" + (selected ? " selected" : "")} title={d.id}>
                {d.name || d.id}
              </div>
            </Html>
          </>
        );
        const inner = (
          <group
            // Markers are UI, not scene geometry: the surface probe (device
            // AGL) and camera fit must never treat the sphere as a rooftop —
            // a selected TX's own marker used to win the "surface below"
            // raycast and pin its AGL to 0.00 m.
            userData={{ __noFit: true }}
            // Named so the entity-POV inset can track the live rendered pose.
            name={`pov-ent-${d.id}`}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
              if (useAppStore.getState().pick) return; // pick owns clicks
              e.stopPropagation();
              selectDevice(d.id);
            }}
          >
            {marker}
          </group>
        );
        return selected ? (
          <GizmoWrapped
            key={d.id}
            position={d.position}
            onCommit={(pos) => void updateDevice(d.id, { position: pos })}
          >
            {inner}
          </GizmoWrapped>
        ) : (
          <group key={d.id} position={d.position}>
            {inner}
          </group>
        );
      })}
    </group>
  );
}

// ----------------------------------------------------------------- actors

/** Actors as base-centered boxes (z = ground contact), colored by actor.color,
 *  selectable like devices, with a yellow trajectory polyline when defined.
 *  In scenario playback the boxes are driven by the current frame's states. */
function Actors({ frameStates }: { frameStates?: Map<string, { position: Vec3; orientation_deg: Vec3 }> }) {
  const scene = useAppStore((s) => s.scene);
  const selectedActorId = useAppStore((s) => s.selectedActorId);
  const selectActor = useAppStore((s) => s.selectActor);
  const updateActor = useAppStore((s) => s.updateActor);
  const highlightedWaypoint = useAppStore((s) => s.highlightedWaypoint);
  const env = useAppStore((s) => s.resolvedEnvironment);
  const markerScale = useAppStore((s) => s.viewport.markerScale);
  if (!scene) return null;
  const wpRadius = deviceMarkerRadius(scene, env, markerScale) * 0.4;

  return (
    <group>
      {scene.actors.map((a) => {
        const state = frameStates?.get(a.id);
        const position = state?.position ?? a.position;
        // orientation_deg is [yaw, pitch, roll] - yaw is index 0.
        const yawDeg = (state?.orientation_deg ?? a.orientation_deg)[0];
        const [l, w, h] = a.shape.size_m;
        const color = a.color ?? "#a78bfa";
        const selected = a.id === selectedActorId;
        const matProps = {
          color,
          transparent: true,
          opacity: 0.85,
          emissive: selected ? PICKER_COLOR : "#000000",
          emissiveIntensity: selected ? 0.7 : 0,
        } as const;
        // A real 0.6 m drone projects to ~3 px in a campus-scale scene — the
        // "Add UAV did nothing" report. Scale the VISUAL up to device-marker
        // visibility (RF still uses the true size_m box); 1x in small rooms.
        const droneScale =
          a.kind === "uav"
            ? Math.max(1, (wpRadius / 0.4) * 2.2 / Math.max(l, w))
            : 1;
        // position is the base center: lift the box by half its height so it
        // sits on the ground contact plane. Yaw about Z (up).
        const body = (
          <>
            {/* Selection/drag is handled by the wrapping DraggableGroup. */}
            {a.kind === "uav" ? (
              <group scale={[droneScale, droneScale, droneScale]}>
                <UavModel l={l} w={w} h={h} matProps={matProps} />
              </group>
            ) : (
              <mesh position={[0, 0, h / 2]}>
                <boxGeometry args={[l, w, h]} />
                <meshStandardMaterial {...matProps} />
              </mesh>
            )}
            <Html
              position={[0, 0, h * droneScale + 0.4]}
              center
              zIndexRange={[10, 0]}
            >
              <div className={"device-label" + (selected ? " selected" : "")} title={a.id}>
                {a.name || a.id}
              </div>
            </Html>
          </>
        );
        const inner = (
          <group
            // Named so the entity-POV inset can track the LIVE rendered pose
            // (scenario playback moves these groups, not the stored scene).
            name={`pov-ent-${a.id}`}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
              if (useAppStore.getState().pick) return; // pick owns clicks
              e.stopPropagation();
              selectActor(a.id);
            }}
          >
            {body}
          </group>
        );
        const actorNode =
          selected && !frameStates ? (
            <GizmoWrapped
              key={a.id}
              position={position}
              rotation={[0, 0, (yawDeg * Math.PI) / 180]}
              onCommit={(pos) => void updateActor(a.id, { position: pos })}
            >
              {inner}
            </GizmoWrapped>
          ) : (
            <group key={a.id} position={position} rotation={[0, 0, (yawDeg * Math.PI) / 180]}>
              {inner}
            </group>
          );
        return (
          <Fragment key={a.id}>
            {actorNode}
            {a.trajectory && a.trajectory.waypoints.length > 0 && (
              // WORLD-SPACE sibling, deliberately outside the actor's group:
              // as a child it inherited live gizmo translations (the counter-
              // transform used the React prop, which only updates on commit),
              // so recording waypoints while dragging the actor visually
              // dragged the whole authored path along with it.
              <ActorTrajectoryLine
                waypoints={a.trajectory.waypoints}
                markerRadius={wpRadius}
                highlightIndex={
                  highlightedWaypoint?.actorId === a.id
                    ? highlightedWaypoint.index
                    : null
                }
              />
            )}
          </Fragment>
        );
      })}
    </group>
  );
}

/** Procedural quadrotor in the spirit of a real camera drone: rounded
 *  fuselage + canopy, four diagonal arms with motor pods, crossed rotor
 *  blades under semi-transparent spin disks, landing skids and a front
 *  gimbal camera. All proportions derive from shape.size_m [l, w, h] so
 *  custom sizes still work; body parts share matProps so selection emissive
 *  applies to the whole airframe. */
function UavModel({
  l,
  w,
  h,
  matProps,
}: {
  l: number;
  w: number;
  h: number;
  matProps: {
    color: string;
    transparent: boolean;
    opacity: number;
    emissive: string;
    emissiveIntensity: number;
  };
}) {
  const dark = { ...matProps, color: "#1e293b" };
  const armLen = Math.hypot(l, w) * 0.62;
  const rotorZ = h * 0.86;
  const tips = [
    [l * 0.48, w * 0.48],
    [l * 0.48, -w * 0.48],
    [-l * 0.48, w * 0.48],
    [-l * 0.48, -w * 0.48],
  ] as const;
  return (
    <group position={[0, 0, h * 0.45]}>
      {/* fuselage: main hull + tapered canopy on top */}
      <mesh>
        <boxGeometry args={[l * 0.46, w * 0.34, h * 0.5]} />
        <meshStandardMaterial {...matProps} />
      </mesh>
      <mesh position={[l * 0.04, 0, h * 0.32]}>
        <boxGeometry args={[l * 0.3, w * 0.24, h * 0.22]} />
        <meshStandardMaterial {...matProps} />
      </mesh>
      {/* four diagonal arms out to the motor pods */}
      {tips.map(([tx, ty], i) => (
        <mesh
          key={`arm${i}`}
          position={[tx / 2, ty / 2, h * 0.08]}
          rotation={[0, 0, Math.atan2(ty, tx)]}
        >
          <boxGeometry args={[armLen, w * 0.07, h * 0.14]} />
          <meshStandardMaterial {...dark} />
        </mesh>
      ))}
      {/* motor pods + crossed blades + spin disks */}
      {tips.map(([tx, ty], i) => (
        <group key={`rotor${i}`} position={[tx, ty, 0]}>
          <mesh position={[0, 0, h * 0.28]}>
            <cylinderGeometry args={[l * 0.055, l * 0.07, h * 0.36, 12]} />
            <meshStandardMaterial {...dark} />
          </mesh>
          {/* two thin blades, phase-offset per rotor so they don't look cloned */}
          <mesh position={[0, 0, rotorZ - h * 0.35]} rotation={[0, 0, (i * Math.PI) / 3]}>
            <boxGeometry args={[l * 0.42, w * 0.035, h * 0.03]} />
            <meshStandardMaterial {...dark} />
          </mesh>
          <mesh
            position={[0, 0, rotorZ - h * 0.35]}
            rotation={[0, 0, (i * Math.PI) / 3 + Math.PI / 2]}
          >
            <boxGeometry args={[l * 0.42, w * 0.035, h * 0.03]} />
            <meshStandardMaterial {...dark} />
          </mesh>
          {/* translucent disk suggesting the spinning prop */}
          <mesh position={[0, 0, rotorZ - h * 0.33]} rotation={[Math.PI / 2, 0, 0]}>
            <cylinderGeometry args={[l * 0.24, l * 0.24, h * 0.015, 24]} />
            <meshStandardMaterial {...matProps} opacity={0.22} depthWrite={false} />
          </mesh>
        </group>
      ))}
      {/* landing skids: two rails + four legs */}
      {([-1, 1] as const).map((sy) => (
        <group key={`skid${sy}`}>
          <mesh position={[0, sy * w * 0.26, -h * 0.42]}>
            <boxGeometry args={[l * 0.62, w * 0.05, h * 0.06]} />
            <meshStandardMaterial {...dark} />
          </mesh>
          {([-1, 1] as const).map((sx) => (
            <mesh
              key={`leg${sx}`}
              position={[sx * l * 0.18, sy * w * 0.22, -h * 0.22]}
              rotation={[sy * 0.35, 0, 0]}
            >
              <boxGeometry args={[l * 0.045, w * 0.045, h * 0.42]} />
              <meshStandardMaterial {...dark} />
            </mesh>
          ))}
        </group>
      ))}
      {/* front gimbal camera under the nose */}
      <group position={[l * 0.2, 0, -h * 0.32]}>
        <mesh>
          <sphereGeometry args={[h * 0.16, 14, 10]} />
          <meshStandardMaterial {...dark} />
        </mesh>
        <mesh position={[h * 0.12, 0, 0]}>
          <boxGeometry args={[h * 0.1, h * 0.12, h * 0.12]} />
          <meshStandardMaterial
            {...matProps}
            color="#0ea5e9"
            opacity={0.95}
          />
        </mesh>
      </group>
    </group>
  );
}

/** Yellow polyline + per-waypoint dots through an actor's authored waypoints,
 *  drawn in absolute world coordinates (never parented to the actor, so a
 *  gizmo drag cannot move the already-recorded path). The waypoint picked in
 *  the Inspector list renders emphasized (cyan, larger). */
function ActorTrajectoryLine({
  waypoints,
  markerRadius,
  highlightIndex,
}: {
  waypoints: Vec3[];
  markerRadius: number;
  highlightIndex: number | null;
}) {
  return (
    <group>
      {waypoints.length > 1 && <Line points={waypoints} color="#ffee58" lineWidth={2} />}
      {waypoints.map((wp, i) => {
        const hot = i === highlightIndex;
        return (
          <mesh key={i} position={wp}>
            <sphereGeometry args={[hot ? markerRadius * 2.2 : markerRadius, 16, 12]} />
            <meshStandardMaterial
              color={hot ? "#22d3ee" : "#ffee58"}
              emissive={hot ? "#22d3ee" : "#000000"}
              emissiveIntensity={hot ? 0.9 : 0}
              transparent
              opacity={hot ? 0.95 : 0.75}
            />
          </mesh>
        );
      })}
    </group>
  );
}

// -------------------------------------------------------------- ray paths

/** Shared ray-polyline overlay: applies the store filters/color-by to a set of
 *  paths and draws them (with interaction dots when requested). Single source
 *  of truth for the static results overlay, the trajectory-frame overlay and
 *  the scenario-frame overlay. */
function PathLines({ paths, showInteractions = true }: { paths: RayPath[]; showInteractions?: boolean }) {
  const selectedPathId = useAppStore((s) => s.selectedPathId);
  const selectPath = useAppStore((s) => s.selectPath);
  const pathTypeFilter = useAppStore((s) => s.pathTypeFilter);
  const strongestN = useAppStore((s) => s.strongestN);
  const minPowerDbm = useAppStore((s) => s.minPowerDbm);
  const hiddenLinkDevices = useAppStore((s) => s.hiddenLinkDevices);
  const materialFilter = useAppStore((s) => s.materialFilter);
  const colorBy = useAppStore((s) => s.colorBy);
  const lineWidthByPower = useAppStore((s) => s.lineWidthByPower);
  const env = useAppStore((s) => s.resolvedEnvironment);
  // Interaction dots shrink indoors (수정사항 #9): 0.18 m outdoors was
  // oversized for a lab room.
  const interactionRadius = 0.18 * envScale(env);

  // Same filter pipeline as the results table (single source of truth).
  const visible = useMemo(
    () =>
      filterPaths(paths, {
        pathTypeFilter,
        strongestN,
        minPowerDbm,
        hiddenLinkDevices,
        materialFilter,
      }),
    [paths, pathTypeFilter, strongestN, minPowerDbm, hiddenLinkDevices, materialFilter],
  );
  // Color/width ranges are computed over the visible set.
  const range = useMemo(() => powerRange(visible), [visible]);

  return (
    <group>
      {visible.map((p) => {
        const selected = p.path_id === selectedPathId;
        const color = selected ? SELECTED_PATH_COLOR : pathColor(p, colorBy, range);
        const baseWidth = lineWidthByPower ? powerWidth(p, range) : 2;
        return (
          <group key={p.path_id}>
            <Line
              points={p.vertices}
              color={color}
              lineWidth={selected ? Math.max(4, baseWidth) : baseWidth}
              onClick={(e) => {
                e.stopPropagation();
                selectPath(p.path_id);
              }}
            />
            {showInteractions &&
              p.interactions.map((it, i) => (
                <mesh key={`${p.path_id}_i${i}`} position={it.point}>
                  <sphereGeometry args={[interactionRadius, 12, 8]} />
                  <meshBasicMaterial color={color} />
                </mesh>
              ))}
          </group>
        );
      })}
    </group>
  );
}

function RayPaths() {
  const pathResults = useAppStore((s) => s.pathResults);
  if (!pathResults) return null;
  return <PathLines paths={pathResults.paths} />;
}

// ------------------------------------------------------------ trajectory

/** One UE's pulsing marker + visited trail at the current step. */
function UeMarker({
  ueId,
  samples,
  step,
  color,
  radius,
}: {
  ueId: string;
  samples: TrajectoryResultSet["samples"];
  step: number;
  color: string;
  /** Scene-scaled marker radius (same sizing as static device markers — a
   *  hardcoded 0.5 m used to vanish to sub-pixel in campus-scale scenes). */
  radius: number;
}) {
  const markerRef = useRef<THREE.Mesh>(null);
  const s = Math.max(0, Math.min(samples.length - 1, step));
  const current = samples[s];
  const trail = useMemo(
    () => samples.slice(0, s + 1).map((x) => x.position),
    [samples, s],
  );
  useFrame((state) => {
    const mat = markerRef.current?.material as THREE.MeshStandardMaterial | undefined;
    if (mat) mat.emissiveIntensity = 0.5 + 0.4 * Math.sin(state.clock.elapsedTime * 4);
  });
  if (!current) return null;
  return (
    <group>
      {trail.length > 1 && <Line points={trail} color="#ffee58" lineWidth={2} />}
      <group position={current.position}>
        <mesh ref={markerRef}>
          <sphereGeometry args={[radius, 24, 16]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.6} />
        </mesh>
        <Html position={[0, 0, radius * 2.4]} center zIndexRange={[10, 0]}>
          <div className="device-label selected">{ueId}</div>
        </Html>
      </group>
    </group>
  );
}

/** Trajectory playback overlay: every routed UE's marker + trail at the
 *  current step, plus the step's live rays (all UEs merged) when the result
 *  carries include_paths and the Trajectory-rays toggle is on. */
function TrajectoryOverlay({ trajectory }: { trajectory: TrajectoryResultSet }) {
  const trajFrame = useAppStore((s) => s.trajFrame);
  const trajUeFrames = useAppStore((s) => s.trajUeFrames);
  const trajPlaying = useAppStore((s) => s.trajPlaying);
  const showTrajectoryRays = useAppStore((s) => s.showTrajectoryRays);
  const scene = useAppStore((s) => s.scene);
  const env = useAppStore((s) => s.resolvedEnvironment);
  const markerScale = useAppStore((s) => s.viewport.markerScale);
  // Same scene-scaled sizing as the static device markers, slightly smaller
  // so the moving UE reads as "the animated one".
  const ueRadius = scene ? deviceMarkerRadius(scene, env, markerScale) * 0.9 : 0.5;

  // Only draw the playback marker while playback is actually ENGAGED (fresh
  // run leaves the rays toggle on; playing; scrubbed). A project reopen
  // auto-loads the LAST stored trajectory — rendering its marker then put a
  // phantom "moved RX" at a stale start position the moment the user pressed
  // Simulate paths (which switches to Results and turns the rays toggle off).
  const engaged =
    showTrajectoryRays ||
    trajPlaying ||
    trajFrame > 0 ||
    Object.keys(trajUeFrames).length > 0;

  const ueIds = trajectoryUeIds(trajectory);
  // Each UE follows its own scrub bar when set, else the master frame.
  const frameFor = (ueId: string) => trajUeFrames[ueId] ?? trajFrame;
  if (!engaged) return null;
  const framePaths = ueIds.flatMap((ueId) => {
    const samples = samplesForUe(trajectory, ueId);
    const s = Math.max(0, Math.min(samples.length - 1, frameFor(ueId)));
    return samples[s]?.paths ?? [];
  });

  return (
    <group>
      {showTrajectoryRays && framePaths.length > 0 && (
        <PathLines paths={framePaths} showInteractions={false} />
      )}
      {ueIds.map((ueId, i) => (
        <UeMarker
          key={ueId}
          ueId={ueId}
          samples={samplesForUe(trajectory, ueId)}
          step={frameFor(ueId)}
          color={UE_COLORS[i % UE_COLORS.length]}
          radius={ueRadius}
        />
      ))}
    </group>
  );
}

// ------------------------------------------------------------- scenario

/** Per-frame device markers (positions from the scenario frame's device_states,
 *  falling back to the scene device for kind/color). */
function ScenarioDevices({ states }: { states: Map<string, Vec3> }) {
  const scene = useAppStore((s) => s.scene);
  if (!scene) return null;
  const radius = deviceMarkerRadius(
    scene,
    useAppStore.getState().resolvedEnvironment,
    useAppStore.getState().viewport.markerScale,
  );
  return (
    <group>
      {scene.devices.map((d) => {
        // Frame states only carry devices ATTACHED to moving actors; a static
        // TX/RX stays at its scene position - it must not vanish during
        // playback (reported: "scenario 켜면 TX가 사라짐").
        const pos = states.get(d.id) ?? d.position;
        return (
          <group key={d.id} position={pos}>
            <mesh>
              <sphereGeometry args={[radius, 24, 16]} />
              <meshStandardMaterial color={d.color} />
            </mesh>
            <Html position={[0, 0, radius * 2.4]} center zIndexRange={[10, 0]}>
              <div className="device-label">{d.id}</div>
            </Html>
          </group>
        );
      })}
    </group>
  );
}

/** Whole scenario overlay for the current frame: actors + devices + paths. */
function ScenarioOverlay({ showPaths }: { showPaths: boolean }) {
  const scenario = useAppStore((s) => s.scenario);
  const scenarioFrame = useAppStore((s) => s.scenarioFrame);
  if (!scenario || scenario.frames.length === 0) return null;
  const frame = scenario.frames[Math.max(0, Math.min(scenario.frames.length - 1, scenarioFrame))];

  const actorStates = new Map(
    frame.actor_states.map((s) => [s.id, { position: s.position, orientation_deg: s.orientation_deg }]),
  );
  const deviceStates = new Map(frame.device_states.map((s) => [s.id, s.position]));

  return (
    <group>
      <Actors frameStates={actorStates} />
      <ScenarioDevices states={deviceStates} />
      {showPaths && frame.paths && <PathLines paths={frame.paths} showInteractions={false} />}
    </group>
  );
}

// ------------------------------------------------------------ screenshot

/** Max width (px) of the captured viewport JPEG sent to the VLM. */
const SCREENSHOT_MAX_WIDTH = 800;

/** Registers a viewport-capture fn with the store (VLM). Reads the WebGL canvas
 *  as a JPEG data URL on demand, downscaled to at most SCREENSHOT_MAX_WIDTH so
 *  the payload stays small; requires preserveDrawingBuffer. */
/** Downscale a freshly-rendered canvas to a JPEG data URL (<= max width),
 *  preserving aspect ratio. Returns null on a zero-sized or tainted canvas. */
function canvasToJpeg(src: HTMLCanvasElement): string | null {
  try {
    const w = src.width;
    const h = src.height;
    if (w === 0 || h === 0) return null;
    if (w <= SCREENSHOT_MAX_WIDTH) return src.toDataURL("image/jpeg", 0.6);
    const scale = SCREENSHOT_MAX_WIDTH / w;
    const off = document.createElement("canvas");
    off.width = SCREENSHOT_MAX_WIDTH;
    off.height = Math.max(1, Math.round(h * scale));
    const ctx = off.getContext("2d");
    if (!ctx) return src.toDataURL("image/jpeg", 0.6);
    ctx.drawImage(src, 0, 0, off.width, off.height);
    return off.toDataURL("image/jpeg", 0.6);
  } catch {
    return null;
  }
}

function ScreenshotCapture() {
  const gl = useThree((s) => s.gl);
  const scene = useThree((s) => s.scene);
  const camera = useThree((s) => s.camera);
  const registerViewportCapture = useAppStore((s) => s.registerViewportCapture);
  const registerMultiViewCapture = useAppStore((s) => s.registerMultiViewCapture);
  const registerAgentCapture = useAppStore((s) => s.registerAgentCapture);
  useEffect(() => {
    registerViewportCapture(() => canvasToJpeg(gl.domElement));
    // SEAM-Agent: capture 6 RGB + triangle-id views of the prim's mesh (found
    // by mesh name in the live graph) into offscreen render targets, then
    // restore the visible frame. Returns [] when the mesh is not loaded yet.
    registerAgentCapture((meshName) => {
      const mesh = scene.getObjectByName(meshName) as THREE.Mesh | null;
      if (!mesh || !(mesh as THREE.Mesh).isMesh) return [];
      try {
        return captureAgentViews(gl, mesh);
      } finally {
        // Restore the on-screen frame: the capture rendered offscreen targets,
        // but re-render the real camera so nothing flashes / stays stale.
        try {
          gl.render(scene, camera);
        } catch {
          // best-effort; the next rAF repaints regardless
        }
      }
    });
    // Multi-view (paper roadmap #3): render 4 azimuth poses around the scene
    // center with a TEMPORARY camera so the VLM sees the geometry from every
    // side. The user's camera is never moved; we re-render it after the loop
    // so the visible frame is untouched. Falls back to a single current-view
    // capture when scene bounds are unknown.
    registerMultiViewCapture(() => {
      const b = useAppStore.getState().sceneBounds;
      const single = canvasToJpeg(gl.domElement);
      if (!b) return single ? [single] : [];
      try {
        const center = new THREE.Vector3(
          (b.min[0] + b.max[0]) / 2,
          (b.min[1] + b.max[1]) / 2,
          (b.min[2] + b.max[2]) / 2,
        );
        // Match the user camera's distance to the scene center so the framing
        // is comparable; guard against a degenerate (co-located) distance.
        const dist = Math.max(1, camera.position.distanceTo(center));
        // A gentle downward tilt (30% of the radius) so the views look down
        // onto the scene rather than dead-level.
        const elev = dist * 0.3;
        const persp = camera instanceof THREE.PerspectiveCamera ? camera : null;
        const tmp = new THREE.PerspectiveCamera(
          persp?.fov ?? 45,
          persp?.aspect ?? gl.domElement.width / Math.max(1, gl.domElement.height),
          persp?.near ?? 0.1,
          persp?.far ?? 5000,
        );
        tmp.up.set(0, 0, 1); // Z-up world (same as the real camera)
        const shots: string[] = [];
        for (const azDeg of [0, 90, 180, 270]) {
          const az = (azDeg * Math.PI) / 180;
          tmp.position.set(
            center.x + Math.cos(az) * dist,
            center.y + Math.sin(az) * dist,
            center.z + elev,
          );
          tmp.lookAt(center);
          tmp.updateMatrixWorld();
          gl.render(scene, tmp);
          const shot = canvasToJpeg(gl.domElement);
          if (shot) shots.push(shot);
        }
        return shots.length > 0 ? shots : single ? [single] : [];
      } finally {
        // Restore the visible frame: re-render with the real camera so the
        // temporary poses never flash on screen.
        try {
          gl.render(scene, camera);
        } catch {
          // best-effort restore; the next rAF frame repaints regardless
        }
      }
    });
    viewportPngGetter = () => {
      try {
        const c = gl.domElement;
        return c.width > 0 ? c.toDataURL("image/png") : null;
      } catch {
        return null;
      }
    };
    return () => {
      registerViewportCapture(null);
      registerMultiViewCapture(null);
      registerAgentCapture(null);
      viewportPngGetter = null;
    };
  }, [gl, scene, camera, registerViewportCapture, registerMultiViewCapture, registerAgentCapture]);
  return null;
}

// -------------------------------------------------------------- radio map

/** Auto (data) dB range of a radio map, for display + legend. */
export function radioMapRange(rm: RadioMapResultSet): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (const row of rm.values) {
    for (const v of row) {
      if (v !== null) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
  }
  return Number.isFinite(min) ? [min, max] : [0, 1];
}

function makeRadioMapTexture(
  rm: RadioMapResultSet,
  cmap: RadioMapColormap,
  vmin: number | null,
  vmax: number | null,
): THREE.CanvasTexture {
  const { nx, ny } = rm.grid;
  const canvas = document.createElement("canvas");
  canvas.width = nx;
  canvas.height = ny;
  const ctx = canvas.getContext("2d")!;
  const [autoMin, autoMax] = radioMapRange(rm);
  // Manual vmin/vmax (Sionna RT GUI parity) override the data range; values
  // outside clamp to the ends. Kept ordered defensively.
  let min = vmin ?? autoMin;
  let max = vmax ?? autoMax;
  if (min > max) [min, max] = [max, min];
  const span = max > min ? max - min : 1;
  for (let iy = 0; iy < ny; iy++) {
    const row = rm.values[iy] ?? [];
    for (let ix = 0; ix < nx; ix++) {
      const v = row[ix];
      if (v === null || v === undefined) continue;
      const t = (v - min) / span;
      ctx.fillStyle = radioMapCss(t, cmap);
      // Canvas rows grow downward; world +Y is row iy, so flip vertically.
      ctx.fillRect(ix, ny - 1 - iy, 1, 1);
    }
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.magFilter = THREE.NearestFilter;
  tex.minFilter = THREE.NearestFilter;
  tex.generateMipmaps = false;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function RadioMapPlane({ radioMap }: { radioMap: RadioMapResultSet }) {
  const viewport = useAppStore((s) => s.viewport);
  const texture = useMemo(
    () => makeRadioMapTexture(radioMap, viewport.rmColormap, viewport.rmVmin, viewport.rmVmax),
    [radioMap, viewport.rmColormap, viewport.rmVmin, viewport.rmVmax],
  );
  useEffect(() => () => texture.dispose(), [texture]);

  const { grid } = radioMap;
  const w = grid.nx * grid.cell_size_m;
  const h = grid.ny * grid.cell_size_m;
  const z = grid.origin[2] !== 0 ? grid.origin[2] : grid.height_m;
  return (
    // PlaneGeometry lies in XY facing +Z, which is exactly our ground plane.
    <mesh position={[grid.origin[0] + w / 2, grid.origin[1] + h / 2, z]} userData={{ __noFit: true }}>
      <planeGeometry args={[w, h]} />
      <meshBasicMaterial
        map={texture}
        transparent
        opacity={0.85}
        side={THREE.DoubleSide}
        depthWrite={false}
      />
    </mesh>
  );
}

// ----------------------------------------------------- segmentation tint

/** Faces above this cap make the per-vertex color buffer (and the toNonIndexed
 *  clone) too heavy to build interactively; the 2D overlay still conveys the
 *  split, so the 3D tint is skipped past it. */
const SEG_TINT_MAX_FACES = 800_000;

/** Translucent per-face material tint of the SOURCE prim while a segmentation
 *  preview is active (visual/results modes only). Builds an overlay mesh from a
 *  NON-INDEXED clone of the source geometry with per-face vertex colors keyed by
 *  the predicted material id, rendered slightly in front (polygonOffset) so it
 *  reads as a wash over the real mesh without z-fighting. The shared source
 *  geometry is never mutated. */
function SegmentationTint() {
  const three = useThree((s) => s.scene);
  const segPreview = useAppStore((s) => s.segPreview);
  const mode = useAppStore((s) => s.mode);
  const scene = useAppStore((s) => s.scene);

  // The tint only makes sense over the textured/lit mesh (visual/results); in
  // RF/validation/AI modes the mesh is already recolored by material id.
  const active = segPreview !== null && (mode === "visual" || mode === "results");

  const built = useMemo(() => {
    if (!active || !segPreview) return null;
    const prim = scene?.prims.find((p) => p.id === segPreview.primId) ?? null;
    const meshName = prim?.mesh_ref?.mesh_name;
    if (!meshName) return null;
    const src = three.getObjectByName(meshName) as THREE.Mesh | null;
    if (!src || !(src as THREE.Mesh).isMesh) return null;

    const faceMats = segPreview.faceMaterials;
    if (faceMats.length === 0 || faceMats.length > SEG_TINT_MAX_FACES) return null;

    // Non-indexed geometry so each triangle owns 3 unique vertices we can color
    // independently. toNonIndexed() returns a FRESH geometry for indexed input
    // (never mutating the shared source); for already-non-indexed input it
    // returns `this`, so clone() there to keep the source pristine.
    const srcGeom = src.geometry as THREE.BufferGeometry;
    const geom = srcGeom.index ? srcGeom.toNonIndexed() : srcGeom.clone();
    const pos = geom.getAttribute("position");
    const triCount = Math.floor(pos.count / 3);
    const colors = new Float32Array(pos.count * 3);
    const col = new THREE.Color();
    for (let f = 0; f < triCount; f++) {
      // faceMaterials is in mesh face order; guard shorter arrays defensively.
      col.set(segmentationClassColor(faceMats[f] ?? 0));
      const base = f * 9;
      for (let v = 0; v < 3; v++) {
        colors[base + v * 3] = col.r;
        colors[base + v * 3 + 1] = col.g;
        colors[base + v * 3 + 2] = col.b;
      }
    }
    geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));

    const material = new THREE.MeshBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.75,
      side: THREE.DoubleSide,
      // Nudge the wash toward the camera so it wins the depth test over the
      // coincident source faces without a visible gap.
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -1,
      depthWrite: false,
    });

    // Match the source mesh's world transform exactly (it may be nested under
    // transformed parents), then freeze the matrix.
    src.updateWorldMatrix(true, false);
    const matrix = src.matrixWorld.clone();
    return { geom, material, matrix };
  }, [active, segPreview, scene, three]);

  useEffect(() => {
    return () => {
      if (built) {
        built.geom.dispose();
        built.material.dispose();
      }
    };
  }, [built]);

  if (!built) return null;
  return (
    <mesh
      ref={(self) => {
        if (!self) return;
        // The overlay mesh is a direct child of the r3f scene root (parent is
        // effectively identity), so the source's world matrix IS this mesh's
        // local matrix. Decompose it so three keeps it in sync each frame.
        built.matrix.decompose(self.position, self.quaternion, self.scale);
      }}
      geometry={built.geom}
      material={built.material}
      renderOrder={997}
      userData={{ __noFit: true }}
    />
  );
}

// -------------------------------------------------------- textured overlay

/** Non-pickable textured backdrop loaded from scene.assets.visual_overlay_uri.
 *  Its meshes have raycast disabled so clicks fall through to the pickable
 *  scene (and onPointerMissed still fires when nothing else is hit). Kept in its
 *  own Suspense/error boundary by the caller so a missing overlay is silent. */
function OverlayScene({ url }: { url: string }) {
  const gltf = useGLTF(url);
  const gl = useThree((s) => s.gl);
  const group = useRef<THREE.Group>(null);
  // Clone so multiple viewers / remounts don't fight over one cached graph, and
  // so we can freely disable raycasting on this instance's meshes.
  const object = useMemo(() => gltf.scene.clone(true), [gltf]);
  useEffect(() => {
    const maxAniso = gl.capabilities.getMaxAnisotropy();
    let unlit = 0;
    object.traverse((obj) => {
      // Disabling raycast makes the whole backdrop non-interactive: pointer
      // events pass straight through to the pickable scene beneath it.
      (obj as THREE.Object3D).raycast = () => null;
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      const replaced = mats.map((mat) => {
        const map = (mat as THREE.MeshStandardMaterial).map;
        if (!map) return mat; // untextured backdrop parts keep their lit look
        // Photogrammetry textures have the capture lighting baked in; running
        // scene lights over the scan's noisy normals only adds shading blotches.
        // Render textured backdrop parts unlit (Cesium/3D-Tiles convention),
        // with anisotropy so grazing-angle facades stay legible.
        map.anisotropy = Math.min(8, maxAniso);
        map.needsUpdate = true;
        unlit += 1;
        return new THREE.MeshBasicMaterial({ map, side: mat.side });
      });
      mesh.material = Array.isArray(mesh.material) ? replaced : replaced[0];
    });
    // World-space bounds after the Y-up -> Z-up rotation below; must match the
    // RF scene.glb footprint. Kept as a debug line so misaligned overlays are
    // diagnosable from the console without pixel inspection.
    if (group.current) {
      group.current.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(group.current);
      if (import.meta.env.DEV) {
        console.debug(
          "[overlay] world bounds",
          box.min.toArray().map((v) => Number(v.toFixed(1))),
          box.max.toArray().map((v) => Number(v.toFixed(1))),
          `| unlit textured mats: ${unlit}`,
        );
      }
    }
  }, [object, gl]);
  // Overlay GLBs follow the glTF spec (+Y up); the app world is Z-up ENU
  // (camera up=[0,0,1]; scene.glb is baked to Z-up at import, external
  // overlays are copied verbatim). Rotating +90 deg about X maps
  // (x, y, z)_gltf -> (x, -z, y)_world, aligning the backdrop with the RF
  // scene. Composed via a parent group so a root transform inside the GLB
  // is preserved rather than overwritten.
  return (
    <group ref={group} rotation={[Math.PI / 2, 0, 0]} userData={{ __noFit: true }}>
      <primitive object={object} />
    </group>
  );
}

/** Overlay wrapped in its own Suspense + error boundary so a missing/broken
 *  overlay GLB never breaks the main viewer. */
function OverlayBackdrop({ url }: { url: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  if (failed) return null;
  return (
    <Suspense fallback={null}>
      <AssetBoundary key={url} url={url} fallback={null} onFailed={() => setFailed(true)}>
        <OverlayScene url={url} />
      </AssetBoundary>
    </Suspense>
  );
}

// True while the cursor hovers/drags a gizmo axis. onPointerMissed consults
// this so grabbing an arrow that overlays empty space can never be
// misread as a background click (which would clear the selection and
// unmount the gizmo mid-drag) - robust regardless of event ordering.
let gizmoBusy = false;

// Camera pose getter registered by ViewerHotkeys for the render button.
let cameraPoseGetter: (() => { position: Vec3; target: Vec3 }) | null = null;
// Full-resolution PNG of the live canvas (exactly what the user sees) for the
// paper-ready "save view" button; requires preserveDrawingBuffer.
let viewportPngGetter: (() => string | null) | null = null;
/** Set while the entity-POV inset is mounted: resolves with a full-resolution
 *  PNG of the POV camera, captured inside the next render frame (rendering
 *  outside the rAF loop proved unreliable under remote desktops). */
let povPngGetter: (() => Promise<string | null>) | null = null;

// --------------------------------------------------------------- hotkeys

/** Viewer keyboard shortcuts (Sionna RT GUI parity):
 *  R reset camera · F fit scene · K add TX at cursor · L add RX at cursor
 *  (placed at the surface hit + 1.5 m along its normal) · S slice plane ·
 *  M radio-map overlay. Ignored while typing in form fields. */
/** AABB of the current selection (prim meshes by mesh name, device/actor
 *  markers by position), or null when nothing is selected. Point-only boxes
 *  get a small pad so framing math never degenerates. */
function selectionBox(
  three: THREE.Scene,
  st: {
    selection: string[];
    selectedDeviceId: string | null;
    selectedActorId: string | null;
    scene: Scene | null;
  },
): THREE.Box3 | null {
  const box = new THREE.Box3();
  let any = false;
  for (const primId of st.selection) {
    const prim = st.scene?.prims.find((p) => p.id === primId);
    const meshName = prim?.mesh_ref?.mesh_name;
    const obj = meshName ? three.getObjectByName(meshName) : null;
    if (obj) {
      box.expandByObject(obj);
      any = true;
    }
  }
  if (st.selectedDeviceId && st.scene) {
    const d = st.scene.devices.find((x) => x.id === st.selectedDeviceId);
    if (d) {
      box.expandByPoint(new THREE.Vector3(d.position[0], d.position[1], d.position[2]));
      any = true;
    }
  }
  if (st.selectedActorId && st.scene) {
    const a = st.scene.actors.find((x) => x.id === st.selectedActorId);
    if (a) {
      box.expandByPoint(new THREE.Vector3(a.position[0], a.position[1], a.position[2]));
      any = true;
    }
  }
  if (!any) return null;
  if (box.getSize(new THREE.Vector3()).length() < 1) box.expandByScalar(2);
  return box;
}

/** Registers the store's downward surface probe: highest walkable scene
 *  surface at (x, y) with z <= belowZ. Powers the device inspector's
 *  "height above surface" readout (RX over terrain, TX over the rooftop
 *  beneath it) without an API round-trip. Candidates are meshes only —
 *  drei Line overlays extend Mesh AND read raycaster.camera in raycast, so
 *  they must be excluded explicitly (same rule as the trajectory drape). */
function SurfaceProbe() {
  const three = useThree((s) => s.scene);
  const registerSurfaceProbe = useAppStore((s) => s.registerSurfaceProbe);
  useEffect(() => {
    const ray = new THREE.Raycaster();
    const probe = (x: number, y: number, belowZ?: number): number | null => {
      const isOverlay = (obj: THREE.Object3D): boolean => {
        for (let cur: THREE.Object3D | null = obj; cur; cur = cur.parent) {
          if (cur.userData.__noFit) return true;
          // The selection gizmo mounts its handle/plane meshes at the device
          // position (often directly under the scene root); its upward faces
          // must never read as "the surface below the device".
          if (cur.type && cur.type.startsWith("TransformControls")) return true;
        }
        return false;
      };
      const candidates: THREE.Object3D[] = [];
      let top = -Infinity;
      three.traverse((obj) => {
        const m = obj as THREE.Mesh & { isLineSegments2?: boolean };
        if (m.isMesh && !m.isLineSegments2 && m.visible && !isOverlay(m)) {
          candidates.push(m);
          if (m.geometry.boundingSphere === null) m.geometry.computeBoundingSphere();
          const bs = m.geometry.boundingSphere;
          if (bs) {
            const worldZ = m.localToWorld(bs.center.clone()).z + bs.radius;
            if (worldZ > top) top = worldZ;
          }
        }
      });
      if (candidates.length === 0) return null;
      ray.set(new THREE.Vector3(x, y, top + 10), new THREE.Vector3(0, 0, -1));
      const hits = ray.intersectObjects(candidates, false);
      let best: number | null = null;
      for (const h of hits) {
        // Walkable = upward-facing; and when belowZ is given, only surfaces
        // at/under the device (a canopy above it is not its ground).
        const n = h.face?.normal
          ?.clone()
          .transformDirection(h.object.matrixWorld);
        if (n && n.z <= 0.1) continue;
        if (belowZ !== undefined && h.point.z > belowZ + 0.01) continue;
        if (best === null || h.point.z > best) best = h.point.z;
      }
      return best;
    };
    registerSurfaceProbe(probe);
    return () => registerSurfaceProbe(null);
  }, [three, registerSurfaceProbe]);
  return null;
}

/** Blender's "orbit around selection": while enabled, selecting an object
 *  re-pivots the orbit target to it (camera position untouched, so the view
 *  direction eases onto the new pivot without a jump-cut). */
function OrbitSelectionPivot() {
  const controls = useThree((s) => s.controls) as {
    target?: THREE.Vector3;
    update?: () => void;
  } | null;
  const three = useThree((s) => s.scene);
  // Subscribed for effect scheduling only; the gate below re-reads the store
  // at effect time. The toggle must NOT come in as a prop: props cross the
  // r3f reconciler boundary later than this component's own store
  // subscription, so a selection made right after switching the toggle off
  // still re-pivoted using the stale prop (live-verified).
  const enabled = useAppStore((s) => s.viewport.orbitSelection);
  const selection = useAppStore((s) => s.selection);
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);
  const selectedActorId = useAppStore((s) => s.selectedActorId);
  useEffect(() => {
    const st = useAppStore.getState();
    if (!st.viewport.orbitSelection || !controls?.target) return;
    const box = selectionBox(three, st);
    if (!box) return;
    controls.target.copy(box.getCenter(new THREE.Vector3()));
    controls.update?.();
  }, [enabled, selection, selectedDeviceId, selectedActorId, controls, three]);
  return null;
}

function ViewerHotkeys() {
  const camera = useThree((s) => s.camera);
  const gl = useThree((s) => s.gl);
  const three = useThree((s) => s.scene);
  const controls = useThree((s) => s.controls) as { target?: THREE.Vector3; update?: () => void } | null;
  // The key handler resolves camera/controls AT KEYPRESS TIME via this
  // getter: its effect otherwise captures drei's pre-makeDefault placeholder
  // camera, and every camera hotkey (R/F/view snaps) silently moves the
  // wrong instance (live-verified: R left the real camera untouched).
  const getThree = useThree((s) => s.get);
  const addDevice = useAppStore((s) => s.addDevice);
  const setViewport = useAppStore((s) => s.setViewport);
  const toggleOverlay = useAppStore((s) => s.toggleOverlay);
  const pointer = useRef<{ x: number; y: number } | null>(null);
  // Ghost-preview placement (AODT style): K/L arm a translucent marker that
  // follows the cursor's surface hit; click places, Esc cancels.
  const [placing, setPlacing] = useState<"tx" | "rx" | null>(null);
  const [ghost, setGhost] = useState<Vec3 | null>(null);
  const placingRef = useRef<typeof placing>(null);
  const ghostRef = useRef<Vec3 | null>(null);
  placingRef.current = placing;
  ghostRef.current = ghost;
  // UI buttons arm the same ghost placement as the K/L hotkeys (mouse
  // discoverability); the store field is consumed once here.
  const placeArm = useAppStore((s) => s.placeArm);
  useEffect(() => {
    if (placeArm) {
      setPlacing(placeArm);
      useAppStore.getState().armPlacement(null);
    }
  }, [placeArm]);
  // A panel pick and the ghost placement must never be active together (both
  // own viewport clicks and Esc); arming a pick clears any live ghost.
  const pickActiveForGhost = useAppStore((s) => s.pick !== null);
  useEffect(() => {
    if (pickActiveForGhost) {
      setPlacing(null);
      setGhost(null);
    }
  }, [pickActiveForGhost]);

  // Frame the camera to the scene extents whenever the project changes: the
  // fixed default pose is right for a lab room but far too close for a
  // hundreds-of-meters outdoor scene (reported: outdoor loads over-zoomed).
  const projectIdForFrame = useAppStore((s) => s.projectId);
  const boundsForFrame = useAppStore((s) => s.sceneBounds);
  const envForFrame = useAppStore((s) => s.resolvedEnvironment);
  const framedProject = useRef<string | null>(null);
  useEffect(() => {
    if (!projectIdForFrame || !boundsForFrame) return;
    // Key the guard on the CAMERA too: drei's makeDefault swaps the default
    // camera in after mount, and framing the placeholder camera leaves the
    // real one at the stock pose (seen as an over-zoomed initial view).
    const key = `${projectIdForFrame}:${(camera as THREE.Camera).uuid}`;
    if (framedProject.current === key) return;
    framedProject.current = key;
    const b = boundsForFrame;
    const center = new THREE.Vector3(
      (b.min[0] + b.max[0]) / 2,
      (b.min[1] + b.max[1]) / 2,
      (b.min[2] + b.max[2]) / 2,
    );
    const radius = Math.max(
      1,
      Math.hypot(b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]) / 2,
    );
    const persp = camera as THREE.PerspectiveCamera;
    // The environment split exists for a reason: an indoor room wants a
    // close-in, human-scale three-quarter view that looks INTO the volume, while
    // an outdoor site wants a raised site-survey overview with margin for
    // orbiting. Same fit math, different composition per environment.
    const preset =
      envForFrame === "indoor"
        ? { dir: new THREE.Vector3(1, -1.1, 0.85), margin: 1.15 }
        : { dir: new THREE.Vector3(1, -0.9, 1.25), margin: 1.35 };
    const dist =
      (radius / Math.tan(((persp.fov ?? 45) * Math.PI) / 360)) * preset.margin;
    camera.position.copy(
      center.clone().addScaledVector(preset.dir.clone().normalize(), dist),
    );
    // Indoor: aim at standing height inside the room, not the geometric
    // center of the shell - reads like walking into the model.
    if (envForFrame === "indoor") {
      const aim = center.clone();
      aim.z = Math.min(center.z, b.min[2] + 1.5);
      controls?.target?.copy(aim);
    } else {
      controls?.target?.copy(center);
    }
    controls?.update?.();
  }, [projectIdForFrame, boundsForFrame, envForFrame, camera, controls]);

  useEffect(() => {
    cameraPoseGetter = () => ({
      position: [camera.position.x, camera.position.y, camera.position.z],
      target: controls?.target
        ? [controls.target.x, controls.target.y, controls.target.z]
        : [0, 0, 0],
    });
    return () => {
      cameraPoseGetter = null;
    };
  }, [camera, controls]);

  // Local clipping must be on for the slice plane to work at all.
  useEffect(() => {
    gl.localClippingEnabled = true;
    // Dev/debug handles: let tooling (and bug reports) inspect the live
    // three.js graph without a React devtools round-trip. Dev builds only -
    // production stays clean (audit polish).
    if (import.meta.env.DEV) {
      (window as unknown as { __stwScene?: THREE.Scene }).__stwScene = three;
      (window as unknown as { __stwCamera?: THREE.Camera }).__stwCamera = camera;
      (window as unknown as { __stwControls?: unknown }).__stwControls = controls;
    }
    // `camera`/`controls` in deps: drei's makeDefault swaps both in after
    // mount — without them __stwCamera/__stwControls keep pointing at dead
    // placeholders and every probe reads objects that aren't rendering.
  }, [gl, three, camera, controls]);

  useEffect(() => {
    const canvas = gl.domElement;
    const onMove = (e: PointerEvent) => {
      const r = canvas.getBoundingClientRect();
      pointer.current = {
        x: ((e.clientX - r.left) / r.width) * 2 - 1,
        y: -((e.clientY - r.top) / r.height) * 2 + 1,
      };
    };
    canvas.addEventListener("pointermove", onMove);

    const surfacePos = (): Vec3 | null => {
      if (!pointer.current) return null;
      const ray = new THREE.Raycaster();
      // Fresh camera (not the effect closure) — see the getThree note above.
      ray.setFromCamera(new THREE.Vector2(pointer.current.x, pointer.current.y), getThree().camera);
      const hits = ray
        .intersectObjects(three.children, true)
        .filter((h) => (h.object as THREE.Mesh).isMesh && h.object.visible);
      const hit = hits[0];
      // Indoor rooms are meters wide: a 1.5 m normal push off a wall lands
      // the device mid-room. Scale the offset to the environment.
      const off = useAppStore.getState().resolvedEnvironment === "indoor" ? 0.4 : 1.5;
      if (hit) {
        const pos = hit.point.clone();
        if (hit.face) {
          const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld);
          pos.addScaledVector(n, off);
        } else {
          pos.z += off;
        }
        return [pos.x, pos.y, pos.z];
      }
      // No geometry under the cursor: drop onto the z=0 ground plane.
      const t = new THREE.Vector3();
      if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), t)) {
        return [t.x, t.y, off];
      }
      return null;
    };
    const onGhostMove = () => {
      if (placingRef.current) setGhost(surfacePos());
    };
    canvas.addEventListener("pointermove", onGhostMove);
    // Capture-phase click commits the ghost BEFORE r3f selection handlers run.
    const onPlaceClick = (e: PointerEvent) => {
      const kind = placingRef.current;
      if (!kind || e.button !== 0) return;
      // An active panel pick owns viewport clicks; ghost placement yields.
      if (useAppStore.getState().pick) return;
      // Scenario playback replaces the static device layer: a device placed
      // now would be invisible until playback ends (audit finding).
      const st = useAppStore.getState();
      if (st.mode === "results" && st.scenario !== null && st.scenario.frames.length > 0) {
        useAppStore.setState({
          notice: "Placement blocked during scenario playback — clear the scenario first",
        });
        setPlacing(null);
        setGhost(null);
        return;
      }
      e.stopImmediatePropagation();
      e.preventDefault();
      const pos = ghostRef.current ?? surfacePos();
      if (pos) void addDevice(kind, pos);
      setPlacing(null);
      setGhost(null);
    };
    canvas.addEventListener("pointerdown", onPlaceClick, { capture: true });

    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)) {
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      // Shadow the effect-closure camera/controls with keypress-time values:
      // the closure can hold drei's pre-makeDefault placeholder, making every
      // camera hotkey silently move a camera that isn't rendering.
      const camera = getThree().camera;
      const controls = getThree().controls as {
        target?: THREE.Vector3;
        update?: () => void;
      } | null;
      const { viewport } = useAppStore.getState();
      // Blender numpad-style view snaps (e.code so Shift works): 1 front,
      // 3 right, 7 top; Shift = the opposite side. Keeps the orbit target
      // and distance - only the viewing direction snaps.
      if (["Digit1", "Digit3", "Digit7", "Numpad1", "Numpad3", "Numpad7"].includes(e.code)) {
        const tgt = (controls?.target ?? new THREE.Vector3(0, 0, 0)) as THREE.Vector3;
        const dist = Math.max(1, camera.position.distanceTo(tgt));
        const s = e.shiftKey ? -1 : 1;
        const dir = e.code.endsWith("1")
          ? new THREE.Vector3(0, -s, 0) // front: camera on -Y looking +Y
          : e.code.endsWith("3")
            ? new THREE.Vector3(s, 0, 0) // right: camera on +X looking -X
            : new THREE.Vector3(0, 0, s); // top: camera on +Z looking down
        camera.position.copy(tgt.clone().addScaledVector(dir, dist));
        // Top/bottom need a non-parallel up hint (+Y); Z-up otherwise.
        const topLike = e.code.endsWith("7");
        camera.up.set(0, topLike ? 1 : 0, topLike ? 0 : 1);
        camera.lookAt(tgt);
        controls?.update?.();
        e.preventDefault();
        return;
      }
      switch (e.key.toLowerCase()) {
        case "r": {
          // Reset = the same env-aware framing as project open, not a fixed
          // outdoor pose that dwarfs indoor rooms.
          const st = useAppStore.getState();
          const b = st.sceneBounds;
          if (!b) {
            camera.position.set(35, -35, 25);
            controls?.target?.set(0, 0, 0);
            controls?.update?.();
            break;
          }
          const center = new THREE.Vector3(
            (b.min[0] + b.max[0]) / 2,
            (b.min[1] + b.max[1]) / 2,
            (b.min[2] + b.max[2]) / 2,
          );
          const radius = Math.max(
            1,
            Math.hypot(b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]) / 2,
          );
          const persp = camera as THREE.PerspectiveCamera;
          const indoor = st.resolvedEnvironment === "indoor";
          const preset = indoor
            ? { dir: new THREE.Vector3(1, -1.1, 0.85), margin: 1.15 }
            : { dir: new THREE.Vector3(1, -0.9, 1.25), margin: 1.35 };
          const dist =
            (radius / Math.tan(((persp.fov ?? 45) * Math.PI) / 360)) * preset.margin;
          camera.position.copy(
            center.clone().addScaledVector(preset.dir.clone().normalize(), dist),
          );
          if (indoor) center.z = Math.min(center.z, b.min[2] + 1.5);
          controls?.target?.copy(center);
          controls?.update?.();
          break;
        }
        case "f": {
          const box = new THREE.Box3();
          // With a selection, F frames THAT (Blender's numpad-period);
          // without one it frames the whole scene. Overlays/helpers tagged
          // __noFit are always excluded (fitting to them flies the camera
          // far past the actual scene - audit finding).
          const noFit = (o: THREE.Object3D | null): boolean => {
            for (let cur = o; cur; cur = cur.parent) {
              if (cur.userData.__noFit) return true;
            }
            return false;
          };
          const st = useAppStore.getState();
          const selBox = selectionBox(three, st);
          if (selBox) {
            box.copy(selBox);
          } else {
            three.traverse((obj) => {
              const mesh = obj as THREE.Mesh;
              if (mesh.isMesh && mesh.visible && !noFit(mesh)) box.expandByObject(mesh);
            });
          }
          if (box.isEmpty()) return;
          const center = box.getCenter(new THREE.Vector3());
          const radius = box.getSize(new THREE.Vector3()).length() / 2;
          const persp = camera as THREE.PerspectiveCamera;
          const dist = (radius / Math.tan(((persp.fov ?? 45) * Math.PI) / 360)) * 1.15;
          const dir = camera.position.clone().sub(controls?.target ?? center).normalize();
          camera.position.copy(center.clone().addScaledVector(dir, dist));
          controls?.target?.copy(center);
          controls?.update?.();
          break;
        }
        case "k":
        case "l": {
          const st = useAppStore.getState();
          if (st.pick) return; // pick owns the viewport
          if (st.mode === "results" && st.scenario !== null && st.scenario.frames.length > 0) {
            useAppStore.setState({
              notice: "Placement blocked during scenario playback — clear the scenario first",
            });
            return;
          }
          setPlacing(e.key.toLowerCase() === "k" ? "tx" : "rx");
          setGhost(surfacePos());
          break;
        }
        case "escape":
          setPlacing(null);
          setGhost(null);
          break;
        case "s":
          setViewport({ showSlice: !viewport.showSlice });
          break;
        case "m":
          // Keep the hotkey honest: with no computed radio map the checkbox
          // is disabled, so the hotkey explains instead of silently flipping
          // an invisible flag (audit finding).
          if (!useAppStore.getState().radioMap) {
            useAppStore.setState({ notice: "No radio map yet — run Simulate radio map first" });
            break;
          }
          toggleOverlay("radioMap");
          break;
        default:
          return;
      }
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointermove", onGhostMove);
      canvas.removeEventListener("pointerdown", onPlaceClick, { capture: true });
      window.removeEventListener("keydown", onKey);
    };
  }, [gl, camera, three, controls, addDevice, setViewport, toggleOverlay]);

  if (!placing || !ghost) return null;
  return (
    // Translucent ghost marker following the cursor (always on top).
    <mesh position={ghost} renderOrder={999} userData={{ __noFit: true }}>
      <sphereGeometry args={[0.6, 20, 14]} />
      <meshBasicMaterial
        color={placing === "tx" ? "#ff5252" : "#2e9bff"}
        transparent
        opacity={0.55}
        depthTest={false}
      />
    </mesh>
  );
}

// ---------------------------------------------------------------- pick mode

const PICK_COLOR = "#ffd54f";
/** Click-vs-orbit discrimination thresholds (standard tap test). */
const PICK_MAX_MOVE_PX = 5;
const PICK_MAX_MS = 500;

/** Store-driven click-to-place: while a PickRequest is active, taps on the
 *  viewport resolve to world points (scene mesh first, z=0 ground fallback)
 *  and flow back to the requesting panel via the store. Orbiting stays fully
 *  usable during a pick — only genuine taps (small move, short press) place.
 */
function PickController() {
  const gl = useThree((s) => s.gl);
  const camera = useThree((s) => s.camera);
  const three = useThree((s) => s.scene);
  const pick = useAppStore((s) => s.pick);
  const pickPoints = useAppStore((s) => s.pickPoints);
  const [hover, setHover] = useState<Vec3 | null>(null);
  const pickRef = useRef(pick);
  pickRef.current = pick;
  const downRef = useRef<{ x: number; y: number; t: number; moved: boolean } | null>(null);

  useEffect(() => {
    if (!pick) {
      setHover(null);
      return;
    }
    const canvas = gl.domElement;

    // Overlays (pick dots themselves, radio-map planes, trajectory markers,
    // mesh-RM discs...) are tagged __noFit; picking must only see the actual
    // scene geometry or the ghost dot occludes its own next raycast.
    const isOverlay = (obj: THREE.Object3D): boolean => {
      for (let cur: THREE.Object3D | null = obj; cur; cur = cur.parent) {
        if (cur.userData.__noFit) return true;
      }
      return false;
    };

    // Returns the SURFACE hit (what the dot shows, matching the crosshair) -
    // the committed point adds heightOffset along +Z at commit time.
    const resolve = (clientX: number, clientY: number): Vec3 | null => {
      const req = pickRef.current;
      if (!req) return null;
      const r = canvas.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((clientX - r.left) / r.width) * 2 - 1,
        -((clientY - r.top) / r.height) * 2 + 1,
      );
      const ray = new THREE.Raycaster();
      // BVH fast path only tracks the first hit; that is all picking needs.
      ray.firstHitOnly = true;
      ray.setFromCamera(ndc, camera);
      if (req.target === "surface") {
        const hits = ray
          .intersectObjects(three.children, true)
          .filter(
            (h) =>
              (h.object as THREE.Mesh).isMesh &&
              h.object.visible &&
              !isOverlay(h.object),
          );
        if (hits[0]) {
          const p = hits[0].point;
          return [p.x, p.y, p.z];
        }
      }
      const t = new THREE.Vector3();
      if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), t)) {
        return [t.x, t.y, 0];
      }
      return null;
    };

    // rAF-throttled hover resolve: at most one full raycast per frame, so a
    // fast pointer on a dense scene never queues a raycast backlog.
    let hoverPending: { x: number; y: number } | null = null;
    let hoverRaf = 0;
    const flushHover = () => {
      hoverRaf = 0;
      if (!hoverPending) return;
      const { x, y } = hoverPending;
      hoverPending = null;
      setHover(resolve(x, y));
    };
    const onMove = (e: PointerEvent) => {
      hoverPending = { x: e.clientX, y: e.clientY };
      if (!hoverRaf) hoverRaf = requestAnimationFrame(flushHover);
      const d = downRef.current;
      if (d && Math.hypot(e.clientX - d.x, e.clientY - d.y) > PICK_MAX_MOVE_PX) {
        d.moved = true;
      }
    };
    // Capture-phase down/up so a committing tap runs BEFORE r3f's synthetic
    // events (marker selection, onPointerMissed) — same pattern as the K/L
    // ghost placement.
    const onDown = (e: PointerEvent) => {
      if (e.button !== 0 || gizmoBusy) return;
      downRef.current = { x: e.clientX, y: e.clientY, t: performance.now(), moved: false };
    };
    const onUp = (e: PointerEvent) => {
      const d = downRef.current;
      downRef.current = null;
      if (!pickRef.current || !d || e.button !== 0 || gizmoBusy) return;
      const isTap =
        !d.moved &&
        Math.hypot(e.clientX - d.x, e.clientY - d.y) <= PICK_MAX_MOVE_PX &&
        performance.now() - d.t <= PICK_MAX_MS;
      if (!isTap) return; // orbit/pan drag — never place
      // Only commit taps that end over the canvas (a drag ending off-canvas
      // resolves via the window listener but must not place).
      if (e.target !== canvas) return;
      const p = resolve(e.clientX, e.clientY);
      if (!p) return;
      // Do NOT stop propagation here: OrbitControls must see this pointerup
      // to end its internal drag state, or the camera keeps orbiting with the
      // button released. Selection is suppressed in the r3f handlers instead.
      useAppStore.getState().addPickPoint(p);
    };
    const onCancel = () => {
      downRef.current = null;
    };
    const onKey = (e: KeyboardEvent) => {
      // Esc completes a "multi" pick with the points placed so far (>=2) and
      // cancels anything else - see finishPick.
      if (e.key === "Escape") useAppStore.getState().finishPick();
    };

    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerdown", onDown, { capture: true });
    // Up on window so a drag that ends off-canvas still resets the tap test.
    window.addEventListener("pointerup", onUp, { capture: true });
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("keydown", onKey);
    return () => {
      if (hoverRaf) cancelAnimationFrame(hoverRaf);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerdown", onDown, { capture: true });
      window.removeEventListener("pointerup", onUp, { capture: true });
      window.removeEventListener("pointercancel", onCancel);
      window.removeEventListener("keydown", onKey);
    };
  }, [pick, gl, camera, three]);

  // Dot size scales with the scene (a fixed 0.45 m sphere vanishes on a
  // city-scale terrain); clamped so indoor rooms are not drowned either.
  const bounds = useAppStore((s) => s.sceneBounds);
  if (!pick) return null;
  const extent = bounds
    ? Math.max(bounds.max[0] - bounds.min[0], bounds.max[1] - bounds.min[1])
    : 40;
  const dotR = Math.min(3.0, Math.max(0.35, extent * 0.006));
  // Dots render at surface + heightOffset (the actual committed height): on
  // bumpy terrain a surface-level dot half-buries, and the offset dot honestly
  // previews where the UE will fly. A thin stem ties it back to the ground
  // point under the crosshair so the offset never reads as a misplacement.
  const lift = (p: Vec3): Vec3 => [p[0], p[1], p[2] + pick.heightOffset];
  const dot = (p: Vec3, key: string, ghost = false) => (
    <group key={key} userData={{ __noFit: true }}>
      <mesh position={lift(p)} renderOrder={999}>
        <sphereGeometry args={[dotR, 18, 12]} />
        <meshBasicMaterial color={PICK_COLOR} transparent opacity={ghost ? 0.5 : 0.95} depthTest={false} />
      </mesh>
      {pick.heightOffset > dotR && (
        <Line points={[p, lift(p)]} color={PICK_COLOR} lineWidth={1} transparent opacity={0.5} />
      )}
    </group>
  );
  const wantMore = pick.count === "multi" || pickPoints.length < pick.count;
  return (
    <>
      {pickPoints.map((p, i) => dot(p, `p${i}`))}
      {hover && dot(hover, "ghost", true)}
      {/* Rubber-band from the last placed point to the cursor. */}
      {hover && pickPoints.length > 0 && wantMore && (
        <Line
          points={[lift(pickPoints[pickPoints.length - 1]), lift(hover)]}
          color={PICK_COLOR}
          lineWidth={2}
          dashed
          dashSize={dotR}
          gapSize={dotR * 0.6}
        />
      )}
    </>
  );
}

/** How many interpolated samples to insert along each preview segment when
 *  draping onto the surface. ~16 keeps the polyline hugging bumpy terrain
 *  without the raycast count getting silly for a long multi-waypoint route. */
const TRAJ_PREVIEW_SUBDIV = 16;

/** Dashed preview of a planned (not yet simulated) trajectory segment,
 *  published by TrajectorySection while it is mounted.
 *
 *  When "Follow terrain" is on, the backend drapes the path onto the scene
 *  surface + UE height; this reproduces that cosmetically so the dashed preview
 *  matches the eventual solve instead of a straight line cutting through hills.
 *  Purely preview-only: it raycasts the visible scene meshes (the same
 *  overlay-excluding set PickController uses) straight DOWN under each densified
 *  point and lifts to surface + the waypoints' height offset, falling back to
 *  the straight-line z where nothing is below. */
function TrajPreviewLine() {
  const seg = useAppStore((s) => s.trajPreview);
  const picking = useAppStore((s) => s.pick !== null);
  const three = useThree((s) => s.scene);

  // Recompute only when the waypoints change (NOT per-frame): a raycast per
  // densified sample is cheap once but must not run every render.
  const drape = useMemo<Vec3[] | null>(() => {
    if (!seg || seg.length < 2) return seg ?? null;

    // Overlays (pick dots, radio-map planes, grid, trajectory markers…) are
    // tagged __noFit; the drape must only see real scene geometry (identical
    // rule to PickController.isOverlay).
    const isOverlay = (obj: THREE.Object3D): boolean => {
      for (let cur: THREE.Object3D | null = obj; cur; cur = cur.parent) {
        if (cur.userData.__noFit) return true;
      }
      return false;
    };

    // Candidate meshes are collected up front: this raycaster is built with
    // ray.set() (no camera), and drei <Line> overlays READ raycaster.camera
    // in their raycast - intersecting them crashed the Canvas ("Cannot read
    // properties of null (reading 'near')") on any scene with line overlays.
    // NOTE: Line2/LineSegments2 EXTEND Mesh, so isMesh alone does not exclude
    // them - they must be filtered out explicitly.
    const candidates: THREE.Object3D[] = [];
    three.traverse((obj) => {
      const m = obj as THREE.Mesh & { isLineSegments2?: boolean };
      if (m.isMesh && !m.isLineSegments2 && m.visible && !isOverlay(m)) {
        candidates.push(m);
      }
    });
    if (candidates.length === 0) return seg;

    // The path's height above the surface: the committed waypoints already sit
    // at surface + UE height, so recover the offset from the first waypoint's
    // clearance over the ground beneath it (clamped >= 0). A uniform offset
    // matches the backend's "drape onto surface + fixed height" behavior.
    const ray = new THREE.Raycaster();
    ray.firstHitOnly = true;
    const surfaceZ = (x: number, y: number, fromZ: number): number | null => {
      // Cast straight down (-Z) from safely above the point.
      ray.set(new THREE.Vector3(x, y, fromZ + 1000), new THREE.Vector3(0, 0, -1));
      const hits = ray.intersectObjects(candidates, false);
      return hits[0] ? hits[0].point.z : null;
    };

    const s0 = surfaceZ(seg[0][0], seg[0][1], seg[0][2]);
    const offset = s0 === null ? 0 : Math.max(0, seg[0][2] - s0);

    const out: Vec3[] = [];
    for (let i = 0; i < seg.length - 1; i++) {
      const a = seg[i];
      const b = seg[i + 1];
      // Include the segment start once; interpolate through to (and including)
      // the segment end so adjacent segments share their joint sample.
      const steps = TRAJ_PREVIEW_SUBDIV;
      for (let k = i === 0 ? 0 : 1; k <= steps; k++) {
        const t = k / steps;
        const x = a[0] + (b[0] - a[0]) * t;
        const y = a[1] + (b[1] - a[1]) * t;
        const straightZ = a[2] + (b[2] - a[2]) * t;
        const sz = surfaceZ(x, y, straightZ);
        // Surface hit → surface + offset; miss → keep the straight-line z.
        out.push([x, y, sz === null ? straightZ : sz + offset]);
      }
    }
    return out;
  }, [seg, three]);

  if (!drape || picking) return null;
  return (
    <group userData={{ __noFit: true }}>
      <Line points={drape} color={PICK_COLOR} lineWidth={2} dashed dashSize={0.6} gapSize={0.4} />
      {/* Markers only at the ORIGINAL waypoints (not the interpolated samples). */}
      {(seg ?? []).map((p, i) => (
        <mesh key={i} position={p} renderOrder={998}>
          <sphereGeometry args={[0.35, 16, 10]} />
          <meshBasicMaterial color={PICK_COLOR} transparent opacity={0.7} depthTest={false} />
        </mesh>
      ))}
    </group>
  );
}

// ---------------------------------------------------------------- entity POV
// Picture-in-picture "camera view" from a selected TX/RX/actor toward a
// chosen link partner — the BS-perspective look at the UE while paths /
// beamforming results are on screen. Rendered as a second scissored pass over
// the same scene graph, so ray overlays are visible in it and trajectory /
// scenario playback (which animates the named entity groups) is tracked live.
const POV_W = 280;
const POV_H = 172;
const POV_MARGIN = 12;
const POV_TOP = 48; // below the viewport button row
const POV_HEAD_H = 26;

function EntityPovInset({ sourceId, targetId }: { sourceId: string; targetId: string | null }) {
  const cam = useMemo(() => {
    const c = new THREE.PerspectiveCamera(55, POV_W / POV_H, 0.2, 20000);
    c.up.set(0, 0, 1);
    return c;
  }, []);
  const eye = useMemo(() => new THREE.Vector3(), []);
  const aim = useMemo(() => new THREE.Vector3(), []);
  // Snapshot handshake: the button resolves this promise from INSIDE the next
  // useFrame pass (same GL state as the working inset render).
  const povCapture = useRef<((url: string | null) => void) | null>(null);
  useEffect(() => {
    povPngGetter = () =>
      new Promise<string | null>((resolve) => {
        povCapture.current?.(null); // a newer request supersedes a pending one
        povCapture.current = resolve;
      });
    return () => {
      povPngGetter = null;
      povCapture.current?.(null);
      povCapture.current = null;
    };
  }, []);

  // Subscribing with a render priority puts r3f in manual-render mode while
  // this inset is mounted, so the main pass is drawn here too.
  useFrame((state) => {
    const { gl, camera, size } = state;
    const root = state.scene;
    gl.setScissorTest(false);
    gl.setViewport(0, 0, size.width, size.height);
    gl.autoClear = true;
    gl.render(root, camera);

    const store = useAppStore.getState();
    const sc = store.scene;
    if (!sc) return;

    // Anchor position of an entity: the LIVE rendered group when present
    // (playback moves those), otherwise the stored scene pose.
    const anchor = (id: string, out: THREE.Vector3): "device" | "actor" | null => {
      const kind = sc.devices.some((d) => d.id === id)
        ? ("device" as const)
        : sc.actors.some((a) => a.id === id)
          ? ("actor" as const)
          : null;
      if (!kind) return null;
      const obj = root.getObjectByName(`pov-ent-${id}`);
      if (obj) {
        obj.getWorldPosition(out);
        return kind;
      }
      const ent =
        kind === "device" ? sc.devices.find((d) => d.id === id) : sc.actors.find((a) => a.id === id);
      if (!ent) return null;
      out.set(ent.position[0], ent.position[1], ent.position[2]);
      return kind;
    };

    const srcKind = anchor(sourceId, eye);
    if (!srcKind) return;
    // Lift the eye above the source's own marker/airframe so it doesn't
    // occlude its view.
    if (srcKind === "actor") {
      const a = sc.actors.find((x) => x.id === sourceId);
      eye.z += (a?.shape.size_m[2] ?? 1) + 0.4;
    } else {
      eye.z +=
        deviceMarkerRadius(sc, store.resolvedEnvironment, store.viewport.markerScale) * 2.2;
    }
    // The POV pass renders the SAME scene graph, so the source's own body
    // (rotors/arms of the scaled drone model) and the marker of any device it
    // carries sit right at the camera and block the shot. Hide them for the
    // POV renders only; the main pass gets them back.
    const povHidden: THREE.Object3D[] = [];
    const hideForPov = () => {
      const ids = [sourceId];
      if (srcKind === "actor") {
        const a = sc.actors.find((x) => x.id === sourceId);
        if (a) ids.push(...a.attached_device_ids);
      }
      for (const id of ids) {
        const g = root.getObjectByName(`pov-ent-${id}`);
        if (g && g.visible) {
          g.visible = false;
          povHidden.push(g);
        }
      }
    };
    const restoreAfterPov = () => {
      for (const g of povHidden) g.visible = true;
      povHidden.length = 0;
    };
    const tgtKind = targetId ? anchor(targetId, aim) : null;
    if (tgtKind === "actor") {
      const a = sc.actors.find((x) => x.id === targetId);
      aim.z += (a?.shape.size_m[2] ?? 1) * 0.6;
    } else if (!tgtKind) {
      aim.set(eye.x, eye.y + 1, eye.z); // no partner: keep a level view
    }
    cam.position.copy(eye);
    cam.lookAt(aim);
    cam.updateMatrixWorld();

    // Pending Snapshot request: render the POV camera over the full canvas,
    // grab it while the buffer still holds it, then repaint the normal main
    // pass so the presented frame looks untouched.
    if (povCapture.current) {
      const resolve = povCapture.current;
      povCapture.current = null;
      cam.aspect = size.width / size.height;
      cam.updateProjectionMatrix();
      gl.autoClear = true;
      hideForPov();
      gl.render(root, cam);
      let url: string | null = null;
      try {
        url = gl.domElement.width > 0 ? gl.domElement.toDataURL("image/png") : null;
      } catch {
        url = null;
      }
      restoreAfterPov();
      cam.aspect = POV_W / POV_H;
      cam.updateProjectionMatrix();
      gl.render(root, camera); // restore the main view underneath the inset
      resolve(url);
    }

    const x = size.width - POV_MARGIN - POV_W;
    const y = size.height - POV_TOP - POV_HEAD_H - POV_H;
    gl.autoClear = false;
    gl.setScissorTest(true);
    gl.setScissor(x, y, POV_W, POV_H);
    gl.setViewport(x, y, POV_W, POV_H);
    gl.clear(true, true, false);
    hideForPov();
    gl.render(root, cam);
    restoreAfterPov();
    gl.setScissorTest(false);
    gl.setViewport(0, 0, size.width, size.height);
    gl.autoClear = true;
  }, 1);

  return null;
}

// Monochrome stroke icons for the viewport corner buttons; they inherit the
// button's currentColor so the .active accent tint applies to the glyph too.
const iconProps = {
  width: 15,
  height: 15,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

function SlidersIcon() {
  return (
    <svg {...iconProps}>
      <path d="M2 4.5h6.2M11.8 4.5H14" />
      <circle cx="10" cy="4.5" r="1.7" />
      <path d="M2 11.5h2.2M7.8 11.5H14" />
      <circle cx="6" cy="11.5" r="1.7" />
    </svg>
  );
}

function OverlayIcon() {
  return (
    <svg {...iconProps}>
      <rect x="2" y="3" width="12" height="10" rx="1.2" />
      <circle cx="5.6" cy="6.2" r="1.1" />
      <path d="M2 11.2l3.8-3.3 2.9 2.5 2.4-1.9 2.9 2.7" />
    </svg>
  );
}

function CameraIcon() {
  return (
    <svg {...iconProps}>
      <path d="M5.6 4.6l1-1.6h2.8l1 1.6h2.1a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5.6a1 1 0 0 1 1-1h2.1z" />
      <circle cx="8" cy="8.6" r="2.3" />
    </svg>
  );
}

function RenderIcon() {
  return (
    <svg {...iconProps}>
      <rect x="2" y="3" width="12" height="10" rx="1.2" />
      <path d="M2 6.2h12M4.6 3l1.4 3.2M7.7 3l1.4 3.2M10.8 3l1.4 3.2" />
    </svg>
  );
}

// ------------------------------------------------------------------ main

export default function Viewer3D() {
  const projectId = useAppStore((s) => s.projectId);
  const scene = useAppStore((s) => s.scene);
  const mode = useAppStore((s) => s.mode);
  const pathResults = useAppStore((s) => s.pathResults);
  const radioMap = useAppStore((s) => s.radioMap);
  const meshRadioMap = useAppStore((s) => s.meshRadioMap);
  const showMeshRadioMapToggle = useAppStore((s) => s.showMeshRadioMap);
  const trajectory = useAppStore((s) => s.trajectory);
  const scenario = useAppStore((s) => s.scenario);
  const showPaths = useAppStore((s) => s.showPaths);
  const showRadioMapToggle = useAppStore((s) => s.showRadioMap);
  const clearSelection = useAppStore((s) => s.clearSelection);
  const viewport = useAppStore((s) => s.viewport);
  const setViewport = useAppStore((s) => s.setViewport);
  const resolvedEnv = useAppStore((s) => s.resolvedEnvironment);
  const pickActive = useAppStore((s) => s.pick);
  const pickCount = useAppStore((s) => s.pickPoints.length);
  const cancelPick = useAppStore((s) => s.cancelPick);
  const [assetFailed, setAssetFailed] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [legendOpen, setLegendOpen] = useState(false);
  const armPlacement = useAppStore((s) => s.armPlacement);

  // Remote-desktop Chrome can skip the initial ResizeObserver callback, which
  // leaves the r3f canvas at its 300x150 default until the window is resized.
  // One synthetic resize after mount guarantees the first measurement
  // everywhere and is a no-op on normal desktops.
  useEffect(() => {
    const t = setTimeout(() => window.dispatchEvent(new Event("resize")), 80);
    return () => clearTimeout(t);
  }, []);

  // Entity POV inset: clicking a TX/RX/actor opens a live view from that
  // entity toward a selectable link partner (close with x; reopens on the
  // next selection).
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);
  const selectedActorId = useAppStore((s) => s.selectedActorId);
  const [povClosed, setPovClosed] = useState(false);
  const [povTargetId, setPovTargetId] = useState<string | null>(null);
  const povSourceId = selectedDeviceId ?? selectedActorId;
  useEffect(() => {
    setPovClosed(false);
    const sc = useAppStore.getState().scene;
    if (!sc || !povSourceId) {
      setPovTargetId(null);
      return;
    }
    // Default partner: TX looks at the first RX, everything else at the
    // first TX; fall back to any other device.
    const dev = sc.devices.find((d) => d.id === povSourceId);
    const wantKind = dev?.kind === "tx" ? "rx" : "tx";
    const first =
      sc.devices.find((d) => d.kind === wantKind && d.id !== povSourceId) ??
      sc.devices.find((d) => d.id !== povSourceId);
    setPovTargetId(first?.id ?? null);
  }, [povSourceId]);
  const povVisible = !!scene && !!povSourceId && !povClosed;
  const povSourceDev = scene?.devices.find((d) => d.id === povSourceId);
  const povSourceLabel = povSourceDev
    ? `${povSourceDev.kind.toUpperCase()} ${povSourceDev.id}`
    : povSourceId ?? "";
  const povCandidates = scene
    ? [
        ...scene.devices
          .filter((d) => d.id !== povSourceId)
          .map((d) => ({ id: d.id, label: `${d.kind.toUpperCase()} ${d.id}` })),
        ...scene.actors
          .filter((a) => a.id !== povSourceId)
          .map((a) => ({ id: a.id, label: `${a.kind} ${a.id}` })),
      ]
    : [];

  // Paper-ready: download exactly the pixels on screen (camera pose, overlays,
  // rays — WYSIWYG), full canvas resolution, PNG.
  const saveView = () => {
    const url = viewportPngGetter?.();
    if (!url) {
      useAppStore.setState({ error: "Viewport capture unavailable (canvas not ready)" });
      return;
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${projectId ?? "scene"}_view_${stamp}.png`;
    a.click();
    useAppStore.setState({ notice: `Saved view as ${a.download}` });
  };

  // POV Snapshot: same WYSIWYG PNG contract as the viewport Snapshot, but
  // rendered from the selected entity's camera at full canvas resolution.
  const savePovView = async () => {
    // Bound the wait: if no frame is produced within 2 s (suspended rAF in a
    // hidden window), fail honestly instead of resolving after the click's
    // user activation has expired (the browser would block the download).
    const url = await Promise.race([
      povPngGetter?.() ?? Promise.resolve(null),
      new Promise<string | null>((r) => setTimeout(() => r(null), 2000)),
    ]);
    if (!url) {
      useAppStore.setState({ error: "POV capture unavailable (view not ready)" });
      return;
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${projectId ?? "scene"}_pov_${povSourceId ?? "view"}_${stamp}.png`;
    a.click();
    useAppStore.setState({ notice: `Saved view as ${a.download}` });
  };

  const doRender = async () => {
    if (!projectId || rendering) return;
    const pose = cameraPoseGetter?.();
    if (!pose) return;
    setRendering(true);
    try {
      const url = await api.renderScene(projectId, {
        camera_position: pose.position,
        look_at: pose.target,
        fov_deg: 45,
        width: 1280,
        height: 720,
        spp: 64,
      });
      // Popup blockers make window.open return null; surface the URL as a
      // notice instead of silently doing nothing.
      const win = window.open(url, "_blank");
      if (!win) useAppStore.setState({ notice: "Render ready: " + url });
    } catch (e) {
      useAppStore.setState({ error: e instanceof Error ? e.message : String(e) });
    } finally {
      setRendering(false);
    }
  };

  const uri = scene?.assets.visual_scene_uri ?? null;
  // Cache-busting: a material split rewrites visual/scene.glb in place (same
  // URI), so a bare URL would keep serving useGLTF's stale cache. glbEpoch is
  // bumped by the segmentation apply/undo flows; appending it as ?v= makes the
  // loader treat the rewritten GLB as a fresh URL. Evicting the prior epoch's
  // entry below keeps the cache from growing unbounded across splits.
  const glbEpoch = useAppStore((s) => s.glbEpoch);
  const baseUrl = projectId && uri ? api.assetUrl(projectId, uri) : null;
  const url = baseUrl ? (glbEpoch > 0 ? `${baseUrl}?v=${glbEpoch}` : baseUrl) : null;
  const prevGlbUrl = useRef<string | null>(null);
  useEffect(() => {
    // Drop the previous epoch's cache entry once the new URL is in play, so the
    // rewritten mesh is re-fetched and old buffers are released.
    if (prevGlbUrl.current && prevGlbUrl.current !== url) {
      useGLTF.clear(prevGlbUrl.current);
    }
    prevGlbUrl.current = url;
  }, [url]);
  const overlayUri = scene?.assets.visual_overlay_uri ?? null;
  const overlayUrl =
    projectId && overlayUri && viewport.showOverlay
      ? api.assetUrl(projectId, overlayUri)
      : null;

  useEffect(() => {
    setAssetFailed(false);
  }, [url]);

  const showRadioMap = radioMap && mode === "results" && showRadioMapToggle;
  const showMeshRadioMap =
    meshRadioMap && meshRadioMap.surfaces.length > 0 && mode === "results" && showMeshRadioMapToggle;
  // Scenario playback owns the actors/devices when a scenario is loaded in
  // Results mode; otherwise actors render at their static scene poses.
  const showScenario = useAppStore((s) => s.showScenario);
  // Scenario playback replaces the device/actor layers - only while the user
  // has it switched ON (a stored scenario must not hijack the viewport).
  const scenarioActive =
    scenario !== null && scenario.frames.length > 0 && mode === "results" && showScenario;
  // Trajectory overlay (marker/trail; per-frame rays gated separately by the
  // independent "Trajectory rays" toggle inside TrajectoryOverlay).
  const trajActive = trajectory !== null && mode === "results" && !scenarioActive;
  const dirPos = directionalPosition(
    viewport.directionalAzimuthDeg,
    viewport.directionalElevationDeg,
  );
  // Grid/axes helpers scale with the scene: fixed sizes either drown an
  // indoor room or vanish on an outdoor site.
  const boundsForHelpers = useAppStore((s) => s.sceneBounds);
  const gridSize = boundsForHelpers
    ? Math.ceil(
        Math.max(
          30,
          Math.max(
            boundsForHelpers.max[0] - boundsForHelpers.min[0],
            boundsForHelpers.max[1] - boundsForHelpers.min[1],
          ) * 1.2,
        ) / 10,
      ) * 10
    : resolvedEnv === "indoor"
      ? 30
      : 200;
  const gridCenter: [number, number] = boundsForHelpers
    ? [
        (boundsForHelpers.min[0] + boundsForHelpers.max[0]) / 2,
        (boundsForHelpers.min[1] + boundsForHelpers.max[1]) / 2,
      ]
    : [0, 0];
  const axesSize = resolvedEnv === "indoor" ? 4 : Math.max(10, gridSize * 0.08);
  // Nice 1/2/5×10^k cell size ≈ gridSize/40 (indoor rooms get ~1 m cells,
  // a 1 km OSM import gets ~25 m cells).
  const gridCell = (() => {
    const raw = Math.max(0.5, gridSize / 40);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * mag;
  })();

  return (
    <div className={"viewer3d" + (pickActive ? " picking" : "")}>
      <Canvas
        // Render speed <-> quality preset (viewport panel): resolution is the
        // dominant draw cost on multi-million-triangle imports. Data-lossless.
        dpr={renderQualityDpr(viewport.renderQuality)}
        flat
        // preserveDrawingBuffer lets captureViewport() read the canvas as a
        // JPEG data URL (AI/VLM) after the frame has been presented.
        gl={{ preserveDrawingBuffer: true }}
        onPointerMissed={() => {
          // Ignore while a gizmo axis is grabbed OR a panel pick is active:
          // a pick tap on empty ground must not clear the user's selection.
          if (!gizmoBusy && !useAppStore.getState().pick) clearSelection();
        }}
      >
        {/* AODT-style dark viewer; lighting/background driven by viewport settings. */}
        <color attach="background" args={[viewport.backgroundColor]} />
        <PerspectiveCamera
          makeDefault
          up={[0, 0, 1]}
          position={[35, -35, 25]}
          fov={45}
          // Outdoor scenes push the far geometry hundreds of meters out; a
          // 0.1 m near plane starves depth precision there (z-fighting on
          // large coplanar surfaces). Indoor keeps the tight near plane.
          near={resolvedEnv === "indoor" ? 0.1 : 1}
          far={5000}
        />
        {/* zoomToCursor = Blender-style wheel zoom toward the pointer. */}
        <OrbitControls makeDefault target={[0, 0, 0]} zoomToCursor={viewport.zoomToCursor} />
        <OrbitSelectionPivot />
        <SurfaceProbe />
        {/* Distance fog (Blender mist): far geometry fades into the background;
            range scales with the scene so it works in a room and on a campus. */}
        {viewport.fogEnabled && (
          <fog attach="fog" args={[viewport.backgroundColor, gridSize * 0.8, gridSize * 3]} />
        )}
        <ambientLight intensity={viewport.ambientIntensity} />
        {/* position defines the hemisphere axis: +Z sky in our Z-up world
            (three's default +Y axis put the "sky" color on the horizon). */}
        <hemisphereLight
          args={["#dfe9f3", "#20262e", viewport.hemisphereIntensity]}
          position={[0, 0, 1]}
        />
        <directionalLight
          position={dirPos}
          intensity={viewport.directionalIntensity}
          color={viewport.directionalColor}
        />
        {/* Blender-style infinite ground grid (drei shader Grid, rotated from
            its default XZ into our Z-up XY plane): follows the camera forever
            and fades with distance, so no scene ever outruns it. Cell size
            still scales with the scene so indoor rooms get fine 1 m cells. */}
        {viewport.showGrid && (
          <Grid
            position={[gridCenter[0], gridCenter[1], 0]}
            rotation={[Math.PI / 2, 0, 0]}
            infiniteGrid
            followCamera
            cellSize={gridCell}
            sectionSize={gridCell * 5}
            cellColor="#232f3d"
            sectionColor="#2f3e50"
            cellThickness={0.6}
            sectionThickness={1.1}
            fadeDistance={gridSize * 3}
            fadeStrength={1.5}
            userData={{ __noFit: true }}
          />
        )}
        {viewport.showAxes && <axesHelper args={[axesSize]} />}
        <ScreenshotCapture />
        <ViewerHotkeys />
        <PickController />
        <TrajPreviewLine />
        {overlayUrl && <OverlayBackdrop url={overlayUrl} />}
        <Suspense
          fallback={
            <Html center>
              <div className="canvas-note">Loading visual scene…</div>
            </Html>
          }
        >
          {url && !assetFailed ? (
            <AssetBoundary key={url} url={url} fallback={<FallbackPrims />} onFailed={() => setAssetFailed(true)}>
              <GLBScene url={url} />
            </AssetBoundary>
          ) : (
            <FallbackPrims />
          )}
        </Suspense>
        {/* Per-face material tint of the source prim while a segmentation
            preview is active (visual/results only; gated internally). */}
        <SegmentationTint />
        {scenarioActive ? (
          <ScenarioOverlay showPaths={showPaths} />
        ) : (
          <>
            {scene && <Devices />}
            {scene && <Actors />}
          </>
        )}
        {/* Static rays and trajectory-frame rays are independent overlays;
            each has its own toggle in the results overlay row. */}
        {pathResults && showPaths && mode === "results" && !scenarioActive && <RayPaths />}
        {showRadioMap && <RadioMapPlane radioMap={radioMap} />}
        {showMeshRadioMap && <MeshRadioMapOverlay result={meshRadioMap} />}
        {trajActive && <TrajectoryOverlay trajectory={trajectory} />}
        {povVisible && povSourceId && (
          <EntityPovInset sourceId={povSourceId} targetId={povTargetId} />
        )}
      </Canvas>
      {showRadioMap && <RadioMapLegend radioMap={radioMap} />}
      {pickActive && (
        <div className="viewer-banner pick-banner">
          <span>
            Click to place: <strong>{pickActive.label}</strong> ({pickCount}/{pickActive.count})
            — orbit freely, Esc cancels
          </span>
          <button onClick={() => cancelPick()} title="Cancel pick (Esc)">
            ×
          </button>
        </div>
      )}
      {scene && (!url || assetFailed) && (
        <div className="viewer-banner">Visual asset missing — showing placeholder geometry</div>
      )}
      {/* Visible placement buttons: arm the same viewer ghost as the K/L
          hotkeys, for mouse discoverability. */}
      <button
        className="viewport-gear viewport-place viewport-place-tx"
        title="Place TX by clicking in the scene (K)"
        onClick={() => armPlacement("tx")}
      >
        +TX
      </button>
      <button
        className="viewport-gear viewport-place viewport-place-rx"
        title="Place RX by clicking (L)"
        onClick={() => armPlacement("rx")}
      >
        +RX
      </button>
      <button
        className={"viewport-gear viewport-help" + (legendOpen ? " active" : "")}
        title="Keyboard shortcuts"
        onClick={() => setLegendOpen((o) => !o)}
      >
        ?
      </button>
      {legendOpen && (
        <div className="hotkey-legend">
          <div className="hotkey-legend-title">Shortcuts</div>
          <ul>
            <li><kbd>R</kbd> reset view</li>
            <li><kbd>F</kbd> fit scene / frame selection</li>
            <li><kbd>1</kbd>/<kbd>3</kbd>/<kbd>7</kbd> front/right/top view (+<kbd>Shift</kbd> opposite)</li>
            <li><kbd>K</kbd> place TX</li>
            <li><kbd>L</kbd> place RX</li>
            <li><kbd>S</kbd> slice</li>
            <li><kbd>M</kbd> radio map</li>
            <li><kbd>Esc</kbd> cancel</li>
            <li>gizmo: click TX/RX then drag X/Y/Z arrows</li>
            <li>panel Pick buttons: yellow crosshair mode</li>
          </ul>
        </div>
      )}
      {povVisible && (
        <div className="pov-panel" style={{ top: POV_TOP, right: POV_MARGIN, width: POV_W }}>
          <div className="pov-head" style={{ height: POV_HEAD_H }}>
            <span className="pov-title" title={`Live view from ${povSourceId}`}>
              {povSourceLabel} →
            </span>
            <select
              value={povTargetId ?? ""}
              title="Link partner the view aims at"
              onChange={(e) => setPovTargetId(e.target.value || null)}
            >
              {povCandidates.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
            <button
              className="pov-snap"
              title="Save this view as a PNG (full resolution)"
              onClick={() => void savePovView()}
            >
              <CameraIcon />
            </button>
            <button className="pov-close" title="Close view" onClick={() => setPovClosed(true)}>
              ×
            </button>
          </div>
          {/* Transparent frame: the GL inset is scissor-rendered underneath. */}
          <div className="pov-frame" style={{ height: POV_H }} />
        </div>
      )}
      {/* Settings cluster: a flex row so the buttons pack with no gap when the
          conditional overlay toggle is absent. */}
      <div className="viewport-cluster">
        <button
          className={"viewport-gear" + (panelOpen ? " active" : "")}
          title="Viewport lighting & display"
          onClick={() => setPanelOpen((o) => !o)}
        >
          <SlidersIcon />
        </button>
        {overlayUri && (
          // Quick toggle for the textured photogrammetry backdrop; the same
          // switch lives in the viewport panel, this is the one-click version.
          <button
            className={"viewport-gear" + (viewport.showOverlay ? " active" : "")}
            title={viewport.showOverlay ? "Hide textured overlay" : "Show textured overlay"}
            onClick={() => setViewport({ showOverlay: !viewport.showOverlay })}
          >
            <OverlayIcon />
          </button>
        )}
        <button
          className="viewport-gear"
          title="Save this exact view as a PNG (what you see, full resolution — paper-ready)"
          onClick={saveView}
        >
          <CameraIcon />
        </button>
        <button
          className={"viewport-gear" + (rendering ? " active" : "")}
          title="Offline path-traced render via Mitsuba (slower, physically shaded — not the on-screen view)"
          disabled={rendering}
          onClick={() => void doRender()}
        >
          <RenderIcon />
        </button>
      </div>
      {panelOpen && <ViewportPanel onClose={() => setPanelOpen(false)} />}
    </div>
  );
}

/** Colorbar for the radio-map overlay (colormap + range follow the viewport
 *  display settings so the legend always matches the rendered texture). */
function RadioMapLegend({ radioMap }: { radioMap: RadioMapResultSet }) {
  const viewport = useAppStore((s) => s.viewport);
  const [autoMin, autoMax] = radioMapRange(radioMap);
  let min = viewport.rmVmin ?? autoMin;
  let max = viewport.rmVmax ?? autoMax;
  if (min > max) [min, max] = [max, min];
  if (!Number.isFinite(min)) return null;
  const stops = Array.from({ length: 11 }, (_, i) =>
    radioMapCss(i / 10, viewport.rmColormap),
  ).join(", ");
  const unit = radioMap.metric === "rss_dbm" ? "dBm" : "dB";
  const label = radioMap.metric === "rss_dbm" ? "RSS" : "Path gain";
  return (
    <div className="radiomap-legend">
      <div className="radiomap-legend-title">{label}</div>
      <div className="radiomap-legend-scale">
        <div className="radiomap-legend-bar" style={{ background: `linear-gradient(to top, ${stops})` }} />
        <div className="radiomap-legend-ticks">
          <span>{max.toFixed(0)} {unit}</span>
          <span>{((min + max) / 2).toFixed(0)}</span>
          <span>{min.toFixed(0)} {unit}</span>
        </div>
      </div>
    </div>
  );
}
