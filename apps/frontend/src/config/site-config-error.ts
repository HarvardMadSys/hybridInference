// Next preserves an explicit digest while redacting server error messages.
// Keep this marker free of configuration values and internal addresses.
export const SITE_CONFIG_ERROR_DIGEST = 'SITE_CONFIG_UNAVAILABLE';

export class SiteConfigLoadError extends Error {
  readonly digest = SITE_CONFIG_ERROR_DIGEST;

  constructor(message: string) {
    super(message);
    this.name = 'SiteConfigLoadError';
  }
}
