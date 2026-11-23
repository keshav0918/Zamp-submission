import { copyFile, mkdir } from "node:fs/promises";

await mkdir("public/ocr", { recursive: true });
await Promise.all([
  copyFile("node_modules/tesseract.js/dist/worker.min.js", "public/ocr/worker.min.js"),
  copyFile("node_modules/@tesseract.js-data/eng/4.0.0/eng.traineddata.gz", "public/ocr/eng.traineddata.gz"),
  ...["lstm", "simd-lstm"].map((kind) => {
    const name = `tesseract-core-${kind}.wasm.js`;
    return copyFile(`node_modules/tesseract.js-core/${name}`, `public/ocr/${name}`);
  }),
]);
