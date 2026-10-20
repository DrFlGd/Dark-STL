// How the page reaches Dark STL's engine: the local server that serves it
// (darkstl/app/server.py). Everything else in web/ talks only to this object.
//
// createPlatform() -> { info, store, api(cmd, args), binUrl(cmd, args), refreshInfo() }
//   info:   app_info (version, games, export formats, whether native dialogs work)
//   api:    one of the engine's commands (darkstl/app/service.py), answered as JSON
//   binUrl: the address of a mesh or picture (GET /bin/<cmd>), for fetch() or <img src>
//   store:  prefs.get(key, fallback) / prefs.set(key, value), small UI preferences, synchronous

const token = document.querySelector('meta[name="darkstl-token"]')?.content || "";

/** One of the engine's commands. Throws an Error with the engine's message. */
export async function api(cmd, args = {}) {
  let res;
  try {
    res = await fetch(`/api/${cmd}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-DarkSTL-Token": token },
      body: JSON.stringify(args),
    });
  } catch {
    throw new Error("Dark STL isn't running any more. Start it again, then reload this page.");
  }
  const data = await res.json().catch(() => ({ error: `${res.status} ${res.statusText}` }));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data.result;
}

export function binUrl(cmd, args = {}) {
  const q = new URLSearchParams({ ...Object.fromEntries(Object.entries(args).filter(([, v]) => v != null)), t: token });
  return `/bin/${cmd}?${q}`;
}

export async function createPlatform() {
  const info = await api("app_info").catch((e) => ({ error: String(e.message || e), games: [], formats: [], methods: [] }));
  let prefs = await api("prefs_get").catch(() => ({}));
  let timer = null;
  const store = {
    prefs: {
      get(key, fallback = null) { return key in prefs ? prefs[key] : fallback; },
      set(key, value) {
        prefs = { ...prefs, [key]: value };
        clearTimeout(timer);
        timer = setTimeout(() => api("prefs_set", { prefs }).catch(() => {}), 250);
      },
    },
  };
  return {
    info,
    store,
    api,
    binUrl,
    async refreshInfo() { Object.assign(info, await api("app_info")); return info; },
  };
}
