# Mission Continuity

**What must an AI agent remember when it forgets everything else?**

Mission Continuity is an experimental application exploring how long-running AI agents can preserve their objectives, operating boundaries, and consequential evidence when their conversation history is compressed.

The project uses a Claude-powered Pydantic AI agent to investigate a fictional billing dispute. It compares ordinary context compaction with an application-level governed-memory approach that preserves mission instructions and policy-required evidence.

## What we're exploring

- How compaction affects an agent's ability to complete a long-running task.
- Which instructions, obligations, and evidence must survive context reduction.
- How memory policy can determine what is retained, reintroduced, or excluded.
- How execution evidence can help reconstruct what an agent attempted and why.

## Architecture

Mission Continuity is built around three distinct components:

- **Pydantic AI + Claude:** The investigation agent and its tools.
- **Application-level memory governance:** Mission continuity, memory policy, and context reconstruction.
- **Sentience Governor:** Runtime execution evidence and governance observations.

A separate application guard prevents prohibited effects. Sentience Governor is used for execution evidence, not as the memory controller or enforcement mechanism.

## Status

Under development for the Horizon Agents Hackathon.

Implementation details, reproducible experiments, and demonstration instructions will be added as the project progresses.

---

Built with [Sentience Governor](https://github.com/crescerelabs/sentience-governor), Pydantic AI, and Claude.
