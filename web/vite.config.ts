import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "site",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
  },
});
