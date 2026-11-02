"""The decision core's SQLite schema, as ordered migrations (07 section 11.1).

Each migration runs once, in order, inside one transaction, and records its
number in `meta.schema_version`. A store newer than this code refuses to
open, so each migration from 6 on also has its way back in DOWNS, for the VM's
follower to put the release before it back (see store.downgrade). Rows that are history (artifact revisions, responses, events, effect
receipts) cannot be changed or deleted: triggers abort the statement.
"""

from __future__ import annotations

# The flags migration 1 made. Frozen: migration 1's text, and so its pinned
# hash, is built from this tuple. A later flag is added by a migration.
FLAGS_1 = ("decision_write", "mobile_read", "mobile_chat_write", "push_send", "connector_effects")
# own_chat_write (migration 7): Patrick's chat with his own Hermes, my-hermes on
# the VM and his Hermes on One, from the phone.
FLAGS_7 = FLAGS_1 + ("own_chat_write",)
# session_manage_write (migration 10): rename, hide and delete Patrick's own chats from the phone.
FLAGS = FLAGS_7 + ("session_manage_write",)
# Patrick's own Hermes: the gateways the phone reaches beside the Bots' own.
OWN_GATEWAYS = ("me", "one")

# Card kinds (07 section 5). "main" is step 6, which waits for phase 7.
KINDS = ("question", "change", "review", "todo", "board_review", "weekly_review", "main")
STATUSES = ("draft", "pending", "resolved", "superseded", "cancelled", "expired")
PRINCIPAL_KINDS = ("patrick", "bramble", "specialist", "push_worker")
OUTBOX_STATES = ("queued", "leased", "sent", "uncertain", "cancelled", "dead", "quarantined")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _frozen(table: str) -> str:
    return f"""
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} rows are history and never change'); END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} rows are history and are never deleted'); END;
"""


# A "Bot told" message that died only because the Bots' gateway was not ready, back in the queue
# (migrations 11 and 12).
_REQUEUE_NOT_READY = """
UPDATE connector_outbox
  SET status = 'queued', lease_until = NULL, attempts = max(attempts - 1, 0),
      reason = 'waiting for the Bots'' gateway: ' || reason,
      updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
  WHERE status = 'dead' AND kind = 'decision.answered'
    AND (reason IN ('could not check the Bots'' gateway', 'could not start Hermes''s Python')
         OR reason LIKE 'the Bots'' gateway is %')
    AND EXISTS (SELECT 1 FROM decisions d WHERE d.id = connector_outbox.decision_id AND d.status = 'resolved')
    AND NOT EXISTS (SELECT 1 FROM effect_receipts e WHERE e.outbox = 'connector'
                    AND e.outbox_id = connector_outbox.id AND e.outcome <> 'failed');
"""


MIGRATIONS: list[str] = [
    # 1: the whole first schema
    f"""
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  is_test INTEGER NOT NULL CHECK (is_test IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE flags (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN {_in(FLAGS_1)}),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  changed_at TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);

CREATE TABLE principals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  kind TEXT NOT NULL CHECK (kind IN {_in(PRINCIPAL_KINDS)}),
  bot TEXT,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  disabled_at TEXT,
  CHECK ((kind IN ('bramble', 'specialist')) = (bot IS NOT NULL))
);
CREATE UNIQUE INDEX principals_one_patrick ON principals(workspace_id) WHERE kind = 'patrick';
CREATE UNIQUE INDEX principals_one_per_bot ON principals(workspace_id, bot) WHERE bot IS NOT NULL;
CREATE UNIQUE INDEX principals_one_push_worker ON principals(workspace_id) WHERE kind = 'push_worker';

CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  kind TEXT NOT NULL CHECK (kind IN ('phone', 'desktop')),
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE sessions (
  id_hash TEXT PRIMARY KEY CHECK (length(id_hash) = 64),
  csrf_hash TEXT NOT NULL CHECK (length(csrf_hash) = 64),
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  method TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  idle_expires_at TEXT NOT NULL,
  fresh_until TEXT NOT NULL,
  revoked_at TEXT,
  revoked_reason TEXT
);

CREATE TABLE recovery_codes (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  salt BLOB NOT NULL,
  code_hash BLOB NOT NULL,
  created_at TEXT NOT NULL,
  used_at TEXT,
  replaced_at TEXT
);

CREATE TABLE pairing_codes (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  code_hash TEXT NOT NULL UNIQUE CHECK (length(code_hash) = 64),
  device_kind TEXT NOT NULL CHECK (device_kind IN ('phone', 'desktop')),
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  cancelled_at TEXT
);

CREATE TABLE sign_in_failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL,
  method TEXT NOT NULL,
  at TEXT NOT NULL
);

CREATE TABLE bot_bindings (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  bot TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  bot_chat_session_id TEXT,
  bound_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, bot)
);

CREATE TABLE room_bindings (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  room_id TEXT NOT NULL,
  name TEXT NOT NULL,
  bound_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, room_id)
);

CREATE TABLE task_assignments (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task TEXT NOT NULL,
  bot TEXT NOT NULL,
  assigned_by TEXT NOT NULL REFERENCES principals(id),
  assigned_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, task)
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  created_by TEXT NOT NULL REFERENCES principals(id),
  created_at TEXT NOT NULL
);

CREATE TABLE artifact_revisions (
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
  media_type TEXT NOT NULL,
  body BLOB NOT NULL,
  created_by TEXT NOT NULL REFERENCES principals(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (artifact_id, revision)
);
{_frozen("artifact_revisions")}

CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  kind TEXT NOT NULL CHECK (kind IN {_in(KINDS)}),
  status TEXT NOT NULL CHECK (status IN {_in(STATUSES)}),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  attention_revision INTEGER NOT NULL CHECK (attention_revision >= 1),
  title TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  artifact_revision INTEGER NOT NULL,
  artifact_sha256 TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
  recommendation TEXT NOT NULL,
  scope_of_yes TEXT NOT NULL,
  required_actor TEXT NOT NULL CHECK (required_actor = 'patrick'),
  risk TEXT NOT NULL CHECK (risk IN ('normal', 'high')),
  urgent INTEGER NOT NULL DEFAULT 0 CHECK (urgent IN (0, 1)),
  requested_by TEXT NOT NULL REFERENCES principals(id),
  task TEXT,
  source_key TEXT,
  expires_at TEXT NOT NULL,
  superseded_by TEXT REFERENCES decisions(id),
  bound_build TEXT,
  consumed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (artifact_id, artifact_revision) REFERENCES artifact_revisions(artifact_id, revision)
);
CREATE INDEX decisions_pending ON decisions(workspace_id, status, updated_at);

CREATE TABLE decision_requests (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  source_key TEXT NOT NULL,
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  first_at TEXT NOT NULL,
  last_at TEXT NOT NULL,
  count INTEGER NOT NULL CHECK (count >= 1),
  PRIMARY KEY (workspace_id, source_key)
);

CREATE TABLE decision_options (
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  revision INTEGER NOT NULL,
  key TEXT NOT NULL,
  label TEXT NOT NULL,
  consequence TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (decision_id, revision, key)
);
{_frozen("decision_options")}

CREATE TABLE task_links (
  id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  kind TEXT NOT NULL CHECK (kind IN ('issue', 'pr', 'kanban', 'room', 'report_page')),
  ref TEXT NOT NULL
);

CREATE TABLE responses (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  revision INTEGER NOT NULL,
  attention_revision INTEGER NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  option_key TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  answered_at TEXT NOT NULL,
  UNIQUE (decision_id, revision),
  FOREIGN KEY (decision_id, revision, option_key) REFERENCES decision_options(decision_id, revision, key)
);
{_frozen("responses")}

CREATE TABLE commands (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
  op TEXT NOT NULL,
  body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, principal_id, idempotency_key)
);

CREATE TABLE events (
  seq INTEGER PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  actor TEXT NOT NULL,
  body_json TEXT NOT NULL,
  at TEXT NOT NULL,
  prev_hash TEXT NOT NULL CHECK (length(prev_hash) = 64),
  hash TEXT NOT NULL UNIQUE CHECK (length(hash) = 64)
);
{_frozen("events")}

CREATE TABLE push_subscriptions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT NOT NULL REFERENCES devices(id),
  sealed BLOB NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  retired_reason TEXT
);

CREATE TABLE push_outbox (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  attention_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN {_in(OUTBOX_STATES)}),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT,
  UNIQUE (decision_id, attention_revision)
);

CREATE TABLE connector_outbox (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  decision_id TEXT REFERENCES decisions(id),
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  body_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN {_in(OUTBOX_STATES)}),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT
);

CREATE TABLE effect_receipts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  outbox TEXT NOT NULL CHECK (outbox IN ('push', 'connector')),
  outbox_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('confirmed', 'failed', 'uncertain')),
  detail_json TEXT NOT NULL,
  at TEXT NOT NULL
);
{_frozen("effect_receipts")}

CREATE TABLE backup_runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('backup', 'restore')),
  path TEXT NOT NULL,
  sha256 TEXT,
  ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
  detail TEXT NOT NULL,
  at TEXT NOT NULL
);
""",
    # 2: a Desktop device remembers which Hermes sign-in paired it (07 section 5.1)
    """
ALTER TABLE devices ADD COLUMN hermes_principal TEXT;
""",
    # 3: passkeys, their one-time challenges, and the read-only feeds for the
    # phone's Work and Room views (07 sections 4 and 5.1, step 3)
    """
CREATE TABLE passkeys (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  credential_id TEXT NOT NULL UNIQUE,
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);

CREATE TABLE webauthn_challenges (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  purpose TEXT NOT NULL CHECK (purpose IN ('register', 'sign_in')),
  challenge_hash TEXT NOT NULL UNIQUE CHECK (length(challenge_hash) = 64),
  session_hash TEXT CHECK (session_hash IS NULL OR length(session_hash) = 64),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  CHECK ((purpose = 'register') = (session_hash IS NOT NULL))
);

CREATE TABLE feed_sources (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room')),
  token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);

CREATE TABLE feed_snapshots (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room')),
  body TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
""",
    # 4: chat from the phone (07 step 4). The feeds gain the Bot Chats'
    # snapshot ("bots"), and the connector, which runs on the VM as the Bots'
    # user, gets a token of its own ("connector"). SQLite cannot change a
    # CHECK, so the two feed tables are rebuilt with their rows kept.
    f"""
CREATE TABLE feed_sources_4 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots', 'connector')),
  token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_sources_4 SELECT workspace_id, name, token_hash, created_at FROM feed_sources;
DROP TABLE feed_sources;
ALTER TABLE feed_sources_4 RENAME TO feed_sources;

CREATE TABLE feed_snapshots_4 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots')),
  body TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_snapshots_4 SELECT workspace_id, name, body, sha256, source_time, received_at FROM feed_snapshots;
DROP TABLE feed_snapshots;
ALTER TABLE feed_snapshots_4 RENAME TO feed_snapshots;

CREATE TABLE chat_messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  target TEXT NOT NULL,
  text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 8000),
  send_key TEXT NOT NULL UNIQUE,
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  status TEXT NOT NULL CHECK (status IN {_in(OUTBOX_STATES)}),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT
);
CREATE INDEX chat_messages_by_target ON chat_messages(workspace_id, target, created_at);
""",
    # 5: Web Push (07 section 8, step 5). The push worker gets a token of its
    # own ("push"), says its two public keys once (the VAPID key the phone
    # subscribes with, and the X25519 key the core seals subscriptions to),
    # and a phone's "no" to alerts is kept so it is never asked again in a loop.
    """
CREATE TABLE feed_sources_5 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots', 'connector', 'push')),
  token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_sources_5 SELECT workspace_id, name, token_hash, created_at FROM feed_sources;
DROP TABLE feed_sources;
ALTER TABLE feed_sources_5 RENAME TO feed_sources;

CREATE TABLE push_keys (
  workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
  vapid_public TEXT NOT NULL CHECK (length(vapid_public) = 87),
  seal_public BLOB NOT NULL CHECK (length(seal_public) = 32),
  set_at TEXT NOT NULL
);

CREATE TABLE push_consent (
  device_id TEXT PRIMARY KEY REFERENCES devices(id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  state TEXT NOT NULL CHECK (state IN ('granted', 'denied')),
  at TEXT NOT NULL
);
""",
    # 6: a subscription keeps the SHA-256 of its endpoint, never the endpoint
    # itself, which stays sealed to the push worker. The app compares it with
    # the browser's own subscription each time it opens: installing the app
    # can give the browser a new one, and the one the core has then answers 410
    # (Patrick's phone test, 1 Nov).
    """
ALTER TABLE push_subscriptions ADD COLUMN endpoint_sha256 TEXT
  CHECK (endpoint_sha256 IS NULL OR length(endpoint_sha256) = 64);
""",
    # 7: chat with Patrick's own Hermes (Patrick, 1 Nov): my-hermes on the VM
    # ("me") and his Hermes on One ("one"). A flag of its own, off in every
    # workspace; a token for each gateway's connector ("own-me", "own-one"),
    # which also sends that Hermes's chat list ("me", "one"); his messages to
    # them in a table of their own, so the Bots' connector can never lease one;
    # and when each gateway's connector was last heard from, whether that
    # Hermes's gateway was running then, and the chats the phone asked to read. A message with no session_id starts a new chat,
    # and the id Hermes gave that chat comes back as made_session_id. SQLite
    # cannot change a CHECK, so three tables are rebuilt with their rows kept.
    """
CREATE TABLE flags_7 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('decision_write', 'mobile_read', 'mobile_chat_write', 'push_send',
                                     'connector_effects', 'own_chat_write')),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  changed_at TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO flags_7 SELECT workspace_id, name, enabled, changed_at, changed_by FROM flags;
DROP TABLE flags;
ALTER TABLE flags_7 RENAME TO flags;
INSERT INTO flags(workspace_id, name, enabled, changed_at, changed_by)
  SELECT id, 'own_chat_write', 0, created_at, 'migration 7' FROM workspaces;

CREATE TABLE feed_sources_7 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots', 'connector', 'push', 'own-me', 'own-one')),
  token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_sources_7 SELECT workspace_id, name, token_hash, created_at FROM feed_sources;
DROP TABLE feed_sources;
ALTER TABLE feed_sources_7 RENAME TO feed_sources;

CREATE TABLE feed_snapshots_7 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots', 'me', 'one')),
  body TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_snapshots_7 SELECT workspace_id, name, body, sha256, source_time, received_at FROM feed_snapshots;
DROP TABLE feed_snapshots;
ALTER TABLE feed_snapshots_7 RENAME TO feed_snapshots;

CREATE TABLE own_messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  gateway TEXT NOT NULL CHECK (gateway IN ('me', 'one')),
  session_id TEXT CHECK (session_id IS NULL OR length(session_id) BETWEEN 1 AND 128),
  made_session_id TEXT CHECK (made_session_id IS NULL OR length(made_session_id) BETWEEN 1 AND 128),
  text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 8000),
  send_key TEXT NOT NULL UNIQUE,
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'sent', 'uncertain', 'cancelled', 'dead', 'quarantined')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reason TEXT,
  CHECK (made_session_id IS NULL OR session_id IS NULL)
);
CREATE INDEX own_messages_by_gateway ON own_messages(workspace_id, gateway, status, created_at);

CREATE TABLE own_gateways (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  gateway TEXT NOT NULL CHECK (gateway IN ('me', 'one')),
  last_seen TEXT NOT NULL,
  ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
  why TEXT CHECK (why IS NULL OR length(why) <= 200),
  wanted_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (workspace_id, gateway)
);
""",
    # 8: which of its states a gateway is in, in the phone's own words (Patrick, 1 Nov, plan Part 1).
    # `state` is what the connector said of Hermes: ready, its gateway not running, or the check
    # itself failing. `peer_*` is the tailnet's own view of the machine the connector posts from
    # (One), written by `tw-decide peers` from `tailscale status --json`, so "One is off" comes from
    # Tailscale and never from the connector going quiet.
    """
ALTER TABLE own_gateways ADD COLUMN state TEXT CHECK (state IS NULL OR state IN ('ready', 'gateway_down', 'check_failed'));
ALTER TABLE own_gateways ADD COLUMN peer_ip TEXT CHECK (peer_ip IS NULL OR length(peer_ip) BETWEEN 7 AND 45);
ALTER TABLE own_gateways ADD COLUMN peer_online INTEGER CHECK (peer_online IS NULL OR peer_online IN (0, 1));
ALTER TABLE own_gateways ADD COLUMN peer_seen TEXT;
ALTER TABLE own_gateways ADD COLUMN peer_checked TEXT;
""",
    # 9: Patrick's reaction to a message in a chat with his own Hermes or a Bot's Bot Chat (redesign
    # step 2, plan Part 11). The chat is a session id on `me` and `one`, and the Bot's profile id on
    # `bots`, where each Bot has one Bot Chat.
    # One row per message: the reaction he wants there now (emoji NULL takes his off), which the
    # connector sets in Hermes with Hermes's own store. Hermes keeps one reaction per person per
    # message, so a row says what should be there, never a change to make: setting it twice is the
    # same as once, and a lease that ran out is simply handed out again. `version` tells a
    # connector's late answer from the newest wish.
    """
CREATE TABLE chat_reactions (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  gateway TEXT NOT NULL CHECK (gateway IN ('me', 'one', 'bots')),
  chat TEXT NOT NULL CHECK (length(chat) BETWEEN 1 AND 128),
  message_id INTEGER NOT NULL CHECK (message_id >= 1),
  emoji TEXT CHECK (emoji IS NULL OR length(emoji) BETWEEN 1 AND 16),
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'set', 'failed')),
  lease_until TEXT,
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 200),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, gateway, chat, message_id)
);
CREATE INDEX chat_reactions_by_status ON chat_reactions(workspace_id, gateway, status, updated_at);
""",
    # 10: renaming, hiding (archive), bringing back and deleting one of Patrick's own chats on Hermes or
    # One (redesign step 3, plan Part 11). A flag of its own, off in every workspace until Patrick turns
    # it on. Each request is one row, done by that gateway's own connector with Hermes's own calls, one
    # at a time in each chat. Each ends in the same state however often it runs (that title, hidden or
    # not, gone), so a lease that ran out is handed out again. The new title is kept here because the
    # connector needs it; it is never in the event chain.
    f"""
CREATE TABLE flags_10 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN {_in(FLAGS_7 + ("session_manage_write",))}),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  changed_at TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO flags_10 SELECT workspace_id, name, enabled, changed_at, changed_by FROM flags;
DROP TABLE flags;
ALTER TABLE flags_10 RENAME TO flags;
INSERT INTO flags(workspace_id, name, enabled, changed_at, changed_by)
  SELECT id, 'session_manage_write', 0, created_at, 'migration 10' FROM workspaces;

CREATE TABLE own_session_ops (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  gateway TEXT NOT NULL CHECK (gateway IN ('me', 'one')),
  session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
  op TEXT NOT NULL CHECK (op IN ('rename', 'archive', 'unarchive', 'delete')),
  title TEXT CHECK ((op = 'rename') = (title IS NOT NULL) AND (title IS NULL OR length(title) BETWEEN 1 AND 100)),
  status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'done', 'failed')),
  lease_until TEXT,
  reason TEXT CHECK (reason IS NULL OR length(reason) <= 200),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  device_id TEXT REFERENCES devices(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX own_session_ops_by_status ON own_session_ops(workspace_id, gateway, status, created_at);
""",
    # 11: a "Bot told" message that died only because the Bots' gateway was not ready goes back in the
    # queue. Until this release the connector called a gateway that was not ready (starting, stopped,
    # reloaded by an install, or its check could not run) a failure, so the message was dead after one
    # try and the Bot that asked was never told Patrick's answer (1 Nov 2026, while an install reloaded
    # the gateway). Nothing was sent in that case, so sending it now is its first delivery. Only rows
    # whose one reason is a readiness check's and whose every receipt is that failure; the failed
    # receipt stays as history. Chat messages are left as they are: Patrick saw those as not sent and
    # may have sent them again himself.
    _REQUEUE_NOT_READY,
    # 12: the same rule once more, for the answer migration 11 put back on 1 Nov at 10:53 PM. The
    # install then ran this release's migrations while the release before's connector was still
    # running; three seconds later that old connector leased the message, met the gateway mid-reload
    # and failed it the old way, so it was dead again with a second failed receipt. Nothing was sent
    # either time. The install now stops every worker before the store migrates (install.sh step 6),
    # so this cannot happen again; this puts back what it did. Same rows only: dead of a readiness
    # check, every receipt that failure, the card still answered.
    _REQUEUE_NOT_READY,
    # 13: a message to his own Hermes that answers one message there (redesign step 4, reply style A):
    # Hermes's own id of the message it answers. The quoted words are in the text itself, as sent.
    """
ALTER TABLE own_messages ADD COLUMN reply_to INTEGER;
""",
]


# The way back from each migration from 6 on: what must still be empty for the
# step back to lose nothing (a query that counts it, and what it is), the
# statements that undo the migration, and what the operator must know after it
# (or ""). store.downgrade runs them, newest first, only when every count is 0.
# Migrations 1 to 5 were never put back.
DOWNS: dict[int, tuple[list[tuple[str, str]], str, str]] = {
    6: ([("SELECT count(*) FROM push_subscriptions WHERE endpoint_sha256 IS NOT NULL",
          "phone alerts that keep their address's SHA-256")],
        """
ALTER TABLE push_subscriptions DROP COLUMN endpoint_sha256;
""", ""),
    7: ([("SELECT count(*) FROM own_messages", "messages to Patrick's own Hermes"),
         ("SELECT count(*) FROM flags WHERE name = 'own_chat_write' AND enabled = 1",
          "workspaces with own_chat_write on")],
        f"""
DROP TABLE own_gateways;
DROP TABLE own_messages;

CREATE TABLE flags_6 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN {_in(FLAGS_1)}),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  changed_at TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO flags_6 SELECT workspace_id, name, enabled, changed_at, changed_by FROM flags
  WHERE name <> 'own_chat_write';
DROP TABLE flags;
ALTER TABLE flags_6 RENAME TO flags;

CREATE TABLE feed_sources_6 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots', 'connector', 'push')),
  token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_sources_6 SELECT workspace_id, name, token_hash, created_at FROM feed_sources
  WHERE name NOT IN ('own-me', 'own-one');
DROP TABLE feed_sources;
ALTER TABLE feed_sources_6 RENAME TO feed_sources;

CREATE TABLE feed_snapshots_6 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('work', 'room', 'bots')),
  body TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO feed_snapshots_6 SELECT workspace_id, name, body, sha256, source_time, received_at FROM feed_snapshots
  WHERE name IN ('work', 'room', 'bots');
DROP TABLE feed_snapshots;
ALTER TABLE feed_snapshots_6 RENAME TO feed_snapshots;
""", "The tokens of the connectors for Patrick's own Hermes are gone with schema 7. The next install of a newer "
         "release makes my-hermes's again by itself; on One, run scripts\\one\\Install-OwnConnector.ps1 -NewToken."),
    # Only status lines: each connector writes its state again within 20 seconds, and `tw-decide peers`
    # the tailnet's view within a minute, so nothing is lost by dropping them.
    8: ([], """
ALTER TABLE own_gateways DROP COLUMN peer_checked;
ALTER TABLE own_gateways DROP COLUMN peer_seen;
ALTER TABLE own_gateways DROP COLUMN peer_online;
ALTER TABLE own_gateways DROP COLUMN peer_ip;
ALTER TABLE own_gateways DROP COLUMN state;
""", ""),
    # A reaction already set lives in Hermes itself; only one still on its way would be lost.
    9: ([("SELECT count(*) FROM chat_reactions WHERE status IN ('queued', 'leased')",
          "reactions on their way to Hermes")], """
DROP TABLE chat_reactions;
""", ""),
    # A request already done lives in Hermes itself; only one still on its way would be lost.
    10: ([("SELECT count(*) FROM own_session_ops WHERE status IN ('queued', 'leased')",
           "chat renames, hides and deletes on their way to Hermes"),
          ("SELECT count(*) FROM flags WHERE name = 'session_manage_write' AND enabled = 1",
           "workspaces with session_manage_write on")], f"""
DROP TABLE own_session_ops;

CREATE TABLE flags_9 (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL CHECK (name IN ('decision_write', 'mobile_read', 'mobile_chat_write', 'push_send',
                                     'connector_effects', 'own_chat_write')),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  changed_at TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
INSERT INTO flags_9 SELECT workspace_id, name, enabled, changed_at, changed_by FROM flags
  WHERE name <> 'session_manage_write';
DROP TABLE flags;
ALTER TABLE flags_9 RENAME TO flags;
""", ""),
    # A data step: no table changed, and a message it put back in the queue is one the older release
    # sends the same way. Nothing to undo.
    11: ([], "", ""),
    # The same data step as 11.
    12: ([], "", ""),
    # The quoted words stay in each message's text; only the link back to what it answers would go.
    13: ([("SELECT count(*) FROM own_messages WHERE reply_to IS NOT NULL",
           "messages to his own Hermes that answer one message, with the link back to it")], """
ALTER TABLE own_messages DROP COLUMN reply_to;
""", ""),
}
