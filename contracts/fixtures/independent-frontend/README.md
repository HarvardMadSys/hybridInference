# Independent frontend fixture

This dependency-free static application proves that a frontend can consume
HybridInference only through its HTTP, auth, error, and SSE contracts. It has
its own package metadata, lockfile, build context, and tests. It must never
import distribution-owned product code or workspace frontend source.

```bash
npm ci
npm test
npm run build
npm run verify:isolated
```

The generated `dist/` directory can be served by any static file server. The
fixture is a conformance consumer, not a production UI or a template that
downstream distributions must fork. `verify:isolated` copies only this
package's declared source into a random temporary root, then repeats install,
lint, test, build, and package checks without a workspace dependency.
