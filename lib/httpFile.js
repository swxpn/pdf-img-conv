import path from "node:path";

const MIME_TYPES = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".pdf": "application/pdf",
  ".zip": "application/zip",
  ".bmp": "image/bmp",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".tiff": "image/tiff",
  ".tif": "image/tiff",
};

export function safeFilename(name) {
  const base = path.basename(String(name || ""));
  if (!base || base === "." || base === "..") {
    return null;
  }
  return base;
}

export function contentTypeFor(filename) {
  const ext = path.extname(filename).toLowerCase();
  return MIME_TYPES[ext] || "application/octet-stream";
}
