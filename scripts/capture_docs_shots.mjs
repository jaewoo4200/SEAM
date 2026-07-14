// Capture the documentation screenshots in docs/images/ from a live dev app.
//
// Drives the real UI headlessly through the dev-only window.__stwStore /
// __stwScene / __stwCamera / __stwControls handles, so every shot shows the
// actual product (no mockups) and the set can be regenerated after UI changes.
//
// Prerequisites: backend on :8000 and the Vite dev server running (any port).
//   node scripts/capture_docs_shots.mjs [--base http://localhost:4610]
//                                       [--out docs/images]
//                                       [--only 06_paths,07_radio_map]
//                                       [--shot-project vfy_shot]
//
// Shots that need a textured drone-scan project (UAV/trajectory/POV) run on the
// project named by --shot-project and are skipped with a warning if it does not
// exist. Solves persist results into whatever projects are used — point those
// shots at a disposable duplicate, not a real project.

import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

// puppeteer-core is a frontend devDependency; resolve it from there so this
// script works no matter which directory it is run from.
const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const puppeteer = require("puppeteer-core");

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const BASE = opt("base", "http://localhost:4610");
const OUT = opt("out", "docs/images");
const ONLY = opt("only", "").split(",").filter(Boolean);
const SHOT_PROJECT = opt("shot-project", "vfy_shot");

const CHROME_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium-browser",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];
const exe = CHROME_CANDIDATES.find((p) => existsSync(p));
if (!exe) {
  console.error("No Chrome/Edge executable found; edit CHROME_CANDIDATES.");
  process.exit(1);
}

mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Open a project and wait until the mesh count is stable (GLB mounted). */
async function openProject(page, id) {
  await page.evaluate(async (pid) => {
    const st = window.__stwStore.getState();
    if (st.projectId !== pid) await st.openProject(pid);
  }, id);
  await page.waitForFunction(
    () => {
      const w = window;
      let m = 0;
      w.__stwScene.traverse((o) => {
        if (o.isMesh && !o.isLineSegments2) m++;
      });
      if (m > 0 && m === w.__lastMeshCount) w.__stableTicks = (w.__stableTicks || 0) + 1;
      else w.__stableTicks = 0;
      w.__lastMeshCount = m;
      return w.__stableTicks >= 3;
    },
    { timeout: 180_000, polling: 1000 },
  );
  await sleep(800);
}

/** Frame the whole scene: bbox from world-space bounding spheres (present
 *  after the first render), camera placed along an azimuth/elevation ray. */
async function autoFrame(page, { az = 0.8, el = 0.45, pad = 1.15 } = {}) {
  await page.evaluate(
    ({ az, el, pad }) => {
      const scene = window.__stwScene;
      const cam = window.__stwCamera;
      const ctrl = window.__stwControls;
      let min = [Infinity, Infinity, Infinity];
      let max = [-Infinity, -Infinity, -Infinity];
      scene.updateMatrixWorld(true);
      scene.traverse((o) => {
        if (!o.isMesh || o.isLineSegments2 || !o.visible) return;
        const g = o.geometry;
        if (!g) return;
        if (!g.boundingSphere) g.computeBoundingSphere();
        const bs = g.boundingSphere;
        if (!bs || !isFinite(bs.radius)) return;
        const c = bs.center.clone().applyMatrix4(o.matrixWorld);
        const s = o.getWorldScale(o.position.clone().set(1, 1, 1));
        const r = bs.radius * Math.max(Math.abs(s.x), Math.abs(s.y), Math.abs(s.z));
        // Skip helper geometry: TransformControls (selection gizmo) carries
        // ~1e5-radius invisible drag planes that exploded the bbox and flung
        // the camera 250 km out (black viewport on any selection shot).
        if (r > 5e3) return;
        min = [Math.min(min[0], c.x - r), Math.min(min[1], c.y - r), Math.min(min[2], c.z - r)];
        max = [Math.max(max[0], c.x + r), Math.max(max[1], c.y + r), Math.max(max[2], c.z + r)];
      });
      const cx = (min[0] + max[0]) / 2;
      const cy = (min[1] + max[1]) / 2;
      const cz = (min[2] + max[2]) / 2;
      const diag = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]);
      const dist = (diag / 2 / Math.tan(((cam.fov ?? 50) * Math.PI) / 360)) * pad;
      // Z-up world: azimuth in the XY plane, elevation toward +Z.
      const dx = Math.cos(az) * Math.cos(el);
      const dy = Math.sin(az) * Math.cos(el);
      const dz = Math.sin(el);
      ctrl.target.set(cx, cy, cz);
      cam.position.set(cx + dx * dist, cy + dy * dist, cz + dz * dist);
      ctrl.update();
    },
    { az, el, pad },
  );
  await sleep(600);
}

const store = (page, code, arg) => page.evaluate(code, arg);

/** Clear any lingering error/notice toasts so they don't pollute the shot. */
async function dismissToasts(page) {
  await page.evaluate(() => {
    const st = window.__stwStore.getState();
    st.dismissError?.();
    st.dismissNotice?.();
  });
  await sleep(200);
}

/** While the entity-POV inset is mounted the main pass is hand-rendered, and a
 *  headless screenshot can catch the swap chain after a clear (black main
 *  viewport). Render one frame synchronously, freeze it as a data-URL <img>
 *  overlaid on the canvas, screenshot, then remove the overlay. */
async function freezeMainCanvas(page) {
  const diag = await page.evaluate(async () => {
    const scene = window.__stwScene;
    const rootObj = scene.__r3f?.root ?? scene.children.find((c) => c.__r3f)?.__r3f?.root;
    const s = rootObj?.getState ? rootObj.getState() : rootObj;
    if (!s?.gl) return "no r3f gl state";
    const gl = s.gl;
    const canvas = gl.domElement;
    const lum = (url) =>
      new Promise((res) => {
        const im = new Image();
        im.onload = () => {
          const c = document.createElement("canvas");
          c.width = 64;
          c.height = 40;
          const ctx = c.getContext("2d");
          ctx.drawImage(im, 0, 0, 64, 40);
          const d = ctx.getImageData(0, 0, 64, 40).data;
          let t = 0;
          for (let i = 0; i < d.length; i += 4) t += d[i] + d[i + 1] + d[i + 2];
          res(t / (d.length / 4) / 3);
        };
        im.src = url;
      });
    const tryRender = async (cam) => {
      if (cam) {
        gl.setRenderTarget(null);
        gl.setScissorTest(false);
        gl.setViewport(0, 0, s.size.width, s.size.height);
        gl.autoClear = true;
        gl.render(s.scene, cam);
      }
      const url = canvas.toDataURL("image/png");
      return { url, l: await lum(url) };
    };
    // Candidates, cheapest first: the buffer as-is (component already drew
    // main + POV corner), then explicit re-renders per camera handle.
    const asIs = await tryRender(null);
    let best = { ...asIs, tag: "as-is" };
    if (best.l < 8) {
      const a = await tryRender(s.camera);
      if (a.l > best.l) best = { ...a, tag: "state.camera" };
    }
    if (best.l < 8 && window.__stwCamera && window.__stwCamera !== s.camera) {
      const b = await tryRender(window.__stwCamera);
      if (b.l > best.l) best = { ...b, tag: "__stwCamera" };
    }
    // Insert the frozen frame as the canvas's NEXT SIBLING so it wins the
    // same stacking context as the canvas (a fixed body-level overlay can
    // land underneath the viewer's own stacking context).
    const img = document.createElement("img");
    img.id = "__shot_freeze";
    img.src = best.url;
    const parent = canvas.parentElement;
    if (getComputedStyle(parent).position === "static") parent.style.position = "relative";
    Object.assign(img.style, {
      position: "absolute",
      left: `${canvas.offsetLeft}px`,
      top: `${canvas.offsetTop}px`,
      width: `${canvas.clientWidth}px`,
      height: `${canvas.clientHeight}px`,
      pointerEvents: "none",
    });
    canvas.insertAdjacentElement("afterend", img);
    return `freeze via ${best.tag} (lum ${best.l.toFixed(1)}; as-is ${asIs.l.toFixed(1)}; cams ${s.camera === window.__stwCamera ? "same" : "DIFFERENT"})`;
  });
  console.log(`  [freeze] ${diag}`);
  await sleep(300);
}

async function unfreezeMainCanvas(page) {
  await page.evaluate(() => document.getElementById("__shot_freeze")?.remove());
}

/** name → stage function. Ordered so same-project shots share an open. */
const SHOTS = [
  // ---- sample_demo: tutorial / getting-started set -------------------------
  {
    name: "01_visual_mode",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("visual");
        st.clearSelection();
      });
      await autoFrame(page, { az: -2.2, el: 0.5 });
    },
  },
  {
    name: "02_inspector",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("visual");
        const prims = st.scene.prims ?? st.scene.objects ?? [];
        const win = prims.find((p) => /window/i.test(p.id)) ?? prims[0];
        if (win) st.selectPrim(win.id);
      });
      await autoFrame(page, { az: -2.2, el: 0.35, pad: 0.9 });
    },
  },
  {
    name: "03_rf_materials",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.clearSelection();
        st.setMode("rf");
      });
      await autoFrame(page, { az: -2.2, el: 0.5 });
    },
  },
  {
    name: "04_validation",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, async () => {
        const st = window.__stwStore.getState();
        st.setMode("validation");
        await st.runValidation?.();
      });
      await sleep(800);
    },
  },
  {
    name: "05_ai_assist",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, async () => {
        const st = window.__stwStore.getState();
        st.setMode("ai");
        await st.suggestMaterials();
      });
      await sleep(800);
    },
  },
  {
    name: "06_paths",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, async () => {
        const st = window.__stwStore.getState();
        st.setMode("results");
        await st.simulatePaths();
      });
      await autoFrame(page, { az: -2.0, el: 0.55, pad: 0.95 });
    },
  },
  {
    name: "07_radio_map",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, async () => {
        const st = window.__stwStore.getState();
        st.setMode("results");
        await st.simulateRadioMap();
        window.__stwStore.setState({ showRadioMap: true, showPaths: false });
      });
      await autoFrame(page, { az: -2.0, el: 0.9, pad: 0.95 });
    },
  },
  {
    name: "08_beamforming",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, async () => {
        const st = window.__stwStore.getState();
        st.setMode("results");
        await st.runBeamforming();
        window.__stwStore.setState({ showBeamforming: true, showRadioMap: false });
      });
      await autoFrame(page, { az: -2.0, el: 0.5, pad: 0.95 });
    },
  },
  {
    name: "09_channel_analysis",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, async () => {
        const st = window.__stwStore.getState();
        st.setMode("results");
        // Positional signature (txId, rxId, numCfrPoints, scsKhz, opts) —
        // mirrors the ChannelPanel's manual Analyze button.
        await st.analyzeChannel("tx_001", "rx_001", 128, 30, { persist: true });
        // A calmer backdrop than the radio-map heatmap for a charts shot.
        window.__stwStore.setState({ showRadioMap: false, showPaths: true });
        // Float the Channel analysis panel over the viewer so the dashboard
        // is the visual subject of the shot.
        st.setPanelDock("channel", "float");
        st.setPanelFloatRect("channel", { x: 640, y: 80, w: 640, h: 820 });
        st.raisePanel("channel");
      });
      await sleep(1200);
      // The dashboard sections are collapsed by default; expand the main one
      // (Section renders a button.solver-caret with "▸ Channel analysis").
      await page.evaluate(() => {
        const btn = [...document.querySelectorAll("button.solver-caret")].find((b) =>
          /channel analysis/i.test(b.textContent ?? ""),
        );
        btn?.click();
      });
      await sleep(1200);
    },
    async after(page) {
      await store(page, () => window.__stwStore.getState().setPanelDock("channel", "right"));
    },
  },
  {
    name: "10_dataset",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("results");
        st.setPanelDock("mlDataset", "float");
        st.setPanelFloatRect("mlDataset", { x: 640, y: 80, w: 620, h: 720 });
        st.raisePanel("mlDataset");
      });
      await sleep(800);
    },
    async after(page) {
      await store(page, () => window.__stwStore.getState().setPanelDock("mlDataset", "right"));
    },
  },
  // ---- textured drone-scan project (disposable duplicate) ------------------
  {
    name: "11_ftc_textured",
    project: SHOT_PROJECT,
    async run(page) {
      await openProject(page, SHOT_PROJECT);
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("visual");
        st.clearSelection();
      });
      await autoFrame(page, { az: -0.9, el: 0.55 });
    },
  },
  {
    name: "12_uav_trajectory",
    project: SHOT_PROJECT,
    async run(page) {
      await openProject(page, SHOT_PROJECT);
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("visual");
        const uav = (st.scene.actors ?? []).find((a) => a.kind === "uav");
        if (uav) st.selectActor(uav.id);
      });
      await autoFrame(page, { az: -0.6, el: 0.5, pad: 0.85 });
      await freezeMainCanvas(page); // POV inset mounted → hand-render the main pass
    },
    async after(page) {
      await unfreezeMainCanvas(page);
    },
  },
  {
    name: "13_trajectory_playback",
    project: SHOT_PROJECT,
    async run(page) {
      await openProject(page, SHOT_PROJECT);
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("results");
        st.clearSelection();
        // Hide the static paths overlay so the per-frame trajectory rays and
        // the moving UE marker are the unambiguous subject of the shot.
        window.__stwStore.setState({ showTrajectoryRays: true, showPaths: false });
        st.setTrajFrame(3);
      });
      await sleep(900);
      await autoFrame(page, { az: -1.2, el: 0.5, pad: 0.9 });
    },
  },
  {
    name: "14_pov_inset",
    project: SHOT_PROJECT,
    async run(page) {
      await openProject(page, SHOT_PROJECT);
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("visual");
        window.__stwStore.setState({ showTrajectoryRays: false });
        st.setTrajFrame(0);
        st.selectDevice("tx_001");
      });
      await sleep(1500); // let the POV inset render its first frames
      await autoFrame(page, { az: -0.9, el: 0.45 });
      await freezeMainCanvas(page); // POV inset mounted → hand-render the main pass
      await sleep(800);
    },
    async after(page) {
      await unfreezeMainCanvas(page);
    },
  },
  // ---- OSM import result ----------------------------------------------------
  {
    name: "15_osm_import",
    project: "osm_test_hyu",
    async run(page) {
      await openProject(page, "osm_test_hyu");
      await store(page, () => {
        const st = window.__stwStore.getState();
        st.setMode("visual");
        st.clearSelection();
      });
      await autoFrame(page, { az: -2.3, el: 0.7 });
    },
  },
  // ---- Import dialog ---------------------------------------------------------
  {
    name: "16_import_dialog",
    async run(page) {
      await openProject(page, "sample_demo");
      await store(page, () => window.__stwStore.getState().setMode("visual"));
      const clicked = await page.evaluate(() => {
        const btn = [...document.querySelectorAll("button")].find(
          (b) => b.textContent?.trim() === "Import",
        );
        if (!btn) return false;
        btn.click();
        return true;
      });
      if (!clicked) throw new Error("Import button not found");
      await sleep(900);
    },
    async after(page) {
      await page.keyboard.press("Escape");
      await sleep(300);
    },
  },
];

const browser = await puppeteer.launch({
  executablePath: exe,
  headless: true,
  protocolTimeout: 300_000,
  args: [
    "--window-size=1600,1000",
    "--hide-scrollbars",
    "--force-color-profile=srgb",
    // Real GPU rasterization: SwiftShader presents multi-pass manual-frameloop
    // WebGL (entity-POV inset) as a black main viewport in screenshots.
    "--enable-gpu",
  ],
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
  page.on("pageerror", (e) => console.warn("  [pageerror]", String(e).slice(0, 160)));
  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForFunction(() => window.__stwStore && window.__stwScene, {
    timeout: 120_000,
  });
  // Projects list must be loaded before openProject calls.
  await page.waitForFunction(
    () => window.__stwStore.getState().projects?.length > 0,
    { timeout: 60_000 },
  );

  const wanted = ONLY.length ? SHOTS.filter((s) => ONLY.includes(s.name)) : SHOTS;
  const failures = [];
  for (const shot of wanted) {
    const dest = path.join(OUT, `${shot.name}.png`);
    process.stdout.write(`shot ${shot.name} ... `);
    try {
      if (shot.project) {
        const ok = await page.evaluate(
          (pid) => window.__stwStore.getState().projects.some((p) => p.project_id === pid),
          shot.project,
        );
        if (!ok) {
          console.log(`SKIP (project ${shot.project} not found)`);
          continue;
        }
      }
      await shot.run(page);
      await dismissToasts(page);
      await page.screenshot({ path: dest, type: "png" });
      console.log(`ok → ${dest}`);
      if (shot.after) await shot.after(page);
    } catch (e) {
      failures.push(shot.name);
      console.log(`FAIL: ${String(e).slice(0, 200)}`);
    }
  }
  if (failures.length) {
    console.error(`\n${failures.length} shot(s) failed: ${failures.join(", ")}`);
    process.exitCode = 1;
  } else {
    console.log(`\nAll ${wanted.length} shots captured into ${OUT}/`);
  }
} finally {
  await browser.close();
}
