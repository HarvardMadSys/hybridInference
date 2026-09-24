from pathlib import Path


def test_frontend_runtime_image_uses_the_checked_bundle():
    dockerfile = Path("deploy/docker/Dockerfile.frontend").read_text()
    # Asset-copy behavior is exercised by prepare-module.test.ts. Python-only
    # backend gates check the recipe wiring without requiring a Node runtime.
    assert "prepare-module.mjs merge" in dockerfile
    assert "prepare-module.mjs check" in dockerfile
    assert "COPY --from=builder /app/.next/standalone ./" in dockerfile


def test_direct_frontend_build_keeps_the_standalone_api_default():
    dockerfile = Path("deploy/docker/Dockerfile.frontend").read_text()

    # `env.ts` deliberately distinguishes an omitted value from an explicit
    # empty same-origin value. Docker ARG always materializes a value, so the
    # Dockerfile itself must own the direct-build default; the demo overrides
    # it with an explicit empty build arg.
    assert "ARG NEXT_PUBLIC_API_BASE=http://localhost:8080" in dockerfile
    assert "ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE" in dockerfile


def test_frontend_rewrite_input_is_pinned_and_dotenv_is_outside_the_build_context():
    dockerfile = Path("deploy/docker/Dockerfile.frontend").read_text()
    dockerignore = Path(".dockerignore").read_text().splitlines()

    assert "ARG BACKEND_INTERNAL_URL=http://backend:8080" in dockerfile
    assert "ENV BACKEND_INTERNAL_URL=$BACKEND_INTERNAL_URL" in dockerfile
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "**/.env" in dockerignore
    assert "**/.env.*" in dockerignore
