import { NextResponse } from "next/server";

export async function GET() {
  return NextResponse.json({
    ok: true,
    runtime: "nextjs",
    message: "Swift Convert API is running on Next.js",
  });
}
