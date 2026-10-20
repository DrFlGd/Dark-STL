// A small shared store for the interface: plain object state, change listeners,
// and a hook that re-renders a component when the parts it reads change.
import { useEffect, useState } from "./html.js";

export function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get: () => state,
    /** Shallow-merge a patch (object or function of the current state). */
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      state = { ...state, ...next };
      for (const l of [...listeners]) l(state);
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}

/** Re-render when select(state) changes (compared by identity, or shallowly for arrays/objects). */
export function useStore(store, select = (s) => s) {
  const [value, setValue] = useState(() => select(store.get()));
  useEffect(() => {
    let last = select(store.get());
    setValue(() => last);
    return store.subscribe((s) => {
      const next = select(s);
      if (!shallowEqual(next, last)) { last = next; setValue(() => next); }
    });
  }, [store]);
  return value;
}

function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || !a || !b) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => Object.is(a[k], b[k]));
}
