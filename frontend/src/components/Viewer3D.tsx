import { Component, Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import type { ThreeEvent } from "@react-three/fiber";
import { Html, Line, OrbitControls, PerspectiveCamera, TransformControls, useGLTF } from "@react-three/drei";
import { useAppStore } from "../store/appStore";
import type { ResolvedEnvironment } from "../envPresets";
import type { Mode } from "../store/appStore";
import { SELECTED_PATH_COLOR } from "./common";
import { filterPaths, pathColor, powerRange, powerWidth } from "../pathFilter";
import { api } from "../api/client";
import { directionalPosition } from "../viewportSettings";
import type { RadioMapColormap } from "../viewportSettings";
import ViewportPanel from "./ViewportPanel";
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
  const showSlice = useAppStore((s) => s.viewport.showSlice);
  const sliceZ = useAppStore((s) => s.viewport.sliceZ);

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
      const orig = mesh.userData.__origMat as THREE.Material | THREE.Material[];
      const prim = findPrim(mesh);
      const selected = prim !== null && selection.includes(prim.id);
      const color = overlayColor(prim, mode, materials, sevMap);
      if (color === null) {
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
  }, [gltf, findPrim, mode, selection, materials, validation, showSlice, sliceZ]);

  return (
    <primitive
      object={gltf.scene}
      onPointerDown={(e: ThreeEvent<PointerEvent>) => {
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
    return Math.min(1.0, Math.max(0.15, maxSpan * 0.02));
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
  const radius = deviceMarkerRadius(scene, env, markerScale);

  return (
    <group>
      {scene.devices.map((d) => {
        const selected = d.id === selectedDeviceId;
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
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
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
  if (!scene) return null;

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
        // position is the base center: lift the box by half its height so it
        // sits on the ground contact plane. Yaw about Z (up).
        const body = (
          <>
            {/* Selection/drag is handled by the wrapping DraggableGroup. */}
            <mesh position={[0, 0, h / 2]}>
              <boxGeometry args={[l, w, h]} />
              <meshStandardMaterial
                color={color}
                transparent
                opacity={0.85}
                emissive={selected ? PICKER_COLOR : "#000000"}
                emissiveIntensity={selected ? 0.7 : 0}
              />
            </mesh>
            <Html position={[0, 0, h + 0.4]} center zIndexRange={[10, 0]}>
              <div className={"device-label" + (selected ? " selected" : "")} title={a.id}>
                {a.name || a.id}
              </div>
            </Html>
            {a.trajectory && a.trajectory.waypoints.length > 1 && (
              // Waypoints are absolute world coords; render outside the actor's
              // local rotation by placing an inverse-less sibling group.
              <ActorTrajectoryLine waypoints={a.trajectory.waypoints} origin={position} yawDeg={yawDeg} />
            )}
          </>
        );
        const inner = (
          <group
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
              e.stopPropagation();
              selectActor(a.id);
            }}
          >
            {body}
          </group>
        );
        return selected && !frameStates ? (
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
      })}
    </group>
  );
}

/** Yellow polyline through an actor's absolute waypoints. Rendered as a child
 *  of the actor group, so undo the group's translation+yaw to draw in world. */
function ActorTrajectoryLine({
  waypoints,
  origin,
  yawDeg,
}: {
  waypoints: Vec3[];
  origin: Vec3;
  yawDeg: number;
}) {
  const local = useMemo(() => {
    const yaw = (yawDeg * Math.PI) / 180;
    const cos = Math.cos(-yaw);
    const sin = Math.sin(-yaw);
    // Convert each absolute waypoint into the actor group's local frame
    // (translate by -origin, then rotate by -yaw about Z).
    return waypoints.map((wp): Vec3 => {
      const dx = wp[0] - origin[0];
      const dy = wp[1] - origin[1];
      const dz = wp[2] - origin[2];
      return [dx * cos - dy * sin, dx * sin + dy * cos, dz];
    });
  }, [waypoints, origin, yawDeg]);
  return <Line points={local} color="#ffee58" lineWidth={2} />;
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
  const colorBy = useAppStore((s) => s.colorBy);
  const lineWidthByPower = useAppStore((s) => s.lineWidthByPower);
  const env = useAppStore((s) => s.resolvedEnvironment);
  // Interaction dots shrink indoors (수정사항 #9): 0.18 m outdoors was
  // oversized for a lab room.
  const interactionRadius = 0.18 * envScale(env);

  // Same filter pipeline as the results table (single source of truth).
  const visible = useMemo(
    () => filterPaths(paths, { pathTypeFilter, strongestN, minPowerDbm, hiddenLinkDevices }),
    [paths, pathTypeFilter, strongestN, minPowerDbm, hiddenLinkDevices],
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

/** UE marker (pulsing blue sphere) at the current sample + yellow visited trail.
 *  When the current sample carries per-waypoint ray paths (include_paths), those
 *  live rays are drawn (respecting the store filters) via the shared PathLines. */
function TrajectoryOverlay({ trajectory }: { trajectory: TrajectoryResultSet }) {
  const trajFrame = useAppStore((s) => s.trajFrame);
  const showPaths = useAppStore((s) => s.showPaths);
  const markerRef = useRef<THREE.Mesh>(null);

  const samples = trajectory.samples;
  const frame = Math.max(0, Math.min(samples.length - 1, trajFrame));
  const current = samples[frame];

  // Trail = visited sample positions up to and including the current frame.
  const trail = useMemo(
    () => samples.slice(0, frame + 1).map((s) => s.position),
    [samples, frame],
  );

  // Pulse the emissive intensity of the UE marker.
  useFrame((state) => {
    const mat = markerRef.current?.material as THREE.MeshStandardMaterial | undefined;
    if (mat) mat.emissiveIntensity = 0.5 + 0.4 * Math.sin(state.clock.elapsedTime * 4);
  });

  if (!current) return null;

  // Live per-frame rays (only present when the trajectory was simulated with
  // include_paths). When absent we fall back to the static pathResults overlay
  // rendered separately by the parent.
  const framePaths = current.paths ?? null;

  return (
    <group>
      {trail.length > 1 && (
        // Yellow trail (AODT legend convention).
        <Line points={trail} color="#ffee58" lineWidth={2} />
      )}
      {showPaths && framePaths && framePaths.length > 0 && (
        <PathLines paths={framePaths} showInteractions={false} />
      )}
      <group position={current.position}>
        <mesh ref={markerRef}>
          <sphereGeometry args={[0.5, 24, 16]} />
          <meshStandardMaterial color="#2e9bff" emissive="#2e9bff" emissiveIntensity={0.6} />
        </mesh>
        <Html position={[0, 0, 1.4]} center zIndexRange={[10, 0]}>
          <div className="device-label selected">{trajectory.ue_id}</div>
        </Html>
      </group>
    </group>
  );
}

/** True when the trajectory's current sample carries live per-frame ray paths
 *  (so the static pathResults overlay should yield to them). */
function trajectoryHasFramePaths(
  trajectory: TrajectoryResultSet | null,
  trajFrame: number,
): boolean {
  if (!trajectory || trajectory.samples.length === 0) return false;
  const frame = Math.max(0, Math.min(trajectory.samples.length - 1, trajFrame));
  const paths = trajectory.samples[frame]?.paths;
  return Array.isArray(paths) && paths.length > 0;
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
        const pos = states.get(d.id);
        if (!pos) return null;
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
function ScreenshotCapture() {
  const gl = useThree((s) => s.gl);
  const registerViewportCapture = useAppStore((s) => s.registerViewportCapture);
  useEffect(() => {
    registerViewportCapture(() => {
      try {
        const src = gl.domElement;
        const w = src.width;
        const h = src.height;
        if (w === 0 || h === 0) return null;
        // Full-res if already narrow enough; otherwise downscale via an
        // offscreen canvas preserving aspect ratio.
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
      viewportPngGetter = null;
    };
  }, [gl, registerViewportCapture]);
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

// --------------------------------------------------------------- hotkeys

/** Viewer keyboard shortcuts (Sionna RT GUI parity):
 *  R reset camera · F fit scene · K add TX at cursor · L add RX at cursor
 *  (placed at the surface hit + 1.5 m along its normal) · S slice plane ·
 *  M radio-map overlay. Ignored while typing in form fields. */
function ViewerHotkeys() {
  const camera = useThree((s) => s.camera);
  const gl = useThree((s) => s.gl);
  const three = useThree((s) => s.scene);
  const controls = useThree((s) => s.controls) as { target?: THREE.Vector3; update?: () => void } | null;
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
    }
  }, [gl, three]);

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
      ray.setFromCamera(new THREE.Vector2(pointer.current.x, pointer.current.y), camera);
      const hits = ray
        .intersectObjects(three.children, true)
        .filter((h) => (h.object as THREE.Mesh).isMesh && h.object.visible);
      const hit = hits[0];
      if (hit) {
        const pos = hit.point.clone();
        if (hit.face) {
          // +1.5 m along the surface normal, like the RT GUI's placement.
          const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld);
          pos.addScaledVector(n, 1.5);
        } else {
          pos.z += 1.5;
        }
        return [pos.x, pos.y, pos.z];
      }
      // No geometry under the cursor: drop onto the z=0 ground plane.
      const t = new THREE.Vector3();
      if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), t)) {
        return [t.x, t.y, 1.5];
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
          notice: "Placement blocked during scenario playback - clear the scenario first",
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
      const { viewport } = useAppStore.getState();
      switch (e.key.toLowerCase()) {
        case "r":
          camera.position.set(35, -35, 25);
          controls?.target?.set(0, 0, 0);
          controls?.update?.();
          break;
        case "f": {
          const box = new THREE.Box3();
          // Skip overlays/helpers (radio-map plane, textured backdrop, pick
          // dots, ghosts, trajectory preview): fitting to them flies the
          // camera far past the actual scene (audit finding). Anything tagged
          // __noFit - or inside a tagged ancestor - is excluded.
          const noFit = (o: THREE.Object3D | null): boolean => {
            for (let cur = o; cur; cur = cur.parent) {
              if (cur.userData.__noFit) return true;
            }
            return false;
          };
          three.traverse((obj) => {
            const mesh = obj as THREE.Mesh;
            if (mesh.isMesh && mesh.visible && !noFit(mesh)) box.expandByObject(mesh);
          });
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
              notice: "Placement blocked during scenario playback - clear the scenario first",
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
            useAppStore.setState({ notice: "No radio map yet - run Simulate radio map first" });
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

    const resolve = (clientX: number, clientY: number): Vec3 | null => {
      const req = pickRef.current;
      if (!req) return null;
      const r = canvas.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((clientX - r.left) / r.width) * 2 - 1,
        -((clientY - r.top) / r.height) * 2 + 1,
      );
      const ray = new THREE.Raycaster();
      ray.setFromCamera(ndc, camera);
      if (req.target === "surface") {
        const hits = ray
          .intersectObjects(three.children, true)
          .filter((h) => (h.object as THREE.Mesh).isMesh && h.object.visible);
        if (hits[0]) {
          const p = hits[0].point;
          return [p.x, p.y, p.z + req.heightOffset];
        }
      }
      const t = new THREE.Vector3();
      if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), t)) {
        return [t.x, t.y, req.heightOffset];
      }
      return null;
    };

    const onMove = (e: PointerEvent) => {
      setHover(resolve(e.clientX, e.clientY));
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
      e.stopImmediatePropagation();
      e.preventDefault();
      useAppStore.getState().addPickPoint(p);
    };
    const onCancel = () => {
      downRef.current = null;
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") useAppStore.getState().cancelPick();
    };

    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerdown", onDown, { capture: true });
    // Up on window so a drag that ends off-canvas still resets the tap test.
    window.addEventListener("pointerup", onUp, { capture: true });
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("keydown", onKey);
    return () => {
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerdown", onDown, { capture: true });
      window.removeEventListener("pointerup", onUp, { capture: true });
      window.removeEventListener("pointercancel", onCancel);
      window.removeEventListener("keydown", onKey);
    };
  }, [pick, gl, camera, three]);

  if (!pick) return null;
  const dot = (p: Vec3, key: string, ghost = false) => (
    <mesh key={key} position={p} renderOrder={999} userData={{ __noFit: true }}>
      <sphereGeometry args={[0.45, 18, 12]} />
      <meshBasicMaterial color={PICK_COLOR} transparent opacity={ghost ? 0.45 : 0.9} depthTest={false} />
    </mesh>
  );
  return (
    <>
      {pickPoints.map((p, i) => dot(p, `p${i}`))}
      {hover && dot(hover, "ghost", true)}
      {/* Rubber-band from the last placed point to the cursor for multi-point picks. */}
      {hover && pickPoints.length > 0 && pickPoints.length < pick.count && (
        <Line
          points={[pickPoints[pickPoints.length - 1], hover]}
          color={PICK_COLOR}
          lineWidth={2}
          dashed
          dashSize={0.5}
          gapSize={0.3}
        />
      )}
    </>
  );
}

/** Dashed preview of a planned (not yet simulated) trajectory segment,
 *  published by TrajectorySection while it is mounted. */
function TrajPreviewLine() {
  const seg = useAppStore((s) => s.trajPreview);
  const picking = useAppStore((s) => s.pick !== null);
  if (!seg || picking) return null;
  return (
    <group userData={{ __noFit: true }}>
      <Line points={[seg[0], seg[1]]} color={PICK_COLOR} lineWidth={2} dashed dashSize={0.6} gapSize={0.4} />
      {seg.map((p, i) => (
        <mesh key={i} position={p} renderOrder={998}>
          <sphereGeometry args={[0.35, 16, 10]} />
          <meshBasicMaterial color={PICK_COLOR} transparent opacity={0.7} depthTest={false} />
        </mesh>
      ))}
    </group>
  );
}

// ------------------------------------------------------------------ main

export default function Viewer3D() {
  const projectId = useAppStore((s) => s.projectId);
  const scene = useAppStore((s) => s.scene);
  const mode = useAppStore((s) => s.mode);
  const pathResults = useAppStore((s) => s.pathResults);
  const radioMap = useAppStore((s) => s.radioMap);
  const trajectory = useAppStore((s) => s.trajectory);
  const trajFrame = useAppStore((s) => s.trajFrame);
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
  const url = projectId && uri ? api.assetUrl(projectId, uri) : null;
  const overlayUri = scene?.assets.visual_overlay_uri ?? null;
  const overlayUrl =
    projectId && overlayUri && viewport.showOverlay
      ? api.assetUrl(projectId, overlayUri)
      : null;

  useEffect(() => {
    setAssetFailed(false);
  }, [url]);

  const showRadioMap = radioMap && mode === "results" && showRadioMapToggle;
  // Scenario playback owns the actors/devices when a scenario is loaded in
  // Results mode; otherwise actors render at their static scene poses.
  const showScenario = useAppStore((s) => s.showScenario);
  // Scenario playback replaces the device/actor layers - only while the user
  // has it switched ON (a stored scenario must not hijack the viewport).
  const scenarioActive =
    scenario !== null && scenario.frames.length > 0 && mode === "results" && showScenario;
  // When the current trajectory sample carries live per-frame rays, they take
  // over from the static pathResults overlay (feature: trajectory live rays).
  const trajActive = trajectory !== null && mode === "results" && !scenarioActive;
  const trajFramePaths = trajActive && trajectoryHasFramePaths(trajectory, trajFrame);
  const dirPos = directionalPosition(
    viewport.directionalAzimuthDeg,
    viewport.directionalElevationDeg,
  );

  return (
    <div className={"viewer3d" + (pickActive ? " picking" : "")}>
      <Canvas
        dpr={[1, 2]}
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
        <PerspectiveCamera makeDefault up={[0, 0, 1]} position={[35, -35, 25]} fov={45} near={0.1} far={5000} />
        <OrbitControls makeDefault target={[0, 0, 0]} />
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
        {/* gridHelper lies in XZ by default; rotate +90° about X into the XY ground plane. */}
        {viewport.showGrid && (
          <gridHelper
            args={resolvedEnv === "indoor" ? [30, 30, "#2c3947", "#1b2531"] : [200, 50, "#2c3947", "#1b2531"]}
            rotation={[Math.PI / 2, 0, 0]}
          />
        )}
        {viewport.showAxes && <axesHelper args={[4]} />}
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
        {scenarioActive ? (
          <ScenarioOverlay showPaths={showPaths} />
        ) : (
          <>
            {scene && <Devices />}
            {scene && <Actors />}
          </>
        )}
        {/* Static rays yield to live trajectory-frame rays when those are shown. */}
        {pathResults && showPaths && mode === "results" && !scenarioActive && !trajFramePaths && <RayPaths />}
        {showRadioMap && <RadioMapPlane radioMap={radioMap} />}
        {trajActive && <TrajectoryOverlay trajectory={trajectory} />}
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
        +TX@
      </button>
      <button
        className="viewport-gear viewport-place viewport-place-rx"
        title="Place RX by clicking (L)"
        onClick={() => armPlacement("rx")}
      >
        +RX@
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
            <li><kbd>F</kbd> fit scene</li>
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
      <button
        className={"viewport-gear viewport-settings-gear" + (panelOpen ? " active" : "")}
        title="Viewport lighting & display"
        onClick={() => setPanelOpen((o) => !o)}
      >
        ⚙
      </button>
      {overlayUri && (
        // Quick toggle for the textured photogrammetry backdrop; the same
        // switch lives in the viewport panel, this is the one-click version.
        <button
          className={"viewport-gear viewport-tex" + (viewport.showOverlay ? " active" : "")}
          title={viewport.showOverlay ? "Hide textured overlay" : "Show textured overlay"}
          onClick={() => setViewport({ showOverlay: !viewport.showOverlay })}
        >
          🖼
        </button>
      )}
      <button
        className="viewport-gear viewport-snap"
        title="Save this exact view as a PNG (what you see, full resolution — paper-ready)"
        onClick={saveView}
      >
        📸
      </button>
      <button
        className={"viewport-gear viewport-render" + (rendering ? " active" : "")}
        title="Offline path-traced render via Mitsuba (slower, physically shaded — not the on-screen view)"
        disabled={rendering}
        onClick={() => void doRender()}
      >
        🎞
      </button>
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
