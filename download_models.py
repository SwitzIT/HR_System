"""Download the local Hugging Face models once, before first use.

Only needed when .env uses LLM_PROVIDER=huggingface_local and/or EMBEDDINGS_PROVIDER=huggingface_local.
Run from the project folder, with the virtual environment active:

    python download_models.py

Files go to the Hugging Face cache (default C:\\Users\\<you>\\.cache\\huggingface on Windows;
set HF_HOME in .env to put it on another drive, e.g. HF_HOME=D:\\hf_cache). The app then
loads the models from that cache. No API credits are used. Safe to run again: files that are
already downloaded are skipped.
"""
import os
import sys

from config import Config  # loads .env (including HF_HOME) before huggingface_hub is imported

from huggingface_hub import snapshot_download

# Weights in safetensors format plus config/tokenizer files; skips duplicate formats
# (.bin, ONNX, OpenVINO, GGUF) that the app never loads.
WANTED = ["*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken", "tokenizer*", "*.jinja"]
SKIP = ["*.bin", "*.pt", "*.h5", "*.msgpack", "*.onnx", "*.gguf", "onnx/*", "openvino/*", "*/onnx/*", "*/openvino/*"]


def fetch(model_id, label):
    if os.path.isdir(model_id):
        print(f"{label}: {model_id} is a local folder, nothing to download.")
        return
    print(f"{label}: downloading {model_id} ...")
    path = snapshot_download(repo_id=model_id, allow_patterns=WANTED, ignore_patterns=SKIP,
                             token=Config.HF_TOKEN or None)
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)
    print(f"{label}: ready ({size / 1e9:.2f} GB) at {path}")


def main():
    targets = []
    if Config.LLM_PROVIDER == "huggingface_local":
        targets.append((Config.LLM_MODEL, "Chat model"))
    if Config.EMBEDDINGS_PROVIDER == "huggingface_local":
        targets.append((Config.EMBEDDINGS_MODEL, "Embeddings model"))
    if not targets:
        print("Nothing to download: .env doesn't use huggingface_local for LLM_PROVIDER or EMBEDDINGS_PROVIDER.")
        return 0
    for model_id, label in targets:
        try:
            fetch(model_id, label)
        except Exception as e:
            print(f"{label}: FAILED to download {model_id}: {e}")
            return 1
    print("\nDone. Next: flask --app app ai-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
