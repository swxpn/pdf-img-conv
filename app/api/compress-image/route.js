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
    const image = form.get("image");
    const targetPercent = Number(form.get("target_percent") || 30);
    const forceCompression = isTruthy(form.get("force_compression"));

    if (!image || typeof image === "string" || !image.name) {
      return NextResponse.json({ error: "No image file provided." }, { status: 400 });
    }

    const ext = path.extname(image.name || "").toLowerCase() || ".img";
    const sessionDir = await fs.mkdtemp(path.join(os.tmpdir(), "imgcompress_"));
    const inputPath = path.join(sessionDir, `input${ext}`);
    await writeUploadedFile(image, inputPath);

    const result = runConversionWorker("compress_image", {
      input_path: inputPath,
      tmp_dir: sessionDir,
      target_percent: targetPercent,
      force_compression: forceCompression,
      original_filename: image.name,
    });

    const sessionId = createSession(sessionDir, {
      image: result.image_name,
    });

    return NextResponse.json({
      session: sessionId,
      image: `/api/download/${sessionId}/${result.image_name}`,
      original_bytes: result.original_bytes,
      compressed_bytes: result.compressed_bytes,
      reduction_percent: result.reduction_percent,
      target_percent: result.target_percent,
      achieved_target: result.achieved_target,
      force_requested: result.force_requested,
      forced_used: result.forced_used,
    });
  } catch (error) {
    return NextResponse.json(
      { error: `Image compression failed: ${error.message}` },
      { status: 500 }
    );
  }
}
