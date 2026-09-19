import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    // OneDrive can transiently lock Rust build DLLs while Cargo compiles.
    // They are outside the frontend source and should never trigger a Vite
    // reload, so keep the Tauri target tree out of the watcher.
    watch: {
      ignored: ["**/src-tauri/target/**"],
    },
  },
  preview: {
    host: "127.0.0.1",
  },
});
