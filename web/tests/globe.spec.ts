// T10 (R7): the page loads, the globe renders, and both default layers are
// populated from the live database.
import { expect, test } from "@playwright/test";

test("APIs return rows", async ({ request }) => {
  const a = await (await request.get("/api/anomaly")).json();
  expect(a.regions.length).toBeGreaterThan(0);
  expect(a.regions[0]).toHaveProperty("z");
  expect(a.regions[0]).toHaveProperty("expected");
  const e = await (await request.get("/api/events/top?hours=24&limit=10")).json();
  expect(e.events.length).toBe(10);
  expect(new Set(e.events.map((x: { url: string }) => x.url)).size).toBe(10);
  const outlets = e.events.map((x: { outlets: number }) => x.outlets);
  expect(outlets).toEqual([...outlets].sort((a: number, b: number) => b - a));
  const d = await (await request.get(`/api/attention/daily?region=${e.events[0].country}&days=30`)).json();
  expect(d.series.length).toBeGreaterThan(0);
});

test("primary events carry props and no Green alerts", async ({ request }) => {
  const p = await (await request.get("/api/events/primary?hours=168")).json();
  expect(p.events.length).toBeGreaterThan(0);
  for (const e of p.events) {
    expect(["usgs", "gdacs", "nws", "tsunami"]).toContain(e.source);
    expect(e.props.alert ?? "").not.toBe("Green");
  }
});

test("clamps and rejects bad params", async ({ request }) => {
  expect((await request.get("/api/attention/daily")).status()).toBe(400);
  const e = await (await request.get("/api/events/top?limit=999999&hours=-5")).json();
  expect(e.hours).toBe(1);
  expect(e.events.length).toBeLessThanOrEqual(2000);
});

test("map renders with both default layers", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(page.locator("#map canvas")).toBeVisible();
  await page.waitForFunction(() => window.__newsradar?.ready === true, null, { timeout: 45_000 });
  const state = await page.evaluate(() => window.__newsradar!);
  expect(state.layers).toEqual(["countries-fill", "events", "primary"]);
  expect(state.primary).toBeGreaterThan(0);
  expect(state.regions).toBeGreaterThan(0);
  expect(state.events).toBeGreaterThan(0);
  expect(errors).toEqual([]);
  await expect(page.locator(".panel h1")).toHaveText("news-radar");
});

test("freshness reports the newest GDELT load, under 30 min old", async ({ request }) => {
  const f = await (await request.get("/api/freshness")).json();
  expect(f.fetched_at).toBeTruthy();
  expect(Date.now() - new Date(f.fetched_at).getTime()).toBeLessThan(30 * 60 * 1000);
});

test("page shows the coverage stamp and re-fetches when the tab becomes visible", async ({ page }) => {
  await page.goto("/");
  await page.waitForFunction(() => window.__newsradar?.ready === true, null, { timeout: 45_000 });
  await expect(page.getByTestId("as-of")).toContainText("Coverage as of");
  const refetch = page.waitForRequest((r) => r.url().includes("/api/freshness"));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await refetch;
});
