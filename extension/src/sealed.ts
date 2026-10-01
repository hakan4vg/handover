// The sealed bridge channel (src-tauri/src/pairing.rs has the other side).
// Every message to the app is AES-256-GCM under the key from pairing, with a
// timestamp and a fresh nonce; the app's answer is sealed under the same key
// and bound to that nonce. Anything else listening on the port gets only
// ciphertext, and an answer that does not open is treated as no answer.

export interface Pairing {
  keyId: string;
  /** Base64 of the 32-byte key. */
  key: string;
}

const encoder = new TextEncoder();
const REQUEST_AAD = encoder.encode('dm-bridge-1 request');
const RESPONSE_AAD = 'dm-bridge-1 response ';

const toBase64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes));
const fromBase64 = (text: string) => Uint8Array.from(atob(text), (char) => char.charCodeAt(0));

const keys = new Map<string, Promise<CryptoKey>>();
function cryptoKey(pairing: Pairing): Promise<CryptoKey> {
  let key = keys.get(pairing.keyId);
  if (!key) {
    key = crypto.subtle.importKey('raw', fromBase64(pairing.key), 'AES-GCM', false, ['encrypt', 'decrypt']);
    keys.set(pairing.keyId, key);
  }
  return key;
}

/** The request body for one message, and the nonce its answer is bound to. */
export async function seal(pairing: Pairing, message: unknown): Promise<{ body: string; nonce: string }> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plain = encoder.encode(JSON.stringify({ t: Date.now(), message }));
  const data = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: REQUEST_AAD }, await cryptoKey(pairing), plain));
  const nonce = toBase64(iv);
  return { body: JSON.stringify({ k: pairing.keyId, iv: nonce, data: toBase64(data) }), nonce };
}

/** The answer to the request sealed with `nonce`; throws if it does not open. */
export async function open(pairing: Pairing, nonce: string, envelope: unknown): Promise<unknown> {
  const { iv, data } = (envelope ?? {}) as { iv?: unknown; data?: unknown };
  if (typeof iv !== 'string' || typeof data !== 'string') throw new Error('The answer is not sealed.');
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: fromBase64(iv), additionalData: encoder.encode(RESPONSE_AAD + nonce) }, await cryptoKey(pairing), fromBase64(data));
  return JSON.parse(new TextDecoder().decode(plain)) as unknown;
}
