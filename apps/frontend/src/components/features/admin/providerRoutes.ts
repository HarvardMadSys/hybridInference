/** Addresses of the admin Providers tab's sections, shared by its section control and links into it. */
export const PROVIDERS_PATH = '/dashboard/admin/providers';
/** Each provider's API keys, and the upstream concurrency of every key the gateway calls. */
export const PROVIDER_KEYS_PATH = `${PROVIDERS_PATH}/keys`;
/** The model catalog, and the per-model overrides that apply on top of it. */
export const PROVIDER_MODELS_PATH = `${PROVIDERS_PATH}/models`;
/** Which providers routing may use: a provider switched off here is left out for every model. */
export const PROVIDER_AVAILABILITY_PATH = `${PROVIDERS_PATH}/availability`;
/** What each key's plan has used of its limits, where a quota extension reports it. */
export const PROVIDER_QUOTAS_PATH = `${PROVIDERS_PATH}/quotas`;
/** How each model's providers perform: latency, errors, cache hits and throughput. */
export const PROVIDER_PERFORMANCE_PATH = `${PROVIDERS_PATH}/performance`;
