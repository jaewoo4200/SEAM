import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { StatusDot } from "./common";
import type { Prim } from "../types/api";

/** Two-step inline confirm: the first click arms the danger state (auto-reverts
 *  after ~4s), the second runs the action. Mirrors the inline-form style used by
 *  RFMaterialPanel rather than a blocking window.confirm. */
function useArmedConfirm(): {
  armed: boolean;
  arm: () => void;
  disarm: () => void;
} {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clear = () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  };
  useEffect(() => clear, []);
  const arm = () => {
    clear();
    setArmed(true);
    timer.current = setTimeout(() => setArmed(false), 4000);
  };
  const disarm = () => {
    clear();
    setArmed(false);
  };
  return { armed, arm, disarm };
}

/** Per-row delete button with a two-step (× → ✓?) inline confirm. */
function RowDeleteButton({
  label,
  disabled,
  onConfirm,
}: {
  label: string;
  disabled: boolean;
  onConfirm: () => void;
}) {
  const { armed, arm, disarm } = useArmedConfirm();
  return (
    <button
      className={"tree-del" + (armed ? " armed" : "")}
      disabled={disabled}
      title={armed ? `Confirm delete ${label}` : `Delete ${label}`}
      onClick={(e) => {
        e.stopPropagation();
        if (armed) {
          disarm();
          onConfirm();
        } else {
          arm();
        }
      }}
      onBlur={disarm}
    >
      {armed ? "✓?" : "×"}
    </button>
  );
}

interface TreeNode {
  name: string;
  path: string;
  prim: Prim | null;
  children: TreeNode[];
}

/** Build a display hierarchy from absolute path-like prim ids. */
function buildTree(prims: Prim[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", prim: null, children: [] };
  for (const prim of prims) {
    const segments = prim.id.split("/").filter(Boolean);
    let node = root;
    let path = "";
    for (const seg of segments) {
      path += "/" + seg;
      let child = node.children.find((c) => c.path === path);
      if (!child) {
        child = { name: seg, path, prim: null, children: [] };
        node.children.push(child);
      }
      node = child;
    }
    node.prim = prim;
  }
  return root.children;
}

function isAdditive(e: MouseEvent): boolean {
  return e.ctrlKey || e.metaKey;
}

function TreeRow({
  node,
  depth,
  collapsed,
  toggleCollapsed,
}: {
  node: TreeNode;
  depth: number;
  collapsed: Set<string>;
  toggleCollapsed: (path: string) => void;
}) {
  const selection = useAppStore((s) => s.selection);
  const selectPrim = useAppStore((s) => s.selectPrim);
  const hiddenPrims = useAppStore((s) => s.hiddenPrims);
  const togglePrimVisibility = useAppStore((s) => s.togglePrimVisibility);
  const hasChildren = node.children.length > 0;
  const isCollapsed = collapsed.has(node.path);
  const selected = node.prim !== null && selection.includes(node.prim.id);
  const rf = node.prim?.rf ?? null;

  return (
    <>
      <div
        className={"tree-row" + (selected ? " selected" : "")}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={(e) => {
          if (node.prim) {
            selectPrim(node.prim.id, isAdditive(e));
          } else if (hasChildren) {
            toggleCollapsed(node.path);
          }
        }}
        title={node.path}
      >
        <span
          className="caret"
          onClick={(e) => {
            if (hasChildren) {
              e.stopPropagation();
              toggleCollapsed(node.path);
            }
          }}
        >
          {hasChildren ? (isCollapsed ? "▸" : "▾") : ""}
        </span>
        {rf && node.prim?.type === "mesh_primitive" && <StatusDot status={rf.assignment_status} />}
        <span
          className={"tree-name" + (node.prim && hiddenPrims.includes(node.prim.id) ? " tree-hidden" : "")}
        >
          {node.prim?.name || node.name}
        </span>
        {rf?.material_id && <span className="tree-mat">{rf.material_id}</span>}
        {node.prim && (
          <button
            className={"tree-eye" + (hiddenPrims.includes(node.prim.id) ? " off" : "")}
            title={hiddenPrims.includes(node.prim.id) ? "Show in viewer" : "Hide in viewer"}
            onClick={(e) => {
              e.stopPropagation();
              togglePrimVisibility(node.prim!.id);
            }}
          >
            {hiddenPrims.includes(node.prim.id) ? "◌" : "◉"}
          </button>
        )}
      </div>
      {hasChildren &&
        !isCollapsed &&
        node.children.map((child) => (
          <TreeRow
            key={child.path}
            node={child}
            depth={depth + 1}
            collapsed={collapsed}
            toggleCollapsed={toggleCollapsed}
          />
        ))}
    </>
  );
}

export default function SceneTree() {
  const scene = useAppStore((s) => s.scene);
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);
  const selectDevice = useAppStore((s) => s.selectDevice);
  const addDevice = useAppStore((s) => s.addDevice);
  const deleteDevice = useAppStore((s) => s.deleteDevice);
  const clearDevices = useAppStore((s) => s.clearDevices);
  const selectedActorId = useAppStore((s) => s.selectedActorId);
  const selectActor = useAppStore((s) => s.selectActor);
  const addActor = useAppStore((s) => s.addActor);
  const deleteActor = useAppStore((s) => s.deleteActor);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const refetchScene = useAppStore((s) => s.refetchScene);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const clearConfirm = useArmedConfirm();
  // Device JSON import: cartesian x/y/z or geographic lat/lon (+alt_m/agl_m),
  // auto-detected per entry; orientation/power carry through. Template +
  // format guide: docs/point_import.md (or GET /api/import/templates).
  const devFileRef = useRef<HTMLInputElement>(null);
  const importDevicesFile = async (e: { target: HTMLInputElement }) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !projectId) return;
    try {
      const parsed: unknown = JSON.parse(await file.text());
      const body = Array.isArray(parsed) ? { devices: parsed } : parsed;
      const resp = await api.importDevices(projectId, body);
      await refetchScene();
      useAppStore.setState({
        notice:
          `Imported devices — ${resp.added_ids.length} added, ${resp.updated_ids.length} updated` +
          (resp.warnings.length > 0 ? ` · ⚠ ${resp.warnings.join(" · ")}` : ""),
      });
    } catch (err) {
      useAppStore.setState({
        error: `device import failed: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  };

  const tree = useMemo(() => buildTree(scene?.prims ?? []), [scene]);

  const toggleCollapsed = (path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  if (!scene) {
    return <div className="empty-state">No scene loaded</div>;
  }

  return (
    <div className="scene-tree">
      <div className="tree-section">Scene · {scene.name || scene.scene_id}</div>
      {tree.length === 0 && <div className="empty-state">Scene has no prims</div>}
      {tree.map((node) => (
        <TreeRow
          key={node.path}
          node={node}
          depth={0}
          collapsed={collapsed}
          toggleCollapsed={toggleCollapsed}
        />
      ))}

      <div className="tree-section tree-section-head" style={{ marginTop: 14 }}>
        <span>Devices</span>
        <span className="tree-head-actions">
          <button disabled={busy !== null} onClick={() => void addDevice("tx")} title="Add transmitter">
            +TX
          </button>
          <button disabled={busy !== null} onClick={() => void addDevice("rx")} title="Add receiver">
            +RX
          </button>
          <button
            disabled={busy !== null || !projectId}
            onClick={() => devFileRef.current?.click()}
            title="Import TX/RX devices from JSON (cartesian x/y/z or geographic lat/lon, orientation supported — see docs/point_import.md)"
          >
            ⤓ JSON
          </button>
          <input
            ref={devFileRef}
            type="file"
            accept="application/json,.json"
            style={{ display: "none" }}
            onChange={(e) => void importDevicesFile(e)}
          />
          <button
            className={clearConfirm.armed ? "danger" : ""}
            disabled={busy !== null || scene.devices.length === 0}
            onClick={() => {
              if (clearConfirm.armed) {
                clearConfirm.disarm();
                void clearDevices();
              } else {
                clearConfirm.arm();
              }
            }}
            onBlur={clearConfirm.disarm}
            title={
              clearConfirm.armed
                ? "Click again to clear all devices"
                : "Clear all radio devices"
            }
          >
            {clearConfirm.armed ? `Really clear ${scene.devices.length}?` : "Clear all"}
          </button>
        </span>
      </div>
      {scene.devices.length === 0 && <div className="empty-state">No devices</div>}
      {scene.devices.map((d) => (
        <div
          key={d.id}
          className={"tree-row" + (selectedDeviceId === d.id ? " selected" : "")}
          style={{ paddingLeft: 22 }}
          onClick={() => selectDevice(d.id)}
          title={`${d.kind} · ${d.id}`}
        >
          <span className="device-icon" style={{ color: d.color }}>
            {d.kind === "tx" ? "▲" : "●"}
          </span>
          <span className="tree-name">{d.id}</span>
          {d.name && <span className="tree-mat">{d.name}</span>}
          <RowDeleteButton
            label={d.id}
            disabled={busy !== null}
            onConfirm={() => void deleteDevice(d.id)}
          />
        </div>
      ))}

      <div className="tree-section tree-section-head" style={{ marginTop: 14 }}>
        <span>Actors</span>
        <span className="tree-head-actions">
          <button disabled={busy !== null} onClick={() => void addActor("car")} title="Add car">
            +Car
          </button>
          <button disabled={busy !== null} onClick={() => void addActor("human")} title="Add human">
            +Human
          </button>
          <button
            disabled={busy !== null}
            onClick={() => void addActor("custom")}
            title="Add custom scatterer"
          >
            +Custom
          </button>
        </span>
      </div>
      {scene.actors.length === 0 && <div className="empty-state">No actors</div>}
      {scene.actors.map((a) => (
        <div
          key={a.id}
          className={"tree-row" + (selectedActorId === a.id ? " selected" : "")}
          style={{ paddingLeft: 22 }}
          onClick={() => selectActor(a.id)}
          title={`${a.kind} · ${a.id}`}
        >
          <span className="device-icon" style={{ color: a.color ?? "#a78bfa" }}>
            {a.kind === "car" ? "▬" : a.kind === "human" ? "☖" : "◆"}
          </span>
          <span className="tree-name">{a.id}</span>
          {a.name && <span className="tree-mat">{a.name}</span>}
          <RowDeleteButton
            label={a.id}
            disabled={busy !== null}
            onConfirm={() => void deleteActor(a.id)}
          />
        </div>
      ))}
    </div>
  );
}
