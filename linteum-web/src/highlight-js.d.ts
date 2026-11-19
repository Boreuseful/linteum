// `highlight.js/lib/common` has no per-subpath declarations in v11; the
// package only ships `types/index.d.ts` for the main entry.
declare module "highlight.js/lib/common" {
  import type { HLJSApi } from "highlight.js";

  const hljs: HLJSApi;
  export default hljs;
}
