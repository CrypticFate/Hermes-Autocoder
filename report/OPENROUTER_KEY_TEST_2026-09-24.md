# OpenRouter key compatibility test — 2026-09-24

The OpenRouter key read from `key.text` authenticated successfully (HTTP 200). No key is included in this report. The key was not copied into deployment secrets or configuration.

## Live results

| Check | Result |
| --- | --- |
| Authentication | HTTP 200; free-tier account |
| Gemma `google/gemma-4-31b-it:free` | HTTP 429; provider metadata identifies the Google AI Studio upstream shared pool as temporarily rate-limited |
| NVIDIA `nvidia/nemotron-3-super-120b-a12b:free` | HTTP 200; valid synthetic function call; reported cost 0 |
| Liquid `liquid/lfm-2.5-2.6b:free` | HTTP 200; valid synthetic function call; reported cost 0 |
| Installed Hermes worker with NVIDIA | Passed actual write/read/final-response tool loop |

The existing `hermes-autocoder-worker:local` image (short ID `dae6bb094499`) ran its real `run_agent.AIAgent` in a disposable container, using the custom Chat Completions mode used by this project. A temporary host-side relay kept the OpenRouter key out of the worker, pinned requests to the free NVIDIA model, capped output at 1,024 tokens, and allowed at most four requests.

Observed live agent sequence:

1. HTTP 200, `write_file` tool call, reported cost 0.
2. HTTP 200, previous tool result included, `read_file` tool call, reported cost 0.
3. HTTP 200, both tool results included, final answer `HERMES_KEY_TEST_OK`, reported cost 0.

The test independently read the resulting file inside the container and verified its contents. Hermes returned `completed: true`; the container process exited with code 0. The container was removed automatically.

A separate request without a `messages` field caused a KeyError in the temporary relay. It did not prevent the three recorded chat requests or successful file verification. This relay was a test harness, not the production proxy.

OpenRouter's key endpoint reported usage 0 and free-model daily counters used 0 / limit 50 / remaining 50 both before and immediately after testing. Those counters did not reflect the five successful calls at observation time; they should not be interpreted as proof that these requests never count toward quota.

## Deployment status and limits of evidence

The current `config.yaml` still points to Groq with `openai/gpt-oss-120b`. Deployment settings were not changed and the maintenance controller was not activated.

The successful test verifies the supplied key, free NVIDIA inference, and the installed Hermes tool loop. It does not exercise the production budget/database proxy, GitHub maintenance workflow, sustained availability, or coding quality.

To use the tested provider/model in deployment, the model secret must contain the OpenRouter key, and provider/model/pricing settings must be updated to:

```yaml
provider_url: https://openrouter.ai/api/v1
model: nvidia/nemotron-3-super-120b-a12b:free
input_usd_per_million: 0
output_usd_per_million: 0
```
