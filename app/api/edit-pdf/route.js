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
    const operation = String(form.get("operation") || "").trim().toLowerCase();

    if (!["merge", "visual_edit"].includes(operation)) {
      return NextResponse.json({ error: "Invalid PDF edit operation." }, { status: 400 });
    }

    const sessionDir = await fs.mkdtemp(path.join(os.tmpdir(), "pdfedit_"));

    if (operation === "merge") {
      const files = form.getAll("pdfs").filter((f) => typeof f !== "string");
      if (files.length < 2) {
        return NextResponse.json(
          { error: "Merge requires at least two PDF files." },
          { status: 400 }
        );
      }

      const inputPaths = [];
      for (const [index, file] of files.entries()) {
        const target = path.join(sessionDir, `merge_${index}.pdf`);
        await writeUploadedFile(file, target);
        inputPaths.push(target);
      }

      const result = runConversionWorker("modify_pdf", {
        operation,
        tmp_dir: sessionDir,
        input_paths: inputPaths,
      });

      const sessionId = createSession(sessionDir, { file: result.filename });
      return NextResponse.json({
        session: sessionId,
        operation,
        file: `/api/download/${sessionId}/${result.filename}`,
        filename: result.filename,
        page_count: result.page_count,
        output_count: result.output_count,
        summary: result.summary,
      });
    }

    const pdf = form.get("pdf");
    if (!pdf || typeof pdf === "string" || !pdf.name) {
      return NextResponse.json(
        { error: "This operation requires exactly one PDF file." },
        { status: 400 }
      );
    }

    const slotsRaw = String(form.get("slots_json") || "").trim();
    if (!slotsRaw) {
      return NextResponse.json({ error: "No page edits were provided." }, { status: 400 });
    }

    let slots;
    try {
      slots = JSON.parse(slotsRaw);
    } catch (e) {
      return NextResponse.json({ error: `Invalid edit payload: ${e.message}` }, { status: 400 });
    }

    const inputPath = path.join(sessionDir, "input.pdf");
    await writeUploadedFile(pdf, inputPath);

    const result = runConversionWorker("modify_pdf", {
      operation,
      tmp_dir: sessionDir,
      input_path: inputPath,
      slots,
    });

    const sessionId = createSession(sessionDir, { file: result.filename });
    return NextResponse.json({
      session: sessionId,
      operation,
      file: `/api/download/${sessionId}/${result.filename}`,
      filename: result.filename,
      page_count: result.page_count,
      output_count: result.output_count,
      summary: result.summary,
    });
  } catch (error) {
    return NextResponse.json({ error: `PDF edit failed: ${error.message}` }, { status: 500 });
  }
}