// Mounts the interface islands into the page: the menu, the main view for the
// current route (each page starts with its command bar), the status bar, the
// dialogs and the menu.
import { html, render } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, initState } from "./state.js";
import { setContext, ctx } from "./context.js";
import { Sidebar } from "./sidebar.js";
import { StatusBar } from "./chrome.js";
import { Home } from "./home.js";
import { Browser } from "./browser.js";
import { ModelPage } from "./modelpage.js";
import { Settings } from "./settings.js";
import { About } from "./about.js";
import { Dialogs } from "./dialogs.js";
import { ContextMenu, onWindowKey } from "./actions.js";

function Main() {
  const route = useStore(ui, (s) => s.route);
  if (route === "settings") return html`<${Settings} />`;
  if (route === "about") return html`<${About} />`;
  if (route.startsWith("model:")) return html`<${ModelPage} key=${route} route=${route} />`;
  if (route.startsWith("browse:")) return html`<${Browser} key=${route} route=${route} />`;
  return html`<${Home} />`;
}

export function mountShell(context) {
  setContext(context);
  window.__darkstl = ctx; // for tests
  initState(context.platform.store);
  const mount = (id, C) => { const el = document.getElementById(id); if (el) render(html`<${C} />`, el); };
  mount("sidebar-root", Sidebar);
  mount("main-root", Main);
  mount("statusbar", StatusBar);
  mount("dialog-root", Dialogs);
  mount("menu-root", ContextMenu);
  addEventListener("keydown", onWindowKey);
}

export { ui };
