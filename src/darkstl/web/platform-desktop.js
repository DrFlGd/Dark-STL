// The desktop app's side of platform.js: the engine reached through the window
// (desktop/src/main.rs), which passes each command to it over a pipe. Meshes and
// pictures come through the window's engine:// addresses (http://engine.localhost/
// on Windows), so <img> and fetch() work as they do with the local server.

const core = globalThis.__TAURI__.core;
const base = core.convertFileSrc("", "engine");

/** One of the engine's commands. Throws an Error with the engine's message. */
export async function api(cmd, args = {}) {
  try {
    return await core.invoke("api", { cmd, args });
  } catch (e) {
    throw new Error(String(e?.message || e));
  }
}

export function binUrl(cmd, args = {}) {
  const q = new URLSearchParams(Object.fromEntries(Object.entries(args).filter(([, v]) => v != null)));
  return `${base}bin/${cmd}?${q}`;
}
