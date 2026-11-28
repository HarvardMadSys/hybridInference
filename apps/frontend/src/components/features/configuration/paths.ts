import { ROUTING_MODELS_PATH, ROUTING_PATH } from '@/components/features/admin/routingRoutes';

/** Admin → Settings, which holds the configuration: where setup ends and the missing-settings banner points. */
export const ADMIN_SETTINGS_PATH = '/dashboard/admin/settings';

/** Admin → Routing → Models, which holds the model catalog and the per-model overrides. */
export const ADMIN_MODELS_PATH = ROUTING_MODELS_PATH;

/** Admin → Routing → Routes, where a model is created and its routes and weights are changed. */
export const ADMIN_ROUTING_PATH = ROUTING_PATH;
