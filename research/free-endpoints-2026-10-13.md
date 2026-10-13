# Free endpoint capacity, October 13, 2026

Test Groq GPT-OSS-120B and Cloudflare GLM-4.7-Flash first. Test Clef as a task router after workers pass an evidence task.

This note uses current primary documentation. It contains no live API tests and no account checks. Live access remains unverified.

## Clef and Cloudflare

Clef chooses among allowed answers and returns probabilities. It does not generate a patch or discover unrestricted tasks. It can select a task, worker, evidence request, or review route. A strong text model must still write tasks and code. Clef uses a 27B base model. Clef-flash uses a 9B base model. [Cloudflare announcement](https://blog.cloudflare.com/clef-decision-models).

Cloudflare gives a Free account 10,000 neurons per day. The allowance resets at 00:00 UTC. Free requests stop at the limit. All models share this allowance. GLM-5.2/5.3/5.3-Flash, Kimi-K2.6/K2.7-Code, and DeepSeek-V4 require paid billing or prepaid credits. Exclude them from this setup. [Pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).

The same pricing table gives these eligible model rates. Calculations below assume no caching and no other account use.

| Model | Neurons per million input/output tokens | Example request | Approximate requests per day |
| --- | --- | --- | --- |
| Clef | 21,818 / no output charge | 2,000 input tokens | 229 |
| Clef-flash | 3,455 / no output charge | 2,000 input tokens | 1,447 |
| GLM-4.7-Flash | 5,500 / 36,400 | 10,000 input + 2,000 output | 78 |
| GPT-OSS-120B | 31,818 / 68,182 | 10,000 input + 2,000 output | 22 |
| Qwen3.8-27B | 40,909 / 290,909 | 10,000 input + 2,000 output | 10 |

These are arithmetic estimates from [Cloudflare pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/), not measured quota behavior. One agent task can consume many requests.

GLM-4.7-Flash supports reasoning, tools, and a 131,072-token context. GPT-OSS-120B supports tools, high reasoning, and a 128,000-token context. These features make both candidates for the evidence test. They do not prove reverse-engineering accuracy. [GLM model](https://developers.cloudflare.com/workers-ai/models/glm-4.7-flash/), [GPT-OSS model](https://developers.cloudflare.com/workers-ai/models/gpt-oss-120b/).

Clef has a 65,536-token context. Clef-flash has 24,576 tokens. Questions and media consume context, and long state text can be truncated. Keep task routing inputs small. [Clef](https://developers.cloudflare.com/workers-ai/models/clef/), [Clef-flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/).

Cloudflare publishes 300 requests per minute for eligible text generation models. It publishes no universal one-inference-at-a-time rule. Clef has no separate rate entry on that page. Treat its exact rate as unverified. [Limits](https://developers.cloudflare.com/workers-ai/platform/limits/).

REST access needs a Cloudflare account, account ID, and token with Workers AI Read/Edit permissions. No worker deployment is necessary for REST calls. [REST setup](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).

## Groq

Groq lists GPT-OSS-120B and Qwen3.8-27B on its Free plan. Each model lists 30 requests per minute, 1,000 per day, 8,000 tokens per minute, and 200,000 per day. Limits apply to the organization. Cached tokens do not count. Exact account limits can differ. The page lists no separate concurrent-request ceiling. A token budget still limits useful parallel work. [Limits](https://console.groq.com/docs/rate-limits).

At 10,000 input plus 2,000 output tokens per request, 200,000 daily tokens allow about 16 requests without caching. The 8,000-token minute limit also requires smaller prompts or effective caching. This is an estimate, not a live result.

Access needs a Groq account and API key. A payment method is required for the Developer upgrade, rather than the Free plan. Keep the account on Free. [Quickstart](https://console.groq.com/docs/quickstart), [Billing](https://console.groq.com/docs/billing-faqs).

## Mistral

Current Mistral quickstart documentation says Free mode enables API access without a credit card. Its usage documentation describes included monthly usage. Exact model rates and monthly limits appear in the account Admin panel. It publishes no fixed free token quantity on these pages. Do not reuse old claims of one billion tokens per month. [Key setup](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key), [Usage](https://docs.mistral.ai/admin/billing-usage/usage-limits).

Mistral applies limits across the organization and workspaces. Its limits include requests per second, tokens per minute, and tokens per month. The help page calls requests per second concurrent requests, which is imprecise. Measure actual simultaneous calls. [Rate help](https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them).

Mistral is a candidate, with unknown usable capacity. Create a normal Studio key and keep pay-as-you-go disabled. Test a listed coding model only if the account permits free access. Public documentation alone does not prove that a specific current model works free. [Subscriptions](https://docs.mistral.ai/admin/billing-usage/subscriptions).

## Gemini Developer API

The Gemini API Free tier is separate from the existing Antigravity integration. A new Free-tier project needs an account and API key. It does not need a billing account. API tiers depend on the project and billing settings. An AI Pro subscription does not establish an unlimited API tier in these documents. [Keys](https://ai.google.dev/gemini-api/docs/api-key), [Billing](https://ai.google.dev/gemini-api/docs/billing).

Gemini 3.8 Flash lists free standard input, output, and thinking. Google describes this model for software engineering and agents. Gemini 4 Argon is excluded from both Free tier and AI Pro. Free prompts can improve Google products. [Pricing](https://ai.google.dev/gemini-api/docs/pricing).

Google now directs developers to AI Studio for exact active model quotas. The public rate page gives no fixed current Gemini 3.8 Flash daily amount. It applies quotas per project, not key. Daily requests reset at midnight Pacific time. More keys in one project do not add capacity. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

Keep this as an optional reserve. Do not count its quota before an account check, especially because the user already found Gemini capacity too small.

## OpenRouter and exclusions

OpenRouter Free offers 50 requests per day across free models. This can support occasional reviews, but many tool turns can consume the allowance in one task. It requires an account and key. Free provider capacity can produce 429 errors even before the platform quota. [Pricing](https://openrouter.ai/pricing), [Limits](https://openrouter.ai/docs/api_reference/limits).

SambaNova's current page labels a plan Free, but asks for a payment method and purchased credits before the first requests. It does not meet the zero-payment requirement. Older free-tier announcements do not establish current eligibility. [Current plans](https://cloud.sambanova.ai/plans).

## What to test

1. Test two simultaneous calls to each eligible provider, with small prompts and strict output limits.
2. Test a tool call and an evidence-grounded Gungeon task with each candidate model.
3. Record response status, rate headers, token use, quota scope, and daily reset.
4. Test Clef against fixed routing cases, including exhausted providers and conflicting file ownership.
5. Give each provider an independent queue and budget after its tests pass.

Always-free means a recurring free allowance, not unlimited inference. The scheduler can remain active all day while a provider waits for reset. Separate workers can do local extraction and tests during that wait.
