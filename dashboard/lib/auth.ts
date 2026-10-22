// Single-password login. The session cookie is "<expiry>.<hmac(expiry)>",
// signed with AUTH_SECRET, so it can be checked without a database.
// Uses Web Crypto so the same code runs in proxy.ts and server components.

export const SESSION_COOKIE = "hedj_session";
export const SESSION_DAYS = 30;

const encoder = new TextEncoder();

function secret(): string {
  const s = process.env.AUTH_SECRET;
  if (!s || s.length < 32) throw new Error("AUTH_SECRET must be set (32+ characters)");
  return s;
}

function toBase64Url(bytes: ArrayBuffer): string {
  let bin = "";
  for (const b of new Uint8Array(bytes)) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sign(value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret()), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return toBase64Url(await crypto.subtle.sign("HMAC", key, encoder.encode(value)));
}

function equal(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function createSession(now = Date.now()): Promise<string> {
  const expires = String(now + SESSION_DAYS * 24 * 3600 * 1000);
  return `${expires}.${await sign(expires)}`;
}

export async function verifySession(token: string | undefined, now = Date.now()): Promise<boolean> {
  if (!token) return false;
  const [expires, mac] = token.split(".");
  if (!expires || !mac || Number(expires) < now) return false;
  return equal(mac, await sign(expires));
}

export async function passwordMatches(attempt: string): Promise<boolean> {
  const expected = process.env.DASHBOARD_PASSWORD;
  if (!expected) return false;
  // Compare HMACs so the comparison takes the same time whatever the input
  return equal(await sign(`pw:${attempt}`), await sign(`pw:${expected}`));
}
