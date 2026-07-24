import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    // Node does not implement Cloudflare's built-in module. Tests use only the
    // constructor/env behavior; Wrangler still bundles the real runtime module.
    alias: {
      "cloudflare:workers": fileURLToPath(
        new URL("./test/cloudflare-workers.ts", import.meta.url),
      ),
    },
  },
  test: {
    include: ["test/**/*.test.ts"],
  },
});
