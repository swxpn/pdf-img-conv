import path from "node:path";
import { promises as fs } from "node:fs";
import { NextResponse } from "next/server";

import { getSession } from "../../../../../lib/sessionStore";
import { contentTypeFor, safeFilename } from "../../../../../lib/httpFile";

export const runtime = "nodejs";

export async function GET(_request, { params }) {
  const sessionId = params.sessionId;
  const name = safeFilename(params.filename);
  if (!name) {
    return new NextResponse("File not found.", { status: 404 });
  }

  const meta = getSession(sessionId);
  if (!meta) {
    return new NextResponse("Session not found or expired.", { status: 404 });
  }

  const filePath = path.join(meta.dir, name);

  try {
    const content = await fs.readFile(filePath);
    return new NextResponse(content, {
      status: 200,
      headers: {
        "Content-Type": contentTypeFor(name),
        "Cache-Control": "no-store",
        "Content-Disposition": `attachment; filename="${name}"`,
      },
    });
  } catch {
    return new NextResponse("File not found.", { status: 404 });
  }
}
