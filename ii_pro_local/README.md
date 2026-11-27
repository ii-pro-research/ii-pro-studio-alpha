# Local Agent Balanced

The trusted entry point is `python -m ii_pro_local --help`. Use a dedicated
Core state directory and a Python file allowlist in an operator-selected Git
repository. Supply two full commit IDs for the baseline and target and the
expected digest of `qwen7b-fast:latest`.

`prepare` creates or returns the current step and displays its frozen operation.
It does not invoke Ollama. After consent, `dispatch` accepts only the displayed
Run ID, Approval ID and operation digest. Each additional generation needs its
own approval. `status` lists durable observations and artifact references.
`stop` records an intent to stop on the owning Core. An in-process caller can
signal while a step is running. The CLI opens its own Core, so a separate CLI
invocation cannot take ownership while another invocation is active. An Ollama
stop is not assumed. An unknown Run blocks continuation and replay.

Registration reads regular loose SHA-1 Git objects and verifies object hashes.
It supports explicit UTF-8 Python paths only. Packed objects, linked Git
directories, symlinks and junctions are refused. Git tools compare the two
commits' selected file contents. They do not show a live worktree status.
Registration does not invoke Git or evaluate its hooks, config or filters.
The operator must select files intended for model use. Credential-pattern
filtering is a secondary check and cannot detect all secrets.

One validated proposal per generation may inspect the registered snapshot or
produce a text diff. Patch checks use AST parsing and never apply or execute
code. A model report and broker evidence are stored separately. Functional
tests require a separate approved Worker operation. Two rejected patch
candidates produce a sealed handoff marked waiting for GPT Engineer.

The input uses a conservative UTF-8 byte limit for a 2048-token model context,
plus the profile's bounded output. Actual tokenization and behavior depend on
Ollama's model template. Only verified memory enters the generation context.
The broker treats model text and recalled observations as data.

Balanced passes an Ollama Structured Outputs schema in `/api/chat` as
`format` and sets temperature to zero. The exact schema is stored in the
step's approval-bound description. It permits only catalog tools, registered
paths, and observed evidence references. The broker still parses and validates
the raw response independently. A rejected proposal stays in the evidence as
received, with a bounded diagnostic for the next approved step.
