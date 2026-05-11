#!/usr/bin/env bash
# =============================================================================
# Enquesta Data Quality Agent — demo launcher
# =============================================================================
# Usage:  ./run_demo.sh
#
# This script:
#   1. Verifies Ollama is running
#   2. Verifies the llama3.2:3b model is pulled
#   3. Pre-warms the model (so first LLM call isn't a 15s cold start)
#   4. Launches the Streamlit UI
# =============================================================================

set -e

echo "[1/4] Checking Ollama..."
if ! command -v ollama &> /dev/null; then
    echo "  ERROR: Ollama not installed. Run: brew install ollama"
    exit 1
fi

if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "  Ollama not running. Starting it..."
    ollama serve &
    sleep 3
fi
echo "  Ollama is up."

echo "[2/4] Checking model llama3.2:3b..."
if ! ollama list | grep -q "llama3.2:3b"; then
    echo "  Model not found. Pulling (this takes ~2 min, one-time)..."
    ollama pull llama3.2:3b
fi
echo "  Model ready."

echo "[3/4] Pre-warming model..."
ollama run llama3.2:3b "ready" > /dev/null 2>&1 || true
echo "  Pre-warmed."

echo "[4/4] Launching Streamlit UI on http://localhost:8501 ..."
streamlit run ui/app.py
