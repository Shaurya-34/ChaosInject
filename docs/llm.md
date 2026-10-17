# Language-model explanations (optional)

The two agents are **rule-based**: the injection agent ranks experiments and the monitoring agent ranks weaknesses with fixed rules, and the resilience score is a fixed formula. A language model can optionally **write the explanation text** for each finding and each proposal. Nothing else changes with or without it.

The tested path is **Groq** (hosted, free tier). Any OpenAI-compatible API works through the same transport: a local Ollama, OpenAI or OpenRouter.

## What the model can and cannot do

| It can | It cannot |
|---|---|
| Reword the explanation of a finding and the rationale of a proposal, in one or two sentences | Change a ranking, a severity, a score, a fix suggestion or any number |
| | Start, stop or change a run, or touch `chaos/policy.json` or `experiments/` |

Every text it returns is checked: it must be short, and **every number in it must already be in the evidence** it was given. A text with an invented number, a bad JSON shape, a timeout, a rate limit or an unreachable endpoint is dropped, the rule-based text stays, and the panel says so ("The language model gave no usable text for N of M item(s) ...").

The dashboard labels each text, for example **"Explained by llama-3.3-70b-versatile (hosted)"**, **"Explained by qwen2.5:7b (local)"** or **"Rule-based explanation"**. The downloadable report carries the same label, and `python scripts/dev.py status` shows which model is active.

## What is sent

Only run evidence the Findings and Suggested-experiments panels already show: experiment name, target container, fault, run ids, stop reason, error rates and latencies. **Not** sent: file paths, hostnames, the operator token, environment variables, log tails, or anything else from your machine. The API key is sent only as the `Authorization` header to the endpoint you configured.

With a **hosted** endpoint such as Groq this data goes to a third party. With a **local** Ollama it never leaves your machine. Do not turn this on for runs whose names or numbers you consider confidential.

## Quick start: Groq

1. Create **your own** free API key in the Groq console (https://console.groq.com). Do not share a key between people.
2. Copy `.env.example` to `.env` and fill in the key. `.env` is **local only**: it is git-ignored and must stay uncommitted. Never put a key in a command-line argument, in an issue, a PR or a message.

   ```text
   GROQ_KEY=<your key>
   LLM_BASE_URL=https://api.groq.com/openai/v1
   ```

   (Or export `LLM_API_KEY` / `GROQ_KEY` and `LLM_BASE_URL` in your shell instead; the environment wins over `.env`.)
3. Pick a chat model from the list your account offers (model names change, so check Groq's current list; `up` tells you if the model is not offered and names some it does). **Tested: `openai/gpt-oss-20b`**: six findings and ten proposals explained in about 2 s, cached afterwards. The number check keeps the rule-based text if a model invents figures.

   `gpt-oss-*` are *reasoning* models: they think before they answer, and with the small output cap used here they can spend all of it thinking and return nothing. `dev.py up` therefore sets `LLM_REASONING_EFFORT=low` for them. For any other reasoning model, export `LLM_REASONING_EFFORT=low` yourself (it is sent to the endpoint as `reasoning_effort`; the values a model accepts differ, for example `low`, `medium` or `high` for `gpt-oss-*`, so check your model's documentation, and if the host rejects it the request is retried without it).
4. Start:

   ```bash
   python scripts/dev.py up --execute --llm openai/gpt-oss-20b
   ```

   Before starting anything, `up` checks that the key is accepted and the model is offered (one small request that sends no run data). It prints that run summaries will be sent to `api.groq.com`. `dev.py status` then says `LLM openai/gpt-oss-20b via groq, key set (from .env)`, never the key.

### How `dev.py` treats `.env` and the key

- It reads `.env` only if the variable is not already in the environment, and **refuses to read it if `git check-ignore` says the file is not ignored**.
- It accepts `LLM_API_KEY` or `GROQ_KEY` (spaces around `=`, quotes, comments and `export` are fine) and passes the key to the API process through `LLM_API_KEY`, in that process's environment only.
- The key is never written to `.dev/`, logs, `results/`, the cache or any API response, never echoed, and never taken from an argument. Errors and `httpx` messages are sanitised: they carry a status code, not headers or bodies.

## Other endpoints

`LLM_BASE_URL` has **no default**: without it the agents stay rule-based and say why.

| Endpoint | `LLM_BASE_URL` | Key |
|---|---|---|
| Groq | `https://api.groq.com/openai/v1` | yes |
| OpenAI | `https://api.openai.com/v1` | yes |
| OpenRouter | `https://openrouter.ai/api/v1` | yes |
| Ollama (local) | `http://127.0.0.1:11434/v1` | no |

A hosted endpoint must use `https` and needs a key. For Ollama: install it, `ollama pull qwen2.5:7b`, start it, set `LLM_BASE_URL=http://127.0.0.1:11434/v1` and run `dev.py up --execute --llm qwen2.5:7b`; `up` checks that the server is running and the model is pulled.

To start the API yourself instead of `dev.py`, set `INTELLIGENCE_REASONING_MODE=live`, `INTELLIGENCE_LIVE_MODEL`, `LLM_BASE_URL` and (for hosted endpoints) `LLM_API_KEY`.

## Speed, the cache and rate limits

- **Cache:** accepted texts are stored in `results/.llm_cache/`, one small file per explanation (the text and the model id only), keyed by a hash of the model and that finding's own data. Reloading the dashboard, or asking again with the same runs, makes **zero** calls. A new run changes only the affected findings, so only those are sent. Delete the folder to regenerate.
- **Cap:** at most 10 findings or proposals go to the model in one request; the rest keep the rule-based text until the next refresh.
- **Rate limits:** an HTTP 429, a 5xx, a timeout or an unreachable host is an ordinary failure: the rule-based text is shown and **nothing more is sent for 30 s** (or the `Retry-After` value, up to 120 s), so the free tier is not hammered.
- The routes are synchronous, so a slow model never blocks Stop or the live panel.

## If it does not work

| You see | Likely cause |
|---|---|
| "Rule-based explanation" and a limitation about `LLM_BASE_URL` | `LLM_BASE_URL` is not set, there is no default |
| a limitation about `LLM_API_KEY` | key not in the environment or `.env` of the shell that started the API |
| `up` stops with "Refusing to read .env" | git does not ignore `.env`; check `.gitignore` |
| `up` stops with "rejected the API key" | wrong or revoked key |
| `up` stops with "does not offer the model" | a model id your account does not list |
| "gave no usable text for N of M", and the log or limitation mentions "no text" | a reasoning model used its whole output budget thinking: set `LLM_REASONING_EFFORT=low` (`dev.py up` does this for `gpt-oss-*`) or pick a non-reasoning model |
| "gave no usable text for N of M" | rate-limited or unreachable, timed out (20 s), or the model invented a number; try again later or another model |
