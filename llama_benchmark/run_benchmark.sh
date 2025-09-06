#!/bin/bash

echo "🚀 Starting Llama Benchmark Suite"
echo "================================"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "Installing dependencies..."
pip install -q aiohttp numpy pandas matplotlib seaborn

echo ""
echo "Running benchmark..."
python3 advanced_benchmark.py

if [ -f "results.json" ]; then
    echo ""
    echo "Generating visualizations..."
    python3 visualize.py
    echo ""
    echo "✅ Benchmark complete!"
    echo "Results saved to: results.json"
    echo "Visualizations saved to: benchmark_results.png"
else
    echo "❌ Benchmark failed - no results file found"
fi
