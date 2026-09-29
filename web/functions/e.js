// Cloudflare Pages Function: first-party analytics collector endpoint.
// Path: /e  (same origin as the site, so ad-blockers do not drop it)
// Enriches the browser payload with Cloudflare edge intelligence — crucially
// cf.asOrganization, the organisation that owns the visitor's IP — and forwards
// to the lbtrack collector on Box B.

// Cross-origin use: sites that are not Cloudflare Pages projects (the Rails/Go/Node demos
// on the Hetzner boxes) load lb.js from levelbrook.com and set
// window.LB_CFG = { endpoint: 'https://levelbrook.com/e' }. Only Levelbrook-owned origins
// are allowed; the event carries its own `site` so attribution stays correct.
const ALLOWED_SUFFIXES = ['.levelbrook.com', '.porchlight.ing'];
function corsOrigin(request) {
  const o = request.headers.get('Origin') || '';
  try {
    const h = new URL(o).hostname;
    if (h === 'levelbrook.com' || h === 'porchlight.ing' || ALLOWED_SUFFIXES.some((s) => h.endsWith(s))) return o;
  } catch (e) {}
  return '';
}
function cors(request) {
  const o = corsOrigin(request);
  return o ? { 'Access-Control-Allow-Origin': o, 'Vary': 'Origin' } : {};
}

export async function onRequestPost(context) {
  const { request, env } = context;
  let body;
  try {
    body = await request.json();
  } catch (e) {
    return new Response('{"ok":false}', { status: 400, headers: { ...json(), ...cors(request) } });
  }
  const cf = request.cf || {};
  const h = request.headers;

  const payload = {
    edge: {
      ip: h.get('CF-Connecting-IP') || '',
      asn: cf.asn || 0,
      asOrganization: cf.asOrganization || '',
      country: cf.country || h.get('CF-IPCountry') || '',
      region: cf.region || '',
      city: cf.city || '',
      postalCode: cf.postalCode || '',
      latitude: cf.latitude || '',
      longitude: cf.longitude || '',
      colo: cf.colo || '',
      timezone: cf.timezone || '',
      httpProtocol: cf.httpProtocol || '',
      tlsVersion: cf.tlsVersion || '',
      verifiedBotCategory: cf.verifiedBotCategory || '',
      ua: h.get('User-Agent') || '',
      acceptLanguage: h.get('Accept-Language') || '',
      host: new URL(request.url).hostname,
    },
    events: Array.isArray(body.events) ? body.events.slice(0, 50) : [],
  };

  // Fire and forget — never make the visitor wait on our collector.
  context.waitUntil(
    fetch(env.LBTRACK_URL || 'https://a.levelbrook.com/i', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-LB-Key': env.LBTRACK_KEY || '' },
      body: JSON.stringify(payload),
    }).catch(() => {})
  );

  return new Response('{"ok":true}', { headers: { ...json(), ...cors(request) } });
}

// Preflight for the cross-origin (box-hosted) sites; keep it cheap.
export async function onRequestOptions(context) {
  const c = cors(context.request);
  return new Response(null, {
    status: 204,
    headers: { ...c, 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'POST,OPTIONS', 'Access-Control-Max-Age': '86400' },
  });
}

function json() {
  return { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };
}
