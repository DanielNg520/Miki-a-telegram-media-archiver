# Completed Work

## Backup Media to Second Group (Hashtag-based)
**Implemented in `miki_sorter_bot/sorting.py`**

Miki now automatically backups incoming media from the source topic to a secondary group (`-1004365154840`) based on specific hashtags in the caption.

### Features
* **Hashtag Routing**: 
  * Captions containing `#JAV` (case-insensitive) are copied to topic ID `2`.
  * Captions containing `#Asian` (case-insensitive) are copied to topic ID `3`.
  * If both are present, `#JAV` takes precedence.
* **Album Support**: Intelligently handles both single media and assembled media groups (albums). If a single media item in an album has the target hashtag, the entire album is successfully routed as a cohesive media group.
* **Non-intrusive Hook**: The logic executes in parallel with the standard `archive_chat_id` routing, meaning normal sorting continues uninterrupted (including conflict handling, direct route mappings, and fallback/lookback mechanisms).

### Implementation Details
1. **`_backup_to_second_group` method added to `Sorting` class**:
   * Uses Telegram's `copy_message` API for single media.
   * Uses Telegram's `copy_messages` API for albums, maintaining their visual grouping.
   * Leverages existing `_album_text(messages)` helper to scan the complete concatenated text of all media in the group.
2. **Execution Hooks**:
   * Hooked into `_deliver_album_messages`: Ensures all assembled albums, delayed albums, and lookback recoveries are caught exactly once.
   * Hooked into `handle_update`: Placed strategically in the three fast-path branches (`direct_decision is not None`, `decision.status == "conflict"`, and standard `decision`) to ensure single-message posts bypass the album builder but still get backed up.

---

# Future Plans (In Queue)

- **Auto-Rotate Source Topic**: Allow Miki to automatically create a new forum topic, switch her `source_thread_id` to listen to it, and lock/close the old source topic.
- **Auto-Delete Closed Topics**: Track when topics are closed and use a background task to permanently delete them after a configurable number of days.
- **Forwarded Media Sender Muting**: Detect forwarded media in the source topic where the original sender's account is visible (not hidden by privacy settings). Automatically mute the person who forwarded it for a specified duration (and optionally delete the media).
