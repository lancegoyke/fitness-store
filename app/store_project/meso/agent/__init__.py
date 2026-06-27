"""The Meso agent: a Claude proposal engine behind the human review gate (B6).

The agent writes ``ProposedChange`` rows grouped into an ``AgentProposalBatch``;
the coach still approves. See ``docs/meso/agent-plan.md``.

- ``client``     — wraps the anthropic SDK (the only network boundary).
- ``validation`` — the deterministic server-side guardrail (contraindications
  enforced here, not just in the prompt).
- ``service``    — grounds the request, calls the client, validates, persists.
"""
