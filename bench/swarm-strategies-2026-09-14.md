# Swarm strategies for software tasks: what actually beats one strong agent

Ranked strongest lever first; each: mechanism, gain + source, fit.

## S1 - Best-of-N with a hard verifier (repeated sampling, tests as selector)
Sample N candidates from a cheap model, keep any that pass the fixed tests; coverage (pass@k)
rises log-linearly with N. DeepSeek-Coder-V2 goes 15.9% -> 56% on SWE-bench Lite at N=250,
past the 43% SOTA; 5 cheap samples beat one GPT-4o/Claude sample at ~3x lower cost.
https://arxiv.org/abs/2407.21787
Fit: our lever - we have tests. Run each block owner N times, take the first test-passing
diff.

## S2 - Verifier strength is the ceiling
Gains appear only if the selector separates right from wrong. Without a hard verifier,
majority vote and reward models plateau after a few hundred samples: MATH coverage
82.9% -> 98.4% (100 -> 10k) but voting stuck 40.5% -> 41.4%. Shallow verifiers pass buggy code
(MAST FM-3.2). https://arxiv.org/abs/2407.21787
Fit: our tests ARE the verifier. Thin tests cap the swarm - strengthen before scaling.

## S3 - Sampling-and-voting scale ("More Agents Is All You Need")
N samples, majority vote; accuracy rises with ensemble size, orthogonal to CoT. +4-9%
HumanEval, +12-24% GSM8K; ensembled Llama2-13B (59%) beats Llama2-70B (54%); bigger gains on
harder tasks. https://arxiv.org/abs/2402.05120
Fit: only for choices tests cannot separate; for code, prefer test-pass selection.

## S4 - Mixture-of-Agents (layered proposers + aggregator)
Several proposers answer, an aggregator synthesizes; stack 2-3 layers. 6 OSS proposers +
aggregator = 65.1% AlpacaEval vs GPT-4o 57.5%; ~2x cheaper than GPT-4-Turbo but slower.
https://arxiv.org/abs/2406.04692
Fit: our finisher is the aggregator - feed block drafts to it. Quality only; correctness from
tests.

## S5 - Self-MoA / specialization vs flat
Aggregating many samples from the ONE best model beats mixing weaker diverse models; MoA is
quality-sensitive and mixing drags the average down. Self-MoA +6.6 pts AlpacaEval, +3.8 avg
over Mixed-MoA. https://arxiv.org/abs/2502.00674
Fit: our soloist matches Opus, so aggregating soloist samples likely beats a mixed free
swarm. Specialize only where the skill differs, not N clones.

## S6 - LLM-as-judge failure modes
A model picking the winner adds self-preference, verbosity, position bias, and is misled by
wrong context. GPT-4 +10% / Claude-v1 +25% self-win; reference-guided grading cut failure
70% -> 15%. https://arxiv.org/abs/2306.05685
Fit: prefer tests. If a judge is unavoidable: different model family, give it the
tests/reference, score per dimension.

## S7 - Debate ~ voting; not worth the tokens
Debate's gains are mostly the majority vote; debate alone adds no expected correctness
(martingale) and homogeneous debate breeds conformity. Voting alone matches debate across 7
benchmarks; sycophancy up to 85.5%, 2.1-3.4x tokens for equal-or-worse accuracy.
https://arxiv.org/abs/2502.08788
Fit: do NOT have 20 reviewers debate. Keep reviewers independent; select by tests or vote.

## S8 - MAST failure taxonomy (design against)
14 modes in 3 buckets - specification 41.8%, inter-agent misalignment 36.9%, verification
21.3%; failures are design, not model. ChatDev 33% correctness; design interventions only
+15.6%. https://arxiv.org/abs/2503.13657
Fit: block-ownership + finisher already fight misalignment/verification. Lock a per-block
contract; make the finisher verify against tests, not just merge.

## S9 - Security / red-team breadth
Many independent reviewers cover more vuln classes than one, but need dedup + a hard verifier
or false positives explode. VulTrial +102% over single; a 3-expert coalition hits 100% CWE
match but 91.5% FP without a verifier (verifier lifts F1 .714 -> .772); AgentFlow's 192
parallel explorers found 10 Chrome 0-days; ensemble verification cut attack-success
52.8% -> 2.0%. https://arxiv.org/abs/2605.17480
Fit: security stage = independent reviewers + exploit verifier + mixed families, deduped in
the finisher.

## What to change to make the swarm actually win
1. Run best-of-N of the SOLOIST, keep the first test-passing diff; stop paying for redundancy
   the tests already resolve (S1, S5).
2. Spend the saved budget on the verifier: mutation tests, runtime checks, a lint/type gate -
   the selector is the ceiling (S2, S8).
3. Reserve the swarm for breadth that pays: independent reviewers on security and edge cases,
   mixed model families, deduped by the finisher (S9).
4. Make the finisher an aggregator, not a voter/debater; never let 20 reviewers debate (S4, S7).
5. Test where the swarm should win: multi-file/underspecified tasks with a strong hidden
   suite, scored on pass@k and tokens (S3).

## Added 2026-09-16: what the 2026 literature says about swarms of coders, and what our runs say back

## S10 - Coordination, not concurrency, is the gain (AgentRoom, arXiv 2608.23740, Aug 2026)
N agents in one CRDT-backed workspace with file-level claims, a broadcast log and per-agent
status through MCP tools; at matched compute the full room beats parallel-merge by +0.21 on
the judge rubric (Welch t=3.35, p=0.003) and a solo agent is 13.7x more likely to "stub and
exit" on a complex task than a pair (p<1e-5). The headline is variance: solo runs swing between
perfect and abandoned, the room narrows the spread.
Ours: the same three tools (claim, inbox, done) exist behind SWARM_COORD=1, and the one thing
the swarm did that the soloist did not, on 2026-09-16, was ship a notebook whose New button
works and a kanban whose panels render. That is the stub-and-exit story: big-pickle alone
passed 18/18 and drew 3 of 16 panels. Fit: the treatment arm is the experiment that matters;
finish it on cline this afternoon.

## S11 - Partition by cohesion, not by feature count (Co-Coder, arXiv 2606.00953, May 2026)
Cohesion-aware task partitioning over 28 DevEval/CodeProjectEval tasks: +14.0 pp pass rate,
up to 2.10x wall-clock speedup, API cost cut up to 3x against sequential and file-based parallel
baselines and Claude Code Agent Teams. The partition is the product; the agents are fungible.
Ours: the roster is a fixed 16-owner feature list, one block per feature whatever the brief.
At size 5 the prototype is told to cut 5 blocks out of 16 concerns and the cut is arbitrary.
Fit: let the prototype choose the block count from coupling (functions that share state go in
one block), and let SWARM_ROSTER_SIZE be a ceiling, not a target.

## S12 - More engineers is not monotone; solo first, swarm as fallback (arXiv 2603.21489)
A manager splits work into at most N units for N asynchronous engineers; performance does not
rise monotonically with N and the optimum depends on decomposition quality and task coupling.
The "single agent first, then multi-agent on failure" setting approximates the practical
strategy and is what the numbers favour.
Ours: every ladder says the same. 19/19 at every size from solo to 20 on the secure brief,
18/18 for two soloists on the 16-feature kanban, tokens 9x and minutes 20x by size 20.
Fit: the default pipeline should be solo -> gate (tests + ui_gate) -> swarm only on a red gate.
Measured cost of that policy on the three briefs: one soloist run, $0.27-1.81 at list.

## S13 - Decomposition quality, not the framework, sets the ceiling (STORM, arXiv 2605.20563)
STORM more than doubles a single Sonnet's weighted score (46.2 vs 20.7) and nearly doubles
git-worktree parallelism (24.6); conflicts per run rise from 0.12 to 2.94 as k grows to 8, and
the diminishing returns of extra engineers are attributed to decomposition, not to the harness.
Ours: 15/16 owners land at size 20 and the finisher still has to reconcile a `handleNoteGet`
shape three owners flagged; the parts pipeline avoids merge conflicts by construction (one
block per owner, frozen structure) and pays for it with the prototype deciding everything.

## S14 - Verifier scripts as the coordination contract (Glite ARF, AgentPatterns 2026-07)
Up to twelve parallel agents over 273 tasks for about $450, with deterministic verifier scripts
enforcing task isolation, immutability of finished work and a materialised overview; ~1%
wall-clock overhead. Prose rules degrade quadratically with agents x steps; scripts do not.
Ours: `verify.py` per owner and the fixed suite are that contract. What was missing until
today is a verifier for the part a person touches: `bench/ui_gate.py` now loads the page
headless, seeds the store through the contract, and fails on an empty panel or a mount error.
It separated 18/18-and-a-shell from 18/18-and-usable on the first run.

## What changes in the harness, in order

1. Solo-first policy with the two gates (S12, S14): one soloist, then tests + ui_gate, swarm
   only on red. This is the cost/time lever; nothing else comes close.
2. ui_gate in the acceptance step for browser briefs (S14), so a green run means a page that
   works, and so the finisher is told which panel is empty.
3. Cohesion-cut blocks with a size ceiling (S11), replacing the fixed roster cut.
4. Keep the coordination tools on by default once the treatment arm is measured (S10), and
   report variance over 3 repeats per configuration, since variance is the swarm's product.
5. (added 2026-09-16 afternoon) The spec skeleton as the draft (`SWARM_SKELETON=1`): on a brief
   past one stream (spec 20) the prototype turn is the stage every free model fails, and it is
   the one stage a script can do, since the contract already fixes the file's shape. Owners then
   write 100-line blocks, which every model here can. This is the swarm's real claim on big
   files, tested first on NIM nemotron at 15:29.

## Added 2026-09-16, afternoon: the repair loop, what the literature says about its shape

## S15 - Browser-in-the-loop TDD, and protocol fit (TDDev, arXiv 2605.17242, May 2026)
Acceptance tests written before the code, the app deployed and driven in a browser, failures
turned into natural-language repair reports ("no submit button found, although email and
password fields were present"). +34 to +48 pp over no-TDD on WebGen-Bench and ArtifactsBench.
The finding that matters for a swarm of free models: the best protocol depends on the model's
style. Holistic models (rewrite whole files) do best with low enforcement, an agentic loop
that decides itself when to test; additive models (Qwen-like) do best with enforced incremental
checks. The wrong protocol erases the gain and multiplies tokens up to 25x.
Ours: `ui_defects` is that repair report, one line per panel or click. muse-spark and
big-pickle rewrite whole files (holistic): the solo + gate + repair loop, low enforcement, is
the matching protocol, and it is the one that reached 14/14. deepseek-v4.1-flash writes in
seven appends and stops (additive): it wants the incremental protocol, a gate after each
block, which the parts pipeline already is. Protocol fit is the reason one policy does not fit
every model, and why the model choice per role is not cosmetic.

## S16 - Rounds: +25 pp on the second, +7-13 on the third, converging by eight (Asuka-Bench, arXiv 2606.05920)
Fifty web tasks, an automated UI agent runs the checks, a user LLM turns them into feedback.
Round two adds about 25 pp, round three 7-13 pp, and eight rounds reach 100% with the strongest
model. Repair ability is a separate axis from one-shot ability: some models that start low
climb faster. DAG-ordered checks (a test runs only once its prerequisites half-pass) cut
tokens 23-26% and point at causes instead of symptoms.
Ours: SWARM_REPAIR_ROUNDS = 3 sits where the marginal round is still worth a turn. The muse
solo on kanban went 7/14 -> 14/14 in two turns; the deepseek solo is being measured. The
scenario's steps are already ordered by dependency (a card must exist before it can be
moved); a failed prerequisite step should skip its dependents in the defect list, which it
does not yet.

## S17 - Keep a revision only if it strictly improves (ReLook, ACL 2026; RubSE, arXiv 2608.24138)
Base models given feedback collapse after two or three rounds: the third revision is worse
than the first. ReLook's fix is a strict acceptance rule, a revision is kept only when its
score beats the best so far; the average trajectory then settles at two rounds. RubSE names
the mechanism, repair coupling: a local edit propagates through layout and shared state and
breaks a region that was fine, so it picks one prioritised repair target per round and keeps
the history of what was tried.
Ours: `bench/solo_first.py` now scores every gate (-100 per failing test, +10 per click a
person can complete, -5 per empty panel, -50 for a load error) and discards a repair turn that
does not beat the kept page, telling the next turn what it broke. Not yet done: one target per
turn; the defect list still carries everything.

## S18 - Silent failures need an interactive oracle (PlayCoder, arXiv 2604.19742; VF-Coder, arXiv 2604.19750)
Ten code LLMs near zero on Play@3 despite high compile rates: the apps run and do nothing
right. A tester agent that drives the GUI finds the unresponsive button the unit test cannot;
visual feedback helps detection (+2.9 pp) far more than it helps the fix (+0.7 pp). PlayCoder
caps the loop at six iterations.
Ours: the same shape, with a scripted scenario instead of an LLM tester (cheaper, deterministic,
and it is the spec's author who knows which clicks matter). Where a build's UI takes an
unforeseen shape the script misses it (F13's probes): an LLM tester is the fallback worth
adding for the briefs after 20, not a replacement.
