// Cloudflare Worker in front of the app's container (see wrangler.jsonc).
import { Container, getContainer } from "@cloudflare/containers";
import { appEnv, forApp } from "./app.js";

export class XcApp extends Container {
  defaultPort = 8000;
  // Stop the container after 15 minutes without requests; the next request starts it again.
  sleepAfter = "15m";

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = appEnv(env);
  }
}

const app = (env) => getContainer(env.XC_APP, "main");

export default {
  async fetch(request, env) {
    return app(env).fetch(forApp(request));
  },

  // Cron triggers (wrangler.jsonc): ask the app to run its scheduled jobs; it checks the hour itself
  // (the nightly job at NIGHTLY_HOUR, reminder emails at REMINDER_HOUR).
  async scheduled(controller, env) {
    if (!env.CRON_SECRET) {
      console.error("CRON_SECRET is not set; the nightly job cannot run.");
      return;
    }
    const res = await app(env).fetch(new Request("https://xc-app/cron/nightly", {
      method: "POST",
      headers: { Authorization: `Bearer ${env.CRON_SECRET}` },
    }));
    const body = await res.text();
    console.log(`Nightly call (${controller.cron}): ${res.status} ${body}`);
    if (!res.ok) throw new Error(`Nightly job failed: ${res.status} ${body}`);
  },
};
