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

/** Read a packed mesh: { header, positions, normals, uvs, index }. */
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
  const index = take(Uint32Array, header.triangles * 3);
  return { header, positions, normals, uvs, index };
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
    const { header, positions, normals, uvs, index } = unpackMesh(buf);
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geom.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
    if (uvs) geom.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
    geom.setIndex(new THREE.BufferAttribute(index, 1));
    const [x0, y0, z0] = header.min, [x1, y1] = header.max;
    geom.translate(-(x0 + x1) / 2, -(y0 + y1) / 2, -z0);
    geom.computeBoundingBox();
    const slots = []; // the materials in the order of the mesh's material slots
    for (const g of header.groups) {
      if (!slots.includes(g.material)) slots.push(g.material);
      geom.addGroup(g.start, g.count, slots.indexOf(g.material));
    }
    return { header, geom, slots, edges: null, dispose() { geom.dispose(); this.edges?.dispose(); } };
  }

  /** Show a mesh from build(). `textures`: { material index: image URL } (textured view), or null for clay.
   *  `keepView`: leave the camera where it is (the same model, drawn again). */
  show(built, { textures = null, wireframe = false, keepView = false } = {}) {
    const { header, geom, slots } = built;
    this.clear();
    const materials = slots.map((mat) => this._material(textures?.[mat], wireframe));
    if (!materials.length) materials.push(this._material(null, wireframe));
    this.mesh = new THREE.Mesh(geom, materials);
    this.scene.add(this.mesh);
    this.shown = built;
    this._addEdges();
    this.bbox = geom.boundingBox.clone();
    this._buildGrid(this.bbox);
    if (!keepView) this.view("iso");
    this.redraw();
    const size = this.bbox.getSize(new THREE.Vector3());
    return { x: size.x, y: size.y, z: size.z, triangles: header.triangles, vertices: header.vertices, edges: header.triangles < EDGES_UP_TO };
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
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.flipY = false; // Dark STL's UVs run down the image
      tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
      tex.anisotropy = 4;
      this.textures.set(url, tex);
    }
    return new THREE.MeshStandardMaterial({ map: tex, roughness: 0.7, metalness: 0.0, side: THREE.DoubleSide, wireframe });
  }

  /** Take the mesh off show. Its geometry belongs to whoever built it; the textures stay for the next. */
  clear() {
    for (const o of [this.mesh, this.edges]) {
      if (o) {
        this.scene.remove(o);
        for (const m of [].concat(o.material)) if (m !== this.edgeMaterial) m.dispose();
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
    this._observer.disconnect();
    window.removeEventListener("resize", this._onWindowResize);
    this.clear();
    for (const t of this.textures.values()) t.dispose();
    this.textures.clear();
    this.controls.dispose();
    this.renderer.dispose();
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
