import { NextResponse } from "next/server";
import { rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// When coverage last landed: the newest GDELT file in fetch_log. The map shows
// this rather than the browser's own fetch time, so a stalled ingest reads as
// an old stamp instead of a fresh-looking page.
export async function GET() {
  const [r] = await rows<{ fetched_at: string | null }>(
    `SELECT max(fl.fetched_at) AS fetched_at
       FROM fetch_log fl JOIN source s ON s.id = fl.source_id
      WHERE s.kind = 'gdelt'`,
  );
  return NextResponse.json({ fetched_at: r?.fetched_at ?? null });
}
