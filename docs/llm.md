# Language-model explanations (optional)

The two agents are **rule-based**: the injection agent ranks experiments and the monitoring agent ranks weaknesses with fixed rules, and the resilience score is a fixed formula. A language model can optionally **write the explanation text** for each finding and each proposal. Nothing else changes with or without it.

## What the model can and cannot do

| It can | It cannot |
|---|---|
| Reword the explanation of a finding and the rationale of a proposal, in one or two sentences | Change a ranking, a severity, a score, a fix suggestion or any number |
| | Start, stop or change a run, or touch `chaos/policy.json` or `experiments/` |

Every text it returns is checked: it must be short, and **every number in it must already be in the evidence** it was given. A text with an invented number, a bad JSON shape, a timeout or an unreachable endpoint is dropped, the rule-based text stays, and the panel says so ("The language model gave no usable text for N of M item(s) ...").

The dashboard labels each text, for example **"Explained by llama-3.3-70b-versatile (hosted)"**, **"Explained by qwen2.5:7b (local)"** or **"Rule-based explanation"**. The downloadable report carries the same label.

## What is sent

Only run data the Findings and Suggested-experiments panels already show: finding and experiment ids, targets, faults, run ids, stop reasons, and the numbers from the run records. **Not** sent: the operator token, environment variables, file paths, the API key (other than as the `Authorization` header to the model endpoint itself), or anything from your machine beyond that.

With a **hosted** endpoint such as Groq this data goes to a third party. With a **local** Ollama it does not leave your machine. Do not turn this on for runs whose names or numbers you consider confidential.

## Option 1: Groq (hosted, free tier available)

1. Create an API key in the Groq console (https://console.groq.com) and pick a chat model from the list your account offers (for example `llama-3.3-70b-versatile`; model names change, so check the current list).
2. Put the key in your shell. **Never** put it in a file in this repository, in a command-line argument, or in an issue or message.

   ```bash
   export LLM_API_KEY="..."                  # macOS / Linux
   ```

   ```powershell
   $env:LLM_API_KEY = "..."                  # Windows PowerShell
   ```
3. Start with the model:

   ```bash
   python scripts/dev.py up --execute --llm llama-3.3-70b-versatile --llm-base-url https://api.groq.com/openai/v1
   ```

   `up` checks first that the key is accepted and that the model is offered, and stops with a clear message if not. It prints that run summaries will be sent to `api.groq.com`.

## Option 2: Ollama (local)

1. Install Ollama (https://ollama.com), then `ollama pull qwen2.5:7b` (or `llama3.1:8b`).
2. Start it (`ollama serve`, or the desktop app), then:

   ```bash
   python scripts/dev.py up --execute --llm qwen2.5:7b
   ```

   The default endpoint is `http://127.0.0.1:11434/v1`, so no key and no flag are needed.

## Other endpoints and manual setup

Any OpenAI-compatible `chat/completions` API works (OpenAI: `https://api.openai.com/v1`, OpenRouter: `https://openrouter.ai/api/v1`). To start the API yourself instead of `dev.py`:

| Variable | Meaning |
|---|---|
| `INTELLIGENCE_REASONING_MODE=live` | turns it on; anything else keeps the rule-based text |
| `INTELLIGENCE_LIVE_MODEL` | model id |
| `LLM_BASE_URL` | default `http://127.0.0.1:11434/v1` |
| `LLM_API_KEY` | required for a hosted endpoint; hosted endpoints must use `https` |

Check what is active with `python scripts/dev.py status` ("explanations: ...").

## Key handling

The key is read from `LLM_API_KEY` in the environment and used only as the `Authorization` header of requests to `LLM_BASE_URL`. It is not logged, not included in error messages or API responses, not written to `.dev/api.json`, and not stored in the cache. `dev.py` takes no key argument.

## Speed and the cache

Accepted texts are cached in `results/.llm_cache/`, one small file per explanation, keyed by a hash of the model and that finding's own data. Reloading the dashboard, or asking again with the same runs, does not call the model. A new run changes only the affected findings, so only those are sent. Delete the folder to regenerate. The routes are synchronous, so a slow model never blocks Stop or the live panel.

## If it does not work

| You see | Likely cause |
|---|---|
| "Rule-based explanation" everywhere and a limitation about `LLM_API_KEY` | key not exported in the shell that started the API |
| "gave no usable text for N of M" | endpoint unreachable, request timed out (20 s), or the model invented a number; try another model |
| `up` stops with "rejected the API key" | wrong or revoked key |
| `up` stops with "does not offer the model" | a model id your account does not list |
