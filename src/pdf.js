import { getDocument, GlobalWorkerOptions, Util } from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import englishModel from "@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz?url";
import ocrWorker from "tesseract.js/dist/worker.min.js?url";
import ocrCore from "tesseract.js-core/tesseract-core-lstm.wasm.js?url";

GlobalWorkerOptions.workerSrc = pdfWorker;

const OCR_TIMEOUT = 30_000;

async function startOcr() {
  const { createWorker, OEM } = await import("tesseract.js");
  let timer;
  let expired = false;
  try {
    return await new Promise((resolve, reject) => {
      const fail = (cause) => {
        const message = String(cause).includes("eng.traineddata")
          ? "The English OCR model could not download. Reload the page and try the scan again."
          : "The OCR engine could not start. Reload the page and try again.";
        reject(new Error(message));
      };
      timer = setTimeout(() => reject(new Error("The OCR engine took too long to start. Try the scan again.")), OCR_TIMEOUT);
      createWorker("eng", OEM.LSTM_ONLY, {
        workerPath: new URL(ocrWorker, location.href).href,
        corePath: new URL(ocrCore, location.href).href,
        langPath: new URL(".", new URL(englishModel, location.href)).href,
        workerBlobURL: false,
        cacheMethod: "none",
        errorHandler: fail,
      }).then((worker) => {
        if (expired) void worker.terminate();
        else resolve(worker);
      }, fail);
    });
  } catch (error) {
    expired = true;
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function withOcrTimeout(operation) {
  let timer;
  try {
    return await Promise.race([
      operation,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("OCR took too long on this page. Try a smaller or clearer scan.")), OCR_TIMEOUT);
      }),
    ]);
  } catch (error) {
    throw error instanceof Error ? error : new Error("OCR could not read this page. Try a clearer scan.");
  } finally {
    clearTimeout(timer);
  }
}

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
    const { data } = await withOcrTimeout(worker.recognize(canvas, {}, { text: false, blocks: true }));
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
        if (!worker) {
          progress(`${label} · loading English OCR`);
          worker = await startOcr();
          await withOcrTimeout(worker.setParameters({ tessedit_pageseg_mode: "6" }));
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
