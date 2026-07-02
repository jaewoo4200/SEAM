import { Component, Suspense, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import * as THREE from "three";
import { Canvas } from "@react-three/fiber";
import type { ThreeEvent } from "@react-three/fiber";
import { Html, Line, OrbitControls, PerspectiveCamera, useGLTF } from "@react-three/drei";
import { useAppStore } from "../store/appStore";
import type { Mode } from "../store/appStore";
import { api } from "../api/client";
import type {
  PathType,
  Prim,
  RadioMapResultSet,
  RFMaterialLibrary,
  ValidationReport,
} from "../types/api";

// All backend coordinates are Z-up ENU meters. The world is NOT rotated;
// instead the camera uses up=[0,0,1] and the grid is rotated into the XY plane.

const ACCENT = "#4fc3f7";
const UNASSIGNED_COLOR = "#ff9800";
const UNMATCHED_COLOR = "#4b5563";
const SELECTED_PATH_COLOR = "#ffee58";

const PATH_COLORS: Record<PathType, string> = {
  los: "#66bb6a",
  reflection: "#4fc3f7",
  diffraction: "#ab47bc",
  scattering: "#ffa726",
  transmission: "#f06292",
  mixed: "#eceff1",
};

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
        const overlay = new THREE.MeshStandardMaterial({
          color: new THREE.Color(color),
          roughness: 0.85,
          metalness: 0.05,
        });
        if (selected) {
          overlay.emissive = new THREE.Color(ACCENT);
          overlay.emissiveIntensity = 0.6;
        }
        created.push(overlay);
        mesh.material = overlay;
      }
    });
    return () => {
      gltf.scene.traverse((obj) => {
        const mesh = obj as THREE.Mesh;
        if (mesh.isMesh && mesh.userData.__origMat !== undefined) {
          mesh.material = mesh.userData.__origMat as THREE.Material | THREE.Material[];
        }
      });
      for (const m of created) m.dispose();
    };
  }, [gltf, findPrim, mode, selection, materials, validation]);

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
  { fallback: ReactNode; onFailed: () => void; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    this.props.onFailed();
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

// ---------------------------------------------------------------- devices

function Devices() {
  const scene = useAppStore((s) => s.scene);
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);
  const selectDevice = useAppStore((s) => s.selectDevice);
  if (!scene) return null;

  return (
    <group>
      {scene.devices.map((d) => {
        const selected = d.id === selectedDeviceId;
        return (
          <group
            key={d.id}
            position={d.position}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => {
              e.stopPropagation();
              selectDevice(d.id);
            }}
          >
            {d.kind === "tx" ? (
              // Cone apex is +Y by default; rotate +90° about X so it points +Z (up).
              <mesh rotation={[Math.PI / 2, 0, 0]}>
                <coneGeometry args={[0.45, 1.1, 16]} />
                <meshStandardMaterial
                  color={d.color}
                  emissive={selected ? ACCENT : "#000000"}
                  emissiveIntensity={selected ? 0.8 : 0}
                />
              </mesh>
            ) : (
              <mesh>
                <sphereGeometry args={[0.5, 24, 16]} />
                <meshStandardMaterial
                  color={d.color}
                  emissive={selected ? ACCENT : "#000000"}
                  emissiveIntensity={selected ? 0.8 : 0}
                />
              </mesh>
            )}
            <Html position={[0, 0, 1.2]} center zIndexRange={[10, 0]}>
              <div className={"device-label" + (selected ? " selected" : "")}>{d.id}</div>
            </Html>
          </group>
        );
      })}
    </group>
  );
}

// -------------------------------------------------------------- ray paths

function RayPaths() {
  const pathResults = useAppStore((s) => s.pathResults);
  const selectedPathId = useAppStore((s) => s.selectedPathId);
  const selectPath = useAppStore((s) => s.selectPath);
  if (!pathResults) return null;

  return (
    <group>
      {pathResults.paths.map((p) => {
        const selected = p.path_id === selectedPathId;
        const color = selected ? SELECTED_PATH_COLOR : PATH_COLORS[p.path_type];
        return (
          <group key={p.path_id}>
            <Line
              points={p.vertices}
              color={color}
              lineWidth={selected ? 4 : 2}
              onClick={(e) => {
                e.stopPropagation();
                selectPath(p.path_id);
              }}
            />
            {p.interactions.map((it, i) => (
              <mesh key={`${p.path_id}_i${i}`} position={it.point}>
                <sphereGeometry args={[0.18, 12, 8]} />
                <meshBasicMaterial color={color} />
              </mesh>
            ))}
          </group>
        );
      })}
    </group>
  );
}

// -------------------------------------------------------------- radio map

function makeRadioMapTexture(rm: RadioMapResultSet): THREE.CanvasTexture {
  const { nx, ny } = rm.grid;
  const canvas = document.createElement("canvas");
  canvas.width = nx;
  canvas.height = ny;
  const ctx = canvas.getContext("2d")!;
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
  const span = max > min ? max - min : 1;
  for (let iy = 0; iy < ny; iy++) {
    const row = rm.values[iy] ?? [];
    for (let ix = 0; ix < nx; ix++) {
      const v = row[ix];
      if (v === null || v === undefined) continue;
      const t = (v - min) / span;
      // Jet-ish: blue (low) -> red (high).
      ctx.fillStyle = `hsl(${Math.round((1 - t) * 240)}, 90%, 55%)`;
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
  const texture = useMemo(() => makeRadioMapTexture(radioMap), [radioMap]);
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

// ------------------------------------------------------------------ main

export default function Viewer3D() {
  const projectId = useAppStore((s) => s.projectId);
  const scene = useAppStore((s) => s.scene);
  const mode = useAppStore((s) => s.mode);
  const pathResults = useAppStore((s) => s.pathResults);
  const radioMap = useAppStore((s) => s.radioMap);
  const clearSelection = useAppStore((s) => s.clearSelection);
  const [assetFailed, setAssetFailed] = useState(false);

  const uri = scene?.assets.visual_scene_uri ?? null;
  const url = projectId && uri ? api.assetUrl(projectId, uri) : null;

  useEffect(() => {
    setAssetFailed(false);
  }, [url]);

  return (
    <div className="viewer3d">
      <Canvas dpr={[1, 2]} onPointerMissed={() => clearSelection()}>
        <color attach="background" args={["#0d1420"]} />
        <PerspectiveCamera makeDefault up={[0, 0, 1]} position={[35, -35, 25]} fov={50} near={0.1} far={5000} />
        <OrbitControls makeDefault target={[0, 0, 0]} />
        <ambientLight intensity={0.7} />
        <directionalLight position={[30, -20, 50]} intensity={1.1} />
        {/* gridHelper lies in XZ by default; rotate +90° about X into the XY ground plane. */}
        <gridHelper args={[200, 50, "#2c3947", "#1b2531"]} rotation={[Math.PI / 2, 0, 0]} />
        <axesHelper args={[4]} />
        <Suspense
          fallback={
            <Html center>
              <div className="canvas-note">Loading visual scene…</div>
            </Html>
          }
        >
          {url && !assetFailed ? (
            <AssetBoundary key={url} fallback={<FallbackPrims />} onFailed={() => setAssetFailed(true)}>
              <GLBScene url={url} />
            </AssetBoundary>
          ) : (
            <FallbackPrims />
          )}
        </Suspense>
        {scene && <Devices />}
        {pathResults && <RayPaths />}
        {radioMap && mode === "results" && <RadioMapPlane radioMap={radioMap} />}
      </Canvas>
      {scene && (!url || assetFailed) && (
        <div className="viewer-banner">Visual asset missing — showing placeholder geometry</div>
      )}
    </div>
  );
}
