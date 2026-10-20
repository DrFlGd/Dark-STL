// What the interface needs from the rest of the app, set once at start-up by
// app.js: { platform }. And where each page lives in the URL.
export const ctx = {};
export function setContext(values) { Object.assign(ctx, values); }

/** Routes: home | settings | about | browse:<game>[/<category>[/<group>]] (parts URI-encoded) | model:<id>[@<variant>] */
export function routeHash(route) {
  if (route === "home") return "#/";
  if (route.startsWith("browse:")) return `#/browse/${route.slice(7)}`;
  if (route.startsWith("model:")) {
    const [id, variant] = route.slice(6).split("@");
    return `#/model/${encodeURIComponent(id)}${variant ? `?v=${variant}` : ""}`;
  }
  return `#/${route}`;
}

export function routeFromHash(hash) {
  const [path, query = ""] = hash.replace(/^#\/?/, "").split("?");
  if (!path) return "home";
  const [first, ...rest] = path.split("/");
  if (first === "browse" && rest[0]) return `browse:${rest.filter(Boolean).join("/")}`;
  if (first === "model" && rest[0]) {
    const v = new URLSearchParams(query).get("v");
    return `model:${decodeURIComponent(rest[0])}${v ? `@${encodeURIComponent(v)}` : ""}`;
  }
  return ["settings", "about"].includes(first) ? first : "home";
}

/** A place in the library: a game, one of its categories, one of that category's groups. */
export const placeRoute = (game, category, group) =>
  `browse:${[game, category, group].filter((v) => v != null && v !== "").map(encodeURIComponent).join("/")}`;

export function placeOf(route) {
  if (!route?.startsWith("browse:")) return null;
  const [game, category = null, group = null] = route.slice(7).split("/").map(decodeURIComponent);
  return { game, category, group };
}

/** model:<id>[@<variant>] -> { id, variant } */
export function modelOf(route) {
  if (!route?.startsWith("model:")) return null;
  const [id, variant] = route.slice(6).split("@");
  return { id, variant: variant ? decodeURIComponent(variant) : null };
}
