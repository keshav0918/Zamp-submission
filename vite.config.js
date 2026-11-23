import { defineConfig } from "vite";

export default defineConfig({
  server: { proxy: { "/api": "http://127.0.0.1:8001" } },
  build: {
    rollupOptions: {
      output: {
        // Tesseract appends the original language filename to its model directory.
        assetFileNames: ({ names }) => names.includes("eng.traineddata.gz")
          ? "assets/ocr-[hash]/[name][extname]"
          : "assets/[name]-[hash][extname]",
      },
    },
  },
});
