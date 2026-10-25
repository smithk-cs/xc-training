// Run with: npm run test:worker
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { APP_SETTINGS, appEnv, forApp } from "./app.js";

test("every setting in .env.example that the app reads in production is passed through", () => {
  const example = readFileSync(new URL("../.env.example", import.meta.url), "utf8");
  const names = [...example.matchAll(/^#?\s*([A-Z_]+)=/gm)].map((m) => m[1]);
  // Docker-only or demo-only settings are not used on Cloudflare.
  const notOnCloudflare = ["DOMAIN", "TEAM_LOGO_FILE", "SECURE_COOKIES", "AS_OF_DATE"];
  for (const name of names.filter((n) => !notOnCloudflare.includes(n))) {
    assert.ok(APP_SETTINGS.includes(name), `${name} is missing from APP_SETTINGS`);
  }
});

test("appEnv passes set values as strings and leaves out unset ones", () => {
  const env = { SECRET_KEY: "s".repeat(48), NIGHTLY_HOUR: 4, ANTHROPIC_API_KEY: "", XC_APP: {}, UNRELATED: "x" };
  assert.deepEqual(appEnv(env), { SECRET_KEY: "s".repeat(48), NIGHTLY_HOUR: "4" });
});

test("forApp sets the visitor's real address and cannot be fooled", async () => {
  const faked = new Request("https://xc.example/login?x=1", {
    method: "POST",
    body: "csrf=a&username=b",
    headers: { "CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "1.2.3.4", Cookie: "xc_session=abc" },
  });
  const out = forApp(faked);
  assert.equal(out.headers.get("X-Forwarded-For"), "203.0.113.7");
  assert.equal(out.headers.get("X-Forwarded-Proto"), "https");
  assert.equal(out.headers.get("Cookie"), "xc_session=abc");
  assert.equal(out.method, "POST");
  assert.equal(out.url, "https://xc.example/login?x=1");
  assert.equal(await out.text(), "csrf=a&username=b");

  const noIp = forApp(new Request("https://xc.example/", { headers: { "X-Forwarded-For": "1.2.3.4" } }));
  assert.equal(noIp.headers.get("X-Forwarded-For"), null);
});
