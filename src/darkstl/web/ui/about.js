// About: what Dark STL does, the games and files it reads, and what it's built on.
import { html } from "../lib/html.js";
import { info } from "./api.js";
import { CommandBar } from "./layout.js";

const link = (href, text) => html`<a href=${href} target="_blank" rel="noopener noreferrer">${text}</a>`;

export function About() {
  const i = info();
  const games = (i.games || []).filter((g) => g !== "Other models");
  return html`<${CommandBar} id="about-head" title="About Dark STL" count=${html`<span class="page-count" id="about-version">${i.version || ""}</span>`} />
  <div class="pages"><div class="library-settings about-page">
    <section class="ls-section">
      <h2>What it does</h2>
      <p>Dark STL lists the models in your games (FromSoftware's, and Cyberpunk 2077) by game and kind, shows them with their textures, and makes print-ready meshes from them in two steps:</p>
      <ol class="how-list">
        <li><b>Even out the vertices.</b> Game models put few vertices on large flat areas and many on small details. Dark STL spreads the number of vertices you choose evenly over the surface, so detail can go anywhere.</li>
        <li><b>Raise the texture detail.</b> The game paints fine relief (scratches, engravings, stitching, scales) into its normal maps. Dark STL turns that into height and moves each vertex by it.</li>
      </ol>
      <p>Then it exports STL for your slicer, or OBJ, PLY or GLB.</p>
    </section>
    <section class="ls-section">
      <h2>Games and files</h2>
      <p>${games.length ? `Recognised by their folders: ${games.join(", ")}.` : ""} Models are read from .partsbnd, .chrbnd, .objbnd, .geombnd and .mapbnd files (also inside .dcx), loose .flver files and .obj files.
        Cyberpunk 2077 models (.mesh with .xbm textures) are read straight from the game's .archive files, or from files exported with WolvenKit.
        Each game is read by its own importer, so more games can be added; generating and exporting work the same for all of them.</p>
      <p class="muted">Dark STL needs your own copies of the games. It contains no code or data from them.</p>
    </section>
    <section class="ls-section">
      <h2>Built on</h2>
      <ul class="ls-list credits">
        <li><span>The file formats as worked out by the FromSoftware modding community, in particular ${link("https://github.com/JKAnderson/SoulsFormats", "SoulsFormats")} by TKGP and ${link("https://github.com/Grimrukh/soulstruct", "Soulstruct")} by Grimrukh</span></li>
        <li><span>The REDengine formats as worked out by the ${link("https://github.com/WolvenKit/WolvenKit", "WolvenKit")} team (Cyberpunk 2077)</span></li>
        <li><span>The interface of ${link("https://github.com/DrFlGd/Model-Library", "Model Library")} (and SCAD Workshop): its skins, menus, inspector and 3D view</span></li>
        <li><span>${link("https://preactjs.com", "Preact")} (MIT) and ${link("https://github.com/developit/htm", "htm")} (Apache 2.0) for the pages, ${link("https://threejs.org", "three.js")} r186 (MIT) for the 3D view</span></li>
        <li><span>NumPy, SciPy and Pillow for the meshes and textures</span></li>
      </ul>
    </section>
  </div></div>`;
}
