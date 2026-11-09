// The 3D viewer (from Model Library's, itself from Grid Workshop's): a model in
// a Z-up scene on a floor grid in 10 mm squares, orbit with the mouse, three
// views, edges on or off. Dark STL sends meshes in its own packed form
// (darkstl/app/meshpack.py) so each material can carry its game texture.
import * as THREE from "three";
import { OrbitControls } from "./vendor/three/OrbitControls.js";

const STEP = 10; // mm
export const GRAY = "#a3a9ae";
const MAGIC = "DSTLMESH";
const EDGES_UP_TO = 400000; // triangles: more would take long to work out, and be a solid mass of lines
/** Colours of the pieces of a cut model, in turn. */
export const PIECE_COLORS = ["#d08c4a", "#5b9bd5", "#7cb46b", "#c96f9b", "#d9c35a", "#8a7fd1", "#4fb3b0", "#c9644f", "#9aa25a", "#6f8fb0"];
const CUT_COLOR = 0xe0913a, CUT_IDLE = 0x7d8fa3;

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
    this.cutGroup = new THREE.Group();
    this.scene.add(this.cutGroup);
    this.cuts = []; // [{ id, normal, point }] in model millimetres
    this.cutSelected = null;
    this.onCut = null; // (id, { normal, point }, done) while a cut plane is dragged
    this.onCutSelect = null; // (id) when a plane is clicked
    this.placing = null; // (point, normal) for the next click on the model
    this.picking = null; // (triangle, { add, take }) for each click on the model (the Clean tab's selection)
    this.marks = new Map(); // name -> { mesh, count }: triangles of the model drawn over in a colour (Clean tab)
    this._click = null;
    this.explode = 0;
    this._raycaster = new THREE.Raycaster();
    this._drag = null;
    this._onDown = (e) => this._pointerDown(e);
    this._onMove = (e) => this._pointerMove(e);
    this._onUp = (e) => this._pointerUp(e);
    canvas.addEventListener("pointerdown", this._onDown, { capture: true });
    canvas.addEventListener("pointermove", this._onMove);
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
    // a cut model: each piece its own geometry (sharing the vertex buffers), so the pieces can be spread apart
    const pieces = header.pieces ? header.groups.map((g) => {
      const pg = new THREE.BufferGeometry();
      pg.setAttribute("position", geom.getAttribute("position"));
      pg.setAttribute("normal", geom.getAttribute("normal"));
      if (thickness) pg.setAttribute("color", geom.getAttribute("color"));
      pg.setIndex(new THREE.BufferAttribute(index.subarray(g.start, g.start + g.count), 1));
      return { geom: pg, piece: g.material };
    }) : null;
    return { header, geom, slots, offset, pieces, thickness, edges: null,
      dispose() { geom.dispose(); this.edges?.dispose(); for (const p of this.pieces || []) p.geom.dispose(); } };
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
      if (built.pieces) {
        this.mesh = new THREE.Group();
        for (const p of built.pieces) {
          const piece = new THREE.Mesh(p.geom, m);
          piece.userData.piece = p.piece;
          this.mesh.add(piece);
        }
      } else this.mesh = new THREE.Mesh(geom, m);
    } else if (built.pieces) {
      this.mesh = new THREE.Group();
      for (const p of built.pieces) {
        const m = new THREE.MeshStandardMaterial({ color: PIECE_COLORS[p.piece % PIECE_COLORS.length], roughness: 0.6, metalness: 0.02, side: THREE.DoubleSide, wireframe });
        const piece = new THREE.Mesh(p.geom, m);
        piece.userData.piece = p.piece;
        this.mesh.add(piece);
      }
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
    this._applyExplode();
    this._drawCuts();
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
        const mats = new Set(o.isGroup ? o.children.map((c) => c.material) : [].concat(o.material));
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
    this._applyExplode();
    this.redraw();
    return url;
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this.canvas.removeEventListener("pointerdown", this._onDown, { capture: true });
    this.canvas.removeEventListener("pointermove", this._onMove);
    this.canvas.removeEventListener("pointerup", this._onUp);
    this.canvas.removeEventListener("pointercancel", this._onUp);
    this._clearCuts();
    this._observer.disconnect();
    window.removeEventListener("resize", this._onWindowResize);
    this.clear();
    for (const t of this.textures.values()) t.dispose();
    this.textures.clear();
    this.controls.dispose();
    this.renderer.dispose();
  }

  // ---------------------------------------------------------------- marked triangles (Clean tab)

  /** Draw triangles of the mesh on show (their numbers in its index, as the engine counts them) over the model in
   *  `color`, under `name` ("selection", "sheets"...): the same name again replaces them; none takes them off. */
  setMark(name, triangles, color) {
    const old = this.marks.get(name);
    if (old) { this.scene.remove(old.mesh); old.mesh.geometry.dispose(); old.mesh.material.dispose(); this.marks.delete(name); }
    const b = this.shown;
    if (!b || b.pieces || !triangles?.length) { this.redraw(); return; }
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

  // ---------------------------------------------------------------- cut planes

  /** Show cut planes ([{ id, normal, point }] in model millimetres), the selected one highlighted with a handle
   *  to tilt it. Dragging a plane slides it along its normal; dragging the handle turns it. */
  setCuts(cuts, selected) {
    this.cuts = cuts || [];
    this.cutSelected = selected;
    this._drawCuts();
    this.redraw();
  }

  /** A cut's normal facing the camera as it is turned now. */
  facingNormal() {
    const d = this.camera.getWorldDirection(new THREE.Vector3());
    return [-d.x, -d.y, -d.z].map((x) => Math.round(x * 1e4) / 1e4);
  }

  /** Spread the pieces of a cut model apart: 0 together, 1 well apart. */
  setExplode(f) {
    this.explode = f;
    this._applyExplode();
    this.redraw();
  }

  _applyExplode() {
    const b = this.shown;
    this.gridGroup.visible = true;
    if (!b?.pieces || !this.mesh?.isGroup) return;
    const centres = b.header.pieces.map((p) => new THREE.Vector3(...p.center));
    const mid = centres.reduce((a, c) => a.add(c), new THREE.Vector3()).multiplyScalar(1 / Math.max(centres.length, 1));
    const size = this.bbox ? this.bbox.getSize(new THREE.Vector3()).length() : 100;
    for (const child of this.mesh.children) {
      const c = centres[child.userData.piece] || mid;
      const dir = c.clone().sub(mid);
      const len = dir.length();
      if (len > 1e-6) dir.multiplyScalar(1 / len);
      child.position.copy(dir.multiplyScalar(this.explode * size * 0.35 + this.explode * len * 0.6));
    }
    // spread apart, the lower pieces go below the floor, so the floor steps aside meanwhile
    this.gridGroup.visible = this.explode < 0.01;
  }

  _clearCuts() {
    for (const c of [...this.cutGroup.children]) {
      this.cutGroup.remove(c);
      c.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); });
    }
  }

  _cutSize() {
    const bb = this.bbox;
    return bb ? Math.max(bb.getSize(new THREE.Vector3()).length() * 0.8, 20) : 100;
  }

  _drawCuts() {
    this._clearCuts();
    const size = this._cutSize();
    for (const cut of this.cuts) {
      const on = cut.id === this.cutSelected;
      const color = on ? CUT_COLOR : CUT_IDLE;
      const g = new THREE.Group();
      g.userData.cut = cut.id;
      const sheet = new THREE.Mesh(new THREE.PlaneGeometry(size, size),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: on ? 0.28 : 0.16, side: THREE.DoubleSide, depthWrite: false }));
      sheet.userData = { cut: cut.id, part: "sheet" };
      const h = size / 2;
      const rim = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-h, -h, 0), new THREE.Vector3(h, -h, 0), new THREE.Vector3(h, h, 0), new THREE.Vector3(-h, h, 0)]),
        new THREE.LineBasicMaterial({ color, transparent: true, opacity: on ? 0.95 : 0.6 }));
      g.add(sheet, rim);
      if (on) {
        const reach = size * 0.28;
        const stem = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, reach)]),
          new THREE.LineBasicMaterial({ color, depthTest: false, transparent: true }));
        const knob = new THREE.Mesh(new THREE.SphereGeometry(size * 0.03, 20, 12), new THREE.MeshBasicMaterial({ color, depthTest: false, transparent: true }));
        knob.position.z = reach;
        knob.userData = { cut: cut.id, part: "knob" };
        stem.renderOrder = knob.renderOrder = 10;
        g.add(stem, knob);
      }
      g.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), new THREE.Vector3(...cut.normal).normalize());
      g.position.set(...cut.point).add(this.offset);
      this.cutGroup.add(g);
    }
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
    const ray = this._ray(e);
    if (this.placing && this.mesh) {
      const hit = this._raycaster.intersectObject(this.mesh, true)[0];
      if (hit) {
        e.stopImmediatePropagation();
        e.preventDefault();
        const done = this.placing;
        this.placing = null;
        const n = hit.face ? hit.face.normal.clone().transformDirection(hit.object.matrixWorld) : new THREE.Vector3(0, 0, 1);
        done(hit.point.clone().sub(this.offset).toArray(), n.toArray());
        return;
      }
    }
    if (!this.cuts.length) return;
    const hits = this._raycaster.intersectObjects(this.cutGroup.children, true).filter((h) => h.object.userData.part);
    // the handle wins over a sheet in front of it; a sheet only when nothing of the model is nearer
    const knob = hits.find((h) => h.object.userData.part === "knob");
    let hit = knob;
    if (!hit && hits.length) {
      const model = this.mesh ? this._raycaster.intersectObject(this.mesh, true)[0] : null;
      const sheet = hits.find((h) => h.object.userData.part === "sheet");
      if (sheet && (!model || sheet.distance <= model.distance + 1e-3)) hit = sheet;
    }
    if (!hit) return;
    const cut = this.cuts.find((c) => c.id === hit.object.userData.cut);
    if (!cut) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    this.canvas.setPointerCapture?.(e.pointerId);
    if (cut.id !== this.cutSelected) this.onCutSelect?.(cut.id);
    const normal = new THREE.Vector3(...cut.normal).normalize();
    const point = new THREE.Vector3(...cut.point).add(this.offset);
    this._drag = { id: cut.id, part: hit.object.userData.part, normal, point, start: this._alongNormal(ray, point, normal), now: { normal: cut.normal, point: cut.point } };
  }

  /** How far along the line through `point` in `normal`'s direction the ray passes closest. */
  _alongNormal(ray, point, normal) {
    const w0 = ray.origin.clone().sub(point);
    const b = ray.direction.dot(normal), d = ray.direction.dot(w0), e = normal.dot(w0);
    const den = 1 - b * b;
    return den < 1e-6 ? 0 : (e - b * d) / den;
  }

  _pointerMove(e) {
    const d = this._drag;
    if (!d) return;
    const ray = this._ray(e);
    let normal = d.normal, point = d.point;
    if (d.part === "knob") {
      // the handle follows the pointer over a plane facing the camera through the cut's centre
      const facing = new THREE.Plane().setFromNormalAndCoplanarPoint(this.camera.getWorldDirection(new THREE.Vector3()).negate(), d.point);
      const at = ray.intersectPlane(facing, new THREE.Vector3());
      if (!at) return;
      const dir = at.sub(d.point);
      if (dir.lengthSq() < 1e-6) return;
      normal = dir.normalize();
    } else {
      const t = this._alongNormal(ray, d.point, d.normal);
      point = d.point.clone().addScaledVector(d.normal, t - d.start);
    }
    d.now = { normal: normal.toArray(), point: point.clone().sub(this.offset).toArray() };
    const cut = this.cuts.find((c) => c.id === d.id);
    if (cut) { Object.assign(cut, d.now); this._drawCuts(); this.redraw(); }
    this.onCut?.(d.id, d.now, false);
  }

  _pointerUp(e) {
    const c = this._click;
    this._click = null;
    if (c && e.type === "pointerup" && this.picking && this.mesh && !this.mesh.isGroup && Math.hypot(e.clientX - c.x, e.clientY - c.y) < 5 && performance.now() - c.at < 800) {
      this._ray(e);
      const hit = this._raycaster.intersectObject(this.mesh, false)[0];
      if (hit && hit.faceIndex != null) this.picking(hit.faceIndex, { add: e.shiftKey, take: e.altKey || e.ctrlKey || e.metaKey });
    }
    const d = this._drag;
    if (!d) return;
    this._drag = null;
    this.canvas.releasePointerCapture?.(e.pointerId);
    this.onCut?.(d.id, d.now, true);
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
