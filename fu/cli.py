#!/usr/bin/env python3
# cli.py — Hardened
#
#   [CRIT]  Hard caps on --limit and --max-depth; argparse defaults are ceilings, not suggestions
#   [HIGH]  Structured JSON audit log written to LOG_DIR on every invocation
#   [MED]   --timeout-sec capped at MAX_TIMEOUT; cannot be raised by caller
#   [MED]   Path traversal guard: resolved path must stay inside allowed roots
#   [LOW]   Log rotation via RotatingFileHandler (no unbounded disk growth)
#   [LOW]   Exit codes and log records include invocation fingerprint (pid, uid, argv hash)
#
# Permissions reminder (applied by deploy.sh):
#   chmod 550  cli.py          — cliadmin + agentuser(group) r-x; others nothing
#   chown cliadmin:cliadmin    — agent cannot write this file

import os
import sys
import json
import time
import hashlib
import logging
import argparse
from pathlib import Path
from typing import List, Dict
from logging.handlers import RotatingFileHandler

# ── Hard Limits (not overridable by callers) ──────────────────────────────────
HARD_MAX_LIMIT     = 5_000      # absolute ceiling on --limit
HARD_MAX_DEPTH     = 10         # absolute ceiling on --max-depth
HARD_MAX_TIMEOUT   = 30.0       # absolute ceiling on --timeout-sec

# Safe defaults (used when caller omits the flag)
DEFAULT_LIMIT      = 1_000
DEFAULT_MAX_DEPTH  = 3
DEFAULT_TIMEOUT    = 10.0

# Filesystem
IGNORE_DIRS        = {".git", ".cache", "node_modules", "__pycache__", ".venv"}
LOG_DIR            = Path(os.getenv("AGENT_LOG_DIR", "/var/log/agent-cli"))
LOG_FILE           = LOG_DIR / "cli-audit.log"
LOG_MAX_BYTES      = 10 * 1024 * 1024   # 10 MB per file
LOG_BACKUP_COUNT   = 5                  # keep 5 rotated files

# Allowed path roots — callers may only target paths under these prefixes.
# Override via AGENT_ALLOWED_ROOTS env var (colon-separated).
_roots_env = os.getenv("AGENT_ALLOWED_ROOTS", "")
ALLOWED_ROOTS: List[Path] = (
    [Path(r).resolve() for r in _roots_env.split(":") if r]
    if _roots_env
    else []          # empty = no restriction (preserve original behaviour)
)

# Exit codes (per original contract)
EX_SUCCESS    = 0
EX_FAILURE    = 1
EX_NOT_FOUND  = 2
EX_PERM_DENIED = 3
EX_VALIDATION = 4
EX_INTERRUPT  = 130
EX_PARTIAL    = 206


# ── Audit logger setup ────────────────────────────────────────────────────────

def _build_audit_logger() -> logging.Logger:
    """
    Returns a logger that writes structured JSON records to a rotating file.
    Falls back gracefully if LOG_DIR isn't writable (e.g. during dev/test).
    """
    logger = logging.getLogger("agent-cli.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger  # already configured (e.g. re-import in tests)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except (PermissionError, OSError) as exc:
        # Non-fatal: fall back to stderr so the agent can still run
        fallback = logging.StreamHandler(sys.stderr)
        fallback.setFormatter(logging.Formatter("[audit-fallback] %(message)s"))
        logger.addHandler(fallback)
        logger.warning(json.dumps({
            "event": "audit_log_init_failed",
            "reason": str(exc),
            "fallback": "stderr",
        }))

    return logger


AUDIT = _build_audit_logger()


def _invocation_id() -> str:
    """Stable fingerprint for this exact invocation (pid + uid + argv)."""
    raw = f"{os.getpid()}:{os.getuid()}:{' '.join(sys.argv)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def audit(event: str, **fields):
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "invocation_id": _invocation_id(),
        **fields,
    }
    AUDIT.info(json.dumps(record))


# ── Main CLI class ────────────────────────────────────────────────────────────

class AgentCLI:
    def __init__(self):
        self.start_time = time.time()
        self.parser = self._setup_parser()
        self.args = self.parser.parse_args()

        # ── Apply hard caps silently but record when a cap was hit ──────────
        self._cap_applied = {}

        if self.args.limit > HARD_MAX_LIMIT:
            self._cap_applied["limit"] = {"requested": self.args.limit, "capped": HARD_MAX_LIMIT}
            self.args.limit = HARD_MAX_LIMIT

        if self.args.max_depth > HARD_MAX_DEPTH:
            self._cap_applied["max_depth"] = {"requested": self.args.max_depth, "capped": HARD_MAX_DEPTH}
            self.args.max_depth = HARD_MAX_DEPTH

        if self.args.timeout_sec > HARD_MAX_TIMEOUT:
            self._cap_applied["timeout_sec"] = {"requested": self.args.timeout_sec, "capped": HARD_MAX_TIMEOUT}
            self.args.timeout_sec = HARD_MAX_TIMEOUT

        if self.args.show_scope:
            self._print_scope()

        # Audit every invocation at startup
        audit(
            "invocation",
            argv=sys.argv[1:],
            path=self.args.path,
            limit=self.args.limit,
            max_depth=self.args.max_depth,
            timeout_sec=self.args.timeout_sec,
            caps_applied=self._cap_applied if self._cap_applied else None,
        )

    def _setup_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Agent-safe filesystem discovery CLI (hardened)",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )

        # Global flags
        parser.add_argument("--profile",     default=os.getenv("CLI_PROFILE"), help="Configuration profile")
        parser.add_argument("--account",     help="Required for mutations (unused in list)")
        parser.add_argument("--provider",    default="local", help="Cloud or local provider")
        parser.add_argument("--json",        action="store_true", help="Machine-readable output")
        parser.add_argument("--pretty-json", action="store_true", help="Pretty-printed JSON")
        parser.add_argument("--show-scope",  action="store_true", default=True, help="Print execution context")

        # Query flags — defaults are safe; hard caps enforced in __init__
        parser.add_argument("path",           nargs="?", default=".", help="Target directory")
        parser.add_argument("--limit",        type=int,   default=DEFAULT_LIMIT,
                            help=f"Max items to return (hard cap: {HARD_MAX_LIMIT})")
        parser.add_argument("--max-depth",    type=int,   default=DEFAULT_MAX_DEPTH,
                            help=f"Recursion limit (hard cap: {HARD_MAX_DEPTH})")
        parser.add_argument("--timeout-sec",  type=float, default=DEFAULT_TIMEOUT,
                            help=f"Execution timeout in seconds (hard cap: {HARD_MAX_TIMEOUT})")
        parser.add_argument("--fast",         action="store_true", default=True,
                            help="Skip stat() enrichment")
        parser.add_argument("--all",          action="store_true",
                            help="Disable default ignore filters")

        return parser

    def _print_scope(self):
        if self.args.json or self.args.pretty_json:
            return
        scope = (
            f"Scope: [profile: {self.args.profile or 'default'}] "
            f"• [account: {self.args.account or 'none'}] "
            f"• [provider: {self.args.provider}]"
        )
        if self._cap_applied:
            scope += f" • [CAPS APPLIED: {self._cap_applied}]"
        print(f"\033[2m{scope}\033[0m", file=sys.stderr)

    def _error(self, message: str, code: str, hint: str, exit_code: int = EX_FAILURE):
        audit("error", code=code, message=message, exit_code=exit_code)
        err_obj = {
            "ok":      False,
            "error":   message,
            "code":    code,
            "hint":    hint,
            "details": {"path": self.args.path},
        }
        if self.args.json or self.args.pretty_json:
            indent = 2 if self.args.pretty_json else None
            print(json.dumps(err_obj, indent=indent), file=sys.stderr)
        else:
            print(f"Error [{code}]: {message}\nHint: {hint}", file=sys.stderr)
        sys.exit(exit_code)

    # ── Path traversal guard ─────────────────────────────────────────────────

    def _validate_path(self, target: Path):
        """
        If ALLOWED_ROOTS is configured, reject any path that escapes those roots.
        This prevents an agent from walking sensitive directories outside its scope.
        """
        if not ALLOWED_ROOTS:
            return  # no restriction configured

        for root in ALLOWED_ROOTS:
            try:
                target.relative_to(root)
                return  # target is inside this allowed root → OK
            except ValueError:
                continue

        audit(
            "path_traversal_blocked",
            requested=str(target),
            allowed_roots=[str(r) for r in ALLOWED_ROOTS],
        )
        self._error(
            "Requested path is outside allowed roots",
            "PATH_TRAVERSAL",
            f"Set AGENT_ALLOWED_ROOTS to include this path, or use a path under: "
            f"{', '.join(str(r) for r in ALLOWED_ROOTS)}",
            EX_PERM_DENIED,
        )

    # ── Core listing logic ───────────────────────────────────────────────────

    def list_files(self):
        target_path = Path(self.args.path).expanduser().resolve()

        if not target_path.exists():
            self._error("Path does not exist", "PATH_NOT_FOUND",
                        "Verify the directory path exists.", EX_NOT_FOUND)

        if not target_path.is_dir():
            self._error("Target is not a directory", "NOT_A_DIRECTORY",
                        "Use a directory path for 'list' command.", EX_VALIDATION)

        self._validate_path(target_path)

        results: List[Dict] = []
        truncated = False
        base_depth = len(target_path.parts)

        try:
            for root, dirs, files in os.walk(target_path, topdown=True):
                current_depth = len(Path(root).parts) - base_depth

                if current_depth >= self.args.max_depth:
                    dirs.clear()
                    continue

                if not self.args.all:
                    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

                for name in files:
                    if (time.time() - self.start_time) > self.args.timeout_sec:
                        truncated = True
                        audit("truncated", reason="timeout", items_collected=len(results))
                        break

                    if len(results) >= self.args.limit:
                        truncated = True
                        audit("truncated", reason="limit", items_collected=len(results))
                        break

                    full_path = Path(root) / name
                    item = {"path": str(full_path.relative_to(target_path))}

                    if not self.args.fast:
                        try:
                            stat = full_path.stat()
                            item.update({
                                "size_bytes": stat.st_size,
                                "modified":   int(stat.st_mtime),
                            })
                        except PermissionError:
                            item["error"] = "permission_denied"

                    results.append(item)

                if truncated:
                    break

        except PermissionError:
            self._error("Permission denied", "FS_PERM_ERR",
                        "Run with higher privileges or check folder ACLs.", EX_PERM_DENIED)
        except Exception as exc:
            self._error(str(exc), "INTERNAL_ERROR", "Check system logs.", EX_FAILURE)

        self._output_results(results, truncated)

    def _output_results(self, data: List[Dict], truncated: bool):
        elapsed = round(time.time() - self.start_time, 3)

        audit(
            "completed",
            items_returned=len(data),
            truncated=truncated,
            elapsed_sec=elapsed,
        )

        payload = {
            "ok":        True,
            "data":      data,
            "count":     len(data),
            "truncated": truncated,
            "limit":     self.args.limit,
        }

        if self.args.json or self.args.pretty_json:
            indent = 2 if self.args.pretty_json else None
            print(json.dumps(payload, indent=indent))
        else:
            print(f"\nListing: {self.args.path} ({'TRUNCATED' if truncated else 'COMPLETE'})")
            print("-" * 40)
            for item in data:
                print(f"- {item['path']}")
            if truncated:
                print(f"\n[!] Warning: Results truncated at {self.args.limit} items.")

        sys.exit(EX_PARTIAL if truncated else EX_SUCCESS)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        cli = AgentCLI()
        cli.list_files()
    except KeyboardInterrupt:
        audit("interrupted")
        sys.exit(EX_INTERRUPT)
