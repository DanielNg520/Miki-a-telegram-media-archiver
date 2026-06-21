# Phase 3: Topic and Route Management

Phase 3 provides durable, authorized Telegram commands for registering forum topics and managing
hashtag, keyword, and phrase routes.

## Topic Registry

A topic is identified by `chat_id + message_thread_id`. Register it by running this command inside
the destination topic:

```text
/topic_register <unique display name>
```

Registration verifies that:

- The command is inside a supergroup topic.
- The supergroup has forum mode enabled.
- Miki is currently an administrator in the forum.
- The display name is unique within that chat.

Re-registering the same topic updates its display name and reactivates it. List active topics with:

```text
/topic_list
```

Miki watches Telegram topic service updates. Closing a registered topic deactivates it, reopening
reactivates it, and renaming it updates the stored display name when that name remains unique.
Mappings cannot be added to an inactive topic.

## Route Commands

Hashtag routes:

```text
/hashtag_add <topic_id> <hashtag>
/hashtag_replace <topic_id> <hashtag>
/hashtag_remove <topic_id> <hashtag>
/hashtag_list [topic_id]
```

Keyword and phrase routes:

```text
/keyword_add <topic_id> <keyword or quoted phrase>
/keyword_replace <topic_id> <keyword or quoted phrase>
/keyword_remove <topic_id> <keyword or quoted phrase>
/keyword_list [topic_id]
/keyword_find <keyword or quoted phrase>
```

Single-word values are keywords. Multiword values are phrases. Hashtags may be entered with or
without `#`. Values use Unicode case-folding, whitespace normalization, whole-keyword matching, and whitespace-separated phrase
identity.

Adding an existing mapping to its current topic is idempotent. Adding it to another topic is
rejected. The corresponding `replace` command explicitly moves it. Conflicts are scoped to one
chat, so separate supergroups can independently use the same normalized value.

## Authorization

`ADMIN_USER_IDS` contains the Telegram user IDs with full Miki administration rights. Only these
administrators can delegate or revoke route-manager access:

```text
/manager_add <user_id>
/manager_remove <user_id>
```

Delegated route managers are scoped to one chat. They may register topics and manage routes, but
they cannot delegate access to other users.

## Connection to Later Phases

Phase 4 will read these registered topics and normalized mappings while indexing posts. Phase 5
will use them for routing decisions. Phase 3 deliberately does not change the existing static
sorter yet, preventing a partially implemented route engine from affecting live message delivery.
