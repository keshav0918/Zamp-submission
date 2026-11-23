import { getDocument, GlobalWorkerOptions, Util } from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = pdfWorker;

function embeddedWords(items, viewport) {
  return items.flatMap((item) => {
    if (!item.str?.trim()) return [];
    const [a, b, , , x, y] = Util.transform(viewport.transform, item.transform);
    const angle = Math.atan2(b, a);
    const pitch = item.width / item.str.length;
    return Array.from(item.str.matchAll(/\S+/g), (match) => [
      Math.round(x + match.index * pitch * Math.cos(angle)),
      Math.round(y + match.index * pitch * Math.sin(angle)),
      match[0].slice(0, 120),
    ]);
  });
}

async function scannedWords(page, worker, scale) {
  const viewport = page.getViewport({ scale });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  try {
    await page.render({ canvas, viewport }).promise;
    const { data } = await worker.recognize(canvas, {}, { text: false, blocks: true });
    const words = (data.blocks ?? []).flatMap((block) => block.paragraphs)
      .flatMap((paragraph) => paragraph.lines).flatMap((line) => line.words)
      .filter((word) => word.text.trim());
    return {
      words: words.map(({ text, bbox }) => [
        Math.round(bbox.x0 / scale), Math.round((bbox.y0 + bbox.y1) / (2 * scale)), text.trim().slice(0, 120),
      ]),
      ocr: true,
      uncertain: words.some(({ text, confidence }) => /\d/.test(text) && confidence < 60),
    };
  } finally {
    canvas.width = 0;
    canvas.height = 0;
  }
}

export async function readPdf(file, progress) {
  if (file.size > 20 * 1024 * 1024) throw new Error("Choose a PDF smaller than 20 MB.");
  const task = getDocument({ data: await file.arrayBuffer(), isEvalSupported: false, stopAtErrors: true });
  let worker;
  try {
    const pdf = await task.promise;
    if (pdf.numPages < 1 || pdf.numPages > 10) throw new Error("Choose an invoice with 1–10 pages.");
    const pages = [];
    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
      const page = await pdf.getPage(pageNumber);
      const viewport = page.getViewport({ scale: 1 });
      const label = `Page ${pageNumber} of ${pdf.numPages}`;
      const words = embeddedWords((await page.getTextContent()).items, viewport);
      if (words.reduce((size, word) => size + word[2].length, 0) >= 35) {
        progress(`${label} · reading embedded text`);
        pages.push({ words, ocr: false });
      } else {
        progress(`${label} · loading English OCR`);
        if (!worker) {
          const { createWorker, OEM } = await import("tesseract.js");
          worker = await createWorker("eng", OEM.LSTM_ONLY, {
            workerPath: "/ocr/worker.min.js", corePath: "/ocr/", langPath: "/ocr", workerBlobURL: false,
          });
          await worker.setParameters({ tessedit_pageseg_mode: "6" });
        }
        progress(`${label} · recognizing scanned text`);
        const scale = Math.min(3, 2400 / Math.max(viewport.width, viewport.height));
        pages.push(await scannedWords(page, worker, scale));
      }
      if (pages.at(-1).words.length > 2000) throw new Error(`${label} contains too many words to process.`);
    }
    return pages;
  } catch (error) {
    if (error.name === "PasswordException") throw new Error("This PDF is password-protected.");
    throw error;
  } finally {
    if (worker) await worker.terminate();
    await task.destroy();
  }
}
