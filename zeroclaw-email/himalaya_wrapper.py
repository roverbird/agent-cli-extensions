"""
himalaya_wrapper.py — ZeroClaw email skill wrapper

ZeroClaw cannot pass arguments to skill [[tools]] commands at runtime.
The workaround: the agent writes parameters to a JSON state file first,
then calls the tool that reads from it.

State file location: skills/email/.mail_state.json
Schema:
  { "action": "read", "id": "17" }
  { "action": "send", "to": "a@b.com", "subject": "Hi", "body": "Hello" }
"""

import subprocess
import json
import shlex
import sys
import os

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SKILL_DIR, ".mail_state.json")

os.environ["HIMALAYA_LOG"] = "off"


def _run(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, text=True, capture_output=True, env=os.environ
        )
        if result.returncode != 0 and result.stderr.strip():
            return f"CLI ERROR (exit {result.returncode}): {result.stderr.strip()}"
        return result.stdout
    except Exception as e:
        return f"SYSTEM ERROR: {str(e)}"


def list_emails():
    output = _run("himalaya --output json envelope list").strip()
    try:
        json_start = output.find("[")
        if json_start == -1:
            return f"STATUS: No emails found or unexpected output. Raw: {output}"
        data = json.loads(output[json_start:])
        if not data:
            return "STATUS: Success. Inbox is empty."
        response = "STATUS: Success. Found the following emails:\n"
        for item in data[:5]:
            eid = item.get("id")
            subj = item.get("subject")
            sender = item.get("from", {}).get("addr", "Unknown")
            response += f"- ID {eid}: '{subj}' from {sender}\n"
        return response
    except Exception as e:
        return f"STATUS: Error parsing JSON.\nRaw: {output}\nDebug: {str(e)}"


def read_email(email_id):
    body = _run(f"himalaya message read {email_id}")
    if not body or not body.strip():
        return (
            f"STATUS: Warning. Email {email_id} returned empty body. "
            "The message may be HTML-only or the ID may be wrong."
        )
    return f"STATUS: Success. CONTENT OF EMAIL {email_id}:\n\n{body}"


def send_email(to, subject, body):
    email_raw = (
        f"From: hello@domain.com\nTo: {to}\nSubject: {subject}\n\n{body}" # set your address here
    )
    process = subprocess.Popen(
        shlex.split("himalaya message send"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(input=email_raw)
    if process.returncode == 0:
        return f"STATUS: Success. Email sent to {to}."
    return f"STATUS: Failed. Error: {stderr.strip() if stderr.strip() else stdout.strip()}"


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        return {"error": str(e)}


def clear_state():
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass


# ── entry points ─────────────────────────────────────────────────────────────

command = sys.argv[1] if len(sys.argv) > 1 else "list"

if command == "list":
    print(list_emails())

elif command == "read":
    # Usage: python3 wrapper.py read <id>
    if len(sys.argv) < 3:
        print("STATUS: Failed. Usage: python3 wrapper.py read <id>")
        sys.exit(1)
    print(read_email(sys.argv[2]))

elif command == "send":
    # Usage: python3 wrapper.py send <to> <subject> <body>
    if len(sys.argv) < 5:
        print("STATUS: Failed. Usage: python3 wrapper.py send <to> <subject> <body>")
        sys.exit(1)
    print(send_email(sys.argv[2], sys.argv[3], sys.argv[4]))

elif command == "exec":
    # State-file fallback path
    state = load_state()
    if state is None:
        print(f"STATUS: Failed. No state file found at {STATE_FILE}.")
        sys.exit(1)
    if "error" in state:
        print(f"STATUS: Failed. Could not parse state file: {state['error']}")
        sys.exit(1)
    action = state.get("action", "")
    if action == "read":
        email_id = state.get("id", "").strip()
        if not email_id:
            print("STATUS: Failed. State file missing 'id' field.")
        else:
            print(read_email(email_id))
    elif action == "send":
        to      = state.get("to",      "").strip()
        subject = state.get("subject", "No Subject").strip()
        body    = state.get("body",    "").strip()
        if not to:
            print("STATUS: Failed. State file missing 'to' field.")
        else:
            print(send_email(to, subject, body))
    else:
        print(f"STATUS: Failed. Unknown action '{action}'.")
    clear_state()

else:
    print(f"STATUS: Failed. Unknown command '{command}'. Use: list | read <id> | send <to> <subject> <body>")
    sys.exit(1)
