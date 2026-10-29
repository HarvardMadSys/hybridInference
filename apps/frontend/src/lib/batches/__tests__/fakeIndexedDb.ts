/**
 * A minimal IndexedDB for tests: read-write transactions run one after
 * another, as a real one serializes overlapping read-write transactions on one
 * store. Only what the intent store uses.
 */
export function fakeIndexedDb(): IDBFactory {
  const data = new Map<string, unknown>();
  let previous = Promise.resolve();
  const db = {
    close() {},
    transaction() {
      const ready = previous;
      let pending = 0;
      let finish: () => void = () => {};
      const finished = new Promise<void>((resolve) => {
        finish = resolve;
      });
      previous = finished;
      const tx: { oncomplete?: () => void; objectStore: () => unknown } = {
        objectStore: () => ({
          get: (key: string) => request(() => data.get(key)),
          put: (value: unknown, key: string) => request(() => data.set(key, value)),
          delete: (key: string) => request(() => data.delete(key)),
        }),
      };
      function request<T>(value: () => T) {
        const req: { result?: T; onsuccess?: () => void } = {};
        pending += 1;
        void ready.then(() => {
          req.result = value();
          req.onsuccess?.();
          pending -= 1;
          // Complete once no request is left (a callback may have queued another).
          queueMicrotask(() => {
            if (pending === 0) {
              tx.oncomplete?.();
              finish();
            }
          });
        });
        return req;
      }
      return tx;
    },
  };
  return {
    open() {
      const req: { result?: unknown; onsuccess?: () => void } = {};
      queueMicrotask(() => {
        req.result = db;
        req.onsuccess?.();
      });
      return req;
    },
  } as unknown as IDBFactory;
}
