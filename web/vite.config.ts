import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Local dev against the Compose stack.
      "/api": "http://localhost:8091",
    },
  },
});
