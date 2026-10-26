import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    // Cargo outputs and staged sidecars can be locked while native builds run.
    watch: {
      ignored: ["**/src-tauri/target/**", "**/src-tauri/binaries/**"],
    },
  },
  preview: {
    host: "127.0.0.1",
  },
});
