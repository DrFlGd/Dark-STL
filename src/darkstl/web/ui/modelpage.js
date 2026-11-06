// A model's own page (model:<id>[@<variant>]): the 3D view with its textures,
// and the inspector's Model (parts, options, size, file), Textures (every texture,
// and the normal maps cleaned up and upsampled for displacement, before and after),
// Pose (an animation and a frame to hold the model in), Mesh (step 1: the vertices
// evened out to a count), Displace (step 2: the vertices pushed by the textures'
// detail), Finish (post-processing for printing: the whole model made watertight, the
// surfaces inside it removed), Cut (planes placed in the 3D view split it into pieces with
// joints between them, for printing) and Export tabs. A file holding several models (a weapon and its sheath)
// gets a switch between them in the command bar. A configurable model (a weapon and the parts that
// fit it) has a Configure tab first, to choose its parts (configure.js).
import { html, useState, useEffect, useLayoutEffect, useRef, useMemo } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref, resolvedTheme } from "./state.js";
import { routeHash, placeRoute, modelOf } from "./context.js";
import { Icon } from "./icons.js";
import { collectModelItem } from "./games.js";
import { api, binUrl, info, plural, bytesText, followJob, stopJob, toast, showInFolder, pickFolder, pickFile } from "./api.js";
import { CommandBar } from "./layout.js";
import { Inspector, useWidth } from "./inspector.js";
import { Viewer, PIECE_COLORS } from "../viewer.js";
import { ConfigureTab } from "./configure.js";

const ENHANCE = { target_vertices: 1_000_000, method: "subdivide", smooth: true, source: "normal", strength: 1, height_range_mm: 1,
  green: "auto", detail_fraction: 0.0625, max_displacement_mm: 0, integration: "mesh", edge_keeping: 0, deblock: "off",
  upsample: "off", upsample_factor: 0, ai_model: "", deblock_model: "", edge_hints: false, albedo_detail_mm: 0, detail_normals: false, detail_strength: 1 };
const EXPORT = { format: ".stl", units: "mm", textures: true, folder: "" };
const VIEW = { shading: "textured", edges: false };
const JOINT = { kind: "pegs", clearance_mm: 0.2, count: 0, diameter_mm: 5, depth_mm: 6, magnet_diameter_mm: 6, magnet_height_mm: 3, flip: false, print_dowels: true };
const CUT = { from: "enhanced", sheet_mm: 1.2, wall_mm: 1.6, joint: JOINT, lay_flat: true };
const FINISH = { from: "enhanced", sheet_mm: 1.2, fill_hollows: true, close_gaps: false, close_gaps_mm: 1, remove_loose: false, loose_percent: 1 };
const JOINTS = [
  ["flat", "Flat", "The cut faces as they are, to glue together."],
  ["pegs", "Pegs", "Round pegs on one side and holes on the other, wider by the clearance."],
  ["dowels", "Dowels", "Holes on both sides and separate dowels (printed, or bought pins or filament of that diameter)."],
  ["magnets", "Magnets", "Round pockets on both sides for disc magnets, with the clearance around them."],
  ["key", "Key", "A tongue in the shape of the cut face, inset from its edge, into a groove on the other side: the pieces only fit one way."],
];
const KEEP_PLANS = 20; // models whose cuts are remembered
const KEEP_MESHES = 4; // built meshes a model page keeps (on the graphics card) to show again at once
const UNITS = [["mm", "Millimetres (game metres × 1000)"], ["cm", "Centimetres"], ["m", "Metres (the game's own units)"], ["in", "Inches"]];
/** The least room the 3D view gets beside the inspector, and the inspector's rail. */
const STAGE = 440, RAIL = 36;

const msg = (e) => String(e?.message || e);
const num = (n) => Math.round(n).toLocaleString();
const mm = (v) => (v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2));
const sizeText = (s) => (s ? `${mm(s[0])} × ${mm(s[1])} × ${mm(s[2])} mm` : "");
const variantName = (v) => (v || "").replace(/\.(flver|flv)$/i, "");
/** Where new vertices will be on average, for a mesh of `area` m² with `n` vertices: equilateral triangles, about 2 per vertex. */
const spacingMm = (area, n) => (area > 0 && n > 0 ? Math.sqrt((2 * area) / (Math.sqrt(3) * n)) * 1000 : 0);

/** The parts, options and pose as the mesh commands take them, JSON and in a URL. */
function meshArgs(m, off, flags, pose) {
  const parts = off.size ? m.parts.map((_, i) => i).filter((i) => !off.has(i)) : null;
  const p = pose ? [pose.anim, pose.frame] : null;
  return {
    json: { parts, flags, pose: p },
    url: { parts: parts ? parts.join(",") : "all", flags: Object.entries(flags).map(([k, v]) => `${k}:${v ? 1 : 0}`).join(","), pose: p ? `${p[0]}@${p[1]}` : "" },
    key: JSON.stringify([parts, flags, p]),
  };
}
/** A cut as kept: its normal made unit length and its point the model's centre moved onto the plane, so the
 *  plane drawn stays centred on the model however it is moved. `header`: the shown mesh's { min, max }. */
function centred(normal, point, header) {
  const len = Math.hypot(...normal) || 1;
  const n = normal.map((x) => x / len);
  const c = header ? header.min.map((lo, i) => (lo + header.max[i]) / 2) : point;
  const s = n.reduce((a, x, i) => a + x * (point[i] - c[i]), 0);
  return { normal: n, point: c.map((x, i) => x + n[i] * s) };
}

/** The pose as part of a file name: a000_003000_f12. */
const poseSuffix = (pose) => (pose ? `_${pose.anim}_f${pose.frame}` : "");

export function ModelPage({ route }) {
  const { id, variant } = modelOf(route);
  const [m, setM] = useState(null);
  const [error, setError] = useState("");
  // a configurable model: the part chosen in each slot, kept per model (see ConfigureTab)
  const choices = useStore(ui, (st) => (st.configChoices || {})[id] || null);
  const choicesKey = JSON.stringify(choices);
  const rev = useStore(ui, (st) => st.configRev);
  const [switching, setSwitching] = useState(false);
  useEffect(() => {
    let live = true;
    setSwitching(true);
    api("model_open", { id, variant, choices }).then((v) => { if (live) { setM(v); setError(""); setSwitching(false); } },
      (e) => { if (live) { setError(msg(e)); setSwitching(false); } });
    return () => { live = false; };
  }, [id, variant, choicesKey, rev]);
  // a configurable model opens on its Configure tab
  useEffect(() => { if (m?.configuration && m.id === id) ui.set({ inspectorAsk: { page: "model", tab: "configure" } }); }, [m?.id, !!m?.configuration]);
  const entry = m?.entry;
  const placeKey = entry ? JSON.stringify([entry.game, entry.category, entry.group]) : null;
  useEffect(() => {
    ui.set({ modelPlace: entry ? { game: entry.game, category: entry.category, group: entry.group } : null });
    return () => ui.set({ modelPlace: null });
  }, [placeKey]);
  const back = () => (history.length > 1 ? history.back() : (location.hash = entry ? routeHash(placeRoute(entry.game, entry.category)) : "#/"));
  const bar = { id: "mp-head", back: { id: "mp-back", run: back } };
  if (error) return html`<${CommandBar} ...${bar} title="Model" /><div class="pages"><p class="warn-note" role="alert" id="model-error">${error}</p></div>`;
  if (!m) return html`<${CommandBar} ...${bar} title="" /><div class="pages"><p class="muted" id="model-opening">Opening the model…</p></div>`;
  const up = entry ? [[entry.game, routeHash(placeRoute(entry.game))], [entry.category, routeHash(placeRoute(entry.game, entry.category))],
    ...(entry.group ? [[entry.group, routeHash(placeRoute(entry.game, entry.category, entry.group))]] : [])] : [["Opened file", null]];
  const fileName = m.file.split(/[\\/]/).pop();
  const pick = (v) => { location.hash = routeHash(`model:${m.id}@${encodeURIComponent(v)}`); };
  const more = [
    { id: "show", label: "Show in folder", icon: "folder", run: () => showInFolder(m.file) },
    { id: "copy-path", label: "Copy file path", icon: "copy", run: () => navigator.clipboard?.writeText(m.file).then(() => toast("Copied the file's path")) },
    { sep: true },
    collectModelItem({ id: m.id, file: m.file, label: entry?.label || m.name, category: entry?.category, group: entry?.group }, entry?.game),
    { sep: true },
    { id: "open-file", label: "Open another model file…", icon: "file", run: () => ui.set({ dialog: { type: "open-file" } }) },
    ...(entry && !m.configuration ? [{ sep: true }, { id: "make-configurable", label: "Make configurable…", icon: "box",
      run: () => makeConfigurable(m, entry) }] : []),
  ];
  return html`<${CommandBar} ...${bar} up=${up.filter(([, href]) => href)} title=${html`<h1 id="mp-name" title=${entry?.label || m.name}>${entry?.label || m.name}</h1>`}
      count=${html`<span class="page-count" id="mp-count" title=${m.file}>${fileName}</span>`}
      tools=${m.variants.length > 1 ? html`<label class="variant-pick" title="This file holds several models, such as a weapon and its sheath">
        <span class="visually-hidden">Model in this file</span>
        <select id="variant-select" class="compact" value=${m.variant} onChange=${(e) => pick(e.target.value)}>
          ${m.variants.map((v) => html`<option key=${v} value=${v}>${variantName(v)}</option>`)}</select></label>` : null}
      more=${{ id: "mp-more", items: more }} />
    <div class="model-page" id="model-page" data-model=${m.id}>
      <${Workspace} key=${m.sid} m=${m} variant=${variant} switching=${switching} />
    </div>`;
}

/** A configuration of the model's own: the model as its base, no slots yet; it opens with the editor. */
async function makeConfigurable(m, entry) {
  try {
    const r = await api("config_save", { base_id: entry.id, configuration: { label: entry.label || m.name, slots: [] } });
    ui.set({ configEdit: r.id });
    location.hash = routeHash(`model:${r.id}`);
  } catch (e) { toast(msg(e), { error: true }); }
}

function Workspace({ m, variant, switching }) {
  const [off, setOff] = useState(() => new Set());
  const [flags, setFlags] = useState(() => Object.fromEntries(m.options.map((o) => [o.key, o.default])));
  const [stats, setStats] = useState(m.stats);
  const [result, setResult] = useState(m.enhanced ? { ...m.enhanced, rev: 1 } : null);
  const [resultKey, setResultKey] = useState(null);
  const [which, setWhich] = useState(m.cut?.pieces?.length ? "pieces" : m.finished ? "finished" : m.enhanced ? "enhanced" : "original");
  const [pose, setPose] = useState(null); // { anim, frame } or null for the bind pose
  const [busy, setBusy] = useState(false); // the 3D view is fetching a mesh
  const [over, setOver] = useState({}); // material -> the detail texture chosen on the Displace tab ("" = none)
  const [strengths, setStrengthState] = useState({}); // material -> strength multiplier
  const strengthsNow = useRef({}); // the same, readable at once by a click that follows the change
  const setStrengths = (v) => { strengthsNow.current = v; setStrengthState(v); };
  const [run, setRun] = useState(null); // this page's job: { kind: "mesh" | "displace" | "upscale" | "finish" | "cut", progress, message, job, stopping }
  const [finished, setFinished] = useState(m.finished || null); // the model made watertight last (Finish tab)
  // the Cut tab: its planes (kept per model), the one selected, whether the tab is open (the planes show in the
  // 3D view), the pieces last cut and how far apart they're shown
  const [cuts, setCutList] = useState(() => (ui.get().cutPlans || {})[m.id] || []);
  const setCuts = (list) => {
    setCutList(list);
    const all = { ...(ui.get().cutPlans || {}) };
    delete all[m.id];
    if (list.length) all[m.id] = list;
    const keys = Object.keys(all);
    for (const k of keys.slice(0, Math.max(0, keys.length - KEEP_PLANS))) delete all[k];
    setPref({ cutPlans: all });
  };
  const [cutSel, setCutSel] = useState(() => cuts[0]?.id ?? null);
  const [cutOpen, setCutOpen] = useState(false);
  const [pieces, setPieces] = useState(m.cut?.pieces?.length ? m.cut : null);
  const [explode, setExplode] = useState(0.25);
  const [bounds, setBounds] = useState(null); // { min, max } in millimetres of the model shown
  const viewer = useRef(null);
  const saved = useStore(ui, (st) => st.enhance);
  const s = { ...ENHANCE, ...(saved || {}) };
  const set = (patch) => setPref({ enhance: { ...s, ...patch } });
  const args = meshArgs(m, off, flags, pose);
  // the engine keeps a few models open; if this one was let go, open it again and retry
  const call = async (cmd, a) => {
    try { return await api(cmd, a); } catch (e) {
      if (!/was closed/.test(msg(e))) throw e;
      await api("model_open", { id: m.id, variant });
      return api(cmd, a);
    }
  };
  useEffect(() => {
    if (args.key === meshArgs(m, new Set(), Object.fromEntries(m.options.map((o) => [o.key, o.default])), null).key) { setStats(m.stats); return; }
    let live = true;
    call("model_stats", { sid: m.sid, ...args.json }).then((s) => { if (live) setStats(s); }, () => {});
    return () => { live = false; };
  }, [args.key]);
  const box = useRef(null);
  const room = useWidth(box);
  const paneW = Math.max(260, Math.min(520, ui.get().inspectorWidth || 360));
  const crowded = !!room && room - paneW - RAIL < STAGE;
  const enhanced = (r) => { setResult({ ...r, rev: (result?.rev || 0) + 1 }); setResultKey(args.key); setWhich("enhanced"); };
  const detailOf = (mat, i) => (i in over ? over[i] : (s.source === "height" ? mat.height : mat.normal) || "");
  // the settings as stored now, as the engine takes them: a number typed and then a button clicked
  // straight away is saved (on blur) just before the click, before the tab has drawn it again
  const sendSettings = () => {
    const now = { ...ENHANCE, ...(ui.get().enhance || {}) };
    return { ...now, max_displacement_mm: now.max_displacement_mm || null,
      texture_overrides: Object.fromEntries(m.materials.map((mat, i) => [i, detailOf(mat, i)])), material_strength: strengthsNow.current };
  };
  const name = m.entry?.label || variantName(m.variant) || m.name;
  const follow = async (kind, start, label) => {
    setRun({ kind, progress: 0, message: "Starting" });
    try {
      const { job } = await start();
      setRun({ kind, progress: 0, message: "Starting", job });
      return await followJob(job, label, (j) => setRun({ kind, progress: j.progress, message: j.message, job, stopping: j.stopping }));
    } finally { setRun(null); }
  };
  /** Step 1 alone ("mesh"), or step 2 after it ("both"); the result is shown as the enhanced model. */
  const generate = async (step) => {
    const r = await follow(step === "mesh" ? "mesh" : "displace", () => call("enhance", { sid: m.sid, ...args.json, settings: sendSettings(), step }),
      `${step === "mesh" ? "Evening out" : "Displacing"} ${name}`);
    enhanced(r);
    return r;
  };
  /** The Textures tab's step: the normal maps cleaned up and upsampled as the texture settings ask. */
  const upscale = () => follow("upscale", () => call("texture_upscale", { sid: m.sid, ...args.json, settings: sendSettings() }), `Upscaling ${name}'s textures`);
  const shared = { m, args, stats, s, set, run, setRun, call, sendSettings };
  /** Cut the model along the Cut tab's planes; the pieces are shown at once. */
  const cutModel = async (request) => {
    const r = await follow("cut", () => call("cut", { sid: m.sid, ...args.json, ...request }), `Cutting ${name}`);
    setPieces({ ...r, request: JSON.stringify(request) });
    setWhich("pieces");
    return r;
  };
  const cutting = { cuts, setCuts, cutSel, setCutSel, pieces, explode, setExplode, bounds, viewer };
  /** Make the model watertight (Finish tab); the result is shown at once. */
  const finishModel = async (request) => {
    const r = await follow("finish", () => call("finish", { sid: m.sid, ...args.json, ...request }), `Making ${name} watertight`);
    setFinished({ ...r, request: JSON.stringify(request) });
    setWhich("finished");
    return r;
  };
  return html`<div class="model-workspace" ref=${box}>
    <section class="workspace-viewing-area">
      <${Stage} m=${m} variant=${variant} which=${which} setWhich=${setWhich} args=${args} result=${result} finished=${finished} stats=${stats} pose=${pose} onBusy=${setBusy}
        cutting=${cutting} showCuts=${cutOpen} onBounds=${setBounds} />
    </section>
    <${Inspector} page="model" crowded=${crowded} label="Panels" tabs=${[
      ...(m.configuration ? [{ id: "configure", label: "Configure", icon: "layers", body: html`<${ConfigureTab} m=${m} switching=${switching} />` }] : []),
      { id: "model", label: "Model", icon: "box", body: html`<${ModelTab} m=${m} off=${off} setOff=${setOff} flags=${flags} setFlags=${setFlags} stats=${stats} />` },
      { id: "textures", label: "Textures", icon: "image", body: html`<${TexturesTab} ...${shared} over=${over} upscale=${upscale} />` },
      ...(m.animated ? [{ id: "pose", label: "Pose", icon: "play", body: html`<${PoseTab} m=${m} pose=${pose} setPose=${setPose} busy=${busy} call=${call} />` }] : []),
      { id: "mesh", label: "Mesh", icon: "grid", body: html`<${MeshTab} ...${shared} result=${result} generate=${generate} />` },
      { id: "displace", label: "Displace", icon: "stack", body: html`<${DisplaceTab} ...${shared} result=${result} stale=${!!result && !!resultKey && resultKey !== args.key}
        over=${over} setOver=${setOver} strengths=${strengths} setStrengths=${setStrengths} generate=${generate} />` },
      { id: "finish", label: "Finish", icon: "drop", body: html`<${FinishTab} ...${shared} result=${result} finished=${finished} which=${which} setWhich=${setWhich} finishModel=${finishModel} />` },
      { id: "cut", label: "Cut", icon: "scissors", body: html`<${CutTab} ...${shared} ...${cutting} result=${result} finished=${finished} which=${which} setWhich=${setWhich} setOpen=${setCutOpen} cutModel=${cutModel} />` },
      { id: "export", label: "Export", icon: "download", body: html`<${ExportTab} m=${m} args=${args} result=${result} finished=${finished} which=${which} pose=${pose} call=${call} pieces=${pieces} />` },
    ]} />
  </div>`;
}

/** The 3D view: the model with its textures, clay or wireframe, its size, and the views. */
function Stage({ m, variant, which, setWhich, args, result, finished, stats, pose, onBusy, cutting, showCuts, onBounds }) {
  const canvas = useRef(null);
  const viewer = cutting.viewer;
  // the meshes shown lately, built, the newest last: showing the original and the enhanced model in
  // turn (or parts switched off and on again) doesn't fetch or build them again
  const kept = useRef(new Map());
  const shown = useRef(false);
  const view = { ...VIEW, ...(useStore(ui, (s) => s.view) || {}) };
  const [state, setState] = useState({ loading: true, error: null, size: null });
  useEffect(() => {
    const v = new Viewer(canvas.current);
    viewer.current = v;
    window.__viewer = v; // for tests
    const theme = () => v.setTheme(resolvedTheme() !== "light");
    theme();
    addEventListener("ds-theme", theme);
    return () => {
      removeEventListener("ds-theme", theme); v.dispose(); viewer.current = null;
      for (const b of kept.current.values()) b.dispose();
      kept.current.clear();
    };
  }, []);
  // each material's colour texture, for the textured view
  const textures = useMemo(() => {
    const have = new Set(m.textures.map((t) => t.key));
    const out = {};
    m.materials.forEach((mat, i) => { if (mat.albedo && have.has(mat.albedo)) out[i] = binUrl("texture", { sid: m.sid, key: mat.albedo, max: 2048 }); });
    return out;
  }, [m.sid]);
  const key = JSON.stringify([which, which === "enhanced" ? result?.id || result?.rev : which === "pieces" ? cutting.pieces?.id : which === "finished" ? finished?.id : args.key]);
  useEffect(() => {
    let live = true;
    (async () => {
      let built = kept.current.get(key);
      kept.current.delete(key);
      if (!built) {
        setState((s) => ({ ...s, loading: true, error: null }));
        onBusy?.(true);
        const url = () => binUrl("model_mesh", { sid: m.sid, which, uv: 1, ...args.url });
        let res = await fetch(url());
        if (res.status === 400) {
          const text = await res.text();
          if (!/was closed/.test(text)) throw new Error(text);
          await api("model_open", { id: m.id, variant });
          if (which !== "original") { setWhich("original"); return; }
          res = await fetch(url());
        }
        if (!res.ok) throw new Error(await res.text());
        const buf = await res.arrayBuffer();
        if (!viewer.current) return;
        built = viewer.current.build(buf);
      }
      kept.current.set(key, built);
      for (const [k, b] of kept.current) {
        if (kept.current.size <= KEEP_MESHES) break;
        if (b === viewer.current?.shown) continue;
        kept.current.delete(k);
        b.dispose();
      }
      if (!live) return;
      const size = viewer.current.show(built, { textures: view.shading === "textured" && which !== "pieces" && which !== "finished" ? textures : null, wireframe: view.shading === "wire", keepView: shown.current });
      viewer.current.setEdges(view.edges);
      shown.current = true;
      if (which === "pieces") viewer.current.setExplode(cutting.explode);
      onBounds?.({ min: built.header.min, max: built.header.max, which });
      setState({ loading: false, error: null, size, key });
      onBusy?.(false);
    })().catch((e) => { if (live) { setState({ loading: false, error: msg(e), size: null, key }); onBusy?.(false); } });
    return () => { live = false; };
  }, [key, view.shading]);
  // the Cut tab's planes, shown while it is open (not over the pieces they made)
  const { cuts, cutSel, setCuts, setCutSel, explode } = cutting;
  const live = useRef(null);
  live.current = { cuts, setCuts, setCutSel, header: viewer.current?.shown?.header };
  useEffect(() => {
    const v = viewer.current;
    if (!v) return;
    v.onCutSelect = (id) => live.current.setCutSel(id);
    v.onCut = (id, now, done) => {
      if (!done) return;
      const { cuts, setCuts, header } = live.current;
      setCuts(cuts.map((c) => (c.id === id ? { ...c, ...centred(now.normal, now.point, header) } : c)));
    };
    v.setCuts(showCuts && which !== "pieces" ? cuts.map((c) => ({ ...c })) : [], cutSel);
  }, [cuts, cutSel, showCuts, which, state.key]);
  useEffect(() => { if (which === "pieces") viewer.current?.setExplode(explode); }, [explode]);
  const setView = (patch) => setPref({ view: { ...view, ...patch } });
  const edges = (on) => { setView({ edges: on }); viewer.current?.setEdges(on); };
  const seg = (id, label, options, value, onChange) => html`<div class="seg" role="group" aria-label=${label} id=${id}>${options.map(([k, l, title, disabled]) =>
    html`<button type="button" key=${k} data-value=${k} aria-pressed=${value === k ? "true" : "false"} title=${title || l} disabled=${!!disabled} onClick=${() => onChange(k)}>${l}</button>`)}</div>`;
  const size = state.size;
  const noTextures = !Object.keys(textures).length;
  return html`<section class="mp-stage workspace-stage" id="workspace-stage" aria-label="3D view">
    <div class="stage-3d">
      <div class="stage-canvas" id="stage-canvas" data-loading=${state.loading || state.key !== key ? "1" : null}>
        <canvas ref=${canvas} tabindex="0" aria-label="The model in 3D: drag to turn it, right-drag to move, wheel to zoom"></canvas>
        ${state.loading && !shown.current ? html`<div class="stage-note muted">Loading the model…</div>` : null}
        ${state.error ? html`<div class="stage-note"><p class="warn-note" role="alert" id="stage-error">${state.error}</p></div>` : null}
        ${!state.loading && !state.error && size && !size.triangles ? html`<div class="stage-note muted">No parts are switched on (Model tab).</div>` : null}
      </div>
      <div class="stage-bar">
        <span class="stage-what" id="stage-size" title="Width × depth × height, as exported in millimetres">${size?.triangles ? html`${sizeText([size.x, size.y, size.z])}<span class="muted"> · ${plural(size.triangles, "triangle")}</span>` : sizeText(stats?.size_mm)}${pose ? html`<span class="muted stage-pose" id="stage-pose" title="The pose shown (Pose tab)"> · ${pose.anim} frame ${pose.frame}</span>` : null}</span>
        <span class="stage-tools">
          ${seg("show-which", "Show", [["original", "Original"], ["enhanced", "Enhanced", result ? "The enhanced model" : "Make it first (Mesh or Displace tab)", !result],
            ["finished", "Finished", finished ? "The model made watertight, without its textures" : "Make it watertight first (Finish tab)", !finished],
            ["pieces", "Pieces", cutting.pieces ? "The model cut into pieces" : "Cut it first (Cut tab)", !cutting.pieces]], which, setWhich)}
          ${seg("shading", "Shading", [["textured", "Textured", noTextures ? "No colour textures were found for this model" : "With the game's colour textures"], ["clay", "Clay"], ["wire", "Wireframe"]], view.shading, (v) => setView({ shading: v }))}
          <button type="button" class="ghost" id="edges-toggle" aria-pressed=${view.edges ? "true" : "false"} title="Show the triangles' sharp edges" onClick=${() => edges(!view.edges)}>Edges</button>
          ${seg("views", "View", [["iso", "3/4", "Three-quarter view"], ["top", "Top"], ["front", "Front"]], null, (v) => viewer.current?.view(v))}
        </span>
      </div>
    </div>
  </section>`;
}

/** Model: its parts (each can be left out), the importer's options, its size and its file. */
function ModelTab({ m, off, setOff, flags, setFlags, stats }) {
  const toggle = (i) => { const next = new Set(off); next.has(i) ? next.delete(i) : next.add(i); setOff(next); };
  const folder = m.file.slice(0, m.file.length - m.file.split(/[\\/]/).pop().length - 1);
  return html`<div class="tab-pad" id="model-tab">
    <section class="insp-section" id="parts"><h3>Parts</h3>
      <div class="part-list">${m.parts.map((p, i) => html`<label class="check-row part-row" key=${i} data-part=${i}>
        <input type="checkbox" checked=${!off.has(i)} onChange=${() => toggle(i)} />
        <span class="part-name">${p.name}<small class="muted">${m.materials[p.material]?.name || "?"} · ${plural(p.triangles, "triangle")}</small></span></label>`)}</div>
      ${m.parts.length > 2 ? html`<div class="insp-actions"><button type="button" class="ghost" disabled=${!off.size} onClick=${() => setOff(new Set())}>All</button>
        <button type="button" class="ghost" onClick=${() => setOff(new Set(m.parts.map((_, i) => i)))}>None</button></div>` : null}
    </section>
    ${m.options.length ? html`<section class="insp-section" id="options"><h3>Options</h3>${m.options.map((o) => html`<label class="check-row" key=${o.key} title=${o.tooltip}>
      <input type="checkbox" data-option=${o.key} checked=${!!flags[o.key]} onChange=${(e) => setFlags({ ...flags, [o.key]: e.target.checked })} />
      <span>${o.label}${o.tooltip ? html`<small class="muted">${o.tooltip}</small>` : null}</span></label>`)}</section>` : null}
    <section class="insp-section" id="model-size"><h3>Size</h3><dl class="insp-dl">
      <dt>Size</dt><dd id="model-dims">${sizeText(stats.size_mm)}</dd>
      <dt>Vertices</dt><dd>${num(stats.vertices)}</dd>
      <dt>Triangles</dt><dd>${num(stats.triangles)}</dd>
      <dt>Surface</dt><dd>${num(stats.area_m2 * 10000)} cm²</dd></dl>
      <small class="muted">In millimetres, one game metre to 1000 mm: the size the model exports at.</small></section>
    ${m.notes.length ? html`<section class="insp-section" id="model-notes"><h3>Notes</h3>${m.notes.map((n, i) => html`<p class="insp-note" key=${i}>${n}</p>`)}</section>` : null}
    <section class="insp-section" id="model-file"><h3>File</h3><dl class="insp-dl">
      <dt>File</dt><dd>${m.file.split(/[\\/]/).pop()}</dd>
      ${m.variants.length > 1 ? html`<dt>Model</dt><dd>${variantName(m.variant)}</dd>` : null}
      <dt>Folder</dt><dd class="note-path">${folder}</dd>
      ${m.game ? html`<dt>Game</dt><dd>${m.game}</dd>` : null}</dl>
      <div class="insp-actions"><button type="button" class="ghost" onClick=${() => showInFolder(m.file)}>${Icon.folder(14)} Show in folder</button></div>
      ${m.info.length ? html`<details class="file-info"><summary>What's in the file</summary><pre>${m.info.join("\n")}</pre></details>` : null}
    </section>
  </div>`;
}

/** Textures: every texture found for the model, a bigger look at one with Save as PNG, the normal maps
 *  cleaned up and upsampled for displacement (with before and after to zoom into), and which each material uses. */
function TexturesTab({ m, args, s, set, run, setRun, call, sendSettings, over, upscale }) {
  const [sel, setSel] = useState(m.textures[0]?.key || null);
  const [about, setAbout] = useState(null);
  const tex = m.textures.find((t) => t.key === sel);
  useEffect(() => {
    if (!sel) return;
    let live = true;
    setAbout(null);
    api("texture_info", { sid: m.sid, key: sel }).then((r) => { if (live) setAbout(r.info); }, (e) => { if (live) setAbout(msg(e)); });
    return () => { live = false; };
  }, [sel]);
  const save = async () => {
    try {
      const { path } = await api("texture_save", { sid: m.sid, key: sel });
      toast(`Saved ${path.split(/[\\/]/).pop()}`, { ms: 6000, action: { label: "Show in folder", run: () => showInFolder(path) } });
    } catch (e) { toast(msg(e)); }
  };
  const nameOf = (k) => m.textures.find((t) => t.key === k)?.name;
  const [error, setError] = useState(null);
  const [ai, setAi] = useState(null); // { folder, models, included, available } once asked for
  const wantsAi = s.upsample === "ai" || s.deblock === "ai";
  const loadAi = () => api("ai_models").then(setAi, (e) => setError(msg(e)));
  useEffect(() => { if (wantsAi && !ai) loadAi(); }, [wantsAi]);
  const addModel = async () => {
    const path = await pickFile("Add an AI model", ["ONNX models (*.onnx)"]);
    if (path) { try { setAi(await api("ai_model_add", { path })); } catch (e) { setError(msg(e)); } }
  };
  const included = (key) => key === "ai_model" && ai?.bundled?.length;
  const modelSelect = (id, key, title) => html`<label class="field-block" title=${title}><span>${key === "ai_model" ? "Upscaling model" : "Deblocking model"}</span>
    <select id=${id} value=${s[key]} onChange=${(e) => set({ [key]: e.target.value })}>
      <option value="">${included(key) ? "Included (matched to each texture's compression)" : "Choose…"}</option>
      ${(ai?.models || []).map((x) => html`<option key=${x} value=${x}>${x}</option>`)}</select></label>`;
  const maps = useTextureMaps(call, m, args, s, over, run, sendSettings);
  const list = maps?.maps || [];
  const [compare, setCompare] = useState(null); // the map compared, when chosen by hand
  const shownMap = list.find((x) => x.key === compare) || list.find((x) => x.key === sel) || list[0];
  const go = async () => {
    setError(null);
    try {
      const r = await upscale();
      toast(`${plural(r.maps.length, "normal map")} ready for Displace`);
    } catch (e) { if (e.stopped) toast("Stopped upscaling"); else setError(msg(e)); }
  };
  if (!m.textures.length) {
    return html`<div class="tab-pad" id="textures-tab"><p>No textures were found for this model.</p>
      <p class="muted">Map pieces and some objects use textures kept elsewhere in the game. Add the folders that hold them under
        <a href="#/settings">Settings › Texture folders</a>.</p></div>`;
  }
  return html`<div class="tab-pad" id="textures-tab">
    <div class="tex-grid" role="listbox" aria-label="Textures">${m.textures.map((t) => html`<button type="button" key=${t.key} class="tex-tile" role="option"
        aria-selected=${t.key === sel ? "true" : "false"} data-texture=${t.key} title=${t.source} onClick=${() => setSel(t.key)}>
      <img src=${binUrl("texture", { sid: m.sid, key: t.key, max: 192 })} alt="" loading="lazy" /><span>${t.name}</span></button>`)}</div>
    ${tex ? html`<section class="insp-section" id="texture-view"><h3>${tex.name}</h3>
      <a class="tex-big" href=${binUrl("texture", { sid: m.sid, key: tex.key, max: 4096 })} target="_blank" rel="noopener" title="Open at full size">
        <img src=${binUrl("texture", { sid: m.sid, key: tex.key, max: 1024 })} alt=${tex.name} /></a>
      <dl class="insp-dl"><dt>Image</dt><dd id="texture-info">${about || "…"}</dd><dt>From</dt><dd class="note-path">${tex.source}</dd></dl>
      <div class="insp-actions"><button type="button" class="ghost" id="texture-save" onClick=${save}>${Icon.download(14)} Save as PNG</button></div></section>` : null}
    <section class="insp-section" id="texture-upscale"><h3>Upscale for displacement</h3>
      <p class="muted">Displace reads the normal maps below. Clean them up and upsample them here to see what the settings do, texel by texel, and change
        them until the detail looks right; Displace then uses the maps as made here (and makes any it finds missing).</p>
      <label class="field-block" title="Block compression leaves a faint 4-pixel grid. Smoothing melts it but also softens the finest detail; an AI model can remove it more cleanly."><span>Compression clean-up</span>
        <select id="deblock" value=${s.deblock} onChange=${(e) => set({ deblock: e.target.value })}>
          <option value="off">None</option><option value="smooth">Smooth it out</option><option value="ai">AI model</option></select></label>
      <label class="field-block" title="More pixels in the normal map, so vertices closer together than its pixels still get detail of their own. Guided keeps edges where the colour texture has them; AI models invent plausible fine detail."><span>Upsample</span>
        <select id="upsample" value=${s.upsample} onChange=${(e) => set({ upsample: e.target.value })}>
          <option value="off">No</option><option value="guided">Guided by the colour texture</option><option value="ai">AI model</option></select></label>
      ${s.upsample !== "off" ? html`<label class="field-block" title="How many times bigger the map gets. Automatic goes by the Mesh tab's vertex count: no more than the vertex spacing can use."><span>Factor</span>
        <select id="upsample-factor" value=${String(s.upsample_factor || 0)} onChange=${(e) => set({ upsample_factor: Number(e.target.value) })}>
          <option value="0">Automatic (for ${num(s.target_vertices)} vertices)</option><option value="2">2×</option><option value="4">4×</option></select></label>` : null}
      ${wantsAi ? html`<div class="field-block" id="ai-models">
        ${ai && !ai.available ? html`<p class="insp-note">AI models need ONNX Runtime, which this copy of the app doesn't have.</p>` : null}
        ${s.upsample === "ai" ? modelSelect("ai-model", "ai_model", "A 2x or 4x model for normal maps in .onnx form. The app includes RunDevelopment's 4x-Normal-RG0 models (CC BY 4.0), one for each kind of block compression.") : null}
        ${s.deblock === "ai" ? modelSelect("deblock-model", "deblock_model", "A 1x model that removes DDS block artifacts, in .onnx form.") : null}
        <div class="insp-actions"><button type="button" class="ghost" id="ai-model-add" onClick=${addModel}>Add a model…</button>
          ${ai ? html`<button type="button" class="ghost" id="ai-models-folder" onClick=${() => showInFolder(ai.folder)}>Open models folder</button>` : null}</div>
        <small class="muted">${s.upsample === "ai" && ai?.bundled?.length ? "Included: 4x-Normal-RG0 by RunDevelopment (CC BY 4.0). " : ""}Without a graphics card it is slow (about half a minute per 512 px texture, several minutes at 2048 px). Check the licence of models you add before sharing prints made with them.</small></div>` : null}
      ${s.source !== "normal" ? html`<p class="insp-note">Displace is set to read ${s.source === "height" ? "height maps" : "nothing"} (Displace › From), so normal maps aren't used.</p>` : null}
      <div class="insp-actions"><button type="button" class="primary compact" id="upscale-run" disabled=${!!run || !list.length} onClick=${go}>${run?.kind === "upscale" ? "Upscaling…" : `Upscale ${plural(list.length, "map")}`}</button></div>
      <${JobProgress} run=${run?.kind === "upscale" ? run : null} setRun=${setRun} id="upscale-progress" />
      ${error ? html`<p class="warn-note" role="alert" id="upscale-error">${error}</p>` : null}
      ${list.length ? html`<ul class="ls-list upscale-maps" id="upscale-maps">${list.map((x) => html`<li key=${x.key} data-map=${x.key} data-ready=${x.ready ? "1" : "0"}>
        <span>${x.name}<small class="muted">${x.materials.join(", ")} · ${x.size[0]} × ${x.size[1]} px · ${x.texel_mm.toFixed(2)} mm per texel</small>
          ${x.ready && x.steps.length ? html`<small class="muted">${x.steps.join(" · ")}</small>` : null}
          ${x.warnings.map((w, i) => html`<small class="warn-text" key=${i}>${w}</small>`)}</span>
        <span class=${x.ready ? "" : "muted"}>${x.ready ? `${x.factor > 1 ? `${x.factor}× · ` : ""}${x.up_size[0]} × ${x.up_size[1]}` : "not made yet"}</span></li>`)}</ul>`
        : maps ? html`<p class="muted" id="upscale-none">${s.source === "normal" ? "No normal map is read for the parts shown." : ""}</p>` : null}
      ${shownMap ? html`${list.length > 1 ? html`<label class="field-block"><span>Compare</span><select id="compare-map" value=${shownMap.key} onChange=${(e) => setCompare(e.target.value)}>
          ${list.map((x) => html`<option key=${x.key} value=${x.key}>${x.name}</option>`)}</select></label>` : null}
        <${TextureCompare} m=${m} args=${args} map=${shownMap} sendSettings=${sendSettings} />` : null}
    </section>
    <section class="insp-section" id="materials"><h3>Materials</h3>${m.materials.map((mat, i) => html`<div class="material" key=${i} title=${mat.details.join("\n")}>
      <p class="material-name">${mat.name}${mat.guessed ? html` <small class="muted" title="Matched by the textures' names: the material didn't say">(guessed)</small>` : null}</p>
      <dl class="insp-dl">
        <dt>Colour</dt><dd>${nameOf(mat.albedo) || html`<span class="muted">none</span>`}</dd>
        <dt>Normals</dt><dd>${nameOf(mat.normal) || html`<span class="muted">none</span>`}</dd>
        ${mat.height ? html`<dt>Height</dt><dd>${nameOf(mat.height)}</dd>` : null}</dl></div>`)}</section>
  </div>`;
}

/** The normal maps Displace reads with the settings as they are, and whether each is made ready: { maps, spacing_mm }. */
function useTextureMaps(call, m, args, s, over, run, sendSettings) {
  const [maps, setMaps] = useState(null);
  useEffect(() => {
    let live = true;
    call("texture_maps", { sid: m.sid, ...args.json, settings: sendSettings() }).then((r) => live && setMaps(r), () => live && setMaps(null));
    return () => { live = false; };
  }, [args.key, s.target_vertices, s.source, s.green, s.deblock, s.deblock_model, s.upsample, s.ai_model, s.upsample_factor, JSON.stringify(over), !!run]);
  return maps;
}

/** A normal map before and after the texture settings, one over the other with a line to drag between them;
 *  dragged to pan and zoomed with the wheel or the buttons. The engine draws each view at the pixels shown
 *  (texture_crop), so texels stay crisp however far in. */
function TextureCompare({ m, args, map, sendSettings }) {
  const box = useRef(null);
  const width = useWidth(box);
  const H = 260;
  const [tw, th] = map.size;
  const fit = width ? Math.min(width / tw, H / th) : 0;
  const [view, setView] = useState(null); // { key, cx, cy (fractions of the map), scale (px per texel), split, mode }
  useEffect(() => { if (fit && (!view || view.key !== map.key)) setView({ key: map.key, cx: 0.5, cy: 0.5, scale: fit, split: 0.5, mode: "split" }); }, [fit, map.key]);
  const [src, setSrc] = useState(null); // { before, after, view } drawn for a view
  const settingsJson = JSON.stringify(sendSettings());
  useEffect(() => {
    if (!view || !width || !map.ready) { setSrc(null); return undefined; }
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const half = { x: width / 2 / (view.scale * tw), y: H / 2 / (view.scale * th) };
    const rect = { x0: view.cx - half.x, x1: view.cx + half.x, y0: view.cy - half.y, y1: view.cy + half.y };
    const url = (which) => binUrl("texture_crop", { sid: m.sid, ...args.url, key: map.key, which, ...rect, w: Math.round(width * dpr), h: Math.round(H * dpr),
      settings: settingsJson, v: map.factor });
    let live = true;
    const t = setTimeout(() => {
      const load = (which) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i.src); i.onerror = () => ok(null); i.src = url(which); });
      Promise.all([load("before"), load("after")]).then(([before, after]) => { if (live) setSrc({ before, after, view }); });
    }, 120);
    return () => { live = false; clearTimeout(t); };
  }, [view, width, map.key, map.ready, settingsJson]);
  const zoomBy = (f, at) => setView((v) => {
    if (!v) return v;
    const scale = Math.max(fit / 2, Math.min(64, v.scale * f));
    let { cx, cy } = v;
    if (at) { // the texel under the pointer stays where it is
      const tx = v.cx + (at.x - width / 2) / (v.scale * tw), ty = v.cy + (at.y - H / 2) / (v.scale * th);
      cx = tx - (at.x - width / 2) / (scale * tw); cy = ty - (at.y - H / 2) / (scale * th);
    }
    return { ...v, scale, cx, cy };
  });
  const at = (e) => { const r = box.current.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const onWheel = (e) => { e.preventDefault(); zoomBy(e.deltaY < 0 ? 1.25 : 0.8, at(e)); };
  const drag = useRef(null);
  const onDown = (e) => {
    if (e.button !== 0 || !view) return;
    const p = at(e);
    drag.current = { x: e.clientX, y: e.clientY, handle: view.mode === "split" && Math.abs(p.x - view.split * width) < 10 };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onMove = (e) => {
    const d = drag.current;
    if (!d) return;
    if (d.handle) { const p = at(e); setView((v) => ({ ...v, split: Math.max(0, Math.min(1, p.x / width)) })); return; }
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    drag.current = { ...d, x: e.clientX, y: e.clientY };
    setView((v) => ({ ...v, cx: v.cx - dx / (v.scale * tw), cy: v.cy - dy / (v.scale * th) }));
  };
  const onUp = () => { drag.current = null; };
  // until the engine has drawn the view asked for, the last drawing is moved and scaled to where it belongs
  const tf = src && view ? `translate(${((src.view.cx - view.cx) * view.scale * tw).toFixed(1)}px, ${((src.view.cy - view.cy) * view.scale * th).toFixed(1)}px) scale(${(view.scale / src.view.scale).toFixed(4)})` : "none";
  const mode = view?.mode || "split";
  const scaleText = !view ? "" : view.scale >= 1 ? `${view.scale >= 10 ? Math.round(view.scale) : view.scale.toFixed(1)} px per texel` : `1 px per ${(1 / view.scale).toFixed(1)} texels`;
  return html`<div class="compare" id="compare" data-map=${map.key}>
    <div class="compare-bar">
      <div class="seg" role="group" aria-label="Show" id="compare-mode">${[["split", "Split"], ["before", "Before"], ["after", "After"]].map(([v, l]) => html`<button type="button" key=${v} data-value=${v}
        aria-pressed=${mode === v ? "true" : "false"} onClick=${() => setView((x) => ({ ...x, mode: v }))}>${l}</button>`)}</div>
      <span class="compare-zoom"><button type="button" class="ghost compact" id="compare-zoom-out" title="Zoom out" aria-label="Zoom out" onClick=${() => zoomBy(0.5)}>−</button>
        <span class="muted" id="compare-scale">${scaleText}</span>
        <button type="button" class="ghost compact" id="compare-zoom-in" title="Zoom in" aria-label="Zoom in" onClick=${() => zoomBy(2)}>+</button>
        <button type="button" class="ghost compact" id="compare-fit" onClick=${() => setView((v) => ({ ...v, cx: 0.5, cy: 0.5, scale: fit }))}>Fit</button></span></div>
    <div class="compare-box" ref=${box} style=${{ height: `${H}px` }} data-mode=${mode} onWheel=${onWheel} onPointerDown=${onDown} onPointerMove=${onMove} onPointerUp=${onUp} onPointerCancel=${onUp}>
      ${!map.ready ? html`<p class="muted compare-empty">Upscale first to see before and after.</p>`
        : src ? html`${mode !== "after" ? html`<div class="compare-layer"><img src=${src.before} alt="Before" data-which="before" draggable="false" style=${{ transform: tf }} /></div>` : null}
          ${mode !== "before" ? html`<div class="compare-layer" style=${{ clipPath: mode === "split" ? `inset(0 0 0 ${(view.split * 100).toFixed(2)}%)` : "none" }}>
            <img src=${src.after} alt="After" data-which="after" draggable="false" style=${{ transform: tf }} /></div>` : null}
          ${mode === "split" ? html`<div class="compare-divider" style=${{ left: `${(view.split * 100).toFixed(2)}%` }}><span>Before</span><span>After</span></div>` : null}`
        : html`<p class="muted compare-empty">Drawing…</p>`}
    </div>
    <small class="muted">Drag to pan, wheel to zoom, drag the line to compare.${map.ready && map.factor > 1 ? ` After has ${map.factor} × ${map.factor} texels for each of Before's.` : ""}</small>
  </div>`;
}

/** Pose: hold the model at a frame of one of its animations. The pose shown is what Generate and Export use. */
function PoseTab({ m, pose, setPose, busy, call }) {
  const [list, setList] = useState(null); // the animations, once asked for
  const [note, setNote] = useState(null); // where the importer found them, when it says
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("");
  const [playing, setPlaying] = useState(false);
  const [draft, setDraft] = useState(null); // the frame box while it is being typed in
  useEffect(() => {
    let live = true;
    call("model_animations", { sid: m.sid }).then((r) => { if (live) { setList(r.animations); setNote(r.note || null); } }, (e) => { if (live) setError(msg(e)); });
    return () => { live = false; };
  }, [m.sid]);
  const anim = pose && list ? list.find((a) => a.name === pose.anim) : null;
  const last = anim ? Math.max(0, anim.frames - 1) : 0;
  const goTo = (frame) => setPose({ anim: pose.anim, frame: Math.min(last, Math.max(0, Math.round(frame))) });
  const choose = (name) => { setPlaying(false); setPose(name ? { anim: name, frame: 0 } : null); };
  // playing steps a frame on, at the animation's own rate, once the view has drawn the last one
  const tick = useRef(null);
  tick.current = () => { if (!busy && pose && anim) setPose({ anim: pose.anim, frame: pose.frame >= last ? 0 : pose.frame + 1 }); };
  useEffect(() => {
    if (!playing || !anim) return;
    const t = setInterval(() => tick.current(), Math.max(40, 1000 / (anim.fps || 30)));
    return () => clearInterval(t);
  }, [playing, anim?.name]);
  const q = filter.trim().toLowerCase();
  const shown = (list || []).filter((a) => !q || a.name.toLowerCase().includes(q));
  const readable = (list || []).filter((a) => a.frames > 0);
  const seconds = (f) => (anim && anim.fps ? (f / anim.fps).toFixed(2) : "0.00");
  return html`<div class="tab-pad" id="pose-tab">
    ${error ? html`<p class="warn-note" role="alert" id="pose-error">${error}</p>` : null}
    ${!list && !error ? html`<p class="muted" id="pose-loading">Reading the animations…</p>` : null}
    ${list && !list.length ? html`<p id="pose-none">No animations were found for this model.</p>` : null}
    ${list && list.length ? html`<section class="insp-section" id="pose-pick"><h3>Animation</h3>
      <input type="search" id="pose-filter" placeholder=${`Find among ${plural(list.length, "animation")}…`} value=${filter} onInput=${(e) => setFilter(e.target.value)} aria-label="Find an animation" />
      <select id="pose-anim" size="8" value=${pose?.anim || ""} onChange=${(e) => choose(e.target.value)} aria-label="Animation">
        ${shown.map((a) => html`<option key=${a.name} value=${a.name} disabled=${!a.frames} title=${a.source || ""}>${a.name}${a.frames ? ` · ${plural(a.frames, "frame")}, ${a.duration.toFixed(2)} s` : " · can't be read"}</option>`)}</select>
      <small class="muted">${readable.length < list.length ? `${list.length - readable.length} couldn't be read. ` : ""}${note || `Animations come from the model's anibnd file (${m.name}.anibnd), read from beside the model.`}</small>
    </section>` : null}
    ${anim ? html`<section class="insp-section" id="pose-frame-section"><h3>Frame</h3>
      <input type="range" id="pose-frame" min="0" max=${last} step="1" value=${pose.frame} onInput=${(e) => { setPlaying(false); goTo(parseInt(e.target.value, 10)); }} aria-label="Frame" />
      <div class="field-row pose-controls">
        <button type="button" class="ghost" id="pose-prev" title="Previous frame" disabled=${pose.frame <= 0} onClick=${() => { setPlaying(false); goTo(pose.frame - 1); }}>◀</button>
        <input type="number" id="pose-frame-number" min="0" max=${last} step="1" value=${draft ?? pose.frame} aria-label="Frame number"
          onInput=${(e) => setDraft(e.target.value)} onChange=${(e) => { setDraft(null); const v = parseInt(e.target.value, 10); if (Number.isFinite(v)) { setPlaying(false); goTo(v); } }} />
        <span class="muted unit">of ${last}</span>
        <button type="button" class="ghost" id="pose-next" title="Next frame" disabled=${pose.frame >= last} onClick=${() => { setPlaying(false); goTo(pose.frame + 1); }}>▶</button>
        <button type="button" class="ghost" id="pose-play" aria-pressed=${playing ? "true" : "false"} title=${playing ? "Stop" : "Play through the frames"} onClick=${() => setPlaying(!playing)}>${playing ? "Stop" : "Play"}</button>
      </div>
      <small class="muted" id="pose-time">${seconds(pose.frame)} s of ${anim.duration.toFixed(2)} s · ${anim.fps ? `${Math.round(anim.fps)} frames per second` : ""}</small>
    </section>` : null}
    ${list && list.length ? html`<div class="insp-actions"><button type="button" class="ghost" id="pose-reset" disabled=${!pose} onClick=${() => choose("")}>Back to the bind pose</button></div>` : null}
    <p class="muted export-note">The pose shown is what Generate works on and Export writes; each pose generated is kept, so switching back to one doesn't generate it again. Cloth and hair bones the animation doesn't move stay as in the bind pose.</p>
  </div>`;
}

/** A number box saved to the Generate settings. What's typed is kept while the tab draws again
 * (a progress update, a toast going) until it's saved on leaving the box. */
function NumberField({ id, label, value, step, min, max, unit, title, onChange }) {
  const [typed, setTyped] = useState(null); // { over: the value it was typed over, text }
  const shown = typed && typed.over === value ? typed.text : value;
  return html`<label class="field-block" title=${title}><span>${label}</span>
    <span class="field-row"><input type="number" id=${id} value=${shown} step=${step} min=${min} max=${max}
      onInput=${(e) => setTyped({ over: value, text: e.target.value })}
      onChange=${(e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) onChange(v); else setTyped(null); }} />${unit ? html`<span class="muted unit">${unit}</span>` : null}</span></label>`;
}

/** Whether step 1's mesh for the mesh settings as they are is kept by the engine: { kept, vertices, triangles }. */
function useMeshKept(call, m, args, s, run) {
  const [kept, setKept] = useState(null);
  useEffect(() => {
    let live = true;
    call("enhance_mesh", { sid: m.sid, ...args.json, settings: { target_vertices: s.target_vertices, method: s.method, smooth: s.smooth } })
      .then((r) => live && setKept(r), () => live && setKept(null));
    return () => { live = false; };
  }, [args.key, s.target_vertices, s.method, s.smooth, !!run]); // again after each job of this page's, stopped ones too
  return kept;
}

/** A job's progress with its Stop button. */
function JobProgress({ run, setRun, id }) {
  if (!run) return null;
  return html`<div class="job-progress" id=${id}><progress max="1" value=${run.progress}></progress><small class="muted">${run.message}</small>
    ${run.job ? html`<button type="button" class="ghost compact danger-text" id=${`${id}-stop`} disabled=${!!run.stopping} title="Stop; the model shown stays as it is"
      onClick=${() => { setRun({ ...run, stopping: true, message: "Stopping…" }); stopJob(run.job); }}>${run.stopping ? "Stopping…" : "Stop"}</button>` : null}</div>`;
}

/** Mesh: step 1, the model's vertices evened out to the count chosen (upsampled, or decimated by isotropic remeshing). */
function MeshTab({ m, args, stats, s, set, run, setRun, call, result, generate }) {
  const [error, setError] = useState(null);
  const methods = info().methods || [];
  const kept = useMeshKept(call, m, args, s, run);
  const spacing = spacingMm(stats.area_m2, s.target_vertices);
  const go = async () => {
    setError(null);
    try {
      const r = await generate("mesh");
      toast(r.kept ? "Evened out with these settings already: shown" : `Evened out: ${plural(r.stats.vertices, "vertex", "vertices")}. Displace adds the texture detail.`);
    } catch (e) { if (e.stopped) toast("Stopped evening out"); else setError(msg(e)); }
  };
  return html`<div class="tab-pad" id="mesh-tab">
    <section class="insp-section"><h3>Even out the vertices</h3>
      <p class="muted">Step 1: the model's triangles are remade at one even size, so the texture detail has vertices to move and large flat areas get no
        more than they need. Above the model's own count that upsamples it; below it, isotropic remeshing decimates it.</p>
      <${NumberField} id="target-vertices" label="Vertices" value=${s.target_vertices} step=${250000} min=${1000} max=${50000000}
        title="How many vertices the model gets. More keeps finer texture detail and makes a bigger file." onChange=${(v) => set({ target_vertices: Math.round(v) })} />
      <small class="muted" id="spacing">About ${spacing ? `${spacing < 1 ? spacing.toFixed(2) : spacing.toFixed(1)} mm` : "–"} between vertices (now ${num(stats.vertices)} vertices).</small>
      <label class="field-block"><span>Method</span><select id="method" value=${s.method} onChange=${(e) => set({ method: e.target.value })}>
        ${methods.map((x) => html`<option key=${x.id} value=${x.id} disabled=${!x.available}>${x.label}${x.available ? "" : ` (${x.missing || "not installed"})`}</option>`)}</select></label>
      <label class="check-row" title="New vertices follow the curve the model's shading describes, so low-poly cylinders and blades print round instead of faceted. Flat-shaded parts stay flat.">
        <input type="checkbox" id="smooth" checked=${s.smooth} onChange=${(e) => set({ smooth: e.target.checked })} /><span>Round off curved surfaces</span></label>
      ${s.method === "subdivide" && s.target_vertices <= stats.vertices ? html`<p class="insp-note" id="mesh-fewer">Adaptive subdivision only adds vertices: to go below the model's ${num(stats.vertices)}, choose isotropic remeshing.</p>` : null}
      ${kept?.kept ? html`<p class="insp-note" id="mesh-kept">${Icon.check(14)} Done: ${num(kept.vertices)} vertices, ${num(kept.triangles)} triangles. Kept while you work on the textures and the displacement.</p>` : null}
      <div class="insp-actions enhance-go"><button type="button" class="primary" id="mesh-run" disabled=${!!run || !stats.triangles} onClick=${go}>${run?.kind === "mesh" ? "Evening out…" : kept?.kept ? "Show the evened-out model" : "Even out the vertices"}</button></div>
      <${JobProgress} run=${run?.kind === "mesh" ? run : null} setRun=${setRun} id="mesh-progress" />
      ${error ? html`<p class="warn-note" role="alert" id="mesh-error">${error}</p>` : null}
    </section>
    ${result && result.displaced === false ? html`<section class="insp-section" id="mesh-shown"><h3>Shown</h3>
      <p class="muted">The 3D view's Enhanced model is the evened-out one, not displaced yet: ${num(result.stats.vertices)} vertices, ${num(result.stats.faces)} triangles.</p></section>` : null}
  </div>`;
}

/** Displace: step 2, the evened-out mesh's vertices pushed by the detail in the textures. */
function DisplaceTab({ m, args, stats, s, set, run, setRun, call, sendSettings, result, stale, over, setOver, strengths, setStrengths, generate }) {
  const [error, setError] = useState(null);
  const kept = useMeshKept(call, m, args, s, run);
  const maps = useTextureMaps(call, m, args, s, over, run, sendSettings);
  const have = m.textures.map((t) => t.key);
  const detailOf = (mat, i) => (i in over ? over[i] : (s.source === "height" ? mat.height : mat.normal) || "");
  const ready = (maps?.maps || []).filter((x) => x.ready).length, total = (maps?.maps || []).length;
  const go = async () => {
    setError(null);
    try {
      const r = await generate("both");
      toast(r.kept ? "Already generated with these settings" : `Displaced: ${plural(r.stats.vertices, "vertex", "vertices")}${r.mesh_reused ? "" : " (evened out first)"}`);
    } catch (e) { if (e.stopped) toast("Stopped displacing"); else setError(msg(e)); }
  };
  return html`<div class="tab-pad" id="displace-tab">
    <section class="insp-section"><h3>Raise the texture detail</h3>
      <p class="muted">Step 2: the evened-out mesh's vertices are pushed in and out by the relief the textures describe. Change these and displace
        again as often as you like; step 1 and the upscaled textures are kept.</p>
      <label class="field-block"><span>From</span><select id="source" value=${s.source} onChange=${(e) => { setOver({}); set({ source: e.target.value }); }}>
        <option value="normal">Normal maps (turned into height)</option><option value="height">Height or grayscale maps</option><option value="none">Nothing (the evened-out mesh as it is)</option></select></label>
      ${s.source === "normal" ? html`<${NumberField} id="strength" label="Strength" value=${s.strength} step=${0.25} min=${0} max=${50}
        title="1 makes the relief the normal map describes; more exaggerates it." onChange=${(v) => set({ strength: v })} />` : null}
      ${s.source === "height" ? html`<${NumberField} id="height-range" label="Height range" value=${s.height_range_mm} step=${0.1} min=${0} max=${100} unit="mm"
        title="How far white stands above black." onChange=${(v) => set({ height_range_mm: v })} />` : null}
      ${s.source !== "none" ? html`<div class="field-block" id="detail-maps"><span>Detail texture for each material</span>
        ${m.materials.map((mat, i) => html`<label class="mat-row" key=${i} title=${mat.details.join("\n")}><span class="mat-name">${mat.name}</span>
          <select data-material=${i} value=${detailOf(mat, i)} onChange=${(e) => setOver({ ...over, [i]: e.target.value })}>
            <option value="">None</option>${have.map((k) => html`<option key=${k} value=${k}>${m.textures.find((t) => t.key === k).name}</option>`)}</select>
          <input type="number" class="mat-strength" data-strength=${i} value=${strengths[i] ?? 1} step=${0.25} min=${0} max=${20}
            title="Strength for this material alone, on top of the overall strength" aria-label=${`Strength for ${mat.name}`}
            onChange=${(e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) setStrengths({ ...strengths, [i]: v }); }} /></label>`)}</div>` : null}
      ${s.source !== "none" ? html`<details class="more-settings"><summary>More settings</summary>
        ${s.source === "normal" ? html`<label class="field-block" title="Which way the normal maps' green channel points. Automatic uses the game's own convention, or reads it from the texture for other models."><span>Normal map green</span>
          <select id="green" value=${s.green} onChange=${(e) => set({ green: e.target.value })}><option value="auto">Automatic</option><option value="directx">DirectX (Y−)</option><option value="opengl">OpenGL (Y+)</option></select></label>
        <${NumberField} id="detail-radius" label="Detail radius" value=${s.detail_fraction} step=${0.01} min=${0} max=${0.5}
          title="Keeps relief finer than this share of the texture's width. Larger keeps broader shapes; 0 keeps everything (and may bulge)." onChange=${(v) => set({ detail_fraction: v })} />
        <label class="field-block" title="Where the normal map's slopes are added up into heights. On the model's surface is seamless across UV seams and keeps broader shapes; per texture is how version 0.1 worked."><span>Turn slopes into height</span>
          <select id="integration" value=${s.integration} onChange=${(e) => set({ integration: e.target.value })}>
            <option value="mesh">On the model's surface</option><option value="texture">On each texture (v0.1)</option></select></label>
        ${s.integration === "mesh" ? html`<${NumberField} id="edge-keeping" label="Edge keeping" value=${s.edge_keeping} step=${0.25} min=${0} max=${1}
          title="0 to 1. Stops slopes that no real surface could have (hand-painted detail, compression noise) from smearing into their surroundings. Slower; on clean maps it can soften relief." onChange=${(v) => set({ edge_keeping: v })} />` : null}
        <label class="check-row" title="Where the specular or metallic mask changes sharply (a gem in its setting, trim on leather), the surfaces either side are allowed to sit at their own heights.">
          <input type="checkbox" id="edge-hints" checked=${s.edge_hints} onChange=${(e) => set({ edge_hints: e.target.checked })} /><span>Use specular/metallic edges</span></label>
        <${NumberField} id="albedo-detail" label="Detail from the colour texture" value=${s.albedo_detail_mm} step=${0.05} min=${0} max=${5} unit="mm (0: off)"
          title="Adds fine relief guessed from the colour texture: darker than its surroundings reads as deeper (grooves, stitching). Painted patterns can turn into bumps, so keep it small." onChange=${(v) => set({ albedo_detail_mm: v })} />
        <label class="check-row" title="Some materials layer a small tiled normal map on top (leather grain, cloth weave). Adds it as extra fine relief; it is about 0.1 mm deep at print size.">
          <input type="checkbox" id="detail-normals" checked=${s.detail_normals} onChange=${(e) => set({ detail_normals: e.target.checked })} /><span>Add detail normal maps</span></label>
        ${s.detail_normals ? html`<${NumberField} id="detail-strength" label="Detail map strength" value=${s.detail_strength} step=${0.25} min=${0} max=${20}
          onChange=${(v) => set({ detail_strength: v })} />` : null}` : null}
        <${NumberField} id="clamp" label="Most displacement" value=${s.max_displacement_mm} step=${0.1} min=${0} max=${100} unit="mm (0: no limit)"
          title="No vertex moves further than this." onChange=${(v) => set({ max_displacement_mm: v })} />
        <p class="muted">Cleaning up and upsampling the normal maps is on the <a href="#" onClick=${(e) => { e.preventDefault(); ui.set({ inspectorAsk: { page: "model", tab: "textures" } }); }}>Textures tab</a>.</p>
      </details>` : null}
    </section>
    <section class="insp-section">
      <ul class="ls-list" id="displace-status">
        <li><span>Step 1 · Mesh</span><span class=${kept?.kept ? "" : "muted"} id="displace-mesh-state">${kept?.kept ? `${num(kept.vertices)} vertices, kept` : "runs first, with the Mesh tab's settings"}</span></li>
        ${s.source === "normal" && total ? html`<li><span>Textures</span><span class=${ready === total ? "" : "muted"} id="displace-maps-state">${ready === total ? `${plural(total, "normal map")} ready` : `${ready} of ${total} ready; the rest are made first`}</span></li>` : null}
      </ul>
      <div class="insp-actions enhance-go"><button type="button" class="primary" id="displace-run" disabled=${!!run || !stats.triangles} onClick=${go}>${run?.kind === "displace" ? "Displacing…" : result && result.displaced !== false ? "Displace again" : "Displace"}</button></div>
      <${JobProgress} run=${run?.kind === "displace" ? run : null} setRun=${setRun} id="displace-progress" />
      ${error ? html`<p class="warn-note" role="alert" id="enhance-error">${error}</p>` : null}
    </section>
    ${result ? html`<section class="insp-section" id="enhance-result"><h3>${result.displaced === false ? "Evened out, not displaced yet" : "Enhanced"}</h3>
      ${stale ? html`<p class="insp-note">Made with other parts, options or pose than those chosen now: displace again to include the change.</p>` : null}
      <dl class="insp-dl">
        <dt>Vertices</dt><dd>${num(result.stats.vertices)} <span class="muted">from ${num(result.stats.source_vertices)}</span></dd>
        <dt>Triangles</dt><dd>${num(result.stats.faces)}</dd>
        <dt>Size</dt><dd>${sizeText(result.size_mm)}</dd>
        <dt>Relief</dt><dd>up to ${result.stats.max_displacement_mm.toFixed(2)} mm</dd></dl>
      ${result.fields.length ? html`<ul class="ls-list field-list">${result.fields.map((f, i) => html`<li key=${i}><span>${f.material}<small class="muted">${[f.texture, f.convention, ...(f.steps || [])].filter(Boolean).join(" · ")}</small></span>
        <span class="muted" title="One texture pixel covers this much of the model">${f.texel_mm.toFixed(2)} mm/px</span></li>`)}</ul>` : null}
      ${result.warnings.map((w, i) => html`<p class="insp-note" key=${i}>${w}</p>`)}
    </section>` : null}
  </div>`;
}

/** Finish: post-processing for printing. Make watertight wraps the whole model into one solid: every part made
 *  solid and joined, the surfaces inside one another removed, hollows filled, and gaps and loose bits as asked. */
function FinishTab({ m, args, stats, run, setRun, result, finished, which, setWhich, finishModel }) {
  const saved = useStore(ui, (st) => st.finishOpts);
  const o = { ...FINISH, ...(saved || {}) };
  const setOpts = (patch) => setPref({ finishOpts: { ...o, ...patch } });
  const [error, setError] = useState(null);
  const from = o.from === "enhanced" && !result ? "original" : o.from;
  const request = { from, watertight: { sheet_mm: o.sheet_mm, fill_hollows: o.fill_hollows, close_gaps_mm: o.close_gaps ? o.close_gaps_mm : 0,
    loose_percent: o.remove_loose ? o.loose_percent : 0 } };
  const stale = finished?.request && finished.request !== JSON.stringify(request);
  const go = async () => {
    setError(null);
    try {
      const r = await finishModel(request);
      toast(r.kept ? "Made watertight with these settings already: shown" : "Made watertight");
    } catch (e) { if (e.stopped) toast("Stopped making it watertight"); else setError(msg(e)); }
  };
  const st = finished?.stats;
  return html`<div class="tab-pad" id="finish-tab">
    <section class="insp-section"><h3>Make watertight</h3>
      <p class="muted">Wraps the whole model into one solid for printing. Game models are built from parts that cross (arms pushed into sleeves,
        armour over a body) and open surfaces (capes, cloth): every part is made solid and they are joined, so the surfaces inside one another
        are removed and only the outside stays, exactly as it was.</p>
      <div class="field-block"><span>Model</span><div class="seg" role="group" aria-label="Which model to make watertight" id="finish-from">
        <button type="button" aria-pressed=${from === "enhanced" ? "true" : "false"} disabled=${!result} title=${result ? "The enhanced model (Mesh or Displace tab)" : "Make it first (Mesh or Displace tab)"} onClick=${() => setOpts({ from: "enhanced" })}>Enhanced</button>
        <button type="button" aria-pressed=${from === "original" ? "true" : "false"} onClick=${() => setOpts({ from: "original" })}>Original</button></div></div>
      <label class="check-row"><input type="checkbox" id="finish-hollows" checked=${o.fill_hollows} onChange=${(e) => setOpts({ fill_hollows: e.target.checked })} />
        <span>Fill hollows sealed inside<small class="muted">Space shut in by the parts (under a closed coat or helmet) is filled, so the model is solid all through.</small></span></label>
      <label class="check-row"><input type="checkbox" id="finish-gaps" checked=${o.close_gaps} onChange=${(e) => setOpts({ close_gaps: e.target.checked })} />
        <span>Close narrow gaps<small class="muted">Bridges slits between parts (a sleeve around an arm) and grooves up to this wide. Takes longer; the bridges sit just under the surface elsewhere.</small></span></label>
      ${o.close_gaps ? html`<${NumberField} id="finish-gap-mm" label="Gaps up to" value=${o.close_gaps_mm} step=${0.25} min=${0.1} max=${20} unit="mm"
        title="The widest gap closed. Fine grooves in the model narrower than this are filled too, so keep it small." onChange=${(v) => setOpts({ close_gaps_mm: v })} />` : null}
      <label class="check-row"><input type="checkbox" id="finish-loose" checked=${o.remove_loose} onChange=${(e) => setOpts({ remove_loose: e.target.checked })} />
        <span>Remove loose bits<small class="muted">Separate bits that don't touch the model (a floating buckle, specks) and are smaller than this share of it.</small></span></label>
      ${o.remove_loose ? html`<${NumberField} id="finish-loose-pct" label="Smaller than" value=${o.loose_percent} step=${0.5} min=${0.1} max=${100} unit="% of the volume"
        onChange=${(v) => setOpts({ loose_percent: v })} />` : null}
      <details class="more-settings"><summary>More settings</summary>
        <${NumberField} id="finish-sheet" label="Open surfaces" value=${o.sheet_mm} step=${0.2} min=${0.2} max=${20} unit="mm thick"
          title="Open surfaces (capes, single-sided cloth) have no inside; they are thickened to this, inwards." onChange=${(v) => setOpts({ sheet_mm: v })} />
      </details>
      <div class="insp-actions enhance-go"><button type="button" class="primary" id="finish-run" disabled=${!!run || !stats.triangles} onClick=${go}>${Icon.drop(15)} ${run?.kind === "finish" ? "Making it watertight…" : "Make watertight"}</button></div>
      <${JobProgress} run=${run?.kind === "finish" ? run : null} setRun=${setRun} id="finish-progress" />
      ${error ? html`<p class="warn-note" role="alert" id="finish-error">${error}</p>` : null}
    </section>
    ${finished ? html`<section class="insp-section" id="finish-result"><h3>Watertight</h3>
      ${stale ? html`<p class="insp-note" id="finish-stale">Made with other settings than these: make it again to see the change.</p>` : null}
      <ul class="ls-list kv-list" id="finish-stats">
        <li><span>Triangles</span><span>${num(st.triangles)}<small class="muted"> from ${num(st.source_triangles)}</small></span></li>
        <li><span>Volume</span><span>${st.volume_cm3 >= 10 ? num(st.volume_cm3) : st.volume_cm3.toFixed(2)} cm³</span></li>
        <li><span>Size</span><span>${sizeText(st.size_mm)}</span></li>
        <li><span>Separate pieces</span><span>${st.shells}</span></li></ul>
      ${finished.notes.map((n, i) => html`<p class="insp-note" key=${i}>${n}</p>`)}
      ${which !== "finished" ? html`<div class="insp-actions"><button type="button" class="ghost compact" id="finish-show" onClick=${() => setWhich("finished")}>Show it</button></div>` : null}
      <p class="muted">Export and Cut can use it: choose Finished there. It has no textures, as it's for printing.</p>
    </section>` : null}
  </div>`;
}

const deg = (r) => (r * 180) / Math.PI;
const rad = (d) => (d * Math.PI) / 180;
const AXES = [["x", "Across X", [1, 0, 0]], ["y", "Across Y", [0, 1, 0]], ["z", "Across Z", [0, 0, 1]]];
const jointName = (kind) => (JOINTS.find(([k]) => k === kind) || JOINTS[0])[1];
/** Which way a cut faces, in words: across an axis, or tilted. */
function facing(n) {
  for (const [k, label, a] of AXES) if (Math.abs(Math.abs(n[0] * a[0] + n[1] * a[1] + n[2] * a[2]) - 1) < 1e-4) return label.toLowerCase();
  return "tilted";
}

/** Cut: planes placed in the 3D view (dragged to slide them, the round handle dragged to tilt them, or a point
 *  on the model clicked) split the model into watertight pieces, each cut with its own joint and clearance. */
function CutTab({ m, args, stats, run, setRun, result, finished, which, setWhich, setOpen, cutModel, cuts, setCuts, cutSel, setCutSel, pieces, explode, setExplode, bounds, viewer }) {
  useEffect(() => { setOpen(true); return () => setOpen(false); }, []);
  const saved = useStore(ui, (st) => st.cutOpts);
  const o = { ...CUT, ...(saved || {}), joint: { ...JOINT, ...(saved?.joint || {}) } };
  const setOpts = (patch) => setPref({ cutOpts: { ...o, ...patch } });
  const [error, setError] = useState(null);
  const [placing, setPlacing] = useState(false);
  const from = (o.from === "enhanced" && !result) || (o.from === "finished" && !finished) ? "original" : o.from;
  const lo = bounds?.min || [0, 0, 0], hi = bounds?.max || [0, 0, 0];
  const centre = lo.map((v, i) => (v + hi[i]) / 2);
  const sel = cuts.find((c) => c.id === cutSel) || null;
  const offsetOf = (c) => c.normal.reduce((a, x, i) => a + x * (c.point[i] - centre[i]), 0);
  const halfOf = (n) => 0.5 * n.reduce((a, x, i) => a + Math.abs(x) * (hi[i] - lo[i]), 0);
  const put = (n, off) => ({ normal: n, point: centre.map((v, i) => v + n[i] * off) });
  const update = (id, patch) => setCuts(cuts.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  const add = (n) => {
    const id = Math.max(0, ...cuts.map((c) => c.id)) + 1;
    setCuts([...cuts, { id, ...put(n, 0), joint: { ...o.joint } }]);
    setCutSel(id);
  };
  const facingMe = () => {
    return viewer.current?.facingNormal() || [0, -1, 0];
  };
  const remove = (id) => {
    const rest = cuts.filter((c) => c.id !== id);
    setCuts(rest);
    if (cutSel === id) setCutSel(rest[rest.length - 1]?.id ?? null);
  };
  const setJoint = (patch) => {
    if (!sel) return;
    const joint = { ...JOINT, ...sel.joint, ...patch };
    update(sel.id, { joint });
    setOpts({ joint }); // new cuts take the joint set last
  };
  const placeByClick = () => {
    const v = viewer.current;
    if (!v || !sel) return;
    if (placing) { v.placing = null; setPlacing(false); return; }
    setPlacing(true);
    if (which === "pieces") setWhich(result ? "enhanced" : "original");
    v.placing = (point) => {
      setPlacing(false);
      const off = sel.normal.reduce((a, x, i) => a + x * (point[i] - centre[i]), 0);
      update(sel.id, put(sel.normal, off));
    };
  };
  useEffect(() => () => { if (viewer.current) viewer.current.placing = null; }, []);
  const request = { from, cut: { cuts: cuts.map((c) => ({ normal: c.normal, point: c.point, joint: { ...JOINT, ...c.joint } })), sheet_mm: o.sheet_mm, wall_mm: o.wall_mm } };
  const stale = pieces?.request && pieces.request !== JSON.stringify(request);
  const go = async () => {
    setError(null);
    try {
      const r = await cutModel(request);
      toast(r.kept ? "Cut with these planes already: shown" : `Cut into ${plural(r.pieces.length, "piece")}`);
    } catch (e) { if (e.stopped) toast("Stopped cutting"); else setError(msg(e)); }
  };
  const j = sel ? { ...JOINT, ...sel.joint } : null;
  const theta = sel ? deg(Math.acos(Math.max(-1, Math.min(1, sel.normal[2])))) : 0;
  const phi = sel ? deg(Math.atan2(sel.normal[1], sel.normal[0])) : 0;
  const turnTo = (t, p) => {
    const n = [Math.sin(rad(t)) * Math.cos(rad(p)), Math.sin(rad(t)) * Math.sin(rad(p)), Math.cos(rad(t))];
    update(sel.id, put(n, offsetOf(sel)));
  };
  const half = sel ? halfOf(sel.normal) : 0;
  const slide = (v) => update(sel.id, put(sel.normal, Math.max(-half, Math.min(half, v))));
  const stepOf = Math.max(0.1, Math.round(half / 100 * 10) / 10);
  return html`<div class="tab-pad" id="cut-tab">
    <section class="insp-section"><h3>Cut into pieces</h3>
      <p class="muted">Split the model with planes so it prints in pieces that fit your printer, joined by pegs, dowels, magnets or a key.
        Drag a plane in the 3D view to slide it, drag its round handle to tilt it, or click a point on the model to put it there. Each piece comes
        out watertight.</p>
      <div class="field-block"><span>Cut</span><div class="seg" role="group" aria-label="Which model to cut" id="cut-from">
        <button type="button" aria-pressed=${from === "enhanced" ? "true" : "false"} disabled=${!result} title=${result ? "The enhanced model (Mesh or Displace tab)" : "Make it first (Mesh or Displace tab)"} onClick=${() => setOpts({ from: "enhanced" })}>Enhanced</button>
        <button type="button" aria-pressed=${from === "original" ? "true" : "false"} onClick=${() => setOpts({ from: "original" })}>Original</button>
        <button type="button" aria-pressed=${from === "finished" ? "true" : "false"} disabled=${!finished} title=${finished ? "The model made watertight (Finish tab)" : "Make it watertight first (Finish tab)"} onClick=${() => setOpts({ from: "finished" })}>Finished</button></div></div>
      <div class="field-block"><span>Add a cut</span><div class="field-row cut-add" id="cut-add">
        ${AXES.map(([k, label, n]) => html`<button type="button" class="ghost compact" key=${k} data-axis=${k} title=${`A plane ${label.toLowerCase()}, through the middle of the model`} disabled=${!bounds} onClick=${() => add(n)}>${Icon.plus(13)} ${label}</button>`)}
        <button type="button" class="ghost compact" data-axis="view" title="A plane facing you, as the 3D view is turned now" disabled=${!bounds} onClick=${() => add(facingMe())}>${Icon.plus(13)} Facing me</button></div></div>
      ${cuts.length ? html`<ul class="ls-list cut-list" id="cut-list" role="listbox" aria-label="Cuts">${cuts.map((c, i) => html`<li key=${c.id} data-cut=${c.id} role="option" aria-selected=${c.id === cutSel ? "true" : "false"}
          class=${c.id === cutSel ? "selected" : ""} onClick=${() => setCutSel(c.id)}>
        <span>Cut ${i + 1}<small class="muted">${facing(c.normal)} · ${mm(offsetOf(c))} mm from the middle · ${jointName(c.joint?.kind)}${c.joint?.kind !== "flat" ? `, ${(c.joint?.clearance_mm ?? JOINT.clearance_mm).toFixed(2)} mm clearance` : ""}</small></span>
        <button type="button" class="ghost compact" title="Remove this cut" aria-label=${`Remove cut ${i + 1}`} onClick=${(e) => { e.stopPropagation(); remove(c.id); }}>${Icon.trash(14)}</button></li>`)}</ul>`
        : html`<p class="muted" id="cut-none">No cuts yet. Add one, then move it where the model should come apart.</p>`}
    </section>
    ${sel ? html`<section class="insp-section" id="cut-edit"><h3>Cut ${cuts.indexOf(sel) + 1}</h3>
      <label class="field-block" title="Slide the plane along its direction (or drag it in the 3D view)"><span>Position</span>
        <span class="field-row"><input type="range" id="cut-position" min=${-half} max=${half} step=${stepOf} value=${offsetOf(sel)} onInput=${(e) => slide(parseFloat(e.target.value))} />
          <input type="number" id="cut-position-mm" class="compact-number" step=${stepOf} value=${Number(offsetOf(sel).toFixed(2))} onChange=${(e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) slide(v); }} /><span class="muted unit">mm</span></span></label>
      <div class="field-block"><span>Direction</span><div class="field-row">
        ${AXES.map(([k, label, n]) => html`<button type="button" class="ghost compact" key=${k} data-dir=${k} aria-pressed=${facing(sel.normal) === label.toLowerCase() ? "true" : "false"} onClick=${() => update(sel.id, put(n, 0))}>${label.slice(-1)}</button>`)}
        <button type="button" class="ghost compact" data-dir="view" title="Face the plane towards you, as the 3D view is turned now" onClick=${() => update(sel.id, put(facingMe(), 0))}>Facing me</button>
        <button type="button" class="ghost compact" id="cut-place" aria-pressed=${placing ? "true" : "false"} title="Click a point on the model to move the plane through it" onClick=${placeByClick}>${placing ? "Click the model…" : "Pick a point"}</button></div></div>
      <label class="field-block" title="How far the plane leans from lying flat (0°) to standing upright (90°); or drag its round handle in the 3D view"><span>Tilt</span>
        <span class="field-row"><input type="range" id="cut-tilt" min="0" max="180" step="1" value=${theta} onInput=${(e) => turnTo(parseFloat(e.target.value), phi)} /><span class="muted unit">${Math.round(theta)}°</span></span></label>
      <label class="field-block" title="Which way the plane faces around the vertical"><span>Turn</span>
        <span class="field-row"><input type="range" id="cut-turn" min="-180" max="180" step="1" value=${phi} disabled=${theta < 0.5 || theta > 179.5} onInput=${(e) => turnTo(theta, parseFloat(e.target.value))} /><span class="muted unit">${Math.round(phi)}°</span></span></label>
      <div class="field-block"><span>Joint</span><div class="seg joint-seg" role="group" aria-label="Joint" id="cut-joint">${JOINTS.map(([k, label, tip]) => html`<button type="button" key=${k} data-value=${k} title=${tip}
        aria-pressed=${j.kind === k ? "true" : "false"} onClick=${() => setJoint({ kind: k })}>${label}</button>`)}</div>
        <small class="muted" id="cut-joint-about">${(JOINTS.find(([k]) => k === j.kind) || JOINTS[0])[2]}</small></div>
      ${j.kind !== "flat" ? html`<${NumberField} id="cut-clearance" label="Clearance" value=${j.clearance_mm} step=${0.05} min=${0} max=${2} unit="mm"
          title="The gap left on each side between parts that fit together: 0.1 to 0.2 mm for a tight fit on a well-tuned printer, 0.3 mm or more for resin shrinkage or a loose fit." onChange=${(v) => setJoint({ clearance_mm: v })} />` : null}
      ${j.kind === "pegs" || j.kind === "dowels" ? html`<${NumberField} id="cut-diameter" label=${j.kind === "pegs" ? "Peg diameter" : "Dowel diameter"} value=${j.diameter_mm} step=${0.5} min=${0.5} max=${50} unit="mm"
          title=${j.kind === "dowels" ? "1.75 mm takes filament as dowels; 3 or 5 mm suits metal pins" : "The holes are this plus twice the clearance"} onChange=${(v) => setJoint({ diameter_mm: v })} />` : null}
      ${j.kind === "magnets" ? html`<${NumberField} id="cut-magnet-d" label="Magnet diameter" value=${j.magnet_diameter_mm} step=${0.5} min=${0.5} max=${50} unit="mm" onChange=${(v) => setJoint({ magnet_diameter_mm: v })} />
        <${NumberField} id="cut-magnet-h" label="Magnet thickness" value=${j.magnet_height_mm} step=${0.5} min=${0.2} max=${50} unit="mm" title="One magnet goes in each side" onChange=${(v) => setJoint({ magnet_height_mm: v })} />` : null}
      ${j.kind === "pegs" || j.kind === "dowels" || j.kind === "key" ? html`<${NumberField} id="cut-depth" label=${j.kind === "key" ? "Key height" : "Depth into each piece"} value=${j.depth_mm} step=${0.5} min=${0.5} max=${100} unit="mm"
          title="Made shorter where a piece is too thin for it" onChange=${(v) => setJoint({ depth_mm: v })} />` : null}
      ${j.kind === "pegs" || j.kind === "dowels" || j.kind === "magnets" ? html`<label class="field-block" title="How many on each separate area of the cut face. Automatic puts two where they fit (so the pieces can't turn), else one."><span>How many</span>
        <select id="cut-count" value=${String(j.count)} onChange=${(e) => setJoint({ count: Number(e.target.value) })}>
          <option value="0">Automatic</option>${[1, 2, 3, 4].map((n) => html`<option key=${n} value=${String(n)}>${n}</option>`)}</select></label>` : null}
      ${j.kind === "pegs" || j.kind === "key" ? html`<label class="check-row"><input type="checkbox" id="cut-flip" checked=${j.flip} onChange=${(e) => setJoint({ flip: e.target.checked })} />
        <span>${j.kind === "pegs" ? "Pegs" : "The tongue"} on the other side<small class="muted">Normally on the side the round handle points to.</small></span></label>` : null}
      ${j.kind === "dowels" ? html`<label class="check-row"><input type="checkbox" id="cut-print-dowels" checked=${j.print_dowels} onChange=${(e) => setJoint({ print_dowels: e.target.checked })} />
        <span>Make the dowels as pieces too<small class="muted">Off: holes only, for bought pins or filament.</small></span></label>` : null}
      ${cuts.length > 1 ? html`<div class="insp-actions"><button type="button" class="ghost compact" id="cut-joint-all" onClick=${() => setCuts(cuts.map((c) => ({ ...c, joint: { ...j } })))}>Use this joint for every cut</button></div>` : null}
    </section>` : null}
    <section class="insp-section">
      <details class="more-settings"><summary>More settings</summary>
        <${NumberField} id="cut-sheet" label="Open surfaces" value=${o.sheet_mm} step=${0.2} min=${0.2} max=${20} unit="mm thick"
          title="Game models have open surfaces (capes, single-sided cloth) with no inside; they are thickened to this, inwards, before cutting." onChange=${(v) => setOpts({ sheet_mm: v })} />
        <${NumberField} id="cut-wall" label="Wall around joints" value=${o.wall_mm} step=${0.2} min=${0.4} max=${20} unit="mm"
          title="The least material kept between a peg, hole, pocket or key and the outside. Where there's no room for it the cut is left flat there." onChange=${(v) => setOpts({ wall_mm: v })} />
      </details>
      <div class="insp-actions enhance-go"><button type="button" class="primary" id="cut-run" disabled=${!!run || !cuts.length || !stats.triangles} onClick=${go}>${Icon.scissors(15)} ${run?.kind === "cut" ? "Cutting…" : cuts.length ? `Cut with ${plural(cuts.length, "plane")}` : "Cut"}</button></div>
      <${JobProgress} run=${run?.kind === "cut" ? run : null} setRun=${setRun} id="cut-progress" />
      ${error ? html`<p class="warn-note" role="alert" id="cut-error">${error}</p>` : null}
    </section>
    ${pieces ? html`<section class="insp-section" id="cut-result"><h3>${plural(pieces.pieces.length, "piece")}</h3>
      ${stale ? html`<p class="insp-note" id="cut-stale">Cut with other planes or settings than these: cut again to see the change.</p>` : null}
      <label class="field-block" title="Show the pieces apart in the 3D view (Show › Pieces); it doesn't change them"><span>Spread apart</span>
        <input type="range" id="cut-explode" min="0" max="1" step="0.01" value=${explode} onInput=${(e) => { setExplode(parseFloat(e.target.value)); if (which !== "pieces") setWhich("pieces"); }} /></label>
      <ul class="ls-list piece-list" id="piece-list">${pieces.pieces.map((p) => html`<li key=${p.index} data-piece=${p.index} data-kind=${p.kind}>
        <span><i class="piece-swatch" style=${{ background: PIECE_COLORS[p.index % PIECE_COLORS.length] }}></i>${p.name}<small class="muted">${sizeText(p.size_mm)} · ${num(p.triangles)} triangles</small></span>
        <span class="muted">${p.volume_cm3 >= 10 ? p.volume_cm3.toFixed(0) : p.volume_cm3.toFixed(1)} cm³</span></li>`)}</ul>
      ${pieces.notes.map((n, i) => html`<p class="insp-note" key=${i}>${n}</p>`)}
      <p class="muted">Export writes each piece to its own file, laid on a cut face for printing.</p>
    </section>` : null}
  </div>`;
}

/** Export: the enhanced (or original) model as STL, OBJ, PLY or GLB, in a folder. */
function ExportTab({ m, args, result, finished, which: shownWhich, pose, call, pieces }) {
  const saved = useStore(ui, (s) => s.exportOpts);
  const o = { ...EXPORT, ...(saved || {}) };
  const set = (patch) => setPref({ exportOpts: { ...o, ...patch } });
  const fallbackWhich = (w) => ((w === "pieces" && !pieces) || (w === "finished" && !finished) ? (result ? "enhanced" : "original") : w === "enhanced" && !result ? "original" : w);
  const [which, setWhich] = useState(() => fallbackWhich(shownWhich));
  useLayoutEffect(() => { setWhich(fallbackWhich(shownWhich)); }, [result?.rev, pieces?.id, finished?.id]);
  const cutOpts = { ...CUT, ...(useStore(ui, (s) => s.cutOpts) || {}) };
  const [name, setName] = useState("");
  const [folder, setFolder] = useState(o.folder);
  const [fallback, setFallback] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { api("settings_get").then((r) => setFallback(r.export_dir), () => {}); }, []);
  const base = variantName(m.variant) || m.name;
  const auto = `${base}${poseSuffix(pose)}${which === "enhanced" ? "_enhanced" : which === "finished" ? "_watertight" : ""}`;
  const many = which === "pieces";
  const formats = info().formats || [];
  const textured = o.format === ".obj" || o.format === ".glb";
  const browse = async () => { const p = await pickFolder("Export to", folder || fallback); if (p) { setFolder(p); set({ folder: p }); } };
  const go = async () => {
    setBusy(true); setError(null); setDone(null);
    try {
      const { job } = await call("export", { sid: m.sid, which, ...args.json, format: o.format, units: o.units, textures: o.textures, folder: folder || null, name: name.trim() || auto,
        lay_flat: cutOpts.lay_flat });
      const r = await followJob(job, many ? `Exporting ${plural(pieces.pieces.length, "piece")}` : `Exporting ${name.trim() || auto}${o.format}`);
      setDone(r);
    } catch (e) { if (e.stopped) toast("Stopped exporting"); else setError(msg(e)); }
    setBusy(false);
  };
  return html`<div class="tab-pad" id="export-tab">
    <div class="field-block"><span>Model</span><div class="seg" role="group" aria-label="Which model" id="export-which">
      <button type="button" aria-pressed=${which === "enhanced" ? "true" : "false"} disabled=${!result} title=${result ? "The enhanced model" : "Make it first (Mesh or Displace tab)"} onClick=${() => setWhich("enhanced")}>Enhanced</button>
      <button type="button" aria-pressed=${which === "original" ? "true" : "false"} onClick=${() => setWhich("original")}>Original</button>
      <button type="button" aria-pressed=${which === "finished" ? "true" : "false"} disabled=${!finished} title=${finished ? "The model made watertight (Finish tab)" : "Make it watertight first (Finish tab)"} onClick=${() => setWhich("finished")}>Finished</button>
      <button type="button" aria-pressed=${many ? "true" : "false"} disabled=${!pieces} title=${pieces ? "Each piece of the cut model in its own file" : "Cut it first (Cut tab)"} onClick=${() => setWhich("pieces")}>Pieces</button></div>
      ${!result ? html`<small class="muted">Make it first (Mesh or Displace tab) to export the detailed version.</small>` : null}</div>
    <label class="field-block"><span>Format</span><select id="export-format" value=${o.format} onChange=${(e) => set({ format: e.target.value })}>
      ${formats.map((f) => html`<option key=${f.ext} value=${f.ext}>${f.label} (${f.ext})</option>`)}</select></label>
    <label class="field-block"><span>Units</span><select id="export-units" value=${o.units} onChange=${(e) => set({ units: e.target.value })}>
      ${UNITS.map(([k, l]) => html`<option key=${k} value=${k}>${l}</option>`)}</select></label>
    ${many ? html`<label class="check-row" title="Each piece turned so its biggest flat cut face (without pegs or a tongue on it) lies on the bed; dowels stand on end">
      <input type="checkbox" id="export-lay-flat" checked=${cutOpts.lay_flat} onChange=${(e) => setPref({ cutOpts: { ...cutOpts, lay_flat: e.target.checked } })} />
      <span>Lay each piece on a cut face<small class="muted">Off: the pieces keep their place in the model.</small></span></label>`
      : html`<label class="check-row"><input type="checkbox" id="export-textures" checked=${o.textures && textured && which !== "finished"} disabled=${!textured || which === "finished"} onChange=${(e) => set({ textures: e.target.checked })} />
      <span>Include the colour textures${textured ? "" : " (OBJ and GLB only)"}</span></label>`}
    <label class="field-block"><span>Name</span><input type="text" id="export-name" value=${name} placeholder=${auto} onInput=${(e) => setName(e.target.value)} /></label>
    <div class="field-block"><span>Folder</span><div class="field-row">
      <input type="text" id="export-folder" value=${folder} placeholder=${fallback} onInput=${(e) => setFolder(e.target.value)} onChange=${(e) => set({ folder: e.target.value.trim() })} />
      ${info().dialogs ? html`<button type="button" class="ghost" id="export-browse" onClick=${browse}>Browse…</button>` : null}</div></div>
    <div class="insp-actions"><button type="button" class="primary" id="export-run" disabled=${busy} onClick=${go}>${Icon.download(15)} ${busy ? "Exporting…" : "Export"}</button></div>
    ${error ? html`<p class="warn-note" role="alert" id="export-error">${error}</p>` : null}
    ${done ? html`<div class="export-done" id="export-done" role="status"><p><b>Saved</b> ${done.files ? `${plural(done.files.length, "file")} in ${done.path.split(/[\\/]/).pop()}` : done.path.split(/[\\/]/).pop()} <span class="muted">· ${bytesText(done.bytes)}</span></p>
      <p class="note-path muted">${done.path}</p>
      <div class="insp-actions"><button type="button" class="ghost" onClick=${() => showInFolder(done.path)}>${Icon.folder(14)} Show in folder</button></div></div>` : null}
    <p class="muted export-note">STL and PLY are written Z-up, as slicers expect; OBJ and GLB Y-up. Game models often aren't watertight: export the Finished model (Finish tab) or the pieces to print, or check them in your slicer.</p>
  </div>`;
}
