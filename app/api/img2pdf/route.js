import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";
import { NextResponse } from "next/server";

import { runConversionWorker } from "../../../lib/pythonRunner";
import { createSession } from "../../../lib/sessionStore";

export const runtime = "nodejs";

async function writeUploadedFile(file, destination) {
  const bytes = Buffer.from(await file.arrayBuffer());
  await fs.writeFile(destination, bytes);
}

export async function POST(request) {
  try {
    const form = await request.formData();
    const files = form.getAll("images").filter((f) => typeof f !== "string");
    const pageSize = String(form.get("page_size") || "A4").toUpperCase();
    const orientation = String(form.get("orientation") || "portrait").toLowerCase();

    if (!files.length) {
      return NextResponse.json({ error: "No image files provided." }, { status: 400 });
    }

    const sessionDir = await fs.mkdtemp(path.join(os.tmpdir(), "img2pdf_"));
    const imagePaths = [];

    for (const file of files) {
      const ext = path.extname(file.name || "").toLowerCase() || ".img";
      const filePath = path.join(sessionDir, `${crypto.randomUUID()}${ext}`);
      await writeUploadedFile(file, filePath);
      imagePaths.push(filePath);
    }

    const result = runConversionWorker("img2pdf", {
      image_paths: imagePaths,
      tmp_dir: sessionDir,
      page_size: pageSize,
      orientation,
    });

    const sessionId = createSession(sessionDir, {
      pdf: result.pdf_name,
    });

    return NextResponse.json({
      session: sessionId,
      pages: result.pages,
      pdf: `/api/download/${sessionId}/${result.pdf_name}`,
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Conversion failed: ${error.message}` },
      { status: 500 }
    );
  }
}
