const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);

export default async (request) => {
  // Configure this in Netlify UI; do not commit backend URLs or secrets here.
  const backend = (process.env.FLASK_BACKEND_URL || "").trim().replace(/\/+$/, "");

  if (!backend) {
    return new Response(
      "FlankVPN proxy is not configured. Set FLASK_BACKEND_URL in Netlify environment variables to the HTTPS URL of the running Flask backend.",
      { status: 503, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } }
    );
  }

  const incomingUrl = new URL(request.url);
  const target = new URL(incomingUrl.pathname + incomingUrl.search, backend);

  const headers = new Headers(request.headers);
  for (const name of HOP_BY_HOP_HEADERS) headers.delete(name);
  headers.set("x-forwarded-host", incomingUrl.host);
  headers.set("x-forwarded-proto", incomingUrl.protocol.replace(":", ""));

  let upstream;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect: "manual",
    });
  } catch {
    return new Response(
      "Could not connect to the Flask backend. Check FLASK_BACKEND_URL and confirm the backend is running.",
      { status: 502, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } }
    );
  }

  const responseHeaders = new Headers();
  upstream.headers.forEach((value, name) => {
    if (!HOP_BY_HOP_HEADERS.has(name.toLowerCase())) responseHeaders.set(name, value);
  });
  responseHeaders.set("cache-control", "no-store");

  return new Response(
    request.method === "HEAD" || upstream.status === 204 || upstream.status === 304
      ? null
      : await upstream.arrayBuffer(),
    { status: upstream.status, headers: responseHeaders }
  );
};

// Handle the original public path directly, avoiding a catch-all rewrite loop.
export const config = { path: "/*" };
