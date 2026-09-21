import { NextRequest, NextResponse } from "next/server";
import { clampInt, rows } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const region = (q.get("region") ?? "").toUpperCase().slice(0, 8);
  const kind = q.get("kind") === "adm1" ? "adm1" : "country";
  const days = clampInt(q.get("days"), 30, 1, 3650);
  if (!region) return NextResponse.json({ error: "region required" }, { status: 400 });
  const data = await rows<{ day: string; events: number; mentions: number; sources: number }>(
    `SELECT day::text, events, mentions, sources
       FROM attention_daily
      WHERE region_kind = $1 AND region = $2 AND day > current_date - $3::int
      ORDER BY day`,
    [kind, region, days],
  );
  return NextResponse.json({ kind, region, days, series: data });
}
