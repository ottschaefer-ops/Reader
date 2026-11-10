// Sends one Web Push message to each subscription, with no packages:
// RFC 8291 (aes128gcm) encryption and RFC 8292 (VAPID) signing.
//
// usage: node webpush.mjs < job.json
//   job.json: { "subs": [{ endpoint, keys: { p256dh, auth } }], "payload": "...", "subject": "https://..." }
//   env VAPID_PRIVATE_KEY: the base64url P-256 private key (32 bytes)
// Prints one line per subscription: the push service's status code.
import crypto from "node:crypto";

const b64u = b => Buffer.from(b).toString("base64url");
const unb64u = s => Buffer.from(String(s).replace(/=+$/, ""), "base64url");

export function vapidKeys(privB64) {
  const d = unb64u(privB64);
  const ecdh = crypto.createECDH("prime256v1");
  ecdh.setPrivateKey(d);
  const pub = ecdh.getPublicKey(); // 65 bytes, uncompressed
  const key = crypto.createPrivateKey({ format: "jwk", key: {
    kty: "EC", crv: "P-256", d: b64u(d), x: b64u(pub.subarray(1, 33)), y: b64u(pub.subarray(33, 65)) } });
  return { key, pub };
}

export function vapidHeader(endpoint, subject, vk, now = Math.floor(Date.now() / 1000)) {
  const aud = new URL(endpoint).origin;
  const head = b64u(JSON.stringify({ typ: "JWT", alg: "ES256" }));
  const body = b64u(JSON.stringify({ aud, exp: now + 12 * 3600, sub: subject }));
  const sig = crypto.sign("sha256", Buffer.from(head + "." + body), { key: vk.key, dsaEncoding: "ieee-p1363" });
  return `vapid t=${head}.${body}.${b64u(sig)}, k=${b64u(vk.pub)}`;
}

// as (the sender's one-time key pair) and salt may be given for test vectors.
export function encrypt(payload, keys, as = null, salt = crypto.randomBytes(16)) {
  const uaPub = unb64u(keys.p256dh), auth = unb64u(keys.auth);
  if (!as) { as = crypto.createECDH("prime256v1"); as.generateKeys(); }
  const asPub = as.getPublicKey();
  const secret = as.computeSecret(uaPub);
  const ikm = Buffer.from(crypto.hkdfSync("sha256", secret, auth,
    Buffer.concat([Buffer.from("WebPush: info\0"), uaPub, asPub]), 32));
  const cek = Buffer.from(crypto.hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
  const nonce = Buffer.from(crypto.hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
  const c = crypto.createCipheriv("aes-128-gcm", cek, nonce);
  const ct = Buffer.concat([c.update(Buffer.concat([Buffer.from(payload), Buffer.from([2])])), c.final(), c.getAuthTag()]);
  const rs = Buffer.alloc(4); rs.writeUInt32BE(4096);
  return Buffer.concat([salt, rs, Buffer.from([asPub.length]), asPub, ct]);
}

export async function send(sub, payload, subject, vk, ttl = 3600) {
  const r = await fetch(sub.endpoint, { method: "POST", body: encrypt(payload, sub.keys), headers: {
    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
    TTL: String(ttl), Urgency: "normal", Authorization: vapidHeader(sub.endpoint, subject, vk) } });
  return r.status;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const job = JSON.parse(await new Promise(ok => { let s = ""; process.stdin.on("data", d => s += d).on("end", () => ok(s)); }));
  const vk = vapidKeys(process.env.VAPID_PRIVATE_KEY || "");
  for (const sub of job.subs || []) {
    let host = "?"; try { host = new URL(sub.endpoint).host; } catch {}
    try { console.log(host, await send(sub, job.payload, job.subject, vk)); }
    catch (e) { console.log(host, "failed:", e.message); }
  }
}
