// Preact + htm, vendored as plain ES modules (tools/vendor_preact.py): JSX-like
// templates with no build step, so the site still deploys as static files.
import { h, render, Fragment, createRef } from "../vendor/preact/src/index.js";
import htm from "../vendor/htm/index.mjs";

export { h, render, Fragment, createRef };
export { useState, useEffect, useMemo, useRef, useCallback, useLayoutEffect } from "../vendor/preact/hooks/index.js";
export const html = htm.bind(h);
