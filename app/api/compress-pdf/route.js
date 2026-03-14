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

function isTruthy(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return ["1", "true", "yes", "on"].includes(normalized);
}

export async function POST(request) {
  try {
    const form = await request.formData();
    const pdf = form.get("pdf");
    const targetPercent = Number(form.get("target_percent") || 30);
    const forceCompression = isTruthy(form.get("force_compression"));

    if (!pdf || typeof pdf === "string" || !pdf.name) {
      return NextResponse.json({ error: "No PDF file provided." }, { status: 400 });
    }

    const sessionDir = await fs.mkdtemp(path.join(os.tmpdir(), "pdfcompress_"));
    const inputPath = path.join(sessionDir, "input.pdf");
    await writeUploadedFile(pdf, inputPath);

    const result = runConversionWorker("compress_pdf", {
      input_path: inputPath,
      tmp_dir: sessionDir,
      target_percent: targetPercent,
      force_compression: forceCompression,
      original_filename: pdf.name,
    });

    const sessionId = createSession(sessionDir, {
      pdf: result.pdf_name,
    });

    return NextResponse.json({
      session: sessionId,
      pdf: `/api/download/${sessionId}/${result.pdf_name}`,
      original_bytes: result.original_bytes,
      compressed_bytes: result.compressed_bytes,
      reduction_percent: result.reduction_percent,
      target_percent: result.target_percent,
      achieved_target: result.achieved_target,
      profile: result.profile,
      force_requested: result.force_requested,
      forced_used: result.forced_used,
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Compression failed: ${error.message}` },
      { status: 500 }
    );
  }
}
