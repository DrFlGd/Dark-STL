// The 3D viewer (from Model Library's, itself from Grid Workshop's): a model in
// a Z-up scene on a floor grid in 10 mm squares, orbit with the mouse, three
// views, edges on or off. Dark STL sends meshes in its own packed form
// (darkstl/app/meshpack.py) so each material can carry its game texture.
import * as THREE from "three";
import { OrbitControls } from "./vendor/three/OrbitControls.js";

const STEP = 10; // mm
export const GRAY = "#a3a9ae";
const MAGIC = "DSTLMESH";

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
    this.textures = [];
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
    this.redraw();
  }

  setTheme(dark) {
    if (this.dark === !!dark) return;
    this.dark = !!dark;
    this._buildGrid(this.bbox || new THREE.Box3(new THREE.Vector3(-30, -30, 0), new THREE.Vector3(30, 30, 0)));
    this.redraw();
  }

  /** Show a packed mesh. `textures`: { material index: image URL } (textured view), or null for clay.
   *  `keepView`: leave the camera where it is (the same model, drawn again). */
  show(buf, { textures = null, wireframe = false, keepView = false } = {}) {
    const { header, positions, normals, uvs, index } = unpackMesh(buf);
    const geom = new THREE.BufferGeometry();
    // a copy: the page may show the same buffer again (another shading), and translate() moves it in place
    geom.setAttribute("position", new THREE.BufferAttribute(positions.slice(), 3));
    geom.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
    if (uvs) geom.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
    geom.setIndex(new THREE.BufferAttribute(index, 1));
    const [x0, y0, z0] = header.min, [x1, y1] = header.max;
    // rest it on the floor, centred
    geom.translate(-(x0 + x1) / 2, -(y0 + y1) / 2, -z0);
    geom.computeBoundingBox();
    this.clear();
    const materials = [];
    const slot = new Map();
    for (const g of header.groups) {
      if (!slot.has(g.material)) {
        slot.set(g.material, materials.length);
        materials.push(this._material(textures?.[g.material], wireframe));
      }
      geom.addGroup(g.start, g.count, slot.get(g.material));
    }
    if (!materials.length) materials.push(this._material(null, wireframe));
    this.mesh = new THREE.Mesh(geom, materials);
    this.scene.add(this.mesh);
    if (header.triangles < 400000) {
      this.edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 30), this.edgeMaterial);
      this.edges.visible = this.showEdges;
      this.scene.add(this.edges);
    }
    this.bbox = geom.boundingBox.clone();
    this._buildGrid(this.bbox);
    if (!keepView) this.view("iso");
    this.redraw();
    const size = this.bbox.getSize(new THREE.Vector3());
    return { x: size.x, y: size.y, z: size.z, triangles: header.triangles, vertices: header.vertices, edges: !!this.edges };
  }

  _material(url, wireframe) {
    if (!url) {
      const m = this.clay.clone();
      m.wireframe = wireframe;
      return m;
    }
    const tex = new THREE.TextureLoader().load(url, () => this.redraw());
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.flipY = false; // Dark STL's UVs run down the image
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    tex.anisotropy = 4;
    this.textures.push(tex);
    return new THREE.MeshStandardMaterial({ map: tex, roughness: 0.7, metalness: 0.0, side: THREE.DoubleSide, wireframe });
  }

  clear() {
    for (const o of [this.mesh, this.edges]) {
      if (o) {
        this.scene.remove(o);
        o.geometry.dispose();
        for (const m of [].concat(o.material)) if (m !== this.edgeMaterial) m.dispose();
      }
    }
    for (const t of this.textures) t.dispose();
    this.textures = [];
    this.mesh = this.edges = null;
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
