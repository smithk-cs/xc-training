// Plain helpers for worker.js, kept separate so they can be tested without the Workers runtime.

// Variables and secrets handed to the app (see .env.example). Unset ones are left out so app defaults apply.
export const APP_SETTINGS = [
  "DATABASE_URL", "SECRET_KEY", "TOKEN_KEY", "GARMIN_WEBHOOK_SECRET", "CRON_SECRET", "SETUP_CODE",
  "GARMIN_CLIENT_ID", "GARMIN_CLIENT_SECRET", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
  "BASE_URL", "TEAM_TIMEZONE", "TEAM_NAME", "NIGHTLY_HOUR",
];

export function appEnv(env) {
  return Object.fromEntries(APP_SETTINGS.filter((k) => env[k]).map((k) => [k, String(env[k])]));
}

// The visitor's real address, set here so it can't be faked: the app limits sign-in attempts per address.
export function forApp(request) {
  const headers = new Headers(request.headers);
  const ip = request.headers.get("CF-Connecting-IP");
  if (ip) headers.set("X-Forwarded-For", ip);
  else headers.delete("X-Forwarded-For");
  headers.set("X-Forwarded-Proto", "https");
  return new Request(request, { headers });
}
