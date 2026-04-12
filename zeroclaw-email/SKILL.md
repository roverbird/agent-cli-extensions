# Email Skill

Manages the **hellow@domain.com** mailbox.

> **IMPORTANT — read this before using any email function:**
> Only ONE registered tool exists: `list_mail`. There is no `read_mail` tool
> and no `send_mail` tool. Reading and sending MUST be done by calling the
> built-in `shell` tool with the exact commands below. Never call
> `email.read_mail` or `email.send_mail` — they do not exist.

---

## List emails

Call the registered tool: `list_mail`

Returns the 5 most recent emails with numeric IDs, subjects, and senders.

---

## Read an email

Use the **`shell` tool**. Replace `17` with the actual ID:

```
python3 -u skills/email/himalaya_wrapper.py read 17
```

---

## Send an email

Use the **`shell` tool**:

```
python3 -u skills/email/himalaya_wrapper.py send a@example.com "Subject here" "Body text here"
```

Arguments in order: `send <to> <subject> <body>`. Quote multi-word values.

---

## Workflows

**Read the latest email:**
1. `list_mail` → note the top ID
2. `shell`: `python3 -u skills/email/himalaya_wrapper.py read <id>`

**Send an email:**
1. `shell`: `python3 -u skills/email/himalaya_wrapper.py send <addr> "<subject>" "<body>"`

**Reply to an email:**
1. `list_mail` → note ID and sender address
2. `shell`: `python3 -u skills/email/himalaya_wrapper.py read <id>`
3. `shell`: `python3 -u skills/email/himalaya_wrapper.py send <sender> "Re: <subject>" "<reply>"`

---

## STATUS lines

| STATUS | Meaning |
|--------|---------|
| `STATUS: Success.` | Operation completed |
| `STATUS: Warning. empty body` | Email is HTML-only |
| `STATUS: Failed. MAIL_ID not set` | Add `MAIL_ID=<number>` before the python3 command |
| `STATUS: Failed. MAIL_TO not set` | Add `MAIL_TO=<address>` before the python3 command |
| `CLI ERROR (exit N)` | Himalaya error — report text to user |

