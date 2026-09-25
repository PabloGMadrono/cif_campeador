"""Application environment settings, loaded once when this module is imported."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve the project .env independently of the current working directory.
# Existing environment variables take precedence over values in the file.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_OCR_MODEL = os.getenv(
    "OPENROUTER_OCR_MODEL", "qwen/qwen2.5-vl-72b-instruct"
)
LLAMA_CPP_BINARY = os.getenv("LLAMA_CPP_BINARY")
LLAMA_CPP_CPU_BINARY = os.getenv("LLAMA_CPP_CPU_BINARY") or LLAMA_CPP_BINARY
LLAMA_CPP_CUDA_BINARY = os.getenv("LLAMA_CPP_CUDA_BINARY")
SURYA_LLAMA_DEVICE = os.getenv("SURYA_LLAMA_DEVICE", "cpu").strip().lower()
