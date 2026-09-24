import { NextRequest, NextResponse } from "next/server";
import { clampInt, rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// Ground-truth events from the primary feeds: not news, not counted in
// attention, drawn as their own layer. What counts as current is per source
// and lives in the primary_live view (schema.sql), so each adapter adds its
// rule there. hours narrows the quake window only (the view caps it at 72 h).
export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const hours = clampInt(q.get("hours"), 72, 1, 72);
  const data = await rows(
    `SELECT * FROM primary_live
      WHERE source <> 'usgs' OR added_at > now() - make_interval(hours => $1)
      ORDER BY added_at DESC
      LIMIT 1000`,
    [hours],
  );
  return NextResponse.json({ hours, events: data });
}
