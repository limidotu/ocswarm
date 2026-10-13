# Free endpoint tests, October 13, 2026

Use NVIDIA Kimi K3 for the current workers. Use Groq GPT-OSS-120B as the next candidate for an independent provider queue. Clef fits task routing after the scheduler filters forbidden actions.

These results come from live calls on your accounts. Cloudflare and Groq both showed their Free plan as the current plan. No plan upgrade or credit purchase occurred. Provider keys use Windows encryption outside this repository. Gemini remains disabled.

## Concurrent requests

| Endpoint | Simultaneous requests accepted | Measured result |
| --- | ---: | --- |
| NVIDIA Kimi K3 | 2 | Both returned PONG. Combined wall time: 56.843 seconds. |
| Groq GPT-OSS-120B | 12 | All returned PONG. Combined wall time: 1.003 seconds. |
| Cloudflare Clef | 12 | All selected research for missing evidence. Combined wall time: 1.214 seconds. |
| Cloudflare GLM-4.7-Flash | 2 | Both returned PONG. Combined wall time: 8.567 seconds. |

Each group used a thread pool to start its requests together. These samples prove that each endpoint accepted multiple requests in flight. They do not measure parallel GPU execution or sustained coding throughput. Twelve small requests do not establish capacity for twelve continuous agent sessions.

The old worker cap was local. A GLM-5.3 error reduced the global cap, including healthy Kimi workers. The production scheduler still has one global cap. Separate provider queues remain necessary before new providers enter production.

## Capability tests

The evidence fixture contains original MirrorController IL. The tested candidate incorrectly requires three curse and subtracts three. The correct findings are a positive three modifier and no curse threshold in the supplied interaction method.

| Model | Result | Use |
| --- | --- | --- |
| Groq GPT-OSS-120B | Correct verdict, positive three, and no gate in two calls. Requested the exact method through a tool, then interpreted the supplied tool result. | Strongest new candidate in this small sample. |
| NVIDIA Nemotron 3 Ultra | Correct verdict, positive three, and no gate. Identified limits of the excerpts. Incorrectly called an instance field static. | Available fallback. Evidence review remains necessary. |
| Cloudflare GLM-4.7-Flash | Correct main verdict, but added an unsupported claim about runtime subtraction. | Bounded work with a stronger reviewer. |
| Groq Qwen3.8-27B | Correct positive three, but incorrectly returned gate true. Its explanation contradicted that field. | Exclude from primary review. |

Groq and Cloudflare GLM generated Rust for a separate, explicit function contract. Both compiled and passed ten boundary assertions. These assertions covered negative values, zero, reuse, the maximum valid sum, and integer overflow. This code test measures a small implementation, not full game reconstruction.

Both returned valid read_original_il tool calls with MirrorController.Interact as the argument. Only the Groq test continued through the tool result. The test tools used local fixtures. They did not execute arbitrary commands.

Small output limits caused empty final answers while reasoning consumed the budget. Groq needed 4,096 tokens for the code test. Cloudflare needed 8,192 tokens for the evidence test. The Cloudflare code answer also used a Markdown fence and an unexpected key, which required normalization.

## Clef routing

Clef selected research, code, review, and wait correctly in the matching fixed cases. It also selected wait when the schema removed code from a file ownership conflict.

When code remained an allowed choice, Clef chose code despite that conflict. Thus, the scheduler must enforce ownership, provider cooldowns, and quota limits before it builds the choice list. Clef cannot serve as the sole enforcement mechanism.

Clef chooses typed answers. A coding model must still discover tasks and write patches. [Cloudflare Clef announcement](https://blog.cloudflare.com/clef-decision-models).

## Free capacity

Cloudflare provides 10,000 neurons each UTC day across all models. Clef and its coding workers share that allowance. The current plan stops requests at the limit. Paid-only models remain excluded. [Cloudflare pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).

The measured GLM evidence call used 189.168 neurons, including reasoning. An otherwise unused allowance would support about 52 similar calls. It would support fewer after routing and other work. A roughly 220-token Clef decision costs about five neurons at the published rate. Compact routing state matters.

Your Groq limits page showed 30 requests per minute, 1,000 per day, 8,000 tokens per minute, and 200,000 per day for GPT-OSS-120B. The API returned matching request and minute token headers. One uncached evidence call used 5,452 total tokens. At that size, the daily token allowance permits about 36 calls before other usage. One agent task can need many calls. [Groq limits](https://console.groq.com/docs/rate-limits).

NVIDIA Kimi and Nemotron accepted the live tests. NVIDIA GLM-5.3 returned 429 even for one tiny request. The local NVIDIA budget is five requests per minute and 8,000 per UTC day. These are local ceilings, not a verified upstream allowance or uptime guarantee.

A continuous scheduler can run day and night. Fully free inference still stops or waits when provider allowances expire. More worker slots do not create additional tokens.

## Unavailable options

Mistral showed a Free account but required an upgrade to activate API keys. A single small inference returned 429. It contributes no verified capacity. The account result takes precedence over documentation that describes free API access.

OpenCode Mimo and Ling free models both rejected this native CLI with 403. The error restricted the free tier to supported OpenCode access. The tests used the existing client. No client impersonation or quota bypass occurred.

The first Groq requests returned a transport 403 with Python's default user agent. A normal application user agent resolved it. Later requests passed, including twelve concurrent calls.

## Current state and artifacts

The swarm restarted with Kimi first and two initial worker slots. Successful responses can increase the existing cap to eight. GLM-5.3 moved behind the other NVIDIA alternatives. Gemini remains disabled. Existing private candidates survived the restart.

Groq and Cloudflare credentials are ready for use, but their worker queues are not connected to the production scheduler. Clef is tested, not installed as the production orchestrator. The next implementation needs independent provider budgets, compact prompts, and visible per-provider cooldowns.

The control suite passed 39 tests. The self-test and Python compilation passed. These checks do not establish a passing Gungeon baseline.

Raw response files and Rust checks are under runtime/endpoint-tests. The reusable harness is endpoint_probe.py. The source research is research/free-endpoints-2026-10-13.md. That earlier note contains documentation findings, while this report records live results.

Prose lint: 0.58 flags per 100 words.
