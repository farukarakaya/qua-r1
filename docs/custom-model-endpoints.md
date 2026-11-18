# Custom OpenAI-compatible model endpoints

Use `provider: "chat"` for a gateway or self-hosted server that implements the OpenAI Chat Completions API. This changes where Trading-R1 sends model requests, not where it fetches market data or web results.

## Set up a model profile

1. Copy the [example `.providers.json`](../.providers.json) to `~/.trading-r1/providers.json` (or `$TR1_HOME/providers.json` if set). If you already have that file, merge the entries you want into its `profiles` object instead of overwriting it. Keep `gpt-ocr` only if you want a custom OCR endpoint. The repo example is not loaded automatically. To use a file elsewhere, set `TR1_PROVIDERS_CONFIG` to its absolute path.
2. Replace `https://gateway.example/v1` with the gateway's API base URL and `your-model-id` with the model ID it serves. Use the base URL, not the full `/chat/completions` path.
3. Set `GATEWAY_API_KEY` in the backend's environment or save it with the secret store:

   ```sh
   trading-r1 secrets set GATEWAY_API_KEY
   trading-r1 --profile my-gateway
   ```

Keep actual keys out of JSON and Git. The `api_key_env` field names the credential; it does not contain the key. Use a credential issued for that endpoint. A profile that extends a hosted profile can inherit its key settings even when you change the URL, so use a standalone profile like the example.

If the terminal attaches to a separately running backend, that backend must also be able to see the profile file and credential. Set environment overrides where the backend starts, not just in the terminal that attaches to it.

## Compatibility and overrides

- The research agent needs Chat Completions with streaming and function tool calls. The web reader needs structured output using `response_format` with JSON Schema. The example uses the same profile for both; set `reader_profile` to another configured profile if the gateway cannot do structured output.
- A successful model readiness check does not confirm tool calling or structured output. To verify both, try a research question that requires a tool call and reading a web page.
- `TR1_MY_GATEWAY_BASE_URL`, `TR1_MY_GATEWAY_MODEL`, and `TR1_MY_GATEWAY_API_KEY` override the example profile's file settings for the process. Profile names are uppercased and hyphens become underscores.
- The configured gateway receives the prompts and tool results used in model requests. Only send data you are allowed to share with it.

## PDF OCR uses a separate endpoint

`trading-r1 docs add` uses the separate `gpt-ocr` entry in the [example `.providers.json`](../.providers.json). To use a custom OCR service, keep `provider` and `role` as shown, replace `https://ocr.example/v1` with the service's API base URL, and set `model` to a PDF-capable model it serves. Then store its own key and import a PDF:

```sh
trading-r1 secrets set TR1_GPT_OCR_API_KEY
trading-r1 docs add report.pdf
```

`TR1_GPT_OCR_BASE_URL`, `TR1_GPT_OCR_MODEL`, and `TR1_GPT_OCR_API_KEY` can also be set in the environment. To use OpenAI instead, leave out the example `gpt-ocr` entry and omit the OCR overrides. The built-in profile uses `gpt-6.1-luna` and `OPENAI_API_KEY`.

The OCR endpoint must support the OpenAI Responses API with inline PDF input and strict JSON-schema output. Chat Completions support alone is not enough. The entire PDF is sent on first import; an identical file reuses the saved local transcript.
