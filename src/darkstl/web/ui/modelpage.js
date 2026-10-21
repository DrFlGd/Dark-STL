// A model's own page (model:<id>[@<variant>]): the 3D view with its textures,
// and the inspector's Model (parts, options, size, file), Textures, Generate
// (even out the vertices, then displace them by the textures' detail) and
// Export tabs. A file holding several models (a weapon and its sheath) gets a
// switch between them in the command bar.
import { html, useState, useEffect, useLayoutEffect, useRef, useMemo } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref, resolvedTheme } from "./state.js";
import { routeHash, placeRoute, modelOf } from "./context.js";
import { Icon } from "./icons.js";
import { api, binUrl, info, plural, followJob, toast, showInFolder, pickFolder, pickFile } from "./api.js";
import { CommandBar } from "./layout.js";
import { Inspector, useWidth } from "./inspector.js";
import { Viewer } from "../viewer.js";

const ENHANCE = { target_vertices: 1_000_000, method: "subdivide", smooth: true, source: "normal", strength: 1, height_range_mm: 1,
  green: "auto", detail_fraction: 0.0625, max_displacement_mm: 0, integration: "mesh", edge_keeping: 0, deblock: "off",
  upsample: "off", ai_model: "", deblock_model: "", edge_hints: false, albedo_detail_mm: 0, detail_normals: false, detail_strength: 1 };
const EXPORT = { format: ".stl", units: "mm", textures: true, folder: "" };
const VIEW = { shading: "textured", edges: false };
const KEEP_MESHES = 4; // built meshes a model page keeps (on the graphics card) to show again at once
const UNITS = [["mm", "Millimetres (game metres × 1000)"], ["cm", "Centimetres"], ["m", "Metres (the game's own units)"], ["in", "Inches"]];
/** The least room the 3D view gets beside the inspector, and the inspector's rail. */
const STAGE = 440, RAIL = 36;

const msg = (e) => String(e?.message || e);
const num = (n) => Math.round(n).toLocaleString();
const mm = (v) => (v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2));
const sizeText = (s) => (s ? `${mm(s[0])} × ${mm(s[1])} × ${mm(s[2])} mm` : "");
const bytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const variantName = (v) => (v || "").replace(/\.(flver|flv)$/i, "");
/** Where new vertices will be on average, for a mesh of `area` m² with `n` vertices: equilateral triangles, about 2 per vertex. */
const spacingMm = (area, n) => (area > 0 && n > 0 ? Math.sqrt((2 * area) / (Math.sqrt(3) * n)) * 1000 : 0);

/** The parts and options as the mesh commands take them, JSON and in a URL. */
function meshArgs(m, off, flags) {
  const parts = off.size ? m.parts.map((_, i) => i).filter((i) => !off.has(i)) : null;
  return {
    json: { parts, flags },
    url: { parts: parts ? parts.join(",") : "all", flags: Object.entries(flags).map(([k, v]) => `${k}:${v ? 1 : 0}`).join(",") },
    key: JSON.stringify([parts, flags]),
  };
}

export function ModelPage({ route }) {
  const { id, variant } = modelOf(route);
  const [m, setM] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api("model_open", { id, variant }).then((v) => { if (live) { setM(v); setError(""); } }, (e) => { if (live) setError(msg(e)); });
    return () => { live = false; };
  }, [id, variant]);
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
    { id: "open-file", label: "Open another model file…", icon: "file", run: () => ui.set({ dialog: { type: "open-file" } }) },
  ];
  return html`<${CommandBar} ...${bar} up=${up.filter(([, href]) => href)} title=${html`<h1 id="mp-name" title=${entry?.label || m.name}>${entry?.label || m.name}</h1>`}
      count=${html`<span class="page-count" id="mp-count" title=${m.file}>${fileName}</span>`}
      tools=${m.variants.length > 1 ? html`<label class="variant-pick" title="This file holds several models, such as a weapon and its sheath">
        <span class="visually-hidden">Model in this file</span>
        <select id="variant-select" class="compact" value=${m.variant} onChange=${(e) => pick(e.target.value)}>
          ${m.variants.map((v) => html`<option key=${v} value=${v}>${variantName(v)}</option>`)}</select></label>` : null}
      more=${{ id: "mp-more", items: more }} />
    <div class="model-page" id="model-page" data-model=${m.id}>
      <${Workspace} key=${m.sid} m=${m} variant=${variant} />
    </div>`;
}

function Workspace({ m, variant }) {
  const [off, setOff] = useState(() => new Set());
  const [flags, setFlags] = useState(() => Object.fromEntries(m.options.map((o) => [o.key, o.default])));
  const [stats, setStats] = useState(m.stats);
  const [result, setResult] = useState(m.enhanced ? { ...m.enhanced, rev: 1 } : null);
  const [resultKey, setResultKey] = useState(null);
  const [which, setWhich] = useState(m.enhanced ? "enhanced" : "original");
  const args = meshArgs(m, off, flags);
  // the engine keeps a few models open; if this one was let go, open it again and retry
  const call = async (cmd, a) => {
    try { return await api(cmd, a); } catch (e) {
      if (!/was closed/.test(msg(e))) throw e;
      await api("model_open", { id: m.id, variant });
      return api(cmd, a);
    }
  };
  useEffect(() => {
    if (args.key === meshArgs(m, new Set(), Object.fromEntries(m.options.map((o) => [o.key, o.default]))).key) { setStats(m.stats); return; }
    let live = true;
    call("model_stats", { sid: m.sid, ...args.json }).then((s) => { if (live) setStats(s); }, () => {});
    return () => { live = false; };
  }, [args.key]);
  const box = useRef(null);
  const room = useWidth(box);
  const paneW = Math.max(260, Math.min(520, ui.get().inspectorWidth || 360));
  const crowded = !!room && room - paneW - RAIL < STAGE;
  const enhanced = (r) => { setResult({ ...r, rev: (result?.rev || 0) + 1 }); setResultKey(args.key); setWhich("enhanced"); };
  return html`<div class="model-workspace" ref=${box}>
    <section class="workspace-viewing-area">
      <${Stage} m=${m} variant=${variant} which=${which} setWhich=${setWhich} args=${args} result=${result} stats=${stats} />
    </section>
    <${Inspector} page="model" crowded=${crowded} label="Panels" tabs=${[
      { id: "model", label: "Model", icon: "box", body: html`<${ModelTab} m=${m} off=${off} setOff=${setOff} flags=${flags} setFlags=${setFlags} stats=${stats} />` },
      { id: "textures", label: "Textures", icon: "image", body: html`<${TexturesTab} m=${m} />` },
      { id: "enhance", label: "Generate", icon: "layers", body: html`<${EnhanceTab} m=${m} args=${args} stats=${stats} result=${result} stale=${!!result && !!resultKey && resultKey !== args.key}
        call=${call} onDone=${enhanced} />` },
      { id: "export", label: "Export", icon: "download", body: html`<${ExportTab} m=${m} args=${args} result=${result} which=${which} call=${call} />` },
    ]} />
  </div>`;
}

/** The 3D view: the model with its textures, clay or wireframe, its size, and the views. */
function Stage({ m, variant, which, setWhich, args, result, stats }) {
  const canvas = useRef(null);
  const viewer = useRef(null);
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
  const key = JSON.stringify([which, which === "enhanced" ? result?.id || result?.rev : args.key]);
  useEffect(() => {
    let live = true;
    (async () => {
      let built = kept.current.get(key);
      kept.current.delete(key);
      if (!built) {
        setState((s) => ({ ...s, loading: true, error: null }));
        const url = () => binUrl("model_mesh", { sid: m.sid, which, uv: 1, ...args.url });
        let res = await fetch(url());
        if (res.status === 400) {
          const text = await res.text();
          if (!/was closed/.test(text)) throw new Error(text);
          await api("model_open", { id: m.id, variant });
          if (which === "enhanced") { setWhich("original"); return; }
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
      const size = viewer.current.show(built, { textures: view.shading === "textured" ? textures : null, wireframe: view.shading === "wire", keepView: shown.current });
      viewer.current.setEdges(view.edges);
      shown.current = true;
      setState({ loading: false, error: null, size, key });
    })().catch((e) => { if (live) setState({ loading: false, error: msg(e), size: null, key }); });
    return () => { live = false; };
  }, [key, view.shading]);
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
        <span class="stage-what" id="stage-size" title="Width × depth × height, as exported in millimetres">${size?.triangles ? html`${sizeText([size.x, size.y, size.z])}<span class="muted"> · ${plural(size.triangles, "triangle")}</span>` : sizeText(stats?.size_mm)}</span>
        <span class="stage-tools">
          ${seg("show-which", "Show", [["original", "Original"], ["enhanced", "Enhanced", result ? "The enhanced model" : "Generate it first (Generate tab)", !result]], which, setWhich)}
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

/** Textures: every texture found for the model, a bigger look at one with Save as PNG, and which each material uses. */
function TexturesTab({ m }) {
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
    <section class="insp-section" id="materials"><h3>Materials</h3>${m.materials.map((mat, i) => html`<div class="material" key=${i} title=${mat.details.join("\n")}>
      <p class="material-name">${mat.name}${mat.guessed ? html` <small class="muted" title="Matched by the textures' names: the material didn't say">(guessed)</small>` : null}</p>
      <dl class="insp-dl">
        <dt>Colour</dt><dd>${nameOf(mat.albedo) || html`<span class="muted">none</span>`}</dd>
        <dt>Normals</dt><dd>${nameOf(mat.normal) || html`<span class="muted">none</span>`}</dd>
        ${mat.height ? html`<dt>Height</dt><dd>${nameOf(mat.height)}</dd>` : null}</dl></div>`)}</section>
  </div>`;
}

/** A number box saved to the Generate settings. */
function NumberField({ id, label, value, step, min, max, unit, title, onChange }) {
  return html`<label class="field-block" title=${title}><span>${label}</span>
    <span class="field-row"><input type="number" id=${id} value=${value} step=${step} min=${min} max=${max}
      onChange=${(e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) onChange(v); }} />${unit ? html`<span class="muted unit">${unit}</span>` : null}</span></label>`;
}

/** Generate: even out the vertices to the count chosen, then displace them by the detail in the textures. */
function EnhanceTab({ m, args, stats, result, stale, call, onDone }) {
  const saved = useStore(ui, (s) => s.enhance);
  const s = { ...ENHANCE, ...(saved || {}) };
  const set = (patch) => setPref({ enhance: { ...s, ...patch } });
  const [over, setOver] = useState({});
  const [strengths, setStrengthState] = useState({}); // material -> strength multiplier
  const strengthsNow = useRef({}); // the same, readable at once by a click that follows the change
  const setStrengths = (v) => { strengthsNow.current = v; setStrengthState(v); };
  const [ai, setAi] = useState(null); // { folder, models, available } once asked for
  const [run, setRun] = useState(null); // { progress, message } while running
  const [error, setError] = useState(null);
  const methods = info().methods || [];
  const have = m.textures.map((t) => t.key);
  const detailOf = (mat, i) => (i in over ? over[i] : (s.source === "height" ? mat.height : mat.normal) || "");
  const spacing = spacingMm(stats.area_m2, s.target_vertices);
  const wantsAi = s.source === "normal" && (s.upsample === "ai" || s.deblock === "ai");
  const loadAi = () => api("ai_models").then(setAi, (e) => setError(msg(e)));
  useEffect(() => { if (wantsAi && !ai) loadAi(); }, [wantsAi]);
  const addModel = async () => {
    const path = await pickFile("Add an AI model", ["ONNX models (*.onnx)"]);
    if (path) { try { setAi(await api("ai_model_add", { path })); } catch (e) { setError(msg(e)); } }
  };
  const modelSelect = (id, key, title) => html`<label class="field-block" title=${title}><span>${key === "ai_model" ? "Upscaling model" : "Deblocking model"}</span>
    <select id=${id} value=${s[key]} onChange=${(e) => set({ [key]: e.target.value })}>
      <option value="">Choose…</option>${(ai?.models || []).map((x) => html`<option key=${x} value=${x}>${x}</option>`)}</select></label>`;
  const go = async () => {
    setError(null);
    setRun({ progress: 0, message: "Starting" });
    try {
      const overrides = Object.fromEntries(m.materials.map((mat, i) => [i, detailOf(mat, i)]));
      // the settings as stored now: a number typed and then Generate clicked straight away is saved
      // (on blur) just before the click, before this tab has drawn it again
      const now = { ...ENHANCE, ...(ui.get().enhance || {}) };
      const { job } = await call("enhance", { sid: m.sid, ...args.json, settings: { ...now, max_displacement_mm: now.max_displacement_mm || null,
        texture_overrides: overrides, material_strength: strengthsNow.current } });
      const r = await followJob(job, `Generating ${m.entry?.label || variantName(m.variant) || m.name}`, (j) => setRun({ progress: j.progress, message: j.message }));
      onDone(r);
      toast(r.kept ? "Already generated with these settings" : `Generated: ${plural(r.stats.vertices, "vertex", "vertices")}`);
    } catch (e) { setError(msg(e)); }
    setRun(null);
  };
  return html`<div class="tab-pad" id="enhance-tab">
    <section class="insp-section"><h3>1 · Even out the vertices</h3>
      <${NumberField} id="target-vertices" label="Vertices" value=${s.target_vertices} step=${250000} min=${1000} max=${50000000}
        title="How many vertices the enhanced model gets. More keeps finer texture detail and makes a bigger file." onChange=${(v) => set({ target_vertices: Math.round(v) })} />
      <small class="muted" id="spacing">About ${spacing ? `${spacing < 1 ? spacing.toFixed(2) : spacing.toFixed(1)} mm` : "–"} between vertices (now ${num(stats.vertices)} vertices).</small>
      <label class="field-block"><span>Method</span><select id="method" value=${s.method} onChange=${(e) => set({ method: e.target.value })}>
        ${methods.map((x) => html`<option key=${x.id} value=${x.id} disabled=${!x.available}>${x.label}${x.available ? "" : ` (${x.missing || "not installed"})`}</option>`)}</select></label>
      <label class="check-row" title="New vertices follow the curve the model's shading describes, so low-poly cylinders and blades print round instead of faceted. Flat-shaded parts stay flat.">
        <input type="checkbox" id="smooth" checked=${s.smooth} onChange=${(e) => set({ smooth: e.target.checked })} /><span>Round off curved surfaces</span></label>
    </section>
    <section class="insp-section"><h3>2 · Raise the texture detail</h3>
      <label class="field-block"><span>From</span><select id="source" value=${s.source} onChange=${(e) => { setOver({}); set({ source: e.target.value }); }}>
        <option value="normal">Normal maps (turned into height)</option><option value="height">Height or grayscale maps</option><option value="none">Nothing (even out only)</option></select></label>
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
        <label class="field-block" title="Block compression leaves a faint 4-pixel grid. Smoothing melts it but also softens the finest detail; an AI model can remove it more cleanly."><span>Compression clean-up</span>
          <select id="deblock" value=${s.deblock} onChange=${(e) => set({ deblock: e.target.value })}>
            <option value="off">None</option><option value="smooth">Smooth it out</option><option value="ai">AI model</option></select></label>
        <label class="field-block" title="More pixels in the normal map when the model's vertices are closer together than its pixels. Guided keeps edges where the colour texture has them; AI models invent plausible fine detail."><span>Upsample the normal map</span>
          <select id="upsample" value=${s.upsample} onChange=${(e) => set({ upsample: e.target.value })}>
            <option value="off">No</option><option value="guided">Guided by the colour texture</option><option value="ai">AI model</option></select></label>
        ${wantsAi ? html`<div class="field-block" id="ai-models">
          ${ai && !ai.available ? html`<p class="insp-note">AI models need ONNX Runtime, which this copy of the app doesn't have.</p>` : null}
          ${s.upsample === "ai" ? modelSelect("ai-model", "ai_model", "A 2x or 4x model for normal maps, such as 4x-Normal-RG0 (CC0) from openmodeldb.info, in .onnx form.") : null}
          ${s.deblock === "ai" ? modelSelect("deblock-model", "deblock_model", "A 1x model that removes DDS block artifacts, in .onnx form.") : null}
          <div class="insp-actions"><button type="button" class="ghost" id="ai-model-add" onClick=${addModel}>Add a model…</button>
            ${ai ? html`<button type="button" class="ghost" id="ai-models-folder" onClick=${() => showInFolder(ai.folder)}>Open models folder</button>` : null}</div>
          <small class="muted">Models aren't included; check each one's licence before sharing prints made with it.</small></div>` : null}
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
      </details>` : null}
    </section>
    <div class="insp-actions enhance-go">
      <button type="button" class="primary" id="enhance-run" disabled=${!!run || !stats.triangles} onClick=${go}>${run ? "Generating…" : result ? "Generate again" : "Generate"}</button>
    </div>
    ${run ? html`<div class="job-progress" id="enhance-progress"><progress max="1" value=${run.progress}></progress><small class="muted">${run.message}</small></div>` : null}
    ${error ? html`<p class="warn-note" role="alert" id="enhance-error">${error}</p>` : null}
    ${result ? html`<section class="insp-section" id="enhance-result"><h3>Enhanced</h3>
      ${stale ? html`<p class="insp-note">Made with other parts or options than those chosen now: generate again to include the change.</p>` : null}
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

/** Export: the enhanced (or original) model as STL, OBJ, PLY or GLB, in a folder. */
function ExportTab({ m, args, result, which: shownWhich, call }) {
  const saved = useStore(ui, (s) => s.exportOpts);
  const o = { ...EXPORT, ...(saved || {}) };
  const set = (patch) => setPref({ exportOpts: { ...o, ...patch } });
  const [which, setWhich] = useState(result ? "enhanced" : "original");
  useLayoutEffect(() => { setWhich(result ? shownWhich : "original"); }, [result?.rev]);
  const [name, setName] = useState("");
  const [folder, setFolder] = useState(o.folder);
  const [fallback, setFallback] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { api("settings_get").then((r) => setFallback(r.export_dir), () => {}); }, []);
  const base = variantName(m.variant) || m.name;
  const auto = `${base}${which === "enhanced" ? "_enhanced" : ""}`;
  const formats = info().formats || [];
  const textured = o.format === ".obj" || o.format === ".glb";
  const browse = async () => { const p = await pickFolder("Export to", folder || fallback); if (p) { setFolder(p); set({ folder: p }); } };
  const go = async () => {
    setBusy(true); setError(null); setDone(null);
    try {
      const { job } = await call("export", { sid: m.sid, which, ...args.json, format: o.format, units: o.units, textures: o.textures, folder: folder || null, name: name.trim() || auto });
      const r = await followJob(job, `Exporting ${name.trim() || auto}${o.format}`);
      setDone(r);
    } catch (e) { setError(msg(e)); }
    setBusy(false);
  };
  return html`<div class="tab-pad" id="export-tab">
    <div class="field-block"><span>Model</span><div class="seg" role="group" aria-label="Which model" id="export-which">
      <button type="button" aria-pressed=${which === "enhanced" ? "true" : "false"} disabled=${!result} title=${result ? "The enhanced model" : "Generate it first (Generate tab)"} onClick=${() => setWhich("enhanced")}>Enhanced</button>
      <button type="button" aria-pressed=${which === "original" ? "true" : "false"} onClick=${() => setWhich("original")}>Original</button></div>
      ${!result ? html`<small class="muted">Generate it first (Generate tab) to export the detailed version.</small>` : null}</div>
    <label class="field-block"><span>Format</span><select id="export-format" value=${o.format} onChange=${(e) => set({ format: e.target.value })}>
      ${formats.map((f) => html`<option key=${f.ext} value=${f.ext}>${f.label} (${f.ext})</option>`)}</select></label>
    <label class="field-block"><span>Units</span><select id="export-units" value=${o.units} onChange=${(e) => set({ units: e.target.value })}>
      ${UNITS.map(([k, l]) => html`<option key=${k} value=${k}>${l}</option>`)}</select></label>
    <label class="check-row"><input type="checkbox" id="export-textures" checked=${o.textures && textured} disabled=${!textured} onChange=${(e) => set({ textures: e.target.checked })} />
      <span>Include the colour textures${textured ? "" : " (OBJ and GLB only)"}</span></label>
    <label class="field-block"><span>Name</span><input type="text" id="export-name" value=${name} placeholder=${auto} onInput=${(e) => setName(e.target.value)} /></label>
    <div class="field-block"><span>Folder</span><div class="field-row">
      <input type="text" id="export-folder" value=${folder} placeholder=${fallback} onInput=${(e) => setFolder(e.target.value)} onChange=${(e) => set({ folder: e.target.value.trim() })} />
      ${info().dialogs ? html`<button type="button" class="ghost" id="export-browse" onClick=${browse}>Browse…</button>` : null}</div></div>
    <div class="insp-actions"><button type="button" class="primary" id="export-run" disabled=${busy} onClick=${go}>${Icon.download(15)} ${busy ? "Exporting…" : "Export"}</button></div>
    ${error ? html`<p class="warn-note" role="alert" id="export-error">${error}</p>` : null}
    ${done ? html`<div class="export-done" id="export-done" role="status"><p><b>Saved</b> ${done.path.split(/[\\/]/).pop()} <span class="muted">· ${bytes(done.bytes)}</span></p>
      <p class="note-path muted">${done.path}</p>
      <div class="insp-actions"><button type="button" class="ghost" onClick=${() => showInFolder(done.path)}>${Icon.folder(14)} Show in folder</button></div></div>` : null}
    <p class="muted export-note">STL and PLY are written Z-up, as slicers expect; OBJ and GLB Y-up. Game models often aren't watertight, so check them in your slicer before printing.</p>
  </div>`;
}
