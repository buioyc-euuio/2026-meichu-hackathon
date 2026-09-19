#!/bin/bash
set -e
exec "$HOME/work/llama.cpp/build-vulkan/bin/llama-server" \
    -m "$HOME/work/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf" \
    --alias robot-igpu --host 127.0.0.1 --port 18083 \
    -ngl 99 -fa on -ctk q4_0 -ctv q4_0 -c 4096 -np 1 \
    -b 512 -ub 256 -t 4 --jinja --no-webui
