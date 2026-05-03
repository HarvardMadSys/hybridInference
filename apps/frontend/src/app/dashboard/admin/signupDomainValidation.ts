// Mirrors the server-side regex in serving/servers/routers/admin.py.
// Keeping the two in sync is intentional: the server is authoritative,
// the client just gives faster feedback. Each label must start and end
// with an alphanumeric character (no leading/trailing hyphens).
const DOMAIN_RE = /^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/;

export type ValidatedInput =
  | { ok: true; domain: string; isWildcard: boolean }
  | { ok: false; error: string };

export function validateSignupDomainInput(raw: string): ValidatedInput {
  const cleaned = (raw || '').trim().toLowerCase();
  if (!cleaned) return { ok: false, error: 'Enter a domain.' };

  let isWildcard = false;
  let body = cleaned;
  if (body.startsWith('*.')) {
    isWildcard = true;
    body = body.slice(2);
  }

  if (!body) {
    return { ok: false, error: "Wildcard entries need a suffix after '*.' (e.g. *.example.com)." };
  }
  if (/[*@\s]/.test(body)) {
    return { ok: false, error: "Domain may not contain '*', '@', or whitespace." };
  }
  if (!DOMAIN_RE.test(body)) {
    return {
      ok: false,
      error: "Use 'example.com' or '*.example.com' (letters, digits, hyphens; TLD ≥ 2 letters).",
    };
  }
  return { ok: true, domain: body, isWildcard };
}
