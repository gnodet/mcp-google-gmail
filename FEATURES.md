# Gmail MCP Server - New Features

This document describes the newly implemented batch operations, thread operations, and filter management features.

## Feature 1: Batch Operations

### `gmail_batch_delete_messages`
Permanently delete up to 1000 messages at once.

**Example:**
```python
gmail_batch_delete_messages(
    message_ids=["msg1", "msg2", "msg3"],
    account="work"  # optional
)
# Returns: {"success": True, "deleted_count": 3}
```

**Notes:**
- Destructive operation (cannot be undone)
- Messages are permanently deleted, not moved to trash
- Maximum 1000 messages per call

### `gmail_batch_modify_messages`
Add or remove labels on up to 1000 messages at once.

**Example:**
```python
# Archive and mark as read
gmail_batch_modify_messages(
    message_ids=["msg1", "msg2", "msg3"],
    remove_label_ids=["INBOX", "UNREAD"],
    account="personal"  # optional
)
# Returns: {"success": True, "modified_count": 3}

# Star messages
gmail_batch_modify_messages(
    message_ids=["msg1", "msg2"],
    add_label_ids=["STARRED"]
)
```

**Notes:**
- More efficient than individual `modify_message_labels` calls
- Maximum 1000 messages per call
- Must provide at least one of `add_label_ids` or `remove_label_ids`

---

## Feature 2: Thread Operations

### `gmail_list_threads`
List email threads (conversations) with filtering.

**Example:**
```python
gmail_list_threads(
    query="is:unread",
    max_results=50,
    account="work"  # optional
)
# Returns: {
#   "threads": [
#     {
#       "thread_id": "thread123",
#       "snippet": "...",
#       "subject": "Project Update",
#       "from": "alice@example.com",
#       "date": "Mon, 01 Jun 2026 10:00:00",
#       "message_count": 5
#     },
#     ...
#   ],
#   "next_page_token": "...",
#   "result_size_estimate": 150
# }
```

### `gmail_get_thread`
Get full thread with all messages (already existed, enhanced with pagination).

**Example:**
```python
# Get entire thread
gmail_get_thread(thread_id="thread123")

# Paginate long threads
gmail_get_thread(thread_id="thread123", offset=0, limit=25)
```

### `gmail_modify_thread_labels`
Add or remove labels on all messages in a thread.

**Example:**
```python
# Archive entire conversation
gmail_modify_thread_labels(
    thread_id="thread123",
    remove_label_ids=["INBOX"]
)

# Star and mark as important
gmail_modify_thread_labels(
    thread_id="thread123",
    add_label_ids=["STARRED", "IMPORTANT"]
)
```

### `gmail_trash_thread`
Move entire thread to trash.

**Example:**
```python
gmail_trash_thread(thread_id="thread123")
# Returns: {"thread_id": "thread123", "messages": 5}
```

### `gmail_untrash_thread`
Restore entire thread from trash.

**Example:**
```python
gmail_untrash_thread(thread_id="thread123")
# Returns: {"thread_id": "thread123", "messages": 5}
```

---

## Feature 3: Filter Management

### `gmail_list_filters`
List all mail filters.

**Example:**
```python
gmail_list_filters()
# Returns: {
#   "filters": [
#     {
#       "id": "filter123",
#       "criteria": {"from": "notifications@github.com"},
#       "action": {"addLabelIds": ["Label_Github"], "removeLabelIds": ["INBOX"]}
#     },
#     ...
#   ],
#   "count": 15
# }
```

### `gmail_get_filter`
Get specific filter by ID.

**Example:**
```python
gmail_get_filter(filter_id="filter123")
```

### `gmail_create_filter`
Create a new mail filter with criteria and actions.

**Example 1: Auto-archive newsletters**
```python
gmail_create_filter(
    from_email="newsletter@example.com",
    archive=True,
    mark_as_read=True
)
```

**Example 2: Label all emails from a domain**
```python
gmail_create_filter(
    from_email="example.com",
    add_label_ids=["Label_ExampleCom"]
)
```

**Example 3: Forward important emails**
```python
gmail_create_filter(
    subject="URGENT",
    forward_to="mobile@example.com",
    star=True
)
```

**Example 4: Auto-delete spam from specific sender**
```python
gmail_create_filter(
    from_email="spam@badactor.com",
    trash=True
)
```

**Example 5: Complex query-based filter**
```python
gmail_create_filter(
    query="has:attachment larger:5M",
    add_label_ids=["Label_LargeAttachments"],
    mark_as_important=True
)
```

**Available Criteria:**
- `from_email`: Sender email/domain
- `to_email`: Recipient email/domain
- `subject`: Subject line text
- `query`: Gmail search query
- `negated_query`: NOT matching this query
- `has_attachment`: True to match only messages with attachments
- `exclude_chats`: True to exclude chat messages
- `size`: Size in bytes
- `size_comparison`: "larger" or "smaller" (required with size)

**Available Actions:**
- `add_label_ids`: Apply these labels
- `remove_label_ids`: Remove these labels
- `forward_to`: Forward to email address
- `mark_as_read`: True to mark as read
- `mark_as_important`: True/False to mark importance
- `mark_as_spam`: True to move to spam
- `star`: True to star
- `archive`: True to archive (remove from inbox)
- `trash`: True to move to trash

### `gmail_delete_filter`
Delete a filter by ID.

**Example:**
```python
gmail_delete_filter(filter_id="filter123")
# Returns: {"success": True}
```

---

## Multi-Account Support

All tools support the optional `account` parameter for multi-account setups:

```python
# Use default account
gmail_list_threads(query="is:unread")

# Use specific account
gmail_list_threads(query="is:unread", account="work")
gmail_batch_modify_messages(message_ids=[...], account="personal")
```

Use `gmail_list_accounts()` to see available accounts and their email addresses.
