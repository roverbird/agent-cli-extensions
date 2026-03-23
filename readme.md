# Agent CLI Extensions

Agent CLI Extensions for _AI Agent safety_ provides minimal guidelines for designing command-line interfaces (CLI) that can be securely and efficiently used by AI agents. These CLIs are deliberately constrained to be predictable, bounded, and machine-readable.

The core idea is that an AI agent should never operate directly on raw systems such as filesystems, APIs, or cloud environments. Instead, it operates through a custom CLI that acts as a controlled interface. The CLI defines what the agent can read, change, and return, and under what limits those actions occur. It becomes a security and execution boundary.

> An AI agent should operate through a deliberately designed CLI, not directly. In such way, CLI becomes a security boundary.

This approach has two primary goals. First, it reduces security risk by enforcing explicit scope and preventing unbounded or implicit operations. Second, it improves efficiency for LLM-based workflows by producing small, structured outputs with predictable latency, minimizing token usage and retry loops.

> LLMs are expensive when CLI outputs are huge, operations take long, and retries happen due to unclear results. Agent-safe CLI ensures small outputs (bounded results), fast responses (predictable latency), and no ambiguity (fewer retries). This reduces token usage, runtime cost, and iteration loops.

The CLI is an execution surface for agents. Commands return predictable outputs, operate within defined bounds, and expose clear failure modes. This ensures controlled behavior, consistent outcomes, and reliable integration with automated workflows.

The CLI is a read-only interface for the agent and must not be modified by the agent. The agent may perform mutations only on external targets under explicit scope (--account, --profile, --limit) and never on the CLI code, spec, or configuration that defines its safe boundaries. The CLI and its spec must be protected by read-only file permissions, and the agent must runs under a separate user account with no write access to the CLI directory. This ensures the agent can execute the CLI safely but cannot modify its code, spec, or configuration.

## Key Properties of an Agent-Safe CLI

1. Controllable
- bounded (--limit)
- scoped (--account)
- interruptible

2. Predictable
- stable JSON output
- known execution time range
- no hidden behavior

3. Composable
- output feeds next step
- no manual interpretation required

4. Safe Defaults
- no unbounded scans
- no implicit context
- no silent destructive actions
- OpenCLI + x-agent means safe execution contract

This repository contains a minimal extension specification, a simple example, a reference generator prompt, and a working CLI implementation that demonstrates the pattern.

Inspired by "Security and Efficieny for Agents" Talk by Andreas Petersson at 2026-03-21 [Decentralized AI Day](https://luma.com/sb1g8oyb) (Vienna), his original presentation available [here](https://drive.google.com/file/d/1maQ5UjdbmoXC8R1yvYKUGxAUNcD8xzFR/view)

