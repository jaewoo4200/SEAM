import { useMemo, useState } from "react";
import type { MouseEvent } from "react";
import { useAppStore } from "../store/appStore";
import { StatusDot } from "./common";
import type { Prim } from "../types/api";

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
        <span className="tree-name">{node.prim?.name || node.name}</span>
        {rf?.material_id && <span className="tree-mat">{rf.material_id}</span>}
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
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

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

      <div className="tree-section" style={{ marginTop: 14 }}>
        Devices
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
        </div>
      ))}
    </div>
  );
}
