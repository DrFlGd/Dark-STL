// What can be done to a game and its folders, the same from the sidebar's
// right-click menu, a game's ⋯ menu and Settings.
import { ui } from "./state.js";
import { api, followJob, loadOverview, showInFolder, toast, plural } from "./api.js";

export async function rescanFolder(path) {
  try {
    const { job } = await api("folder_rescan", { path });
    loadOverview();
    const r = await followJob(job, `Rescanning ${path.split(/[\\/]/).pop()}`);
    toast(`Found ${plural(r.count, "model")} in ${r.path}`);
  } catch (e) {
    toast(String(e.message || e));
  }
  loadOverview();
}

export function removeFolder(path, game) {
  ui.set({
    dialog: {
      type: "confirm", title: "Remove this folder from the library?", button: "Remove",
      text: `${path} and its models leave ${game}'s library. Nothing is deleted from your disk, and you can add the folder again later.`,
      run: async () => {
        await api("folder_remove", { path });
        await loadOverview();
        toast(`Removed ${path} from the library`);
      },
    },
  });
}

export const changeGame = (path, game) => ui.set({ dialog: { type: "change-game", path, game } });

/** Open the Collect dialog for a game, a place in it ({ game, category, group }) or one model ({ game, model }). */
export const collectInto = (scope) => ui.set({ dialog: { type: "collect", ...scope } });

const under = (file, folder) => {
  const f = folder.replace(/[\\/]+$/, "");
  return file.startsWith(f) && /[\\/]/.test(file.charAt(f.length));
};

/** Whether a model file is one of the copies Dark STL keeps in its own library folder. */
export function isCollected(file) {
  if (!file) return false;
  return (ui.get().overview?.games || []).some((g) => g.folders.some((f) => f.collected && under(file, f.path)));
}

const COLLECTED = "These are already the copies Dark STL keeps.";

/** The menu item that collects a game, or a place in it when `place` is given. */
export function collectItem(game, place) {
  const inApp = game.folders.length > 0 && game.folders.every((f) => f.collected);
  return { id: "game-collect", label: "Collect into the app…", sub: "copy the files its models need, so the game's folder isn't required", icon: "inbox",
    run: () => collectInto({ game: game.name, category: place?.category || null, group: place?.group || null }), disabled: inApp ? COLLECTED : false };
}

/** The menu item that collects one model (a catalog entry or an opened file). */
export function collectModelItem(m, game = m.game) {
  return { id: "collect", label: "Collect into the app…", icon: "inbox", disabled: isCollected(m.file) ? "This is already the copy Dark STL keeps." : false,
    run: () => collectInto({ game: game || null, model: { id: m.id, label: m.label || m.name, category: m.category || null, group: m.group || null } }) };
}

/** The game's menu: add a folder to it, collect it, and each of its folders' actions. */
export function gameMenu(game, place) {
  const items = [
    { id: "game-add-folder", label: "Add another folder to this game…", icon: "plus", run: () => ui.set({ dialog: { type: "add-folder", game: game.name } }) },
    collectItem(game, place),
  ];
  for (const f of game.folders) {
    const name = f.path.split(/[\\/]/).filter(Boolean).pop();
    items.push({ sep: true }, { head: f.collected ? `${name} (kept by Dark STL)` : name });
    items.push({ id: "folder-show", label: "Show in folder", icon: "folder", run: () => showInFolder(f.path), disabled: f.missing ? "This folder isn't there any more." : false });
    items.push({ id: "folder-rescan", label: "Rescan", sub: "after unpacking or adding files", icon: "refresh", run: () => rescanFolder(f.path), disabled: f.missing ? "This folder isn't there any more." : false });
    items.push({ id: "folder-game", label: "Change game…", icon: "edit", run: () => changeGame(f.path, game.name) });
    items.push({ id: "folder-remove", label: "Remove from library…", icon: "trash", danger: true, run: () => removeFolder(f.path, game.name) });
  }
  return items;
}
