import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
buildSync({
  entryPoints: [join(root, "dashboard", "ocswarm-serve.ts")],
  outfile: join(root, "runtime", "dashboard-server.mjs"),
  bundle: true,
  platform: "node",
  format: "esm",
});
