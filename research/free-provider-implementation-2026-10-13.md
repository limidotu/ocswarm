# Free provider integration, October 13, 2026

Open the local dashboard to inspect each provider's workers, daily use, and wait state. The continuous swarm now uses independent NVIDIA, Groq, and Cloudflare queues.

## Roles and limits

NVIDIA Kimi K3 remains the planner and a worker model. NVIDIA Nemotron 3 Ultra, DeepSeek V4.1 Flash, and GLM-5.3 remain free alternatives. GLM stays last after its single-call 429. Native workers retain OpenCode.

Groq GPT-OSS-120B supplies compact workers and independent reviews. Cloudflare GPT-OSS-120B supplies compact workers. I selected the stronger Cloudflare model after GLM made an unsupported evidence claim and failed the first worker check. Clef selects the next eligible task. NVIDIA still discovers and writes new tasks.

| Provider | Initial / maximum worker cap | Local daily limit |
| --- | --- | --- |
| NVIDIA | 6 / 12 | 8,000 requests, paced at five per minute |
| Groq | 1 / 2 | 180,000 tokens and 950 requests |
| Cloudflare | 2 / 6 | 9,500 shared neurons, with coding capped at 6,000 |

The total cap can grow to 19. These caps are scheduler limits, not promised upstream concurrency. Every provider must also have quota and a suitable task.

Cloudflare coding leaves 3,500 neurons of local headroom for Clef. Both uses share the same ledger. Groq also enforces a conservative minute token limit. These ceilings stay below the verified account allowances. [Cloudflare pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/), [Groq limits](https://console.groq.com/docs/rate-limits).

Today's Cloudflare coding calls reached the local ceiling during tests and initial production work. Coding now waits for midnight UTC. Clef can use the remaining shared allowance. NVIDIA and Groq continued during that wait. The new coding partition applies to future calls and resets. It cannot restore allowance already used today.

## Quota and routing behavior

The gateway reserves the estimated input and maximum output before a request. It settles successful charges from returned usage, including reasoning. Calls with unknown usage retain their reservation. Earlier successful probes also contribute to the ledger.

The ledger survives restarts. It separates provider cooldowns and minute windows. A local minute wait can pause one compact worker. A daily wait returns its task to the queue. Other providers continue.

Clef receives at most four eligible choices with compact task descriptions. The scheduler first excludes ownership conflicts and unavailable models. It gives ready candidates and repairs priority. An invalid answer or unavailable Clef route falls back to the first eligible task.

Gemini remains disabled. The gateway does not add a paid provider, purchase credits, or upgrade a plan. Provider keys remain encrypted outside the repository. Child workers receive the loopback address and no provider keys.

## Compact worker and review checks

Groq and Cloudflare use bounded file tools for reading, literal search, listing, writing, and exact replacement. Writes are limited to owned files in a private workspace. The tools reject global edits, path escapes, protected types, and shell calls. Partial reads report their next range.

The client rejects an unapplied done claim. A completed patch must differ from the original source. It preserves a partial candidate and its checkpoint. Context, reasoning, or tool limits return the task to a larger NVIDIA client without losing an attempt.

A separate reviewer still checks every candidate. Groq is preferred when its budget fits. Larger reviews return to NVIDIA. The existing ownership, conflict, merge recovery, rollback, and acceptance checks remain in force.

## Live validation

Both compact worker clients repaired an isolated mirror fixture through real file tools. Both removed the incorrect curse gate and subtraction. They wrote checked addition of three instead. Each result compiled and passed ten boundary assertions, including negative values, reuse, and overflow.

The first Groq run claimed a change without editing. The supervisor rejected it. The client now detects that condition and requires an edit tool before completion. One later call used an unknown tool name. The client now requests an exact supplied name and bounds those retries.

The first fixture also contained a separate review instruction after the worker instruction. I removed that conflict before the final worker tests. This fixture tests the client and a small evidence task. It does not prove full reconstruction accuracy.

Clef selected a known repair over optional research in a live integration test. The locked task never entered its choice list. The simulated three-provider run dispatched all providers and merged only reviewed candidates.

A live Groq review exceeded its context budget and returned retry. The candidate and original remained intact. That path adds Groq to the review exclusion list so NVIDIA can finish the review.

The subsequent NVIDIA Kimi review accepted the same fixture. It cited original IL and marked the integer overflow boundary as a fixture rule. The original assembly hash remained c6c0eca9747651bbe3eeaceab5e9f391c89e2a7dfb22a0d20f7034a42a9845c3. A scan of 191 source, configuration, session, and log files found no raw provider keys.

The control suite passed 56 tests. The self-test and Python compilation passed. The dashboard type check passed, and 40 dashboard tests passed. The browser displayed all three provider rows and Clef decisions. The Gungeon baseline still has existing failures. These harness checks do not establish a passing game suite.

## Files and operation

The new modules are provider_budget.py, provider_routes.py, bounded_agent.py, and clef_router.py. free_gateway.py, free_swarm.py, and ocswarm.py connect them to the supervisor. The Ocswarm server exposes provider telemetry and a budget table. Existing dashboard layouts remain intact.

Usage lives in runtime/free-provider-budgets.json. Compact session checkpoints live in runtime/compact-sessions. Restart checkpoints preserve the previous task state. Both live worker results and their Rust checks remain under runtime/integration-fixture-v2 and runtime/integration-fixture-v3.

The README contains the existing start and stop commands. Use hours zero for the continuous run. The watchdog remains active. A restart runs baseline checks before dispatch, then resumes saved tasks and candidates.
Prose lint: 0.63 flags per 100 words.
