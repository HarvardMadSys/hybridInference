import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["live/**/*.live.test.ts"],
    fileParallelism: false,
    retry: 0,
    testTimeout: 960_000,
  },
});
