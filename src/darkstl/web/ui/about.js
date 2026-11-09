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
      <p>Dark STL lists the models in your games (FromSoftware's, Cyberpunk 2077, the RE Engine games and Borderlands) by game and kind, shows them with their textures, and makes print-ready meshes from them in two steps:</p>
      <ol class="how-list">
        <li><b>Even out the vertices.</b> Game models put few vertices on large flat areas and many on small details. Dark STL spreads the number of vertices you choose evenly over the surface, so detail can go anywhere.</li>
        <li><b>Raise the texture detail.</b> The game paints fine relief (scratches, engravings, stitching, scales) into its normal maps. Dark STL turns that into height and moves each vertex by it.</li>
      </ol>
      <p>Then it exports STL for your slicer, or OBJ, PLY or GLB.</p>
    </section>
    <section class="ls-section">
      <h2>Games and files</h2>
      <p>${games.length ? `Recognised by their folders: ${games.join(", ")}.` : ""} Models are read from .partsbnd, .chrbnd, .objbnd, .geombnd and .mapbnd files (also inside .dcx), loose .flver files, and models other tools export: glTF (.gltf, .glb) from Blender or FModel, FBX (.fbx) from Blender, Maya, Unreal and the modding tools, ActorX (.psk, .pskx) from UModel or FModel, with the textures their materials name, and .obj files.
        The FromSoftware games that pack them in archives (Dark Souls, Dark Souls II and III, Sekiro, Elden Ring) are unpacked by Dark STL itself, models only, into its own library.
        Cyberpunk 2077 models (.mesh with .xbm textures) are read straight from the game's .archive files, or from files exported with WolvenKit.
        Resident Evil models (RE Engine .mesh files with their .mdf2 materials and .tex textures) are read extracted or inside .pak archives with a file list.
        Borderlands, Borderlands 2 and The Pre-Sequel models are read straight from the game's .upk packages, with the large textures from its .tfc caches; their guns can be put together from their parts.
        Borderlands 3 and Tiny Tina's Wonderlands (Unreal 4) are read from their .pak files and Borderlands 4 (Unreal 5) from its .utoc and .ucas containers, and their guns too can be put together from the part meshes, sorted into slots by their names.
        Returnal (Unreal 4) is read from its .pak files the same way.
        Each game is read by its own importer, so more games can be added; generating and exporting work the same for all of them.</p>
      <p class="muted">Dark STL needs your own copies of the games. It contains no code or data from them.</p>
    </section>
    <section class="ls-section">
      <h2>Built on</h2>
      <ul class="ls-list credits">
        <li><span>The file formats as worked out by the FromSoftware modding community, in particular ${link("https://github.com/JKAnderson/SoulsFormats", "SoulsFormats")} by TKGP and ${link("https://github.com/Grimrukh/soulstruct", "Soulstruct")} by Grimrukh</span></li>
        <li><span>Unpacking the game archives as ${link("https://github.com/Atvaark/BinderTool", "BinderTool")} by Atvaark, UXM by TKGP and ${link("https://github.com/Nordgaren/UXM-Selective-Unpack", "UXM Selective Unpacker")} by Nordgaren do it; the archive keys and the bundled file lists come from ${link("https://github.com/vawser/Smithbox", "Smithbox")} (MIT)</span></li>
        <li><span>The REDengine formats as worked out by the ${link("https://github.com/WolvenKit/WolvenKit", "WolvenKit")} team (Cyberpunk 2077)</span></li>
        <li><span>The RE Engine formats as worked out by the Resident Evil modding community, in particular ${link("https://github.com/NSACloud/RE-Mesh-Editor", "RE Mesh Editor")} by NSACloud, ${link("https://github.com/eigeen/ree-pak-rs", "ree-pak")} by eigeen and ${link("https://github.com/Ekey/REE.PAK.Tool", "REE.PAK.Tool")} by Ekey, and for the animations ${link("https://github.com/kagenocookie/REE-Lib", "REE-Lib")} by shadowcookie and ${link("https://github.com/PredatorCZ/RevilLib", "RevilLib")} by PredatorCZ (Dark STL reads them with code of its own), and Ekey's file lists, which come with Dark STL to name the files in the games' .pak archives</span></li>
        <li><span>The Unreal Engine package and mesh formats as ${link("https://github.com/gildor2/UEViewer", "UE Viewer")} by Gildor (MIT) reads them, and how Borderlands 2 builds its guns from parts as the ${link("https://github.com/zuhuHix/BL2_ReBased", "BL2_ReBased")} project documents it; the Unreal 4 and 5 formats (paks, IoStore containers, zen packages, unversioned properties, .usmap mappings) as ${link("https://github.com/FabianFG/CUE4Parse", "CUE4Parse")} (Apache 2.0) reads them</span></li>
        <li><span>The interface of ${link("https://github.com/DrFlGd/Model-Library", "Model Library")} (and SCAD Workshop): its skins, menus, inspector and 3D view</span></li>
        <li><span>${link("https://preactjs.com", "Preact")} (MIT) and ${link("https://github.com/developit/htm", "htm")} (Apache 2.0) for the pages, ${link("https://threejs.org", "three.js")} r186 (MIT) for the 3D view</span></li>
        <li><span>NumPy, SciPy and Pillow for the meshes and textures</span></li>
      </ul>
    </section>
  </div></div>`;
}
