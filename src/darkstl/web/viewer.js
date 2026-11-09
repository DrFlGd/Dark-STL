// The 3D viewer (from Model Library's, itself from Grid Workshop's): a model in
// a Z-up scene on a floor grid in 10 mm squares, orbit with the mouse, three
// views, edges on or off. Dark STL sends meshes in its own packed form
// (darkstl/app/meshpack.py) so each material can carry its game texture.
// The Pose tab's bones show as small balls joined to their parents; the one chosen
// carries a rotate gizmo (three's TransformControls) in its own axes.
import * as THREE from "three";
import { OrbitControls } from "./vendor/three/OrbitControls.js";
import { TransformControls } from "./vendor/three/TransformControls.js";

const STEP = 10; // mm
export const GRAY = "#a3a9ae";
const MAGIC = "DSTLMESH";
const EDGES_UP_TO = 400000; // triangles: more would take long to work out, and be a solid mass of lines
const BONE_COLOR = 0x4fb3e8, BONE_TURNED = 0xe0913a, BONE_CHOSEN = 0xffd166, BONE_LINE = 0x8fb8d6;

/** A bone's turn (x, y, z degrees about its own axes, three's "XYZ" order) followed by `drag`, a turn in the
 *  same axes as the gizmo made it (a quaternion [x, y, z, w]); `flip`: the bone's axes are mirrored, which the
 *  gizmo's aren't. The turn it makes, as three angles again. */
export function addTurn(deg, drag, flip = false) {
  const d = new THREE.Quaternion(...drag);
  if (flip) d.set(-d.x, -d.y, d.z, d.w);
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(...(deg || [0, 0, 0]).map((a) => THREE.MathUtils.degToRad(a)), "XYZ"));
  q.multiply(d).normalize();
  const e = new THREE.Euler().setFromQuaternion(q, "XYZ");
  return [e.x, e.y, e.z].map((a) => Math.round(THREE.MathUtils.radToDeg(a) * 100) / 100 + 0);
}

/** The Thickness shading's colours: thinner than the wall, up to twice it, up to four times, thick enough. */
export const THICKNESS_COLORS = { thin: "#d6453d", near: "#e8913a", fair: "#e2c74e", thick: GRAY };
const THICK_RGB = Object.fromEntries(Object.entries(THICKNESS_COLORS).map(([k, c]) => [k, new THREE.Color(c)]));

/** Colour every vertex by its thickness (mm at game size, times `scale` for the print) against `wall` mm:
 *  red under it (and on single sheets, which have none), orange to yellow up to twice it, fading to clay at four
 *  times. Writes into `out` (r, g, b per vertex) and returns the share of vertices thinner than the wall. */
export function thicknessColours(values, wall, scale, out) {
  const { thin, near, fair, thick } = THICK_RGB;
  const c = new THREE.Color();
  let under = 0;
  for (let i = 0; i < values.length; i++) {
    const f = (values[i] * scale) / wall;
    if (!(f >= 1)) { c.copy(thin); under++; }
    else if (f < 2) c.copy(near).lerp(fair, f - 1);
    else if (f < 4) c.copy(fair).lerp(thick, (f - 2) / 2);
    else c.copy(thick);
    out[i * 3] = c.r; out[i * 3 + 1] = c.g; out[i * 3 + 2] = c.b;
  }
  return values.length ? under / values.length : 0;
}

/** Read a packed mesh: { header, positions, normals, uvs, scalar, index }. */
export function unpackMesh(buf) {
  const bytes = new Uint8Array(buf, 0, 8);
  if (String.fromCharCode(...bytes) !== MAGIC) throw new Error("Not a Dark STL mesh");
  const n = new DataView(buf).getUint32(8, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 12, n)));
  let at = 12 + n;
  const take = (Type, count) => { const a = new Type(buf, at, count); at += count * 4; return a; };
  const positions = take(Float32Array, header.vertices * 3);
  const normals = take(Float32Array, header.vertices * 3);
  const uvs = header.uv ? take(Float32Array, header.vertices * 2) : null;
  const scalar = header.scalar ? take(Float32Array, header.vertices) : null;
  const index = take(Uint32Array, header.triangles * 3);
  return { header, positions, normals, uvs, scalar, index };
}

/** sRGB-to-linear decoding of the colour map in the fragment shader (three's own transfer function). */
function decodeMapInShader(shader) {
  shader.fragmentShader = shader.fragmentShader.replace(
    "#include <map_fragment>",
    `#ifdef USE_MAP
      vec4 sampledDiffuseColor = sRGBTransferEOTF( texture2D( map, vMapUv ) );
      diffuseColor *= sampledDiffuseColor;
    #endif`,
  );
}

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
    this._pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    this.renderer.setPixelRatio(this._pixelRatio);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.5, 20000);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(160, -220, 170);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.screenSpacePanning = true;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a96a3, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.position.set(1, -1.5, 2.5);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0xffffff, 0.7);
    rim.position.set(-2, 2, 1);
    this.scene.add(rim);

    this.clay = new THREE.MeshStandardMaterial({ color: GRAY, roughness: 0.55, metalness: 0.02, side: THREE.DoubleSide });
    this.edgeMaterial = new THREE.LineBasicMaterial({ color: 0x18222d, transparent: true, opacity: 0.55 });
    this.mesh = null;
    this.edges = null;
    this.shown = null; // the built mesh on show
    this.textures = new Map(); // by URL, kept for the meshes shown next
    this.gridGroup = new THREE.Group();
    this.scene.add(this.gridGroup);
    this.showEdges = false;
    this.wireframe = false;
    this.bbox = null;
    this.dark = false;
    this._buildGrid(new THREE.Box3(new THREE.Vector3(-30, -30, 0), new THREE.Vector3(30, 30, 0)));
    this.offset = new THREE.Vector3(); // model millimetres -> scene: the shown mesh is centred and set on the floor
    this.picking = null; // (triangle, { add, take }) for each click on the model (the Clean tab's selection)
    this.marks = new Map(); // name -> { mesh, count }: triangles of the model drawn over in a colour (Clean tab)
    this._click = null;
    // the Pose tab's bones: [{ name, parent, pos (mm), quat, flip, turned }], the one chosen, and what a click
    // on a ball (onBonePick) or a turn of the gizmo (onBoneTurn(name, quaternion)) does
    this.boneGroup = new THREE.Group();
    this.scene.add(this.boneGroup);
    this.bones = [];
    this.boneChosen = null;
    this.onBonePick = null;
    this.onBoneTurn = null;
    this.boneHandle = new THREE.Object3D();
    this.scene.add(this.boneHandle);
    this.gizmo = null;
    this._raycaster = new THREE.Raycaster();
    this._onDown = (e) => this._pointerDown(e);
    this._onUp = (e) => this._pointerUp(e);
    canvas.addEventListener("pointerdown", this._onDown, { capture: true });
    canvas.addEventListener("pointerup", this._onUp);
    canvas.addEventListener("pointercancel", this._onUp);

    this._raf = 0;
    const tick = () => {
      this._raf = 0;
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    this.redraw = () => { if (!this._raf) this._raf = requestAnimationFrame(tick); };
    this.controls.addEventListener("change", this.redraw);
    this.controls.addEventListener("start", this.redraw);
    this._observer = new ResizeObserver(() => this.resize());
    this._observer.observe(canvas.parentElement);
    this._onWindowResize = () => this.resize();
    window.addEventListener("resize", this._onWindowResize);
    this.resize();
  }

  resize() {
    const el = this.canvas.parentElement;
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (ratio !== this._pixelRatio) { this._pixelRatio = ratio; this.renderer.setPixelRatio(ratio); }
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.redraw();
  }

  setEdges(on) {
    this.showEdges = on;
    if (this.edges) this.edges.visible = on;
    else this._addEdges();
    this.redraw();
  }

  /** The sharp edges of the mesh on show, worked out the first time they're wanted (not for huge meshes). */
  _addEdges() {
    const b = this.shown;
    if (this.edges || !this.showEdges || !b || b.header.triangles >= EDGES_UP_TO) return;
    b.edges ||= new THREE.EdgesGeometry(b.geom, 30);
    this.edges = new THREE.LineSegments(b.edges, this.edgeMaterial);
    this.scene.add(this.edges);
  }

  setTheme(dark) {
    if (this.dark === !!dark) return;
    this.dark = !!dark;
    this._buildGrid(this.bbox || new THREE.Box3(new THREE.Vector3(-30, -30, 0), new THREE.Vector3(30, 30, 0)));
    this.redraw();
  }

  /** Make a packed mesh ready to show: its geometry, resting on the floor, centred. The page keeps
   *  what this returns to show it again at once, and frees it with its dispose(). */
  build(buf) {
    const { header, positions, normals, uvs, scalar, index } = unpackMesh(buf);
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geom.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
    if (uvs) geom.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
    // the thickness behind each vertex (Thickness shading): coloured when shown, against the wall asked for
    const thickness = header.scalar === "thickness" ? scalar : null;
    if (thickness) geom.setAttribute("color", new THREE.BufferAttribute(new Float32Array(header.vertices * 3), 3));
    geom.setIndex(new THREE.BufferAttribute(index, 1));
    const [x0, y0, z0] = header.min, [x1, y1] = header.max;
    const offset = [-(x0 + x1) / 2, -(y0 + y1) / 2, -z0];
    geom.translate(...offset);
    geom.computeBoundingBox();
    const slots = []; // the materials in the order of the mesh's material slots
    for (const g of header.groups) {
      if (!slots.includes(g.material)) slots.push(g.material);
      geom.addGroup(g.start, g.count, slots.indexOf(g.material));
    }
    return { header, geom, slots, offset, thickness, edges: null,
      dispose() { geom.dispose(); this.edges?.dispose(); } };
  }

  /** Show a mesh from build(). `textures`: { material index: image URL } (textured view), or null for clay.
   *  `thickness`: { wall, scale } to colour it by its thickness (a mesh built with it), see setThickness.
   *  `keepView`: leave the camera where it is (the same model, drawn again). */
  show(built, { textures = null, wireframe = false, keepView = false, thickness = null } = {}) {
    const { header, geom, slots } = built;
    this.clear();
    this.thinShare = null;
    if (thickness && built.thickness) {
      this.thinShare = thicknessColours(built.thickness, thickness.wall, thickness.scale, geom.getAttribute("color").array);
      const m = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.6, metalness: 0.0, side: THREE.DoubleSide, wireframe });
      this.mesh = new THREE.Mesh(geom, m);
    } else {
      const materials = slots.map((mat) => this._material(textures?.[mat], wireframe));
      if (!materials.length) materials.push(this._material(null, wireframe));
      this.mesh = new THREE.Mesh(geom, materials);
    }
    this.scene.add(this.mesh);
    this.offset.set(...built.offset);
    this.shown = built;
    this._addEdges();
    this.bbox = geom.boundingBox.clone();
    this._buildGrid(this.bbox);
    this.gridGroup.visible = true;
    if (this.bones.length) this._drawBones();
    if (!keepView) this.view("iso");
    this.redraw();
    const size = this.bbox.getSize(new THREE.Vector3());
    return { x: size.x, y: size.y, z: size.z, triangles: header.triangles, vertices: header.vertices, edges: header.triangles < EDGES_UP_TO };
  }

  /** Colour the mesh on show by its thickness again, against another wall or print size: { wall, scale }.
   *  Returns the share of vertices thinner than the wall. */
  setThickness({ wall, scale }) {
    const b = this.shown;
    if (!b?.thickness) return null;
    const color = b.geom.getAttribute("color");
    this.thinShare = thicknessColours(b.thickness, wall, scale, color.array);
    color.needsUpdate = true;
    this.redraw();
    return this.thinShare;
  }

  _material(url, wireframe) {
    if (!url) {
      const m = this.clay.clone();
      m.wireframe = wireframe;
      return m;
    }
    let tex = this.textures.get(url);
    if (!tex) {
      tex = new THREE.TextureLoader().load(url, () => this.redraw());
      // The map is sRGB, but it is uploaded as plain bytes and decoded in the shader (decodeMapInShader):
      // some GPUs and webviews skip the sRGB texture format, which showed dark game textures light and washed out.
      tex.colorSpace = THREE.NoColorSpace;
      tex.flipY = false; // Dark STL's UVs run down the image
      tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
      tex.anisotropy = 4;
      this.textures.set(url, tex);
    }
    const m = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.7, metalness: 0.0, side: THREE.DoubleSide, wireframe });
    m.onBeforeCompile = decodeMapInShader;
    return m;
  }

  /** Take the mesh off show. Its geometry belongs to whoever built it; the textures stay for the next. */
  clear() {
    this.clearMarks();
    for (const o of [this.mesh, this.edges]) {
      if (o) {
        this.scene.remove(o);
        const mats = new Set([].concat(o.material));
        for (const m of mats) if (m !== this.edgeMaterial) m.dispose();
      }
    }
    this.mesh = this.edges = this.shown = null;
    this.redraw();
  }

  /** The current view as a PNG data URL. */
  snapshot() {
    this.controls.update();
    this.gridGroup.visible = false;
    this.renderer.render(this.scene, this.camera);
    const url = this.canvas.toDataURL("image/png");
    this.gridGroup.visible = true;
    this.redraw();
    return url;
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this.canvas.removeEventListener("pointerdown", this._onDown, { capture: true });
    this.canvas.removeEventListener("pointerup", this._onUp);
    this.canvas.removeEventListener("pointercancel", this._onUp);
    this._observer.disconnect();
    window.removeEventListener("resize", this._onWindowResize);
    this.clear();
    this._clearBones();
    if (this.gizmo) { this.scene.remove(this.gizmo.getHelper()); this.gizmo.dispose(); this.gizmo = null; }
    for (const t of this.textures.values()) t.dispose();
    this.textures.clear();
    this.controls.dispose();
    this.renderer.dispose();
  }

  // ---------------------------------------------------------------- bones

  /** Show the model's bones (balls at their pivots, lines to their parents), or none with `bones` empty;
   *  `chosen` names the one with the rotate gizmo. Positions are the model's millimetres, as the mesh's. */
  setBones(bones, chosen) {
    this.bones = bones || [];
    this.boneChosen = chosen && this.bones.some((b) => b.name === chosen) ? chosen : null;
    this._drawBones();
    this.redraw();
  }

  _clearBones() {
    for (const c of [...this.boneGroup.children]) {
      this.boneGroup.remove(c);
      c.geometry?.dispose();
      c.material?.dispose();
    }
  }

  _boneSize() {
    const bb = this.bbox;
    return bb ? Math.max(bb.getSize(new THREE.Vector3()).length() * 0.008, 0.6) : 2;
  }

  _drawBones() {
    this._clearBones();
    const r = this._boneSize();
    const ball = new THREE.SphereGeometry(r, 12, 8);
    const at = (b) => new THREE.Vector3(...b.pos).add(this.offset);
    const lines = [];
    for (const b of this.bones) {
      const chosen = b.name === this.boneChosen;
      const m = new THREE.Mesh(chosen ? ball.clone().scale(1.6, 1.6, 1.6) : ball.clone(),
        new THREE.MeshBasicMaterial({ color: chosen ? BONE_CHOSEN : b.turned ? BONE_TURNED : BONE_COLOR, depthTest: false, transparent: true, opacity: 0.95 }));
      m.position.copy(at(b));
      m.renderOrder = 20;
      m.userData.bone = b.name;
      this.boneGroup.add(m);
      const p = b.parent >= 0 ? this.bones.find((x) => x.index === b.parent) : null;
      if (p) lines.push(at(p), at(b));
    }
    ball.dispose();
    if (lines.length) {
      const seg = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(lines),
        new THREE.LineBasicMaterial({ color: BONE_LINE, depthTest: false, transparent: true, opacity: 0.7 }));
      seg.renderOrder = 19;
      this.boneGroup.add(seg);
    }
    this._placeGizmo();
  }

  /** The rotate gizmo on the chosen bone, in its axes: turning a ring turns the bone about that axis. */
  _placeGizmo() {
    const b = this.bones.find((x) => x.name === this.boneChosen);
    if (!b) {
      if (this.gizmo) { this.gizmo.detach(); this.gizmo.getHelper().visible = false; }
      return;
    }
    if (!this.gizmo) {
      const g = new TransformControls(this.camera, this.canvas);
      g.setMode("rotate");
      g.setSpace("local");
      g.addEventListener("change", this.redraw);
      g.addEventListener("dragging-changed", (e) => {
        this.controls.enabled = !e.value;
        if (e.value) { this._turnStart = this.boneHandle.quaternion.clone(); return; }
        const start = this._turnStart;
        this._turnStart = null;
        if (!start || !this.boneChosen) return;
        const drag = start.clone().invert().multiply(this.boneHandle.quaternion).normalize();
        if (Math.abs(drag.w) > 0.999999) return; // not turned
        this.onBoneTurn?.(this.boneChosen, [drag.x, drag.y, drag.z, drag.w]);
      });
      this.scene.add(g.getHelper());
      this.gizmo = g;
    }
    if (this.gizmo.dragging) return;
    this.boneHandle.position.copy(new THREE.Vector3(...b.pos).add(this.offset));
    this.boneHandle.quaternion.set(...b.quat);
    this.boneHandle.updateMatrixWorld();
    this.gizmo.attach(this.boneHandle);
    this.gizmo.getHelper().visible = true;
    this.gizmo.setSize(0.8);
  }

  // ---------------------------------------------------------------- marked triangles (Clean tab)

  /** Draw triangles of the mesh on show (their numbers in its index, as the engine counts them) over the model in
   *  `color`, under `name` ("selection", "sheets"...): the same name again replaces them; none takes them off. */
  setMark(name, triangles, color) {
    const old = this.marks.get(name);
    if (old) { this.scene.remove(old.mesh); old.mesh.geometry.dispose(); old.mesh.material.dispose(); this.marks.delete(name); }
    const b = this.shown;
    if (!b || !triangles?.length) { this.redraw(); return; }
    const src = b.geom.index.array;
    const n = b.header.triangles;
    const index = new Uint32Array(triangles.length * 3);
    let k = 0;
    for (const t of triangles) {
      if (t < 0 || t >= n) continue;
      index[k++] = src[3 * t]; index[k++] = src[3 * t + 1]; index[k++] = src[3 * t + 2];
    }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", b.geom.getAttribute("position"));
    geom.setIndex(new THREE.BufferAttribute(index.subarray(0, k), 1));
    const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.62, side: THREE.DoubleSide, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
    const mesh = new THREE.Mesh(geom, mat);
    mesh.renderOrder = 5;
    this.scene.add(mesh);
    this.marks.set(name, { mesh, count: k / 3 });
    this.redraw();
  }

  clearMarks() {
    for (const name of [...this.marks.keys()]) {
      const m = this.marks.get(name);
      this.scene.remove(m.mesh); m.mesh.geometry.dispose(); m.mesh.material.dispose();
    }
    this.marks.clear();
  }

  _ray(e) {
    const r = this.canvas.getBoundingClientRect();
    const p = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this._raycaster.setFromCamera(p, this.camera);
    return this._raycaster.ray;
  }

  _pointerDown(e) {
    if (e.button !== 0) return;
    // a click (not a drag, which turns the view) on the model while picking: see _pointerUp
    this._click = this.picking ? { x: e.clientX, y: e.clientY, at: performance.now() } : null;
    this._ray(e);
    if (this.bones.length && this.boneGroup.children.length && !(this.gizmo?.axis)) {
      const hit = this._raycaster.intersectObjects(this.boneGroup.children, false).find((h) => h.object.userData.bone);
      if (hit) {
        e.stopImmediatePropagation();
        e.preventDefault();
        this._click = null; // a bone chosen, not a face
        this.onBonePick?.(hit.object.userData.bone);
      }
    }
  }

  _pointerUp(e) {
    const c = this._click;
    this._click = null;
    if (c && e.type === "pointerup" && this.picking && this.mesh && Math.hypot(e.clientX - c.x, e.clientY - c.y) < 5 && performance.now() - c.at < 800) {
      this._ray(e);
      const hit = this._raycaster.intersectObject(this.mesh, false)[0];
      if (hit && hit.faceIndex != null) this.picking(hit.faceIndex, { add: e.shiftKey, take: e.altKey || e.ctrlKey || e.metaKey });
    }
  }

  _buildGrid(bb) {
    for (const c of [...this.gridGroup.children]) { this.gridGroup.remove(c); c.geometry?.dispose(); }
    const span = Math.max(bb.max.x - bb.min.x, bb.max.y - bb.min.y, 40);
    const step = span > 600 ? STEP * 10 : STEP;
    const half = Math.ceil((span * 0.75) / step) * step;
    const pts = [];
    for (let v = -half; v <= half + 0.01; v += step) pts.push(v, -half, 0, v, half, 0, -half, v, 0, half, v, 0);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: this.dark ? 0x5b6976 : 0x8693a0, transparent: true, opacity: this.dark ? 0.6 : 0.45 }));
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(half * 2, half * 2),
      new THREE.MeshBasicMaterial({ color: this.dark ? 0x26313c : 0xeef1f4, transparent: true, opacity: this.dark ? 0.85 : 0.7, depthWrite: false }));
    plate.position.z = -0.05;
    this.gridGroup.add(plate, lines);
  }

  view(name) {
    const bb = this.bbox || new THREE.Box3(new THREE.Vector3(-30, -30, 0), new THREE.Vector3(30, 30, 30));
    const size = bb.getSize(new THREE.Vector3());
    const center = bb.getCenter(new THREE.Vector3());
    const radius = Math.max(size.length() / 2, 10);
    const half = THREE.MathUtils.degToRad(this.camera.fov / 2);
    const across = Math.atan(Math.tan(half) * (this.camera.aspect || 1));
    const dist = (radius / Math.sin(Math.min(half, across))) * 1.15;
    const dirs = { iso: new THREE.Vector3(0.62, -0.95, 0.75), top: new THREE.Vector3(0, -0.0001, 1), front: new THREE.Vector3(0, -1, 0.08) };
    const dir = (dirs[name] || dirs.iso).normalize();
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(dir, dist);
    this.camera.near = Math.max(dist / 200, 0.1);
    this.camera.far = dist * 50;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.redraw();
  }
}
