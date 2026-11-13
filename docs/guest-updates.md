# Custom guest update emails (fork extension, DEV-60)

This extension is maintained in `johnmarkli/kith-post`, based on upstream v1.3.0
(`d3e60067885e685bd7398e62a680a1a31b37b3a2`). It is separate from the upstream
**Re-send & re-collect** feature: announcements do not reset RSVPs.

## Use

1. Open an event and choose **Email guests**.
2. Select any combination of **All invited guests**, **Not responded**, **RSVP
   yes**, **RSVP no**, and individual guests. Groups are combined, not intersected;
   each email address is included once. All invited includes everyone eligible.
3. Write a subject and plain-text message, then **Preview recipients & message**.
4. Review the exact recipient list/count, message and send mode. Confirm only
   when correct. To change anything, return and create a new preview instead.
5. Review the per-recipient delivery status under **Recent updates and previews**.

Only email guests whose invitation is already sent (including yes/no responses)
are eligible. Unsent invitations and WhatsApp recipients are excluded. There is
no automatic default selection. At most 500 distinct addresses are allowed per
batch; this is an application safety cap, not a guarantee of your Gmail quota.
The subject is limited to 200 characters and the message to 10,000.

Each email includes the custom message, event title and that guest's existing
invitation link. Guests do not see each other's addresses. Event CC lists,
attachments, original invitation threading and WhatsApp are intentionally not
used. RSVP answers, headcounts, dietary notes, invitation delivery metadata and
reminders are unchanged.

## Modes and safety

- **dry-run:** no Gmail call; `.eml` files are saved separately under
  `data/outbox/<event>/updates/<batch>/<delivery>.eml`. A status of **sent** means
  written locally in this mode, not delivered to an inbox.
- **self-only:** each email is redirected to the sender's own inbox. No CC/BCC
  riders or original recipients receive the test messages.
- **live:** explicitly confirmed messages go to the intended recipients through
  the existing Google connection. This extension does not configure Gmail or
  change the server's existing send mode.

Preview snapshots and content are persisted, with subject/message and
recipient names/addresses encrypted using the existing Fernet key. Later RSVP
changes cannot silently expand a frozen preview. Deleted guests, guests no
longer eligible, or changed email addresses are skipped. A changed send mode
requires a new preview. Keep the original encryption key with database backups.

All pages require event ownership, use no-store caching, and protect preview and
confirmation POSTs with session-bound CSRF tokens. Confirmation tokens are bound
to their specific preview. An atomic draft claim prevents repeated or concurrent
confirmation from sending the same batch twice.

External email is not exactly-once: an accepted message can be followed by a
network timeout or process crash. Attempts are recorded before contacting Gmail.
**Uncertain/in-progress/failed batches are never automatically replayed.** Check
the sender's Sent folder and delivery statuses before creating another update.
A Google authentication failure stops further attempts and prompts reconnection.
History retains the ten most recent entries in the composer; existing preview
URLs remain accessible to the event owner. Unused drafts are retained until the
event is deleted; this extension does not introduce a cleanup job.

The feature adds only `guest_updates` and `update_deliveries` tables through the
existing startup schema initialization. Back up production before upgrading;
older app versions ignore these extra tables, so a code rollback need not drop
or rewrite application data.

## Development and upstream maintenance

```bash
uv sync --frozen --python 3.12
uv run --frozen pytest tests/test_updates.py tests/test_updates_web.py tests/test_updates_safety.py
uv run --frozen pytest
uv run --frozen ruff check kith/services/updates.py kith/web/routes_updates.py \
  tests/test_updates.py tests/test_updates_web.py tests/test_updates_safety.py
uv run --frozen mypy kith/services/updates.py kith/web/routes_updates.py
```

Tests use disposable databases/fake contacts and mocked Gmail, including when
checking live/self-only behavior. Never run tests against production data/env.

Before merging future upstream changes, review model/startup schema, auth/CSRF,
Gmail send modes and invitation status semantics. Re-run all regression and
announcement tests, review the resulting source commit, then update the homelab
archive checksum and pin. Do not replace files in a running container or enable
live sending as part of a routine upgrade.
