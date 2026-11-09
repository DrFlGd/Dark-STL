// The model page's Clean tab: what is taken off the model, and how it stands, before the Mesh step. Each step
// goes in a list kept per model, and the engine does the list again on the model as built and posed every time
// it is built (clean.py), so any step can be taken away later. Steps: parts left out; parts hidden inside
// others; faces hidden inside other parts (deeper than a depth); thin sheets (capes, linings, cloth drawn from
// both sides), listed to pick from and highlighted in the 3D view; faces picked in the 3D view, the selection
// spreading to creases; small holes filled, duplicate two-sided faces dropped, vertices welded, flipped faces
// turned; and the transform (print height or scale, turns, mirror, move, standing on the floor), always last.
// Faces are recorded by their part and their place in it, so a step still finds them with other parts left out.
import { html, useState, useEffect, useRef } from "../lib/html.js";
import { Icon } from "./icons.js";
import { plural, toast } from "./api.js";
import { Tip, TipButton, NextStep, NumberField, JobProgress, Slider, ranTab } from "./modelpage.js";

const msg = (e) => String(e?.message || e);
const num = (n) => Math.round(n).toLocaleString();
const mm = (v) => (Math.abs(v) >= 100 ? String(Math.round(v)) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));
const sizeText = (s) => (s ? `${mm(s[0])} × ${mm(s[1])} × ${mm(s[2])} mm` : "");

/** The transform step as a new one starts. */
export const TRANSFORM = { op: "transform", rotate: [0, 0, 0], move: [0, 0, 0], mirror: [false, false, false], scale: 1, height_mm: 0, floor: true };
/** The colours the 3D view marks faces in: picked, found, sheets ticked, and the sheet under the pointer. */
export const MARKS = { selection: "#e0913a", found: "#c04fd0", sheets: "#c04fd0", hover: "#2bb3c0" };
const KINDS = {
  open: ["Open sheet", "One layer with open edges, like a cape, a skirt or a sleeve's lining"],
  "two-sided": ["Two-sided sheet", "Two layers back to back with nothing between them: cloth drawn from both sides"],
  thin: ["Thin layer", "A solid thinner than the thickness above: a flap of cloth, a lining, a strap"],
};
const AXIS = ["X", "Y", "Z"];

/** The page's name for a clean-up list: a hash of it, sent with the list and put in the addresses of the meshes
 *  shown (an address can't hold the list itself). "" for no list. */
export function cleanId(list) {
  if (!list?.length) return "";
  const text = JSON.stringify(list);
  let h1 = 0x811c9dc5, h2 = 0x01000193 ^ 0x5bd1e995;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193);
    h2 = Math.imul(h2 ^ c, 0x5bd1e995) ^ (h2 >>> 15);
  }
  return (h1 >>> 0).toString(16).padStart(8, "0") + (h2 >>> 0).toString(16).padStart(8, "0");
}

/** Sets of face ids (as steps keep them: part -> [start, count, ...]) as one. */
function joinIds(sets) {
  const out = {};
  for (const s of sets) for (const [p, runs] of Object.entries(s || {})) (out[p] ||= []).push(...runs);
  return out;
}

/** What a step is, as the list names it until the engine has done it. */
function opLabel(op) {
  if (op.op === "remove") return { hidden_parts: "Parts hidden inside others", hidden_faces: "Faces hidden inside other parts",
    sheets: "Thin sheets", selection: "Faces selected in the 3D view" }[op.kind] || "Faces removed";
  return { fill_holes: "Small holes filled", dedupe: "Duplicate two-sided sheets dropped", weld: "Vertices welded",
    fix_normals: "Flipped faces turned the right way", transform: "Transform" }[op.op] || op.op;
}

/** Clean: the steps taken so far, and the tools that add them. */
export function CleanTab({ m, args, stats, run, setRun, call, follow, clean, setClean, off, setOff, bounds, viewer, which, setWhich }) {
  const [error, setError] = useState(null);
  const [depth, setDepth] = useState(1);
  const [thickness, setThickness] = useState(0.5);
  const [angle, setAngle] = useState(30);
  const [holes, setHoles] = useState(5);
  const [weld, setWeld] = useState(0.01);
  const [found, setFound] = useState(null); // hidden faces found: { key, faces, count, shown, depth_mm }
  const [sheets, setSheets] = useState(null); // thin sheets found: { key, thickness_mm, regions, more }
  const [ticked, setTicked] = useState(() => new Set()); // the sheets ticked, by index
  const [hover, setHover] = useState(null); // the sheet under the pointer, by index
  const [picking, setPicking] = useState(false);
  const [sel, setSel] = useState(null); // faces picked in the 3D view: { key, shown: [triangles of the mesh shown] }
  // what was found or picked counts only on the model it was found on (the parts, options, pose and steps as they were)
  const cur = (x) => (x && x.key === args.key ? x : null);
  const rows = !clean.length ? [] : stats?.key === args.key && stats.clean ? stats.clean.rows : null;
  const shownOk = !!bounds && bounds.which === "original" && bounds.args === args.key;

  // the 3D view: found, ticked, hovered and picked faces drawn over the model, while it shows the model cleaned so far
  const nowFound = cur(found), nowSheets = cur(sheets), nowSel = cur(sel);
  useEffect(() => {
    const v = viewer.current;
    if (!v) return;
    const on = (x) => (shownOk ? x : null);
    v.setMark("found", on(nowFound?.shown), MARKS.found);
    v.setMark("sheets", on(nowSheets ? nowSheets.regions.filter((r) => ticked.has(r.index)).flatMap((r) => r.shown) : null), MARKS.sheets);
    v.setMark("hover", on(nowSheets && hover != null ? nowSheets.regions[hover]?.shown : null), MARKS.hover);
    v.setMark("selection", on(nowSel?.shown), MARKS.selection);
  }, [bounds, args.key, nowFound, nowSheets, ticked, hover, nowSel, shownOk]);
  useEffect(() => () => {
    const v = viewer.current;
    if (v) { v.clearMarks(); v.picking = null; v.redraw(); }
  }, []);

  // a click on the model while picking: the faces it reaches, added (Shift), taken away (Alt) or picked alone
  const pickNow = useRef(null);
  pickNow.current = async (tri, mods) => {
    const key = args.key;
    try {
      const r = await call("clean_select", { sid: m.sid, ...args.json, face: tri, angle });
      setSel((s) => {
        const had = s && s.key === key ? s.shown : [];
        let next = r.shown;
        if (mods.take) { const out = new Set(r.shown); next = had.filter((t) => !out.has(t)); }
        else if (mods.add) next = [...new Set([...had, ...r.shown])].sort((a, b) => a - b);
        return { key, shown: next };
      });
    } catch (e) { setError(msg(e)); }
  };
  useEffect(() => {
    const v = viewer.current;
    if (v) v.picking = picking ? (tri, mods) => pickNow.current(tri, mods) : null;
  }, [picking, viewer.current]);
  const toOriginal = () => { if (which !== "original") setWhich("original"); };

  /** A step added to the list: before the transform, which stays last. */
  const add = (op) => {
    setError(null);
    const t = clean.findIndex((o) => o.op === "transform");
    setClean(t < 0 ? [...clean, op] : [...clean.slice(0, t), op, ...clean.slice(t)]);
    toOriginal();
    ranTab("clean");
  };
  const drop = (i) => setClean(clean.filter((_, j) => j !== i));
  const find = async (what, extra, label) => {
    setError(null);
    toOriginal();
    const key = args.key;
    try {
      const r = await follow("clean", () => call("clean_find", { sid: m.sid, ...args.json, find: what, ...extra }), label);
      return { ...r, key };
    } catch (e) {
      if (e.stopped) toast("Stopped looking"); else setError(msg(e));
      return null;
    }
  };
  const hiddenParts = async () => {
    const r = await find("hidden_parts", {}, "Looking for parts hidden inside others");
    if (!r) return;
    if (!r.count) { toast("No part is hidden inside another"); return; }
    add({ op: "remove", kind: "hidden_parts", faces: r.faces, parts: r.parts });
    toast(`Removed ${plural(r.parts, "part")} hidden inside others`);
  };
  const hiddenFaces = async () => {
    const r = await find("hidden_faces", { depth_mm: depth }, "Looking for hidden faces");
    if (!r) return;
    setFound(r);
    if (!r.count) toast("No faces are hidden that deep");
  };
  const findSheets = async () => {
    const r = await find("sheets", { thickness_mm: thickness }, "Looking for thin sheets");
    if (!r) return;
    setSheets(r); setTicked(new Set()); setHover(null);
    if (!r.regions.length) toast("No thin sheets found");
  };
  const removeSheets = () => {
    const picked = nowSheets.regions.filter((r) => ticked.has(r.index));
    if (!picked.length) return;
    add({ op: "remove", kind: "sheets", faces: joinIds(picked.map((r) => r.faces_ids)), regions: picked.length,
      thickness_mm: nowSheets.thickness_mm, sheet_kinds: [...new Set(picked.map((r) => r.kind))] });
    setSheets(null); setTicked(new Set()); setHover(null);
  };
  const tick = (i, on) => { const next = new Set(ticked); on ? next.add(i) : next.delete(i); setTicked(next); };
  const removeSelection = async () => {
    if (!nowSel?.shown.length) return;
    try {
      const r = await call("clean_ids", { sid: m.sid, ...args.json, shown: nowSel.shown });
      add({ op: "remove", kind: "selection", faces: r.faces });
      setSel(null);
    } catch (e) { setError(msg(e)); }
  };
  const togglePick = () => { if (!picking) toOriginal(); setPicking(!picking); };
  const togglePart = (i) => { const next = new Set(off); next.has(i) ? next.delete(i) : next.add(i); setOff(next); };

  // the transform: one step, changed in place, always the last
  const tAt = clean.findIndex((o) => o.op === "transform");
  const t = tAt >= 0 ? { ...TRANSFORM, ...clean[tAt] } : TRANSFORM;
  const setT = (patch) => setClean([...clean.filter((o) => o.op !== "transform"), { ...t, ...patch }]);
  const resetT = () => setClean(clean.filter((o) => o.op !== "transform"));
  const turn = (axis) => setT({ rotate: t.rotate.map((r, i) => (i === axis ? (((r + 90) % 360) + 360) % 360 : r)) });
  const scale = stats?.key === args.key && stats.clean ? stats.clean.scale || 1 : 1;
  const game = stats?.key === args.key && stats.clean ? stats.clean.game_size_mm : null;
  const busy = !!run;
  const finding = run?.kind === "clean";

  return html`<div class="tab-pad" id="clean-tab">
    <section class="insp-section"><h3>Clean up the model <${TipButton} tab="clean" /></h3>
      <${Tip} tab="clean" summary="Take off what shouldn't be printed and set the model's size, before its vertices are evened out.">Each step
        goes in the list below and is done again whenever the model is built, with the parts, options and pose chosen, so a step
        can be taken away later. Mesh, Displace, Finish, Cut and Export all work on the cleaned model.<//>
      ${which !== "original" ? html`<p class="insp-note" id="clean-which">The 3D view shows the ${which} model. Clean works on the model
        before the Mesh step: <button type="button" class="link-btn" id="clean-show-original" onClick=${toOriginal}>show it</button>
        to see what each step does. The enhanced model is made again from the cleaned one when you generate it.</p>` : null}
      ${clean.length ? html`<ul class="ls-list clean-steps" id="clean-steps" aria-label="Clean-up steps">${clean.map((op, i) => {
        const row = rows?.[i];
        return html`<li key=${i} data-step=${i} data-op=${op.op} data-kind=${op.kind || null}>
          <span><b class="clean-step-name">${row?.label || opLabel(op)}</b>
            <small class=${`muted clean-step-text${row && /^nothing/.test(row.text) ? " warn-text" : ""}`}>${row ? row.text : stats?.key === args.key && stats.error ? "" : "working it out…"}</small></span>
          <button type="button" class="ghost compact" title="Take this step away: what it removed comes back" aria-label=${`Take away step ${i + 1}`}
            disabled=${busy} onClick=${() => drop(i)}>${Icon.trash(14)}</button></li>`;
      })}</ul>
        ${clean.length > 1 ? html`<div class="insp-actions"><button type="button" class="ghost compact" id="clean-clear" disabled=${busy}
          title="Take every step away: the model as the game has it" onClick=${() => setClean([])}>Take all away</button></div>` : null}`
        : html`<p class="muted" id="clean-none">No steps yet: the model is as the game has it.</p>`}
      ${stats?.key === args.key && stats.error ? html`<p class="warn-note" role="alert" id="clean-list-error">${stats.error}</p>` : null}
      <${JobProgress} run=${finding ? run : null} setRun=${setRun} id="clean-progress" />
      ${error ? html`<p class="warn-note" role="alert" id="clean-error">${error}</p>` : null}
    </section>

    <section class="insp-section" id="clean-parts"><h3>Parts</h3>
      <div class="part-list">${m.parts.map((p, i) => html`<label class="check-row part-row" key=${i} data-part=${i}
          title="Untick to leave this part out of the model: shown, generated and exported without it">
        <input type="checkbox" checked=${!off.has(i)} onChange=${() => togglePart(i)} />
        <span class="part-name">${p.name}<small class="muted">${m.materials[p.material]?.name || "?"} · ${plural(p.triangles, "triangle")}</small></span></label>`)}</div>
      <div class="insp-actions">
        ${m.parts.length > 2 ? html`<button type="button" class="ghost compact" disabled=${!off.size} title="Every part in" onClick=${() => setOff(new Set())}>All</button>
          <button type="button" class="ghost compact" title="Every part out" onClick=${() => setOff(new Set(m.parts.map((_, i) => i)))}>None</button>` : null}
        <button type="button" class="ghost compact" id="clean-hidden-parts" disabled=${busy || !stats?.triangles}
          title="Finds whole pieces of the model that sit inside other parts (a body under armour, teeth inside a head) and removes them"
          onClick=${hiddenParts}>Remove parts hidden inside others</button></div>
    </section>

    <section class="insp-section" id="clean-hidden"><h3>Hidden faces</h3>
      <p class="muted small">Surfaces buried inside other parts (a body's chest under a breastplate) can't be seen and print as lumps inside.</p>
      <${Slider} id="clean-depth" label="Deeper than" value=${depth} min=${0} max=${10} step=${0.25} text=${(v) => `${v} mm`}
        hint="Faces at least this far inside another part are found; 0 finds every face inside one. Keep some depth where parts only just overlap."
        onChange=${setDepth} />
      <div class="insp-actions"><button type="button" class="ghost compact" id="clean-find-hidden" disabled=${busy || !stats?.triangles}
        title="Look for faces inside other parts, deeper than this; they are shown in the 3D view before anything is removed" onClick=${hiddenFaces}>Find hidden faces</button>
        ${nowFound?.count ? html`<button type="button" class="primary compact" id="clean-remove-hidden" disabled=${busy}
          title="Add a step that removes the faces shown"
          onClick=${() => { add({ op: "remove", kind: "hidden_faces", faces: nowFound.faces, depth_mm: nowFound.depth_mm }); setFound(null); }}>Remove ${plural(nowFound.count, "face")}</button>
          <button type="button" class="ghost compact" id="clean-found-clear" title="Stop showing them" onClick=${() => setFound(null)}>Clear</button>` : null}</div>
      ${nowFound ? html`<small class="muted" id="clean-found-count">${nowFound.count ? `${plural(nowFound.count, "face")} found, shown in purple.` : "None found that deep."}</small>` : null}
    </section>

    <section class="insp-section" id="clean-sheets"><h3>Thin sheets</h3>
      <p class="muted small">Cloth with little or no thickness (capes, linings, sleeves drawn from both sides), even where it is part of a bigger piece.
        It can't be printed as it is: find it here, tick what to take off, and remove it.</p>
      <${Slider} id="clean-thickness" label="Thinner than" value=${thickness} min=${0.05} max=${5} step=${0.05} text=${(v) => `${v} mm`}
        hint="Surfaces with another surface closer behind them than this count as a sheet. Open single layers always count."
        onChange=${setThickness} />
      <div class="insp-actions"><button type="button" class="ghost compact" id="clean-find-sheets" disabled=${busy || !stats?.triangles}
        title="List the thin sheets on the model, biggest first" onClick=${findSheets}>Find thin sheets</button></div>
      ${nowSheets ? (nowSheets.regions.length ? html`
        <div class="sheet-head"><label class="check-row" title="Tick or untick every sheet listed">
          <input type="checkbox" id="clean-sheets-all" checked=${ticked.size === nowSheets.regions.length}
            onChange=${(e) => setTicked(e.target.checked ? new Set(nowSheets.regions.map((r) => r.index)) : new Set())} />
          <span>${plural(nowSheets.regions.length, "sheet")}${nowSheets.more ? html` <small class="muted">and ${nowSheets.more} smaller not listed</small>` : null}</span></label></div>
        <ul class="ls-list sheet-list" id="clean-sheet-list" onMouseLeave=${() => setHover(null)}>${nowSheets.regions.map((r) => html`<li key=${r.index}
            data-region=${r.index} data-sheet-kind=${r.kind} class=${hover === r.index ? "hover" : ""} onMouseEnter=${() => setHover(r.index)}>
          <label class="check-row" title=${`${KINDS[r.kind]?.[1] || ""}. Point at it to see it in the 3D view (in blue); ticked sheets show in purple.`}>
            <input type="checkbox" checked=${ticked.has(r.index)} onFocus=${() => setHover(r.index)} onBlur=${() => setHover(null)}
              onChange=${(e) => tick(r.index, e.target.checked)} />
            <span>${KINDS[r.kind]?.[0] || r.kind}<small class="muted">${r.area_cm2 >= 10 ? num(r.area_cm2) : r.area_cm2.toFixed(2)} cm² · ${plural(r.faces, "face")}${r.kind === "open" ? "" : `, about ${r.thickness_mm.toFixed(2)} mm`}</small></span></label></li>`)}</ul>
        <div class="insp-actions"><button type="button" class="primary compact" id="clean-remove-sheets" disabled=${busy || !ticked.size}
          title="Add a step that removes the sheets ticked" onClick=${removeSheets}>${ticked.size ? `Remove ${plural(ticked.size, "sheet")}` : "Tick sheets to remove"}</button></div>`
        : html`<small class="muted" id="clean-sheets-none">None found thinner than ${nowSheets.thickness_mm} mm.</small>`) : null}
    </section>

    <section class="insp-section" id="clean-select"><h3>Pick and remove</h3>
      <p class="muted small">Click the model to pick faces: the pick spreads over the surface until it meets a crease sharper than the angle.
        Shift+click adds to it, Alt+click takes away.</p>
      <${Slider} id="clean-angle" label="Stop at creases sharper than" value=${angle} min=${1} max=${90} step=${1} text=${(v) => `${v}°`}
        hint="Smaller picks less: only the flat or gently curved surface clicked." onChange=${setAngle} />
      <div class="insp-actions">
        <button type="button" class="ghost compact" id="clean-pick" aria-pressed=${picking ? "true" : "false"} disabled=${!stats?.triangles}
          title=${picking ? "Stop picking: clicks turn the view again" : "Pick faces by clicking the model in the 3D view"} onClick=${togglePick}>${picking ? "Picking: click the model" : "Pick faces"}</button>
        <button type="button" class="primary compact" id="clean-remove-selection" disabled=${busy || !nowSel?.shown.length}
          title="Add a step that removes the faces picked" onClick=${removeSelection}>Remove picked faces</button>
        <button type="button" class="ghost compact" id="clean-selection-clear" disabled=${!nowSel?.shown.length} title="Pick nothing" onClick=${() => setSel(null)}>Clear</button></div>
      <small class="muted" id="clean-selection-count">${nowSel?.shown.length ? `${plural(nowSel.shown.length, "face")} picked, shown in orange.` : "Nothing picked."}</small>
    </section>

    <section class="insp-section" id="clean-repair"><h3>Repair</h3>
      <div class="clean-repair-row"><${NumberField} id="clean-holes-mm" label="Fill holes up to" value=${holes} step=${0.5} min=${0.01} max=${1000} unit="mm across"
          title="Closes small openings in the surface, such as gaps between eyelids or seams left open; bigger openings (a cape's edge) stay open."
          onChange=${setHoles} />
        <button type="button" class="ghost compact" id="clean-add-holes" disabled=${busy} title="Add a step that fills these holes"
          onClick=${() => add({ op: "fill_holes", size_mm: holes })}>${Icon.plus(13)} Fill</button></div>
      <div class="clean-repair-row"><${NumberField} id="clean-weld-mm" label="Weld vertices closer than" value=${weld} step=${0.01} min=${0} max=${100} unit="mm"
          title="Joins vertices this close into one, so edges cut apart by texture seams or split normals meet again (for watertight checks and hole filling)."
          onChange=${setWeld} />
        <button type="button" class="ghost compact" id="clean-add-weld" disabled=${busy} title="Add a step that welds these vertices"
          onClick=${() => add({ op: "weld", distance_mm: weld })}>${Icon.plus(13)} Weld</button></div>
      <div class="insp-actions">
        <button type="button" class="ghost compact" id="clean-add-dedupe" disabled=${busy}
          title="Where the same triangle is there twice (a sheet drawn from both sides with the same vertices), one is kept"
          onClick=${() => add({ op: "dedupe", distance_mm: 0.01 })}>Drop duplicate faces</button>
        <button type="button" class="ghost compact" id="clean-add-normals" disabled=${busy}
          title="Turns over faces that face the other way from the surface around them (outwards, on a closed surface)"
          onClick=${() => add({ op: "fix_normals" })}>Turn flipped faces</button></div>
    </section>

    <section class="insp-section" id="clean-transform"><h3>Size and placement</h3>
      <${NumberField} id="clean-height" label="Print height" value=${t.height_mm || 0} step=${5} min=${0} max=${100000} unit="mm (0: the game's size)"
        title="Scales the model so it stands this tall (after the turns below). Displace, Finish, Cut and Export then work in these millimetres."
        onChange=${(v) => setT({ height_mm: Math.max(0, v) })} />
      ${t.height_mm ? null : html`<${NumberField} id="clean-scale" label="Scale" value=${Math.round((t.scale || 1) * 10000) / 100} step=${5} min=${0.01} max=${100000} unit="% of the game's size"
        title="One game metre is 1000 mm at 100%." onChange=${(v) => setT({ scale: Math.max(0.0001, v / 100) })} />`}
      <div class="field-block"><span>Turn</span><div class="field-row clean-turn" id="clean-turn">
        ${AXIS.map((a, i) => html`<button type="button" class="ghost compact" key=${a} data-axis=${a.toLowerCase()} title=${`Turn the model a quarter turn about ${a}`}
          onClick=${() => turn(i)}>${a} +90°</button>`)}</div></div>
      <div class="field-row clean-xyz">${AXIS.map((a, i) => html`<${NumberField} key=${a} id=${`clean-rotate-${a.toLowerCase()}`} label=${`About ${a}`} value=${t.rotate[i]}
        step=${5} min=${-360} max=${360} unit="°" title=${`Turn the model this many degrees about ${a} (X first, then Y, then Z)`}
        onChange=${(v) => setT({ rotate: t.rotate.map((r, j) => (j === i ? v : r)) })} />`)}</div>
      <div class="field-block"><span>Mirror</span><div class="field-row" id="clean-mirror">
        ${AXIS.map((a, i) => html`<button type="button" class="ghost compact" key=${a} data-axis=${a.toLowerCase()} aria-pressed=${t.mirror[i] ? "true" : "false"}
          title=${`Mirror the model across ${a} (left and right swapped, for X)`} onClick=${() => setT({ mirror: t.mirror.map((x, j) => (j === i ? !x : x)) })}>${a}</button>`)}</div></div>
      <div class="field-row clean-xyz">${AXIS.map((a, i) => html`<${NumberField} key=${a} id=${`clean-move-${a.toLowerCase()}`} label=${`Move ${a}`} value=${t.move[i]}
        step=${1} min=${-1e7} max=${1e7} unit="mm" title=${`Move the model this far along ${a}`}
        onChange=${(v) => setT({ move: t.move.map((x, j) => (j === i ? v : x)) })} />`)}</div>
      <label class="check-row" title="Moves the model up or down so its lowest point is at height 0, as a slicer puts it on the bed">
        <input type="checkbox" id="clean-floor" checked=${tAt >= 0 && t.floor} onChange=${(e) => setT({ floor: e.target.checked })} />
        <span>Stand it on the floor</span></label>
      ${tAt >= 0 ? html`<dl class="insp-dl" id="clean-size"><dt>Print size</dt><dd>${sizeText(stats?.size_mm)}</dd>
          ${game && Math.abs(scale - 1) > 1e-6 ? html`<dt>In the game</dt><dd class="muted" id="clean-game-size">${sizeText(game)}</dd>` : null}</dl>
        <div class="insp-actions"><button type="button" class="ghost compact" id="clean-transform-reset" disabled=${busy}
          title="The game's own size, turn and place again" onClick=${resetT}>Reset</button></div>` : null}
    </section>
    ${clean.length ? html`<${NextStep} id="clean-next" steps=${[
      { tab: "mesh", label: "Mesh", title: "Step 1: even out the cleaned model's vertices" },
      { tab: "export", label: "Export", ghost: true, title: "Write the cleaned model to a file as it is" }]} />` : null}
  </div>`;
}
