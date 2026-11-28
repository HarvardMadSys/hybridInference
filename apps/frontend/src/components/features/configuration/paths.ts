import { PROVIDER_MODELS_PATH } from '@/components/features/admin/providerRoutes';

/** Admin → Settings, which holds the configuration: where setup ends and the missing-settings banner points. */
export const ADMIN_SETTINGS_PATH = '/dashboard/admin/settings';

/** Admin → Providers → Models, which holds the model catalog and the per-model overrides. */
export const ADMIN_MODELS_PATH = PROVIDER_MODELS_PATH;

/** Admin → Routing, where a model is created and its routes and weights are changed. */
export const ADMIN_ROUTING_PATH = '/dashboard/admin/routing';
