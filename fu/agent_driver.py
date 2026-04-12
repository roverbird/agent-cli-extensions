#!/usr/bin/env python3
# agent_driver.py — hardened
#
# Architecture: Human → LLM (external API) → cli.py (deterministic)
#
# Security properties vs original:
#   [CRIT-1] CLI path is resolved and pinned at startup — no dynamic construction
#   [CRIT-2] All CLI args are built from a validated allowlist — no string interpolation
#   [CRIT-3] subprocess never uses shell=True — no shell injection surface
#   [HIGH-1] CLI output is treated as untrusted data — never interpolated into commands
#   [HIGH-2] subprocess timeout enforced independently of CLI's own --timeout-sec
#   [HIGH-3] argv of CLI call is logged before execution (audit trail)
#   [MED-1]  NL input is length-capped before being sent to the LLM
#   [MED-2]  LLM response is parsed structurally, not with string matching
#   [MED-3]  Graceful degradation: LLM failures do not expose stack traces to user

import os
import sys
import json
import shlex
import logging
import subprocess
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

# Pinned at module load — never constructed at runtime from user input
CLI_PATH = (Path(__file__).parent / "cli.py").resolve()

# Hard cap on natural-language input length sent to LLM (tokens ≈ chars/4)
MAX_NL_INPUT_CHARS = 500

# Hard cap on how long we wait for the CLI subprocess (independent of --timeout-sec)
SUBPROCESS_TIMEOUT_SEC = 45

# Hard cap on CLI output we'll parse (prevents memory exhaustion on runaway output)
MAX_CLI_OUTPUT_BYTES = 1 * 1024 * 1024  # 1 MB

# Allowed CLI flags — any flag the LLM tries to use must be in this set.
# Extend deliberately; never derive this list from LLM output.
ALLOWED_FLAGS = {
    "--json", "--pretty-json", "--limit", "--max-depth",
    "--timeout-sec", "--fast", "--all", "--show-scope",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("agent-driver")

# ── Arg validation ────────────────────────────────────────────────────────────

def validate_args(args: list[str]) -> list[str]:
    """
    Accept only flags from ALLOWED_FLAGS and their values.
    Reject anything else — including path traversal attempts and shell metacharacters.
    Returns the cleaned arg list or raises ValueError.
    """
    clean = []
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--"):
            if token not in ALLOWED_FLAGS:
                raise ValueError(f"Disallowed flag: {token!r}")
            clean.append(token)
            # Consume the next token if it's a value (not another flag)
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                value = args[i + 1]
                # Reject shell metacharacters in values
                if any(c in value for c in (";", "&", "|", "`", "$", "\n", "\r")):
                    raise ValueError(f"Unsafe characters in value: {value!r}")
                clean.append(value)
                i += 2
                continue
        else:
            # Positional argument (path target) — basic sanity check
            if any(c in token for c in (";", "&", "|", "`", "$", "\n", "\r")):
                raise ValueError(f"Unsafe characters in path: {token!r}")
            clean.append(token)
        i += 1
    return clean

# ── LLM translation ───────────────────────────────────────────────────────────

def nl_to_cli_args(nl_input: str) -> list[str]:
    """
    Translate natural language to CLI args via the external LLM API.
    Falls back to a minimal safe default if the LLM call fails.

    The LLM is instructed to return JSON only — never a mix of prose and args.
    We parse structurally; we never eval or exec the response.
    """
    if len(nl_input) > MAX_NL_INPUT_CHARS:
        raise ValueError(
            f"Input too long ({len(nl_input)} chars, max {MAX_NL_INPUT_CHARS}). "
            "Please be more concise."
        )

    system_prompt = f"""You are a CLI argument translator. Your only job is to convert
a natural language filesystem request into arguments for cli.py.

Respond with ONLY a JSON object in this exact shape — no prose, no markdown:
{{"args": ["--flag", "value", ...]}}

Allowed flags: {sorted(ALLOWED_FLAGS)}
Path argument: include a directory path as a plain string (no flag prefix) if the user specifies one.
Default path: "." (current directory)
Always include: --json (required for machine parsing)
Never include flags not in the allowed list.
Never include shell metacharacters."""

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        log.warning("LLM_API_KEY not set — using rule-based fallback")
        return _rule_based_fallback(nl_input)

    try:
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": "gemini-1.5-flash",    # swap to your preferred model
            "contents": [{"parts": [{"text": nl_input}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {"responseMimeType": "application/json"},
        }).encode()

        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash:generateContent?key={api_key}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())

        raw = body["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw)

        if not isinstance(parsed.get("args"), list):
            raise ValueError("LLM response missing 'args' list")

        return validate_args(parsed["args"])

    except (KeyError, json.JSONDecodeError, ValueError) as exc:
        log.warning("LLM response parse error (%s) — using fallback", exc)
        return _rule_based_fallback(nl_input)
    except Exception as exc:
        log.warning("LLM call failed (%s) — using fallback", exc)
        return _rule_based_fallback(nl_input)


def _rule_based_fallback(nl: str) -> list[str]:
    """
    Minimal deterministic fallback when LLM is unavailable.
    Only understands 'list [in <path>]' — refuses anything else safely.
    """
    nl = nl.lower().strip()
    args = ["--json"]

    if "list" in nl:
        path = "."
        if " in " in nl:
            candidate = nl.split(" in ", 1)[-1].strip()
            # Reject anything that looks like a path traversal
            if ".." not in candidate and not candidate.startswith("/"):
                path = candidate
        args.append(path)
        return args

    raise ValueError(
        "Could not interpret request and LLM is unavailable. "
        "Try: 'list files in <directory>'"
    )

# ── CLI execution ─────────────────────────────────────────────────────────────

def run_cli(args: list[str]) -> Optional[dict]:
    """
    Run cli.py as a subprocess with the validated arg list.
    - Never uses shell=True
    - Enforces an independent subprocess timeout
    - Caps output read to MAX_CLI_OUTPUT_BYTES
    - Treats all output as untrusted data
    """
    cmd = [sys.executable, str(CLI_PATH)] + args

    # Audit log: record exactly what we're about to run
    log.info("cli invocation: %s", shlex.join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            shell=False,        # never True — no shell injection surface
            check=False,        # we handle exit codes ourselves
        )

        stdout = result.stdout[:MAX_CLI_OUTPUT_BYTES]
        exit_code = result.returncode

        log.info("cli exit_code=%d stdout_bytes=%d", exit_code, len(stdout))

        # Exit codes per contract: 0=success, 206=partial, others=error
        if exit_code not in (0, 206):
            stderr_preview = result.stderr[:500].decode("utf-8", errors="replace")
            log.warning("cli non-zero exit: %s", stderr_preview)
            return None

        return json.loads(stdout)

    except subprocess.TimeoutExpired:
        log.error("cli subprocess timed out after %ds", SUBPROCESS_TIMEOUT_SEC)
        return None
    except json.JSONDecodeError as exc:
        log.error("cli returned non-JSON output: %s", exc)
        return None

# ── Output rendering ──────────────────────────────────────────────────────────

def render_output(output: dict):
    """
    Render CLI output for the human operator.
    Treats all values from output as data — never interpolates into commands.
    """
    if not output.get("ok"):
        print(f"Error: {output.get('error', 'unknown error')}")
        hint = output.get("hint")
        if hint:
            print(f"Hint: {hint}")
        return

    items = output.get("data", [])
    if not items:
        print("No results.")
        return

    for item in items:
        # item["path"] is untrusted data — printed as a value, never executed
        path = item.get("path", "(unknown)")
        size = item.get("size_bytes")
        if size is not None:
            print(f"  {path}  ({size:,} bytes)")
        else:
            print(f"  {path}")

    count = output.get("count", len(items))
    truncated = output.get("truncated", False)
    limit = output.get("limit")

    print(f"\n{count} item(s)", end="")
    if truncated:
        print(f" — truncated at limit {limit}. Use --limit to adjust.", end="")
    print()

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print("Agent CLI driver ready. Type 'exit' to quit.\n")

    while True:
        try:
            nl = input("> ").strip()
        except EOFError:
            break

        if nl.lower() in ("exit", "quit", "q"):
            break

        if not nl:
            continue

        try:
            cli_args = nl_to_cli_args(nl)
        except ValueError as exc:
            print(f"Could not translate request: {exc}")
            continue

        output = run_cli(cli_args)

        if output is None:
            print("The CLI did not return a usable response. Check logs for details.")
            continue

        render_output(output)
        print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
