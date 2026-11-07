/** Addresses of the admin Batches tab's pages, shared by its section control and links into it. */
export const BATCHES_PATH = '/dashboard/admin/batches';
/** The requests the batch worker ran, which Recent Requests leaves out. `?batch=<id>` narrows it. */
export const BATCH_REQUESTS_PATH = `${BATCHES_PATH}/requests`;
