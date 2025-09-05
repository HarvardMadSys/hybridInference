# hybridInference

A high-performance hybrid inference server providing local deployment and offline API access to various LLM providers.

## Project Structure

```
hybrid-inference/
├── serving/         # API serving layer
├── routing/         # Request routing logic
├── database/        # Database abstraction layer
├── client/          # Client implementations
├── llama_benchmark/ # Performance benchmarking suite
├── utils/           # Shared utilities
├── test/            # Test suite
├── config/          # Configuration files
├── scripts/         # Utility scripts
└── docs/            # Documentation
```

## Development Setup

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended) or conda

### Quick Start with uv (Recommended)

```bash
# Clone the repository
git clone <repository-url>
cd hybridInference

# Set up development environment
make setup-dev

# Or manually:
uv venv -p 3.10
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv sync
```

### Alternative: conda Setup

```bash
# Create and activate conda environment
conda create -n hybrid_inference python=3.10 -y
conda activate hybrid_inference

# Install dependencies from pyproject.toml
pip install -e .
```

## Package Management

This project uses `pyproject.toml` for dependency management (PEP 517/518 standard).

### Adding Dependencies

```bash
# Add runtime dependency
uv add fastapi httpx pydantic

# Add development dependency
uv add --group dev pytest black mypy

# Update a package
uv add pandas --upgrade

# Sync all dependencies
uv sync
```

### Development Workflow

```bash
# Format code
make format

# Run linters
make lint

# Type checking
make typecheck

# Run tests
make test

# Run all checks
make check

# Clean build artifacts
make clean
```

## Configuration

### 1. Environment Variables

Create a `.env` file from the template:

```bash
cp .env.example .env
```

Edit `.env` and add your API keys:

```env
OPENAI_API_KEY=your-actual-openai-api-key
LLAMA_API_KEY=your-actual-llama-api-key
GEMINI_API_KEY=your-actual-gemini-api-key
```

### 2. Start Local vLLM Server

```bash
# Start with default settings (4 GPUs: 4,5,6,7)
./scripts/start_vllm.sh

# Use all 8 GPUs (0-7)
USE_ALL_GPU=true ./scripts/start_vllm.sh

# Custom model path
MODEL_PATH="/path/to/your/model" ./scripts/start_vllm.sh

# Custom port
PORT=8002 ./scripts/start_vllm.sh
```

## Code Quality Standards

This project follows industry best practices:

- **Code Style**: Google Python Style Guide (enforced by yapf)
- **Linting**: ruff with extensive rule sets
- **Type Checking**: mypy with strict mode
- **Documentation**: Google-style docstrings (pydocstyle)
- **Pre-commit Hooks**: Automated quality checks
- **Security**: Secret scanning with gitleaks

### Pre-commit Hooks

Pre-commit hooks run automatically on git commit:

```bash
# Install pre-commit hooks (done by make setup-dev)
pre-commit install

# Run manually on all files
pre-commit run --all-files

# Skip hooks temporarily
git commit --no-verify
```

## Testing

```bash
# Run all tests
make test

# Verbose output
make test-verbose

# With coverage
make test-cov

# Specific test file
uv run pytest test/test_routing.py

# Run tests with markers
uv run pytest -m "not slow"  # Skip slow tests
uv run pytest -m integration  # Only integration tests
```

## Contributing

1. Create a feature branch
2. Make your changes
3. Run `make all` to format and validate code
4. Submit a pull request

## License

Proprietary - All rights reserved