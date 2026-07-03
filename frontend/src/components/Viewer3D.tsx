import { Component, Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
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

  const sevMap = useMemo(() => severityByPrim(validation), [validation]);

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

/** Marker radius scaled to scene size AND environment: small indoor rooms
 *  need ~0.08 m markers, outdoor campuses ~0.5 m. */
function deviceMarkerRadius(scene: Scene, env: ResolvedEnvironment = "outdoor"): number {
  const pos = scene.devices.map((d) => d.position);
  const base = (() => {
    if (pos.length < 1) return 0.5;
    let maxSpan = 0;
    for (let axis = 0; axis < 3; axis++) {
      const vals = pos.map((p) => p[axis]);
      maxSpan = Math.max(maxSpan, Math.max(...vals) - Math.min(...vals));
    }
    return Math.min(0.6, Math.max(0.08, maxSpan * 0.02));
  })();
  return Math.max(0.06, base * envScale(env));
}

/** X/Y/Z translate gizmo attached to the currently selected marker group.
 *  Root-cause fix for the earlier "all input dead" regression: committing a
 *  move refetches the scene and remounts the marker, which could unmount
 *  TransformControls MID-DRAG and leave OrbitControls.enabled=false forever.
 *  This component (a) commits on mouse-up, (b) force-restores orbit controls
 *  on commit AND on unmount, so input can never stay locked. */
function SelectionGizmo({
  target,
  onCommit,
}: {
  target: THREE.Object3D;
  onCommit: (pos: Vec3) => void;
}) {
  const controls = useThree((s) => s.controls) as { enabled?: boolean } | null;
  useEffect(() => {
    return () => {
      // Whatever happens (remount, deselect, crash), orbit comes back.
      if (controls) controls.enabled = true;
    };
  }, [controls, target]);
  return (
    <TransformControls
      object={target}
      mode="translate"
      size={0.8}
      onMouseUp={() => {
        const p = target.position;
        if (controls) controls.enabled = true;
        onCommit([p.x, p.y, p.z]);
      }}
    />
  );
}

function Devices() {
  const scene = useAppStore((s) => s.scene);
  const env = useAppStore((s) => s.resolvedEnvironment);
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);
  const selectDevice = useAppStore((s) => s.selectDevice);
  const updateDevice = useAppStore((s) => s.updateDevice);
  const [gizmoTarget, setGizmoTarget] = useState<THREE.Group | null>(null);
  if (!scene) return null;
  const radius = deviceMarkerRadius(scene, env);

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
        return (
          <group
            key={d.id}
            position={d.position}
            // The selected marker registers itself as the gizmo target.
            ref={selected ? setGizmoTarget : undefined}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
              e.stopPropagation();
              selectDevice(d.id);
            }}
          >
            {marker}
          </group>
        );
      })}
      {gizmoTarget && selectedDeviceId && (
        <SelectionGizmo
          target={gizmoTarget}
          onCommit={(pos) => void updateDevice(selectedDeviceId, { position: pos })}
        />
      )}
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
  const [gizmoTarget, setGizmoTarget] = useState<THREE.Group | null>(null);
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
        return (
          <group
            key={a.id}
            position={position}
            rotation={[0, 0, (yawDeg * Math.PI) / 180]}
            ref={selected && !frameStates ? setGizmoTarget : undefined}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
              e.stopPropagation();
              selectActor(a.id);
            }}
          >
            {body}
          </group>
        );
      })}
      {gizmoTarget && selectedActorId && !frameStates && (
        <SelectionGizmo
          target={gizmoTarget}
          onCommit={(pos) => void updateActor(selectedActorId, { position: pos })}
        />
      )}
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
  const colorBy = useAppStore((s) => s.colorBy);
  const lineWidthByPower = useAppStore((s) => s.lineWidthByPower);
  const env = useAppStore((s) => s.resolvedEnvironment);
  // Interaction dots shrink indoors (수정사항 #9): 0.18 m outdoors was
  // oversized for a lab room.
  const interactionRadius = 0.18 * envScale(env);

  // Same filter pipeline as the results table (single source of truth).
  const visible = useMemo(
    () => filterPaths(paths, { pathTypeFilter, strongestN, minPowerDbm }),
    [paths, pathTypeFilter, strongestN, minPowerDbm],
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
  const radius = deviceMarkerRadius(scene);
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
    return () => registerViewportCapture(null);
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
    <mesh position={[grid.origin[0] + w / 2, grid.origin[1] + h / 2, z]}>
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
      console.debug(
        "[overlay] world bounds",
        box.min.toArray().map((v) => Number(v.toFixed(1))),
        box.max.toArray().map((v) => Number(v.toFixed(1))),
        `| unlit textured mats: ${unlit}`,
      );
    }
  }, [object, gl]);
  // Overlay GLBs follow the glTF spec (+Y up); the app world is Z-up ENU
  // (camera up=[0,0,1]; scene.glb is baked to Z-up at import, external
  // overlays are copied verbatim). Rotating +90 deg about X maps
  // (x, y, z)_gltf -> (x, -z, y)_world, aligning the backdrop with the RF
  // scene. Composed via a parent group so a root transform inside the GLB
  // is preserved rather than overwritten.
  return (
    <group ref={group} rotation={[Math.PI / 2, 0, 0]}>
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
  const setShowRadioMap = useAppStore((s) => s.setShowRadioMap);
  const pointer = useRef<{ x: number; y: number } | null>(null);

  // Local clipping must be on for the slice plane to work at all.
  useEffect(() => {
    gl.localClippingEnabled = true;
    // Dev/debug handle: lets tooling (and bug reports) inspect the live
    // three.js graph without a React devtools round-trip.
    (window as unknown as { __stwScene?: THREE.Scene }).__stwScene = three;
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

    const placeDevice = (kind: "tx" | "rx") => {
      if (!pointer.current) return;
      const ray = new THREE.Raycaster();
      ray.setFromCamera(new THREE.Vector2(pointer.current.x, pointer.current.y), camera);
      const hits = ray
        .intersectObjects(three.children, true)
        .filter((h) => (h.object as THREE.Mesh).isMesh && h.object.visible);
      let pos: THREE.Vector3 | null = null;
      const hit = hits[0];
      if (hit) {
        pos = hit.point.clone();
        if (hit.face) {
          // +1.5 m along the surface normal, like the RT GUI's placement.
          const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld);
          pos.addScaledVector(n, 1.5);
        } else {
          pos.z += 1.5;
        }
      } else {
        // No geometry under the cursor: drop onto the z=0 ground plane.
        const t = new THREE.Vector3();
        if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), t)) {
          pos = t.setZ(1.5);
        }
      }
      if (pos) void addDevice(kind, [pos.x, pos.y, pos.z]);
    };

    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)) {
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const { viewport, showRadioMap } = useAppStore.getState();
      switch (e.key.toLowerCase()) {
        case "r":
          camera.position.set(35, -35, 25);
          controls?.target?.set(0, 0, 0);
          controls?.update?.();
          break;
        case "f": {
          const box = new THREE.Box3();
          three.traverse((obj) => {
            const mesh = obj as THREE.Mesh;
            if (mesh.isMesh && mesh.visible) box.expandByObject(mesh);
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
          placeDevice("tx");
          break;
        case "l":
          placeDevice("rx");
          break;
        case "s":
          setViewport({ showSlice: !viewport.showSlice });
          break;
        case "m":
          setShowRadioMap(!showRadioMap);
          break;
        default:
          return;
      }
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      canvas.removeEventListener("pointermove", onMove);
      window.removeEventListener("keydown", onKey);
    };
  }, [gl, camera, three, controls, addDevice, setViewport, setShowRadioMap]);

  return null;
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
  const [assetFailed, setAssetFailed] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);

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
  const scenarioActive = scenario !== null && scenario.frames.length > 0 && mode === "results";
  // When the current trajectory sample carries live per-frame rays, they take
  // over from the static pathResults overlay (feature: trajectory live rays).
  const trajActive = trajectory !== null && mode === "results" && !scenarioActive;
  const trajFramePaths = trajActive && trajectoryHasFramePaths(trajectory, trajFrame);
  const dirPos = directionalPosition(
    viewport.directionalAzimuthDeg,
    viewport.directionalElevationDeg,
  );

  return (
    <div className="viewer3d">
      <Canvas
        dpr={[1, 2]}
        flat
        // preserveDrawingBuffer lets captureViewport() read the canvas as a
        // JPEG data URL (AI/VLM) after the frame has been presented.
        gl={{ preserveDrawingBuffer: true }}
        onPointerMissed={() => clearSelection()}
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
      {scene && (!url || assetFailed) && (
        <div className="viewer-banner">Visual asset missing — showing placeholder geometry</div>
      )}
      <button
        className={"viewport-gear" + (panelOpen ? " active" : "")}
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
