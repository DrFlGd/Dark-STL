// Dark STL: start-up and routing. The interface is in ui/ (Preact + htm, no
// build step, shared with Model Library and SCAD Workshop); the engine is
// reached through platform.js.
import { createPlatform } from "./platform.js";
import { mountShell, ui } from "./ui/shell.js";
import { routeFromHash } from "./ui/context.js";
import { loadOverview } from "./ui/api.js";

// errors the page tests read back (tests/test_page.py)
window.__errors = [];
window.addEventListener("error", (e) => window.__errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => window.__errors.push(String(e.reason?.message || e.reason)));

function route() {
  ui.set({ route: routeFromHash(location.hash), navOpen: false, menu: null });
}

async function start() {
  const platform = await createPlatform();
  mountShell({ platform });
  route();
  window.addEventListener("hashchange", route);
  ui.set({ ready: true });
  loadOverview();
}

start();
