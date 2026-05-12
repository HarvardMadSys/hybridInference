from pathlib import Path


def test_frontend_runtime_image_includes_public_assets():
    dockerfile = Path("deploy/docker/Dockerfile.frontend").read_text()

    assert "COPY --from=builder /app/public public" in dockerfile
    assert "# COPY --from=builder /app/public public" not in dockerfile
