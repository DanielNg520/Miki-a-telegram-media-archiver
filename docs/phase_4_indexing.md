# Phase 4: Post Indexing

Phase 4 creates Miki's compact searchable library. Telegram continues to store the media; Miki
stores only identifiers, source metadata, a short caption preview, and normalized search tokens.

## Indexed Post Model

Each Telegram media message stores:

- Source chat, topic, and message IDs
- Media type
- Telegram `media_group_id`, when present
- Logical post key
- Message timestamp
- Sender ID and bot status
- Source kind: user Telegram post, Miki copy, or external bot
- Extractor version
- Availability state
- Caption preview, limited to 500 characters

The unique physical identity is `source_chat_id + source_message_id`. Reprocessing the same message
updates its existing record.

Album members retain separate message IDs so Telegram can copy each item later. They share one
logical post key derived from the chat and `media_group_id`, allowing retrieval to treat the album
as one result and preserve its member order.

## Token Extraction

Extractor version 1 retains:

- Hashtags
- ALL-CAPS identifiers with at least two letters
- Mixed letter-number codes such as `RX7`, `A320`, and `FC-2`
- Capitalized words unless capitalization is explained only by sentence position
- Exact configured keywords
- Exact configured multiword phrases

Values use Unicode case-folding and are deduplicated by post, token kind, and normalized value.
The extractor is deterministic and does not call an AI model.

## New and Edited Messages

Miki indexes media only in active registered topics of `ARCHIVE_CHAT_ID`. New messages and edited
messages use the same atomic upsert operation. Editing a caption deletes the previous token set and
inserts the newly extracted set in one database transaction, preventing stale keywords.

Successful copies made by the existing sorter are indexed directly using Telegram's returned
destination message ID. This is necessary because a bot should not rely on receiving a new update
for a message it sent itself.

## Duplicate and Loop Metadata

Duplicate updates cannot create duplicate post rows because physical Telegram identity is unique.
Posts created by Miki are marked `miki_copy`; messages from another bot are marked `external_bot`.
Later routing phases can use this source metadata to prevent recursive copying without excluding
bot-created records from the searchable library.

## Controlled Reindexing

Configured Miki administrators may run:

```text
/reindex [batch_size]
```

The default batch size is 100 and the allowed range is 1-1000. Only posts with an older extractor
version are selected. Reindexing uses the stored caption preview, so content beyond 500 characters
cannot be recovered during a future rebuild unless it was represented by an already stored token.
This is the deliberate storage tradeoff chosen for Miki's compact library.
