# Agentic chat -- connector + widget dev commands
#
# One place for the day-to-day "ship the connector / recycle workers / prove
# the crudhub bridge / drive the live matrix" loop. Every target that talks to
# a box reads creds from an env file (default: the harness .env.159) and runs
# through the connector's own .venv (the only interpreter here with pyfsr).
#
#   make help                 # list targets
#   make deploy         # THE deploy command -- see below
#   make ship-install         # connector only: bump → build → install → verify workers recycled (the release-path install; `make ship` is the front door)
#   make verify               # confirm every worker is on info.json's version AND the pinned framework
#   make warmup / make health # exercise the live connector (proves the crudhub bridge)
#   make bridge-check         # SSH the box and prove probes/crudhub resolves in the worker ctx
#   make matrix               # drive the live prompt/flow matrix through the deployed widget
#
# Pick the target box with BOX=<alias> (pyfsr registry; `scripts/box.sh --list`).
#
# --- Deploying: use `make deploy` -----------------------------------
# The framework (fsr-playbook-framework → PyPI) and this connector are a
# producer/consumer pair, and every outage in that seam has been an ORDERING
# mistake, not a code bug:
#
#   * shipping before the framework release is visible on PyPI. The PLATFORM
#     pip-installs requirements.txt itself when the connector lands; if the pin
#     isn't on the index yet that install fails, the connector goes live, and
#     every op dies on operations._import_fsr_playbooks's version guard.
#     (Hit identically on connector 0.5.54 and 0.5.57.) The
#     `requirements_installed='In-Progress'` flag people chase is the SYMPTOM
#     of that failed pip run -- not the cause, and not worth poking.
#   * pinning a framework version that lacks a symbol the connector imports --
#     the whole fsr_soc_triage import then dies on-box (the 0.4.43 outage).
#
# `make deploy` enforces the order so neither is a thing to remember:
#   release (framework's own main/clean-tree/tests guards)
#     → poll PyPI until it ACTUALLY serves the wheel (the GitHub release only
#       triggers the publish workflow; that gap is the bug)
#     → symbol-verified pin bump (scripts/bump_framework.sh refuses a skew)
#     → make ship-install
# Every step no-ops when already satisfied, so running it is always safe.
#
#   make deploy              # ship the current pin (releases nothing)
#   make deploy FW=next      # release the next framework patch, then ship
#   make deploy FW=0.6.4     # release that exact version, then ship
#   make deploy FW=next BUMP=minor BOX=<alias>
#
# With no FW it will NOT invent a version to release (a release is permanent and
# public) -- but it refuses to ship a pin that isn't published, which is exactly
# the 0.5.57 failure, caught before anything touches the box.
#
# NEVER hand-install a wheel onto the box to get past a dependency failure: it
# hides a bad release and leaves the box in a state no customer install can
# reproduce. Fix the release, then re-run.
#
# `make ship-dev` is NOT an exception to that -- it is the ITERATION loop, and
# it never rescues a release. It rsyncs the framework working tree onto the box
# in seconds so debugging does not cost a published version: chasing one
# compiler bug burned 0.6.9 AND 0.6.10, neither of which fixed it, because the
# box was the only way to observe the failure and PyPI was the only way to
# reach the box. A dev-patched box REPORTS THE WHEEL'S VERSION WHILE RUNNING
# OTHER CODE, so `make ship-dev-status` before believing any result from one,
# and land the change through `deploy` before it counts.

COMMA       := ,
REPO        := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
# ONE python env for every target, from scripts/dev_env.sh: $FSR_DEV_VENV, default
# the main checkout's .venv (a worktree shares it). `make setup` builds it from
# pyproject.toml + requirements.txt and stamps their hash. A missing or stale env
# stops the make run below, with the fix command: there is no fallback venv.
MAIN_CHECKOUT := $(shell bash "$(REPO)/scripts/box.sh" --main-checkout "$(REPO)")
VENV        := $(shell bash "$(REPO)/scripts/dev_env.sh" venv)
DEV_ENV_STATUS := $(shell bash "$(REPO)/scripts/dev_env.sh" status)
# (a dry run, make -n, executes nothing, so it needs no env)
ifneq ($(filter-out setup help doctor,$(MAKECMDGOALS)),)
ifeq ($(findstring n,$(firstword $(filter-out --% %=%,$(MAKEFLAGS)))),)
ifneq ($(DEV_ENV_STATUS),ok)
$(error dev env at $(VENV) is $(DEV_ENV_STATUS). Fix: make setup   (FRESH=1 make setup recreates it))
endif
endif
endif
PY          := $(VENV)/bin/python
# Every target imports the framework the ONE resolver names (an agent tree's
# own checkout, via .fsr_src), ahead of the venv's editable install of the main
# checkout's. Without this a tree's sweeps silently graded the shared framework.
# deploy.sh applies the same rule to its own test step.
FRAMEWORK_DIR := $(shell bash "$(REPO)/scripts/framework_dir.sh" --soft "$(REPO)")
ifneq ($(FRAMEWORK_DIR),)
export PYTHONPATH := $(FRAMEWORK_DIR)$(if $(PYTHONPATH),:$(PYTHONPATH),)
export FSRPB_FRAMEWORK_DIR := $(FRAMEWORK_DIR)
endif
WIDGETS     ?= $(HOME)/WebstormProjects/fsr_all_widgets
# --- the target box: ONE registry, NO default box ------------------------------
# Pick a box with BOX=<alias>: an alias in the pyfsr registry (~/.pyfsr/instances.toml)
# on scripts/box.sh's agent-targetable list (`scripts/box.sh --list`). The registry
# answers the env file and the ssh target; there is no default box and no other
# knob. ENV= / FSR_ENV= / SSH_HOST= are retired (one release: a human gets a
# warning and a registered env file is mapped to its alias; agents are refused).
# Resolved once here; `box-guard` prints the target or stops with the reason.
_RESOLVED   := $(shell BOX='$(BOX)' ENV='$(ENV)' FSR_ENV='$(FSR_ENV)' SSH_HOST='$(SSH_HOST)' bash "$(REPO)/scripts/box.sh" --need 'env,ssh?' --make)
RESOLVED_BOX := $(word 1,$(_RESOLVED))
BOX_ENV     := $(word 2,$(_RESOLVED))
BOX_SSH     := $(word 3,$(_RESOLVED))
BOX_SSH_BIN := $(word 4,$(_RESOLVED))
ifneq ($(RESOLVED_BOX),)
# Every recipe and child script sees the one resolved box (and the ssh shim).
export BOX := $(RESOLVED_BOX)
export BOX_ENV BOX_SSH BOX_SSH_BIN
export PATH := $(BOX_SSH_BIN):$(PATH)
unexport ENV FSR_ENV SSH_HOST
endif
BOX_TARGETS := deploy release-ship ship-install verify dev-push loop ship-dev ship-dev-status ship-dev-restore live quick-live live-rows live-writes live-integration live-autonomy p3-proof ship-accept warmup health ssh workers logs bridge-check mcp-bridge-check configure eval-selfcheck eval-s1 eval-effect session-export plant release-scores
BUMP        ?= patch
BOX_INTEG   := /opt/cyops-integrations/integrations
BOX_PY      := /opt/cyops/configs/integrations/packages/integrations_env/bin/python

# Source the env file, then run whatever follows with pyfsr on PATH.
define with_env
set -a; . "$(BOX_ENV)"; set +a; PATH="$(VENV)/bin:$$PATH"
endef

.PHONY: check ship ship-install framework-path session-export session-import session-summary setup live live-autonomy known-answer quality-gate loop dev-push quick-live doctor test mutants hooks ship-dev ship-dev-status ship-dev-restore regrade help deploy release-ship ship-widget build verify warmup health ssh workers logs bridge-check matrix bump-framework local-turn live-utterances preflight t1-scenarios tool-sweep conv conv-suite models live-integration p3-proof ship-accept live-rows live-writes eval-s1 eval-effect configure eval-ab plant release-scores provision

# --- The two front doors live in the root repo (scripts/door.sh); these forward there,
# telling it which repo you are in. `make ship` is the front door; the old connector
# install is `make ship-install` (the release path, user-run).
check: ## FRONT DOOR 1: tests for what you changed, no box (+ local-chat when the agent loop changed). Forwards to the root repo's door
	@$(MAKE) --no-print-directory -C "$(FSR_ALL_WIDGETS)" check DOOR_REPO="$(REPO)"

ship: ## FRONT DOOR 2: push, stamp on BOX=<alias> only when the diff ships, then promote (DRY_RUN=1 stops before the merge). Forwards to the root repo's door
	@$(MAKE) --no-print-directory -C "$(FSR_ALL_WIDGETS)" ship DOOR_REPO="$(REPO)" $(if $(RESOLVED_BOX),BOX=$(RESOLVED_BOX),$(if $(BOX),BOX=$(BOX),)) $(if $(DRY_RUN),DRY_RUN=1,)

framework-path: ## Print the fsr_playbooks every target imports (check this before trusting a sweep from a worktree or agent tree)
	@$(PY) -c "import fsr_playbooks, os; print(os.path.dirname(os.path.dirname(os.path.abspath(fsr_playbooks.__file__))))"

help: ## List targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  box=$(or $(RESOLVED_BOX),<none: pass BOX=>)  env=$(BOX_ENV)  bump=$(BUMP)"

# --- local turn harness (PL) -- fully in-process, no box ------------------
# Drive one agentic chat_turn in-process: Frank (or fake) LLM, persona from a
# local fixture (or read-only SOAR), tool execution local. The inner loop for
# the persona-spine-unification plan -- no ship, no publish, no box-wait.
INTENT      ?= $(if $(PLAYBOOK),build,triage)
MODULE      ?= incidents
MSG         ?= triage this incident and pivot on the IPs
# frank, NOT fake. The fake provider returns a canned "[PL fake turn]" string,
# so with `LLM ?= fake` the default invocation of every LLM tier graded a
# scripted stub: `make t1-scenarios` was permanently RED (the stub fails
# complete_playbook / compiles / investigated / grounded_in_record on nearly
# every row) and said nothing whatsoever about any model. A perma-red suite
# stops being read, which is the same invariant TESTING.md enforces one tier
# down (the widget repo's `make test-unit` must exit 0 ... it never sits perma-red).
#
# This is also the harness-lies-look-like-model-regressions class: two models
# "agreeing exactly" means the model was never in the loop.
#
# The fake provider is still the right tool for a DETERMINISTIC, network-free
# gate -- that is `make test` (tests/test_local_turn_*.py drive pytest fixtures
# and do not read this variable). Pass LLM=fake explicitly for a no-network smoke run.
LLM         ?= frank
PERSONA     ?= $(if $(PLAYBOOK),none,fixture)
TOOLS       ?= cassette
RUNS        ?= 1
# Turns driven CONCURRENTLY (one process each; the turn is network-bound).
JOBS        ?= 3
# Which gateway model the local (Frank) tiers drive. The virtual key permits
# several -- list them with `make models`. Empty = the harness default
# (tooling/harness/llm.py DEFAULT_FRANK_MODEL, Qwen3.8). GLM is the second
# opinion: a defect both models reproduce is a tool/prompt defect, one only
# one model shows is likelier model-specific.
#   MODEL=coding-b200/qwen3.8-27b-nvfp4   (Qwen3.8, the default)
#   MODEL=coding-b200/glm-5.3             (GLM-5.3)
MODEL       ?=
# `--model ""` would be passed through as a literal empty model id, so only
# emit the flag when MODEL is actually set.
MODEL_FLAG   = $(if $(MODEL),--model "$(MODEL)",)

local-turn: ## Drive ONE chat_turn in-process (LLM=fake|frank PERSONA=none|fixture|soar-ro TOOLS=cassette|live INTENT= MODULE= MSG=; PLAYBOOK=<fixture|path> mounts a playbook for a build turn: intent build, persona none; prints the turn DIAGNOSIS -- rounds, empty/failed calls, loop interventions, unused tools; SCENARIO=<sweep scenario> runs its recorded world + mounted record + ask (chat-sweep-list), MODEL= picks the model, RAW=1 the raw transcript, MAX_ROUNDS=n MAX_WASTED=0.3 -> exit 1 over budget)
	@FSRPB_DEV=1 $(PY) scripts/local_turn.py \
	  --intent "$(INTENT)" $(if $(PLAYBOOK),,--module "$(MODULE)") \
	  $(if $(and $(SCENARIO),$(if $(findstring command,$(origin MSG)),,default)),,--message "$(MSG)") \
	  --llm "$(LLM)" --persona "$(PERSONA)" --tools "$(TOOLS)" $(if $(PLAYBOOK),--playbook-yaml "$(PLAYBOOK)",) \
	  $(if $(MODEL),--model "$(MODEL)",) $(if $(RAW),,--diagnose) \
	  $(if $(SCENARIO),--scenario "$(SCENARIO)",) \
	  $(if $(MAX_ROUNDS),--max-rounds $(MAX_ROUNDS),) $(if $(MAX_WASTED),--max-wasted $(MAX_WASTED),)

live-utterances: ## LIVE golden-utterance OUTCOME gate: drive each tests/golden_utterances.json ask through the REAL chat_turn on BOX=<alias> (box model + data) and assert resolved intent, card type, no unknown_connector spray, and analyst language. The on-box layer that catches what routing unit tests cannot (the build-on-triage defect). BOX= required; ONLY=<name> one case; LLM=frank. Run before make stable.
	@if [ -z "$(BOX_ENV)" ]; then echo "live-utterances needs BOX=<alias> (scripts/box.sh --list)"; exit 2; fi
	@$(with_env); FSRPB_DEV=1 $(PY) scripts/live_utterances.py --python "$(PY)" --llm "$(or $(LLM),frank)" $(if $(ONLY),--only "$(ONLY)",)

# A1: the same golden authoring tasks, the same model, an OUTSIDE agent (claude -p
# on the toolkit MCP server) vs the in-product WIDGET loop; ONE grader. Results
# + transcripts in exports/ab/<stamp> (re-grade with REGRADE=<dir>, no re-run).
#   make eval-ab DRY=1                       no model, no box: proves the plumbing
#   make eval-ab KIND=investigate ...        A2: 3 known-answer investigations (no box on
#                                            either side; the outside agent reads the recorded
#                                            world over loopback, see scripts/world_server.py)
#   make eval-ab SIDE=widget MODEL=coding-b200/eco     free (Frank), box-free
#   make eval-ab BOX=<alias> MODEL=claude-haiku-5-5 ANTHROPIC_ENV=<env file>   PAID, both sides
# The outside side reads the box (read-only; every mutating toolkit tool is
# denied), so it needs BOX=<alias>; there is no default.
eval-ab: ## A/B outside agent vs widget loop on golden authoring tasks (SIDE=both|widget|outside TASKS= RUNS=2 MODEL= DRY=1 REGRADE=<dir> BOX=<alias> for outside; ANTHROPIC_ENV=<file> for a claude model on the widget side)
	@FSRPB_DEV=1 FSR_TOOLKIT_EXE="$(or $(FSR_TOOLKIT_EXE),$(HOME)/PycharmProjects/fsr-agentic-toolkit/.venv/bin/fsr-agentic-toolkit)" \
	  $(PY) scripts/eval_ab.py --side "$(or $(SIDE),both)" $(if $(KIND),--kind "$(KIND)",) $(if $(findstring command,$(origin RUNS)),--runs $(RUNS),) \
	  $(if $(TASKS),--tasks "$(TASKS)",) $(if $(MODEL),--model "$(MODEL)",) \
	  --tools "$(or $(TOOLS),cassette)" $(if $(DRY),--dry-run,) $(if $(REGRADE),--regrade "$(REGRADE)",) \
	  $(if $(ANTHROPIC_ENV),--anthropic-env "$(ANTHROPIC_ENV)",) \
	  $(if $(and $(BOX),$(BOX_ENV)),--box-env "$(BOX_ENV)",) $(if $(OUT),--out "$(OUT)",)

# Parallel when pytest-xdist is installed (it is in the `test` extra): the suite
# is network-free and order-independent, and 85s serial becomes ~22s. SERIAL=1
# runs it in one process (for a test that only fails under interleaving).
# pytest-randomly (also in the extra) shuffles the order on every run. This is
# THE gate: scripts/deploy.sh runs exactly this target.
# Every run prints its shuffle seed (pytest -q hides the header that carries it).
# Simply expanded once: a recursive `?=` re-ran the shell on every use, so the
# printed seed and the seed pytest got differed and the replay hint never replayed.
ifeq ($(origin SEED),undefined)
# python3, not $(PY): the seed needs no dev venv (a CI runner has none).
SEED := $(shell python3 -c "import random; print(random.randrange(2**32))" 2>/dev/null)
endif
XDIST = $(if $(SERIAL),,$(shell $(PY) -c "import xdist" 2>/dev/null && echo "-n auto --dist worksteal"))
# The widgets checkout, for the parity tests that read widget source
# (test_read_only_chip_parity). Without it they skip, and a skip proves nothing:
# the read-only chip sets had drifted for weeks behind exactly that skip.
FSR_ALL_WIDGETS ?= $(HOME)/WebstormProjects/fsr_all_widgets
# The harness that holds the canonical verdict vocabulary (test_verdict_vocab_parity).
FSR_WIDGET_HARNESS ?= $(FSR_ALL_WIDGETS)/fortisoar-widget-harness

test: ## T0 gate: the WHOLE pytest suite -- outer tests/ AND the connector's own tests/ (network-free, ~22s parallel; SERIAL=1)
	@test -d "$(FSR_ALL_WIDGETS)/widgets-src" || { echo "make test: FSR_ALL_WIDGETS='$(FSR_ALL_WIDGETS)' has no widgets-src/. The parity tests would skip, and a skip proves nothing. Set FSR_ALL_WIDGETS to the fsr_all_widgets checkout."; exit 2; }
	@$(PY) -c "import pytest_randomly" 2>/dev/null || { echo "make test: pytest-randomly is missing from $(VENV): the gate would run unshuffled. Fix: make setup"; exit 2; }
	@echo ">> test order seed: $(SEED)   (replay: make test SEED=$(SEED))"
	@FSRPB_DEV=1 SKIP_TOOLING_TESTS="$(SKIP_TOOLING_TESTS)" FSR_ALL_WIDGETS="$(FSR_ALL_WIDGETS)" FSR_WIDGET_HARNESS="$(FSR_WIDGET_HARNESS)" $(PY) -m pytest -q $(XDIST) --randomly-seed=$(SEED) $(PYTEST_ARGS)
	@# A full green run on a clean tree lets pre-push skip re-running the same code.
	@$(if $(strip $(PYTEST_ARGS)$(SKIP_TOOLING_TESTS)),true,bash scripts/hooks/test_pass_cache.sh mark editable)

mutants: ## T0: revert each named fix in a temp copy and expect its test red (ARGS="--only ID"; box-free, ~2-3 min)
	@FSRPB_DEV=1 FSR_ALL_WIDGETS="$(FSR_ALL_WIDGETS)" $(PY) scripts/mutation_check.py $(ARGS)

setup: ## Build/refresh the ONE dev python env from pyproject.toml + requirements.txt (FRESH=1 recreates; FSR_DEV_VENV=<dir> relocates)
	@bash "$(REPO)/scripts/dev_env.sh" setup

hooks: ## Install the repo git hooks (pre-push runs `make test`) -- see .github/workflows/ci.yml for the server-side gate
	@bash scripts/install_hooks.sh

t1-scenarios: ## T1 behavioural-invariant suite, BOX-FREE (LLM=fake|frank RUNS=n S=<scenario> JOBS=n). frank != box model: structural invariants only, never disposition
	@FSRPB_DEV=1 $(PY) scripts/t1_scenarios.py $(S) --llm "$(LLM)" --runs "$(RUNS)" --jobs "$(JOBS)"

preflight: ## T0 (~0.5s, offline): can the discovery tools resolve what each scenario ASKS FOR? Run BEFORE any S-slice. (S=s3 for one)
	@FSRPB_DEV=1 $(PY) scripts/preflight.py $(S) --verbose

# A mounted playbook (PLAYBOOK=) is a BUILD-intent turn against the built-in
# persona: the INTENT/PERSONA `?=` defaults above pick that.

# --- T0: the whole tool surface, deterministically ------------------------
# No LLM, no box, ~2s. Asserts the contract every tool must honour (resolvable /
# never-raises / gate-integrity / injection-rejected / schema-sane) across the
# WHOLE registry, then runs behavioural cases for the tools that genuinely run
# offline. This is the only tier that covers ALL tools -- an LLM-driven run can
# never guarantee it touched tool 58.
tool-sweep: ## T0 (~2s, offline, no LLM): contract invariants over EVERY tool + behavioural cases (TOOL=<one> STRICT=1)
	@FSRPB_DEV=1 $(PY) scripts/tool_sweep.py \
	  $(if $(TOOL),--tool "$(TOOL)",) $(if $(STRICT),--strict,)

# --- T1: conversations ----------------------------------------------------
# `local-turn` (PLAYBOOK= mounts a playbook) drives ONE turn; repeating --message only stacks user
# messages inside a single turn. These drive a real multi-turn conversation and
# the chat_resume halt/resume round-trip.
# A conversation tier is pointless against the fake provider -- it cannot carry
# state or emit a card -- so these targets default LLM to `frank`.
#
# ⚠️ Target-specific assignment, NOT `$(if $(LLM),$(LLM),frank)`: the file-level
# `LLM ?= fake` has ALREADY given LLM a value, so the `if` never sees an empty
# one and every run silently used the FAKE provider. That is exactly how it
# presented -- both gateway models returning byte-identical results (1 tool,
# no offers), which reads as a model/gateway fault and is really "no model ran".
# Same trap the INTENT/PERSONA defaults above record. CLI LLM= still wins.
conv: LLM = frank
conv: ## T1: drive ONE scripted conversation (SCRIPT=<name|path> [LLM= MODEL= TOOLS=])
	@FSRPB_DEV=1 $(PY) scripts/local_conversation.py \
	  --script "$(SCRIPT)" --llm "$(LLM)" \
	  --tools "$(TOOLS)" $(MODEL_FLAG)

conv-suite: ## T1 conversation suite, BOX-FREE: multi-turn continuity + resume (S=<scenario> RUNS=n JOBS=n MODEL=)
	@FSRPB_DEV=1 $(PY) scripts/conv_scenarios.py \
	  --llm "$(LLM)" --runs "$(RUNS)" --jobs "$(JOBS)" \
	  $(if $(S),--scenario "$(S)",) $(MODEL_FLAG)

# The POINT of this target is the COMPARISON, so a failing first model must not
# abort before the second one runs -- otherwise the one case you most want to
# see side by side (model A fails, does B?) is the one case it refuses to show.
# `-` per line so each model always runs; the summary re-derives the verdict.
# --- T1-wide: breadth, not depth -----------------------------------------
# conv-suite pins KNOWN defects with hand-authored scenarios. chat-sweep asks
# the other question -- "is it sound across the range of things analysts
# actually say?" -- over a JSON corpus in scripts/conversations/sweep/.
# Adding a scenario is a JSON file, never Python; expectations are declared by
# name against the vocabulary in scripts/chat_sweep.py (an unknown key is a
# hard error, so a typo can never silently grade nothing).
chat-sweep: ## T1-WIDE: drive the whole realistic-conversation corpus, box-free (CATEGORY= S= SAMPLE=n RUNS=n JOBS=n ARGS='--out f.json'). LIST=1 prints the corpus and each row's expectations instead (no LLM, no network)
	@FSRPB_DEV=1 $(PY) scripts/chat_sweep.py \
	  $(if $(LIST),--list,--llm "$(LLM)" --runs "$(RUNS)" --jobs "$(JOBS)" \
	  $(if $(CATEGORY),--category "$(CATEGORY)",) \
	  $(if $(SAMPLE),--sample "$(SAMPLE)",) \
	  $(if $(S),--scenario "$(S)",) $(MODEL_FLAG) $(ARGS))

analyst-sim: ## ANALYST SIM: an LLM plays the analyst (Frank, free) -- vague asks, choice cards, Apply, refinements -- and every failure is attributed to a layer -- default eco, vague voice, one session per goal; VOICES=all MODELS="coding-b200/eco coding-b200/balanced" for the full matrix (GOALS= VOICES= MODELS= RUNS= JOBS= OUT=)
	@FSRPB_DEV=1 $(PY) scripts/analyst_sim.py --runs "$(RUNS)" --jobs "$(JOBS)" \
	  $(if $(GOALS),--goals $(GOALS),) $(if $(VOICES),--voices $(VOICES),) \
	  $(if $(MODELS),--models $(MODELS),) $(if $(OUT),--out "$(OUT)",) $(ARGS)

# The cheap first move whenever the HARNESS changed: one row per surface, so a
# plumbing break shows up for ~1/7th the tokens of the full corpus. Run this
# before the full sweep, always -- a broken driver spends the whole corpus to
# tell you the same thing one row would have.

# Grading is PURE -- it reads finished conversations and returns verdicts. So a
# change that touches only grader code never needs the gateway: re-grade the
# stored transcripts instead. ~0.2s and zero tokens, vs ~15 min and a re-rolled
# model. Re-drive ONLY when the connector or prompt changed, i.e. when the
# transcripts themselves would differ.
#   make chat-sweep ARGS='--keep-conversations --out /tmp/sweep.json'   # once
#   make regrade DUMP=/tmp/sweep.json                                   # often
regrade: ## Re-grade a --keep-conversations dump: no model, no network (DUMP=path)
	@test -n "$(DUMP)" || { echo "usage: make regrade DUMP=/tmp/sweep.json"; exit 2; }
	@FSRPB_DEV=1 $(PY) scripts/chat_sweep.py --regrade "$(DUMP)"

# Known-answer rows: each mounts a recorded connector world (`strict_ops`) whose
# evidence decides the alert, and grades the VERDICT against it -- the only
# offline rows that check the answer rather than the structure. Record a new
# world from a real alert with scripts/record_world.py. RUNS=2: a wrong verdict
# twice is a defect.
known-answer: ## Grade triage VERDICTS against recorded evidence (box-free; MODEL= RUNS=2)
	@FSRPB_DEV=1 $(PY) scripts/chat_sweep.py --llm "$(LLM)" --category known_answer \
	  --runs $(if $(filter 1,$(RUNS)),2,$(RUNS)) --jobs 3 $(MODEL_FLAG) $(ARGS)

# The release gate. A connector release (`make deploy FW=...`) refuses without the
# stamp this writes for the current commit. Runs tool-sweep STRICT=1,
# t1-scenarios LLM=fake and known-answer LLM=frank (free gateway) in turn, on a
# clean tree; times are in TESTING.md.
quality-gate: ## Run the box-free quality tiers and stamp HEAD; `make deploy FW=...` refuses without the stamp (clean tree; ~see TESTING.md)
	@python3 scripts/quality_gate.py run

# Old name for `chat-sweep LIST=1`.
chat-sweep-list: LIST = 1
chat-sweep-list: chat-sweep

models: ## List the models this gateway virtual key actually permits
	@FSRPB_DEV=1 $(PY) scripts/list_models.py

# --- agent-quality analysis ----------------------------------------------
# What do agents ACTUALLY get wrong? Counts, not anecdote. One analysis core,
# two sources -- and they disagree, so run BOTH before concluding anything:
#   local = the long-lived dev/eval corpus.
#   box   = production-ish, but boxes keep few sessions (a connector rename
#           orphaned the old on-box db), so a pull is a tiny N: use `session-export`
#           and STORE=1 to analyze the kept history instead.
TOOL ?=
SESSION_ARGS ?=

# STORE=1 BOX=<alias> (or STORE=<alias> | STORE=all): read the exported local store instead of a box.
SESSION_FROM = $(if $(STORE),--store $(if $(filter 1,$(STORE)),$(or $(RESOLVED_BOX),$(error STORE=1 needs BOX=<alias>; or pass STORE=<alias>|all)),$(STORE)),--env "$(BOX_ENV)")

# --- session history: keep what the boxes forget --------------------------
# Boxes keep few sessions (the on-box db is keyed by the connector name, so the
# rename orphaned the old one). Export pulls them into ~/.fsr-agentic/sessions
# (FSR_AGENTIC_SESSIONS=<dir> overrides), incremental, never deletes. Read-only
# on the box. THE schedulable command:  make session-export BOX=<alias>
session-export: ## Export every session of BOX=<alias> into the local store (incremental, idempotent, never deletes; read-only on the box). Schedule this.
	@FSRPB_DEV=1 $(PY) scripts/session_store.py export --env "$(BOX_ENV)" --box "$(RESOLVED_BOX)"

session-import: ## Import sessions from a connector sessions db FILE into the local store as BOX=<alias> DB=<path> (never overwrites an existing session id; the file is copied, not modified; no box access)
	@test -n "$(DB)" || { echo "session-import: pass DB=<path to the sqlite file> and BOX=<alias>"; exit 2; }
	@test -n "$(RESOLVED_BOX)" || { echo "session-import: pass BOX=<alias>"; exit 2; }
	@FSRPB_DEV=1 $(PY) scripts/session_store.py import --db "$(DB)" --box "$(RESOLVED_BOX)"

session-summary: ## Sessions per box per day, synthetic vs real, from the local store (BOX=<alias> DAYS=30)
	@FSRPB_DEV=1 $(PY) scripts/session_store.py summary $(if $(RESOLVED_BOX),--box $(RESOLVED_BOX),) $(if $(DAYS),--days $(DAYS),)

analyze: SOURCE = local
analyze: ## Bucket agent tool errors + signal density (TOOL=validate_yaml to scope; SOURCE=local (default) reads the LOCAL session store, SOURCE=box pulls the $(BOX_ENV) box's sessions, a small N; STORE=1 BOX=<alias> reads the exported store)
	@FSRPB_DEV=1 $(PY) scripts/session_analyze.py \
	  $(if $(STORE),$(SESSION_FROM),$(if $(filter box,$(SOURCE)),--env "$(BOX_ENV)",)) \
	  $(if $(TOOL),--tool $(TOOL),) $(SESSION_ARGS)

LIMIT ?= 30
SOURCE ?= box
OUT ?=
session-health: ## (STORE=1 BOX=<alias> reads the exported local store, box unreachable) Chat-session HEALTH: failing tools with inputs+outputs, classed model/guard/env/tool_bug, recovered vs stuck (SOURCE=box|local LIMIT=30 OUT=report.md; budget: MAX_STUCK=n MAX_FAIL_RATE=0.2 -> exit 1 when exceeded; SESSION=<id> prints that one box session's turn-by-turn DIAGNOSIS instead)
ifneq ($(SESSION),)
	@FSRPB_DEV=1 $(PY) scripts/session_analyze.py $(SESSION_FROM) \
	  --session "$(SESSION)" --diagnose
else
	@FSRPB_DEV=1 $(PY) scripts/session_health.py \
	  $(if $(filter local,$(SOURCE)),--source local,$(SESSION_FROM)) \
	  --limit $(LIMIT) $(if $(OUT),--out $(OUT),) \
	  $(if $(MAX_STUCK),--max-stuck $(MAX_STUCK),) \
	  $(if $(MAX_FAIL_RATE),--max-fail-rate $(MAX_FAIL_RATE),) $(SESSION_ARGS)
endif

premise-advisory-rate: ## How often the verdict card showed the records advisory, per verdict turn, read from stored cards (BOX=<alias> reads that box; STORE=1 BOX=<alias> the exported store; LIMIT=60 sessions)
	@FSRPB_DEV=1 $(PY) scripts/premise_advisory_rate.py $(SESSION_FROM) --limit $(LIMIT)

session-bundle: ## One session -> one review file (SESSION=<id> [TURN=n] [OUT=file.md]; reads the local session store by default, STORE=<alias> narrows it, BOX=<alias> reads that box live): per round tools offered vs called, args, results, guard firings, tokens, answer, then the fixed verdict list
	@test -n "$(SESSION)" || { echo "session-bundle: pass SESSION=<session id>"; exit 2; }
	@FSRPB_DEV=1 $(PY) scripts/session_bundle.py "$(SESSION)" \
	  $(if $(STORE),--store $(if $(filter 1,$(STORE)),$(or $(RESOLVED_BOX),all),$(STORE)),$(if $(BOX),--env "$(BOX_ENV)",)) \
	  $(if $(TURN),--turn $(TURN),) $(if $(OUT),--out "$(OUT)",)

# --- release / ship / recycle ---------------------------------------------
# Three paths:
#   deploy → THE deploy command: framework release (if asked) + pin bump
#                  + connector ship, in the one order that is safe. Start here.
#   ship         → CONNECTOR only, onto the BOX=<name> box (no default box; pyfsr registry via scripts/box.sh).
#                  Assumes the pinned framework is already published.
#   ship-widget  → WIDGET via the harness's canonical ship-verify, onto whatever
#                  the harness .env points at (its own env file -- NOT $(BOX_ENV)).
FW ?=
deploy: ## THE deploy command: ship the connector (version self-corrects). Add FW=next|X.Y.Z only when you changed the framework (that publishes to PyPI). No FW = ship against the current pin.
	@bash "$(REPO)/scripts/deploy_chain.sh" $(if $(FW),--fw $(FW),) --bump $(BUMP) $(if $(NOTES),--notes "$(NOTES)",)

# Deprecated: `release-ship` was renamed to `deploy`. Kept as a quiet alias for
# muscle memory / stale scripts; intentionally has no `##` so help never lists it.
release-ship:
	@echo ">> 'make release-ship' is now 'make deploy' -- running it for you." >&2
	@$(MAKE) deploy FW="$(FW)" BUMP="$(BUMP)" NOTES="$(NOTES)"

bump-framework: ## Pin fsr-playbooks to VERSION=x.y.z; verifies symbols before writing. Prefer `make deploy FW=<v>`, which also releases + ships.
	@bash "$(REPO)/scripts/bump_framework.sh" $(VERSION)

ship-dev: ## ITERATE, don't ship: rsync the framework WORKING TREE onto the BOX=<alias> box + recycle. Seconds, no PyPI. Leaves the box on NO published version. ACTION=status: is the box dev-patched? (a patched box reports the wheel's version while running other code; check before believing any result). ACTION=restore: reinstall the pinned wheel and clear the marker. SYNC=tar: for boxes whose sudo user cannot rsync (fortidemo/managed appliances hand labuser a broad NOPASSWD allowlist with no rsync/tee); ships the same tree via allowlisted tar+find+rm
	@bash "$(REPO)/scripts/ship_dev.sh" $(if $(filter status,$(ACTION)),--status,$(if $(filter restore,$(ACTION)),--restore,$(if $(ACTION),$(error ACTION must be status or restore),)))

# Old names for `ship-dev ACTION=status|restore`.
ship-dev-status: ACTION = status
ship-dev-status: ship-dev
ship-dev-restore: ACTION = restore
ship-dev-restore: ship-dev

# Runs FIRST for every box-reaching target: prints the resolved target, or stops
# with the reason (re-run unexported, so a deprecated knob's warning or refusal shows).
.PHONY: box-guard
BOX_NEED := env,ssh
# doctor reports a box with no ssh profile instead of refusing it.
doctor: BOX_NEED := env,ssh?
session-export: BOX_NEED := env
box-guard:
	@BOX='$(or $(RESOLVED_BOX),$(BOX))' ENV='$(ENV)' FSR_ENV='$(FSR_ENV)' SSH_HOST='$(SSH_HOST)' bash "$(REPO)/scripts/box.sh" --need '$(BOX_NEED)'

$(BOX_TARGETS): box-guard
# `doctor` reaches a box unless LOCAL=1.
ifeq ($(LOCAL),)
doctor: box-guard
endif

ship-install: ## Install the CONNECTOR (the release path, not a front door): bump ($(BUMP)) → build → gate on the box resolving the framework pin → install → verify workers + framework (NOVERIFY=1 skips the worker-rollout gate and the post-ship warmup)
	@$(with_env); bash "$(REPO)/scripts/deploy.sh" --bump $(BUMP) $(if $(NOVERIFY),--no-verify,)
	@# Force a warmup so the op catalog re-syncs with the live box on every ship
	@# (a reinstall preserves the prior DB; without this a ship can leave a stale
	@# catalog missing newly-configured connectors, e.g. fortinet-fortiguard-ioc).
	@# Non-fatal: a warmup failure warns rather than failing the ship.
	@if [ -z "$(NOVERIFY)" ]; then $(with_env); cd "$(REPO)/scripts" && $(PY) -c "from fsr_live import LiveFSR; r=LiveFSR.from_env().execute('warmup', {'force': True}); print('warmup ok=', getattr(r,'ok',None), '| counts=', (getattr(r,'data',{}) or {}).get('counts'), '|', getattr(r,'error',None) or 'no error')" || echo "WARN: post-ship warmup failed - run 'make warmup' to refresh the op catalog"; fi

build: ## Bump + build the tarball only, no install (CI / dry run)
	@bash "$(REPO)/scripts/deploy.sh" --bump $(BUMP) --no-install

verify: ## Probe health_check until the worker set settles; assert every worker is on info.json's version AND the pinned fsr_playbooks
	@$(with_env); $(PY) "$(REPO)/scripts/verify_workers.py"

# --- the dev loop: speed first ------------------------------------------------
# For building the agent. Local (your Python + Frank) -> copy the working trees
# onto the box -> ~35s live smoke. Releases are NOT this: `make deploy`
# (tag, PyPI, CI) and the full UI sweep. See scripts/loop.sh for the stages.
loop: ## THE dev command: local tests + Frank agent smoke -> dev-push -> live smoke (~2-3 min). LOCAL_ONLY=1 SKIP_LOCAL=1 NO_AGENT=1 WIDGET=1|0
	@VIA_LOOP=1 bash "$(REPO)/scripts/loop.sh"

dev-push: ## Copy framework + connector WORKING TREES onto the BOX=<alias> box, install unmet requirements.txt pins, one recycle, wait for every worker on this build (seconds, not a release). SYNC=tar: for boxes whose sudo user cannot rsync (fortidemo labuser allowlist); same trees via allowlisted tar+find+rm
	@bash "$(REPO)/scripts/dev_push.sh"

live: ## Box gates on the BOX=<alias> box, through the INSTALLED connector. MODE=quick (~35s smoke; NO_AGENT=1 drops the LLM turn) | rows (sweep rows that mount a real record; CATEGORY= S= RUNS= ARGS=; read-only) | writes (WRITING rows, approve, read back; CATEGORY= S= RUNS= DEVICE= KEEP=1 ARGS=; needs FSR_WRITABLE=1) | integration (POST-SHIP gate; ONLY= NO_AGENT=1 GOLDEN=1) | autonomy (one-alert smoke first, aborts on failure; then seeded alerts, ~6 paid turns; cleans up) | p3 (agent-bound FortiGate on demand; invoke twice)
	@case "$(MODE)" in \
	  quick) $(with_env); cd "$(REPO)/scripts" && $(PY) live_integration.py --only health,compile,validate_ok,validate_bad,resolve,render_jinja$(if $(NO_AGENT),,$(COMMA)chat_action_card) ;; \
	  rows) $(with_env); FSRPB_DEV=1 $(PY) scripts/live_rows.py \
	    $(if $(CATEGORY),--category "$(CATEGORY)",) \
	    $(if $(S),--scenario "$(S)",) $(if $(RUNS),--runs "$(RUNS)",) $(ARGS) ;; \
	  writes) $(with_env); FSRPB_DEV=1 $(PY) scripts/live_writes.py \
	    $(if $(CATEGORY),--category "$(CATEGORY)",) \
	    $(if $(S),--scenario "$(S)",) $(if $(RUNS),--runs "$(RUNS)",) \
	    $(if $(DEVICE),--device "$(DEVICE)",) $(if $(KEEP),--keep,) $(ARGS) ;; \
	  integration) $(with_env); cd "$(REPO)/scripts" && $(PY) live_integration.py \
	    $(if $(ONLY),--only $(ONLY),) $(if $(NO_AGENT),--no-agent,) $(if $(GOLDEN),--golden,) ;; \
	  autonomy) $(with_env); cd "$(REPO)/scripts" && $(PY) live_autonomy.py $(ARGS) ;; \
	  p3) $(with_env); cd "$(REPO)/scripts" && $(PY) live_integration.py --only agent_block ;; \
	  *) echo "usage: make live MODE=quick|rows|writes|integration|autonomy|p3 BOX=<alias>"; exit 2 ;; \
	esac

# --- doctor ------------------------------------------------------------------
# Run this BEFORE believing any test result. Read-only, ~15s: repo drift, which
# fsr_playbooks the venv imports, framework pin vs tag, box connector/widget/
# framework versions, mixed worker builds, dev-patch marker, box LLM, and the
# integrations a sweep row needs. Exits 1 on any FAIL (a result taken now
# could not be trusted). LOCAL=1 skips the box.
doctor: ## Preflight: fail loud on anything that would make a test result lie (LOCAL=1 no box, RELEASE=1 dev-pushed box = FAIL, JSON=1)
	@$(if $(filter ok,$(DEV_ENV_STATUS)),$(PY),python3) "$(REPO)/scripts/doctor.py" $(if $(BOX_ENV),--env "$(BOX_ENV)" --box $(RESOLVED_BOX),$(if $(LOCAL),,$(if $(BOX),--box $(BOX),))) $(if $(BOX_SSH),--ssh-host $(BOX_SSH),) $(if $(LOCAL),--local,) $(if $(JSON),--json,) $(if $(RELEASE),--release,)

# --- live exercise (proves the crudhub/probes bridge through the real worker) ---
warmup: ## Run the connector's warmup op live (populates the op catalogue via crudhub)
	@$(with_env); cd "$(REPO)/scripts" && $(PY) -c "from fsr_live import LiveFSR; r=LiveFSR.from_env().execute('warmup'); print('warmup ok=', getattr(r,'ok',None), '|', getattr(r,'error',None) or 'no error')"

health: ## Run health_check live and print worker identity (pid/version)
	@$(with_env); cd "$(REPO)/scripts" && $(PY) -c "from fsr_live import LiveFSR; r=LiveFSR.from_env().execute('health_check'); import json; print(json.dumps(getattr(r,'data',{}) or {}, indent=2, default=str)[:1200])"

# --- box diagnostics (SSH) -------------------------------------------------
ssh: ## Open an interactive shell on the box
	@ssh $(BOX_SSH)

workers: ## Show the running integration-agent worker pids + start times on the box
	@ssh $(BOX_SSH) 'ps -o pid,lstart,cmd -C python 2>/dev/null | grep -iE "gunicorn|integrations-agent" | grep -v grep || ps aux | grep -iE "fsr-integrations|gunicorn" | grep -v grep'

logs: ## Tail the box integrations log (filter with Q=<pattern>)
	@ssh $(BOX_SSH) 'tail -n 120 /var/log/cyops/cyops-integrations/integrations.log 2>/dev/null | grep -iE "$${Q:-.}"' Q=$(Q)

bridge-check: ## SSH the box and prove integrations.crudhub + probes bridge resolve in the worker context
	@ssh $(BOX_SSH) 'cd $(BOX_INTEG) && DJANGO_SETTINGS_MODULE=integrations.settings $(BOX_PY) -' < "$(REPO)/scripts/bridge_check_remote.py"

mcp-bridge-check: ## Read-only probe: can the live client reach FortiSOAR 8.0's native MCP gateway (client.mcp.list_tools)? Settles the dynamic-tool-surface substrate.
	@$(with_env); cd "$(REPO)/scripts" && $(PY) mcp_bridge_check.py

# --- POST-SHIP acceptance gate ---------------------------------------------
# The offline tiers (tool-sweep / t1-scenarios / conv-suite / chat-sweep) are
# hermetic by design: tools answer from a cassette, so NOTHING in them proves
# FortiSOAR accepts a playbook the assistant pushed. `live_integration.py` is
# the only thing on disk that does -- it drives the INSTALLED connector through
# /api/integration/execute/ and asserts compile→push→dry_run→run for real. It
# sat unreachable from make for exactly as long as it existed.
#
# It is BOX-TOUCHING, so it cannot join the offline gate and is not folded into
# `ship` (a deploy that also runs live LLM turns is a slow deploy). Run it after
# a ship, and treat a FAIL as blocking the release:
#
#   make live MODE=integration                  # full gate, incl. live-LLM paths
#   make live MODE=integration NO_AGENT=1       # deterministic paths only (fast)
#   make live MODE=integration ONLY=compile,push
#   make live MODE=integration GOLDEN=1         # refresh tests/golden/live/
# Old names for `live MODE=...`: hidden aliases (no ## line, so they stay out of make help).
quick-live: MODE = quick
live-rows: MODE = rows
live-writes: MODE = writes
live-integration: MODE = integration
live-autonomy: MODE = autonomy
p3-proof: MODE = p3
quick-live live-rows live-writes live-integration live-autonomy p3-proof: live

ship-accept: ## ship-install, then immediately gate on the live-integration acceptance run
	@$(MAKE) ship-install BUMP=$(BUMP)
	@$(MAKE) live MODE=integration

# --- build-persona evals (docs/plans/build-persona-validation-plan.md) -----
# The bar these hold: a playbook the assistant built or edited RUNS on a real box
# and produces the right observable result -- not "the right tools were called".
# Box-touching by design (ENV=<env file>); they self-clean.
eval-selfcheck: ## Prove the eval harness itself measures what it claims to (no LLM; deterministic edit)
	@$(with_env); cd "$(REPO)/scripts" && $(PY) eval_selfcheck.py

eval-s1: ## P4 live: CASE=create (default) a build turn authors a playbook, Accept saves it, the saved steps match the offer, and it RUNS and creates the asked-for alert; CASE=refine Accept, then refine in the same chat with nothing open, Apply updates the SAME playbook and it runs with the change (RUNS=2)
	@$(with_env); cd "$(REPO)/scripts" && FSRPB_DEV=1 $(PY) $(if $(filter refine,$(CASE)),eval_s1_refine_saved.py,eval_s1_create.py) --runs $(or $(RUNS),2)

eval-effect: ## Live, FREE (no LLM): the runtime last mile of effect verification -- verify_playbook's effect inventory names the written field + its source step, then the fixture playbook is pushed, run, and the created record read back to confirm the computed value actually landed (not just that a record appeared). RUNS=1
	@$(with_env); cd "$(REPO)/scripts" && FSRPB_DEV=1 $(PY) eval_effect_readback.py --runs $(or $(RUNS),1)

pack: ## Build the Agentic Assistant pack (connector + both widgets + dashboard, one IMPORT WIZARD import -- Content Hub skips bundled connectors/widgets) and the release bundle beside it (pack + README + guides). Builds missing artifacts without bumping; refuses a zip pyfsr's validator rejects. ARGS='--no-bundle' | '--refresh-dashboard --env <path>'
	@$(PY) "$(REPO)/scripts/build_agentic_assistant_pack.py" $(ARGS)

configure: ## Create or update a connector configuration on the ENV box, then health-check it. WHAT=agentic-assistant (default) | fortigate | fortiedr | ... ; secrets come from the environment (OPENAI_API_KEY / ANTHROPIC_API_KEY for agentic-assistant). ARGS='--default --name X --dry-run'. List: make configure ARGS=--list
	@$(with_env); $(PY) "$(REPO)/scripts/configure_connector.py" $(if $(filter --list,$(ARGS)),,$(or $(WHAT),agentic-assistant)) $(ARGS)

provision: ## Provision an EXPERIMENTAL box with the Agentic Assistant from a pack: enable PyPI in the box pip.conf (ssh), import via the Import Wizard (pyfsr), publish both widgets. BOX=<alias> (or omit to be ASKED for URL/user/pass for a box that was never set up); PACK=<zip> or RELEASE=<tag|latest>; ARGS='--dry-run|--skip-pypi|--skip-publish'
	@$(PY) "$(REPO)/scripts/provision_box.py" $(if $(BOX),--box $(BOX)) $(if $(PACK),--pack $(PACK)) $(if $(RELEASE),--from-release $(RELEASE)) $(ARGS)

plant: ## Plant/find/clean up needles in an external source (ServiceNow) for live graded rows. VERB=health|plant|find|cleanup; plant needs CONFIG=<ServiceNow config name> (run VERB=health first); SCENARIO= (default snow_incident_needle) TOKEN= for find. Writes only to the lab ServiceNow, never FortiSOAR
	@$(with_env); $(PY) "$(REPO)/scripts/planter.py" $(or $(VERB),health) \
	  $(if $(SCENARIO),--scenario "$(SCENARIO)",) $(if $(CONFIG),--config "$(CONFIG)",) $(if $(TOKEN),--token "$(TOKEN)",) $(ARGS)

release-scores: ## P3: plant the ServiceNow needles, run the N5 planted rows on the INSTALLED connector, append a row (version, commit, model, per-scenario score) to docs/release_scores.jsonl. CONFIG=<ServiceNow config> required; RUNS= (default 1; paid when the box model is paid); SHOW=1 prints the table, no box
	@$(if $(SHOW),$(PY) "$(REPO)/scripts/release_scores.py" --show,$(with_env); $(PY) "$(REPO)/scripts/release_scores.py" --role "$(or $(ROLE),experimental)" --config "$(CONFIG)" --runs $(or $(RUNS),1) $(ARGS))


# --- widget side (delegates to the fsr_all_widgets repo) -------------------
matrix: ## Drive the live prompt/flow matrix (T1-T10/P1-P5) through the deployed widget
	@$(MAKE) -C "$(WIDGETS)" test-matrix-live BOX=$(RESOLVED_BOX)

ship-widget: ## Ship the WIDGET: canonical lint→unit→e2e→deploy→live-sweep (WIDGET=agenticAssistant BUMP=patch). Deploys to the harness .env box.
	@$(MAKE) -C "$(WIDGETS)" ship-verify WIDGET=$(or $(WIDGET),agenticAssistant) BUMP=$(BUMP)
