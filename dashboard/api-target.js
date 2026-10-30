// Where the dashboard sends its /api and /v1 requests: the ChangeWatch API (next.config.js).
// In Docker it is the api container. On Vercel it is your ChangeWatch server, and every request
// carries a project key through this proxy, so the build stops unless that address is https.

const VERCEL_HINT =
  "Set API_URL to your ChangeWatch server's https address, e.g. https://changewatch.example.com " +
  "(Vercel: Project > Settings > Environment Variables), then redeploy.";

function apiTarget(env) {
  const url = String(env.NEXT_PUBLIC_API_URL || env.API_URL || "").trim().replace(/\/+$/, "");
  if (env.VERCEL) {
    if (!/^https:\/\/[^/\s]+/.test(url)) {
      throw new Error(url ? `API_URL must start with https:// on Vercel (got ${url}). ${VERCEL_HINT}` : VERCEL_HINT);
    }
    return url;
  }
  return url || "http://localhost:8000";
}

module.exports = { apiTarget };
