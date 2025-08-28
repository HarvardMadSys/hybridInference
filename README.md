# hybridInference

## Installation

### conda 
First, create a conda environment and activate it:
```bash
# Create a new conda environment
conda create -n hybrid_inference python=3.10 -y
conda activate hybrid_inference
```

Then, install the required dependencies:
```bash
pip install -r requirements.txt
```
### uv
```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Quick Start

### 1. Start Local vLLM Server

Use the provided script to start a vLLM server:

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

### 2. Set Up API Keys

Create a `.env` file from the example template:

```bash
cp .env.example .env
```

Then edit `.env` and add your API keys:

```bash
# Edit .env file
OPENAI_API_KEY=your-actual-openai-api-key
LLAMA_API_KEY=your-actual-llama-api-key
```