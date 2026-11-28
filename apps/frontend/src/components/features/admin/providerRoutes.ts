/** Addresses of the admin Providers tab's pages, shared by its section control and links into it. */
export const PROVIDERS_PATH = '/dashboard/admin/providers';
/**
 * Overview's sections below the registry. Keys, Quotas and Availability were
 * pages of their own; an old link to one lands on its section here.
 */
export const PROVIDER_KEYS_ANCHOR = 'keys';
export const PROVIDER_QUOTAS_ANCHOR = 'quotas';
/** How each model's providers perform: latency, errors, cache hits and throughput. */
export const PROVIDER_PERFORMANCE_PATH = `${PROVIDERS_PATH}/performance`;
