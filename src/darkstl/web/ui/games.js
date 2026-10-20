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

/** The game's menu: add a folder to it, and each of its folders' actions. */
export function gameMenu(game) {
  const items = [{ id: "game-add-folder", label: "Add another folder to this game…", icon: "plus", run: () => ui.set({ dialog: { type: "add-folder", game: game.name } }) }];
  for (const f of game.folders) {
    const name = f.path.split(/[\\/]/).filter(Boolean).pop();
    items.push({ sep: true }, { head: name });
    items.push({ id: "folder-show", label: "Show in folder", icon: "folder", run: () => showInFolder(f.path), disabled: f.missing ? "This folder isn't there any more." : false });
    items.push({ id: "folder-rescan", label: "Rescan", sub: "after unpacking or adding files", icon: "refresh", run: () => rescanFolder(f.path), disabled: f.missing ? "This folder isn't there any more." : false });
    items.push({ id: "folder-game", label: "Change game…", icon: "edit", run: () => changeGame(f.path, game.name) });
    items.push({ id: "folder-remove", label: "Remove from library…", icon: "trash", danger: true, run: () => removeFolder(f.path, game.name) });
  }
  return items;
}
