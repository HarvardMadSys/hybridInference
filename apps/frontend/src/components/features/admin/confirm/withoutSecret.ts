/**
 * An error message with a just-typed secret blanked out, for errors that may
 * echo a submitted value back. The backend already avoids that; this is a
 * second guard wherever a write carries a secret.
 */
export function withoutSecret(message: string, secret: string): string {
  return secret ? message.split(secret).join('••••') : message;
}
