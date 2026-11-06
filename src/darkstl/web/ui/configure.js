// The model page's Configure tab, for a configurable model (a weapon kept as a body and separate
// parts): a part chosen in each slot (magazine, sight, barrel...), or a preset that chooses them
// all, puts the model together; the Mesh, Displace, Cut and Export tabs then work on what was put
// together. Edit… changes the configuration: its slots, the parts each slot offers and where each
// part goes, kept in the user's own list (configs.py in the engine).
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { routeHash } from "./context.js";
import { Icon } from "./icons.js";
import { api, plural, toast } from "./api.js";

const KEEP = 40; // configurable models whose choices are remembered
const msg = (e) => String(e?.message || e);
const r3 = (v) => Math.round(v * 1000) / 1000;

/** Remember the parts chosen for model `id` (the page opens it again with them). */
export function setChoices(id, choices) {
  const all = { ...(ui.get().configChoices || {}) };
  delete all[id];
  if (choices) all[id] = choices;
  const keys = Object.keys(all);
  for (const k of keys.slice(0, Math.max(0, keys.length - KEEP))) delete all[k];
  setPref({ configChoices: all });
}

/** The model is opened again (its configuration was saved). */
const reopen = () => ui.set({ configRev: (ui.get().configRev || 0) + 1 });

async function save(configuration, m) {
  const r = await api("config_save", { configuration });
  if (r.id !== m.id) location.hash = routeHash(`model:${r.id}`);
  reopen();
  return r;
}

export function ConfigureTab({ m, switching }) {
  const editing = useStore(ui, (s) => s.configEdit) === m.id;
  const [naming, setNaming] = useState(null); // a preset's name being typed
  const c = m.configuration;
  if (editing) return html`<${ConfigEditor} m=${m} />`;
  const choose = (patch) => setChoices(m.id, { ...c.chosen, ...patch });
  const isDefault = JSON.stringify(c.chosen) === JSON.stringify(c.defaults);
  const presetNow = c.presets.find((p) => JSON.stringify(p.choices) === JSON.stringify(c.chosen));
  const savePreset = async () => {
    const name = (naming || "").trim();
    if (!name) return;
    try {
      await save({ ...c, presets: [...c.presets.filter((p) => p.name !== name), { name, choices: c.chosen }] }, m);
      setNaming(null);
      toast(`Saved the preset ${name}`);
    } catch (e) { toast(msg(e)); }
  };
  return html`<div class="tab-pad" id="configure-tab">
    <section class="insp-section"><h3>${c.label}</h3>
      <p class="muted">${plural(c.slots.length, "slot")} on ${c.base_name}${c.source ? html`<br /><small title=${c.source}>${/[\\/]/.test(c.source) ? `From ${c.source.split(/[\\/]/).pop()}` : c.source}</small>` : null}</p>
      ${c.guessed ? html`<p class="insp-note" id="config-guessed">These parts were matched by their file names in the weapon's folder. Check they belong together, or Edit… to change them.</p>` : null}
      ${switching ? html`<p class="muted" id="config-switching">Putting it together…</p>` : null}
    </section>
    ${c.presets.length ? html`<section class="insp-section" id="config-presets"><h3>Presets</h3>
      <select id="preset-select" value=${presetNow?.name || ""} onChange=${(e) => {
        const p = c.presets.find((x) => x.name === e.target.value);
        if (p) setChoices(m.id, p.choices);
      }}>
        <option value="" disabled>${presetNow ? "" : "Choose a preset…"}</option>
        ${c.presets.map((p) => html`<option key=${p.name} value=${p.name}>${p.name}</option>`)}
      </select></section>` : null}
    <section class="insp-section" id="config-slots"><h3>Parts</h3>
      ${c.slots.length ? c.slots.map((s) => html`<label class="field config-slot" key=${s.id} data-slot=${s.id}>
        <span>${s.label}</span>
        <select id=${`slot-${s.id}`} value=${c.chosen[s.id] || ""} disabled=${switching} onChange=${(e) => choose({ [s.id]: e.target.value })}>
          ${s.required ? null : html`<option value="">None</option>`}
          ${s.options.map((o) => html`<option key=${o.id} value=${o.id} title=${o.file}>${o.label}</option>`)}
        </select>
        ${c.chosen[s.id] ? html`<small class="muted config-file">${s.options.find((o) => o.id === c.chosen[s.id])?.file}</small>` : null}
      </label>`) : html`<p class="muted" id="config-empty">No slots yet: Edit… to add the parts that fit this model.</p>`}
      <div class="insp-actions">
        <button type="button" class="ghost" id="config-defaults" disabled=${isDefault} onClick=${() => setChoices(m.id, null)}>Defaults</button>
        ${c.slots.length ? html`<button type="button" class="ghost" id="config-preset-new" onClick=${() => setNaming(naming == null ? "" : null)}>${Icon.plus(14)} Save as preset…</button>` : null}
        <button type="button" class="ghost" id="config-edit" onClick=${() => ui.set({ configEdit: m.id })}>${Icon.edit(14)} Edit…</button>
      </div>
      ${naming != null ? html`<form class="config-preset-form" onSubmit=${(e) => { e.preventDefault(); savePreset(); }}>
        <input type="text" id="preset-name" placeholder="Preset name" value=${naming} onInput=${(e) => setNaming(e.target.value)} autofocus />
        <button type="submit" class="primary compact" disabled=${!naming.trim()}>Save</button></form>` : null}
    </section>
    <section class="insp-section"><p class="muted">The parts chosen here are what the Mesh, Displace, Cut and Export tabs work on. Each part is listed on the Model tab, where it can be left out too.</p></section>
  </div>`;
}

/** A copy of the configuration to change: offsets shown in millimetres. */
function draftOf(c) {
  return {
    label: c.label,
    slots: c.slots.map((s) => ({ ...s, options: s.options.map((o) => ({ ...o,
      offset_mm: (o.attach?.offset || [0, 0, 0]).map((v) => r3(v * 1000)), rotate: o.attach?.rotate || [0, 0, 0] })) })),
  };
}

function ConfigEditor({ m }) {
  const c = m.configuration;
  const [d, setD] = useState(() => draftOf(c));
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(null); // the slot a part is being added to
  const setSlot = (i, patch) => setD({ ...d, slots: d.slots.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  const setOption = (i, k, patch) => setSlot(i, { options: d.slots[i].options.map((o, j) => (j === k ? { ...o, ...patch } : o)) });
  const addSlot = () => {
    let n = d.slots.length + 1;
    while (d.slots.some((s) => s.id === `slot${n}`)) n++;
    setD({ ...d, slots: [...d.slots, { id: `slot${n}`, label: `Slot ${n}`, required: false, default: null, options: [] }] });
  };
  const addPart = (i, item) => {
    const have = new Set(d.slots[i].options.map((o) => o.id));
    let id = item.stem.toLowerCase(), n = 2;
    while (have.has(id)) id = `${item.stem.toLowerCase()}_${n++}`;
    setSlot(i, { options: [...d.slots[i].options, { id, label: item.label || item.stem, file: item.name, path: item.file,
      offset_mm: [0, 0, 0], rotate: [0, 0, 0] }] });
  };
  const done = () => ui.set({ configEdit: null });
  const submit = async () => {
    setBusy(true);
    try {
      const slots = d.slots.map((s) => ({ id: s.id, label: s.label, required: !!s.required, kind: s.kind || "parts",
        default: s.options.some((o) => o.id === s.default) ? s.default : null,
        options: s.options.map((o) => ({ id: o.id, label: o.label, path: o.path, variant: o.variant ?? null, parts: o.parts ?? null,
          attach: { ...(o.attach || {}), offset: o.offset_mm.map((v) => (Number(v) || 0) / 1000), rotate: o.rotate.map((v) => Number(v) || 0) } })) }));
      await save({ ...c, label: d.label.trim() || c.label, slots }, m);
      done();
      toast("Saved to your configurations");
    } catch (e) { toast(msg(e)); } finally { setBusy(false); }
  };
  const remove = async () => {
    setBusy(true);
    try {
      const r = await api("config_delete", { id: m.id });
      done();
      setChoices(m.id, null);
      if (r.still_listed) reopen();
      else location.hash = "#/";
      toast(r.still_listed ? "Removed your version: the list's own shows again" : "Removed the configuration");
    } catch (e) { toast(msg(e)); } finally { setBusy(false); }
  };
  const vec = (values, onSet, label, unit) => html`<div class="config-vec" role="group" aria-label=${label}>
    <span class="muted">${label}</span>${values.map((v, a) => html`<input key=${a} type="number" step="any" class="compact" aria-label=${`${label} ${"XYZ"[a]} (${unit})`}
      value=${v} onChange=${(e) => onSet(values.map((x, b) => (b === a ? e.target.value : x)))} />`)}<span class="muted">${unit}</span></div>`;
  return html`<div class="tab-pad" id="config-editor">
    <section class="insp-section"><h3>Edit configuration</h3>
      <label class="field"><span>Name</span><input type="text" id="config-label" value=${d.label} onInput=${(e) => setD({ ...d, label: e.target.value })} /></label>
      <p class="muted">Base model: ${c.base_name}. Saved to your own list${c.source && c.source !== "Your configurations" ? `, in place of the one from ${c.source}` : ""}.</p>
    </section>
    ${d.slots.map((s, i) => html`<section class="insp-section config-edit-slot" key=${s.id} data-slot=${s.id}>
      <div class="config-row">
        <input type="text" class="config-slot-label" aria-label="Slot name" value=${s.label} onInput=${(e) => setSlot(i, { label: e.target.value })} />
        <button type="button" class="ghost icon-btn" title="Remove this slot" onClick=${() => setD({ ...d, slots: d.slots.filter((_, j) => j !== i) })}>${Icon.trash(14)}</button>
      </div>
      <label class="check-row"><input type="checkbox" checked=${!!s.required} onChange=${(e) => setSlot(i, { required: e.target.checked })} />
        <span>Always has a part<small class="muted">Otherwise None can be chosen.</small></span></label>
      ${s.options.map((o, k) => html`<div class="config-edit-option" key=${o.id}>
        <div class="config-row">
          <input type="text" aria-label="Part name" value=${o.label} onInput=${(e) => setOption(i, k, { label: e.target.value })} />
          <label class="check-row" title="Chosen when the model is opened"><input type="radio" name=${`default-${s.id}`} checked=${s.default === o.id} onChange=${() => setSlot(i, { default: o.id })} /><span>Default</span></label>
          <button type="button" class="ghost icon-btn" title="Remove this part" onClick=${() => setSlot(i, { options: s.options.filter((_, j) => j !== k) })}>${Icon.trash(14)}</button>
        </div>
        <small class="muted config-file" title=${o.path}>${o.file}${o.attach?.socket ? ` · on ${o.attach.socket}` : ""}</small>
        ${vec(o.offset_mm, (v) => setOption(i, k, { offset_mm: v }), "Move", "mm")}
        ${vec(o.rotate, (v) => setOption(i, k, { rotate: v }), "Turn", "°")}
      </div>`)}
      ${adding === i ? html`<${PartPicker} game=${c.game} onPick=${(item) => addPart(i, item)} onClose=${() => setAdding(null)} />`
        : html`<div class="insp-actions"><button type="button" class="ghost add-part" onClick=${() => setAdding(i)}>${Icon.plus(14)} Add part…</button></div>`}
    </section>`)}
    <section class="insp-section">
      <div class="insp-actions"><button type="button" class="ghost" id="config-add-slot" onClick=${addSlot}>${Icon.plus(14)} Add slot</button></div>
      <p class="muted">Parts taken from the game usually sit in place already; Move and Turn adjust one that doesn't (along the model's own axes, Z up).</p>
      <div class="insp-actions">
        <button type="button" class="primary compact" id="config-save" disabled=${busy} onClick=${submit}>Save</button>
        <button type="button" class="ghost" id="config-cancel" disabled=${busy} onClick=${done}>Cancel</button>
        ${c.source === "Your configurations" ? html`<button type="button" class="ghost" id="config-delete" disabled=${busy} onClick=${remove}>${Icon.trash(14)} Remove</button>` : null}
      </div>
    </section>
  </div>`;
}

/** Search the game's models for a part to add. */
function PartPicker({ game, onPick, onClose }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState(null);
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => api("models_query", { game, q, limit: 40 }).then((r) => {
      if (live) setItems(r.items.filter((x) => !x.configurable));
    }, () => { if (live) setItems([]); }), 150);
    return () => { live = false; clearTimeout(t); };
  }, [q]);
  return html`<div class="part-picker">
    <div class="config-row"><input type="search" class="part-search" placeholder="Search this game's models" value=${q} onInput=${(e) => setQ(e.target.value)} autofocus />
      <button type="button" class="ghost" onClick=${onClose}>Done</button></div>
    <ul class="ls-list part-results">${(items || []).map((x) => html`<li key=${x.id}>
      <button type="button" class="ghost part-result" data-file=${x.name} title=${x.file} onClick=${() => onPick(x)}>${x.label}<small class="muted"> ${x.name}</small></button></li>`)}</ul>
    ${items && !items.length ? html`<p class="muted">Nothing found.</p>` : null}
  </div>`;
}
