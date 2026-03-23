#!/usr/bin/env python3

# To ensure agent cannot modify the CLI, apply read-only permissions:
#
# chmod 555 cli.py
# chmod 444 agent-extensions.json
#
# Run agent under a separate user without write access to CLI directory.


import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional

# --- Configuration & Constants ---
DEFAULT_LIMIT = 1000
DEFAULT_MAX_DEPTH = 3
DEFAULT_TIMEOUT = 10.0
IGNORE_DIRS = {".git", ".cache", "node_modules", "__pycache__", ".venv"}

# Exit Codes per Contract
EX_SUCCESS = 0
EX_FAILURE = 1
EX_NOT_FOUND = 2
EX_PERM_DENIED = 3
EX_VALIDATION = 4
EX_INTERRUPT = 130
EX_PARTIAL = 206

class AgentCLI:
    def __init__(self):
        self.start_time = time.time()
        self.parser = self._setup_parser()
        self.args = self.parser.parse_args()
        
        if self.args.show_scope:
            self._print_scope()

    def _setup_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Agent-safe filesystem discovery CLI",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )
        
        # Global Flags
        parser.add_argument("--profile", default=os.getenv("CLI_PROFILE"), help="Configuration profile")
        parser.add_argument("--account", help="Required for mutations (unused in list)")
        parser.add_argument("--provider", default="local", help="Cloud or local provider")
        parser.add_argument("--json", action="store_true", help="Machine-readable output")
        parser.add_argument("--pretty-json", action="store_true", help="Pretty-printed JSON")
        parser.add_argument("--show-scope", action="store_true", default=True, help="Print execution context")

        # Query Safety Flags
        parser.add_argument("path", nargs="?", default=".", help="Target directory")
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max items to return")
        parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="Recursion limit")
        parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT, help="Execution timeout")
        parser.add_argument("--fast", action="store_true", default=True, help="Skip stat() enrichment")
        parser.add_argument("--all", action="store_true", help="Disable default ignore filters")

        return parser

    def _print_scope(self):
        if self.args.json or self.args.pretty_json:
            return
        scope = f"Scope: [profile: {self.args.profile or 'default'}] • [account: {self.args.account or 'none'}] • [provider: {self.args.provider}]"
        print(f"\033[2m{scope}\033[0m", file=sys.stderr)

    def _error(self, message: str, code: str, hint: str, exit_code: int = EX_FAILURE):
        err_obj = {
            "ok": False,
            "error": message,
            "code": code,
            "hint": hint,
            "details": {"path": self.args.path}
        }
        if self.args.json or self.args.pretty_json:
            indent = 2 if self.args.pretty_json else None
            print(json.dumps(err_obj, indent=indent), file=sys.stderr)
        else:
            print(f"Error [{code}]: {message}\nHint: {hint}", file=sys.stderr)
        sys.exit(exit_code)

    def list_files(self):
        target_path = Path(self.args.path).expanduser().resolve()
        
        if not target_path.exists():
            self._error("Path does not exist", "PATH_NOT_FOUND", "Verify the directory path exists.", EX_NOT_FOUND)
        
        if not target_path.is_dir():
            self._error("Target is not a directory", "NOT_A_DIRECTORY", "Use a directory path for 'list' command.", EX_VALIDATION)

        results = []
        truncated = False
        base_depth = len(target_path.parts)

        try:
            for root, dirs, files in os.walk(target_path, topdown=True):
                # Apply Max Depth
                current_depth = len(Path(root).parts) - base_depth
                if current_depth >= self.args.max_depth:
                    dirs.clear() # Stop recursion
                    continue

                # Apply Filters (unless --all)
                if not self.args.all:
                    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                
                for name in files:
                    # Enforce Timeout
                    if (time.time() - self.start_time) > self.args.timeout_sec:
                        truncated = True
                        break

                    # Enforce Limit
                    if len(results) >= self.args.limit:
                        truncated = True
                        break

                    full_path = Path(root) / name
                    item = {"path": str(full_path.relative_to(target_path))}
                    
                    # Enrichment (Slow Mode)
                    if not self.args.fast:
                        try:
                            stat = full_path.stat()
                            item.update({
                                "size_bytes": stat.st_size,
                                "modified": int(stat.st_mtime)
                            })
                        except PermissionError:
                            item["error"] = "permission_denied"

                    results.append(item)

                if truncated:
                    break

        except PermissionError:
            self._error("Permission denied", "FS_PERM_ERR", "Run with higher privileges or check folder ACLs.", EX_PERM_DENIED)
        except Exception as e:
            self._error(str(e), "INTERNAL_ERROR", "Check system logs.", EX_FAILURE)

        self._output_results(results, truncated)

    def _output_results(self, data: List[Dict], truncated: bool):
        payload = {
            "ok": True,
            "data": data,
            "count": len(data),
            "truncated": truncated,
            "limit": self.args.limit
        }

        if self.args.json or self.args.pretty_json:
            indent = 2 if self.args.pretty_json else None
            print(json.dumps(payload, indent=indent))
        else:
            # Human-pretty output
            print(f"\nListing: {self.args.path} ({'TRUNCATED' if truncated else 'COMPLETE'})")
            print("-" * 40)
            for item in data:
                print(f"- {item['path']}")
            if truncated:
                print(f"\n[!] Warning: Results truncated at {self.args.limit} items.")

        sys.exit(EX_PARTIAL if truncated else EX_SUCCESS)

if __name__ == "__main__":
    try:
        cli = AgentCLI()
        cli.list_files()
    except KeyboardInterrupt:
        sys.exit(EX_INTERRUPT)
