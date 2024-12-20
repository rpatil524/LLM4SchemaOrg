[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.svg)](https://doi.org/10.5281/zenodo.14391666)


# LLM4SchemaOrg

This repository contains the code and results for the paper [LLM4Schema.org: Generating Schema.org Markups with Large Language Models](https://semantic-web-journal.net/content/llm4schemaorg-generating-schemaorg-markups-large-language-models).

# Structure

- Every inputs/outputs for the XP with 180 webpages are located under `data/WDC/Pset`.
- Every inputs/outputs for the XP with Schema.org examples are located under `data/WDC/SchemaOrg`.

# Pre-requisites:

- Python 3.10+ is required.

- Setup [llama-cpp-python](https://llama-cpp-python.readthedocs.io/en/stable/server/) (steps may vary based on your hardware):
```bash
# Install with server module on CUDA capable machine
CMAKE_ARGS="-DLLAMA_CUDA=on -DLLAMA_CUBLAS=on" FORCE_CMAKE=1 pip install -U "llama-cpp-python[server]" --force-reinstall --no-deps --no-cache-dir

# After modifying the config files, start the server
python -m llama_cpp.server --config configs/llama_cpp.json

# Optional: Launch the chatbot-ui for manual debug
docker run -e OPENAI_API_HOST=http://localhost:8084 -e OPENAI_API_KEY="" --network host ghcr.io/mckaywrigley/chatbot-ui:main
```

- Install Spacy:
```bash
pip install spacy
python -m spacy download en_core_web_md
```

- Install the remaining dependencies:
```bash
pip install -r requirements.txt
```

- Setup a SPARQL endpoint containing schema.org definition and examples
```bash
rdflib-endpoint serve --port 5001 schemaorg/schemaorg-all-http.nt
rdflib-endpoint serve --port 5002 schemaorg/examples/schemaorg-all-examples.ttl
```

# Reproduce main experiment in the paper

## Steps
- Generate prompts using LLM
```bash
snakemake -p -s snakefiles/generate.smk -c1 --rerun-incomplete --config data_dir=data/WDC/Pset prompt_template=text2kg_prompt3
```

- Evaluate against human prompt
```bash
snakemake -p -s snakefiles/evaluate.smk -c1 --rerun-incomplete --config data_dir=data/WDC/Pset prompt_template=text2kg_prompt3
```

- Assemble to a final csv
```bash
snakemake -p -s snakefiles/evaluate.smk -c1 --rerun-incomplete --config data_dir=data/WDC/Pset prompt_template=text2kg_prompt3
```

- Use the notebooks to obtain elements used in the paper.

## Hardware information

```
System:
    Distro: Ubuntu 22.04.3 LTS (Jammy Jellyfish)
Memory:
  RAM: total: 33.25 GiB used: 5.43 GiB (16.3%)
CPU:
  Info: 18-core model: Intel Xeon Platinum 8260 bits: 64 
  Speed (MHz): avg: 2394 
  Flags: avx avx2 ht lm nx pae sse sse2 sse3 sse4_1 sse4_2 ssse3 vmx
Graphics:
  Device-2: NVIDIA TU104GL [Tesla T4] driver: nvidia v: 535.161.07
  VRAM: 15360MiB
```

# Validate LLMs capability as Conformance and Factuality checker

## Steps

- Conformance:
```bash
python markup/schemaorg_examples_dataset.py evaluate-compliance-checker Mixtral_8x7B_Instruct schemaorg/examples/misc/compliance.parquet .tmp/compliance_mixtral_p --template prompts/validation/compliance.json
```

- Factual:
```bash
python markup/schemaorg_examples_dataset.py evaluate-factual-checker Mixtral_8x7B_Instruct schemaorg/examples/misc/factual-extrinsic.parquet data/WDC/SchemaOrg/factual_extrinsic_mixtral_p --template prompts/validation/factual_p.json

python markup/schemaorg_examples_dataset.py evaluate-factual-checker Mixtral_8x7B_Instruct schemaorg/examples/misc/factual-intrinsic.parquet data/WDC/SchemaOrg/factual_intrinsic_mixtral_p --template prompts/validation/factual_p.json
```

# Bring your own LLM

- In `markup/models/llm.py`, add a new class that inherits:
    - `LlamaCPP`: any quantized model that are available on HuggingFace.
    - `GPT`: any model deployed on OpenAI's API v1 servers. 
    - `AbstractModelLLM`: others.

- Rewrite the method `predict` and `query` in your child class if necessary.

# Bring your own validator

- In `markup/models/validator.py`, add a new class that `AbstractValidator`:
  - `validate` method: returns a score
  - `map_reduce_validate` method: if your validator requires long text as input, e.g, the entire webpage, this method should split the input into chunks, apply `validate` on each chunk, then aggregate the results.

