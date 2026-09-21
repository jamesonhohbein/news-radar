import { defineConfig } from "@playwright/test";

// T10 runs against a server that is already up (the systemd service on the
// homelab host, or `PORT=3087 npm run start` locally) and the live database.
export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  use: { baseURL: process.env.BASE_URL ?? "http://127.0.0.1:3087", headless: true },
  reporter: "list",
});
