# Testing Guide for New Features

This document provides test scenarios for the newly implemented features.

## Prerequisites

1. Authenticate with `uvx mcp-google-gmail@latest auth`
2. Start the server: `uvx mcp-google-gmail@latest`
3. Connect your MCP client (Claude Desktop, etc.)

## Feature 1: Batch Operations

### Test 1.1: Batch Delete Messages
```
Prompt: "Find 3 spam messages and delete them permanently using batch delete"
Expected: Uses gmail_search_messages to find spam, then gmail_batch_delete_messages
```

### Test 1.2: Batch Modify Messages
```
Prompt: "Find 5 unread promotional emails and archive them all at once"
Expected: Uses gmail_search_messages with query "is:unread category:promotions", 
         then gmail_batch_modify_messages with remove_label_ids=["INBOX", "UNREAD"]
```

### Test 1.3: Batch Star Messages
```
Prompt: "Star all emails from alice@example.com from the last week"
Expected: Uses gmail_search_messages with "from:alice@example.com after:YYYY/MM/DD",
         then gmail_batch_modify_messages with add_label_ids=["STARRED"]
```

## Feature 2: Thread Operations

### Test 2.1: List Threads
```
Prompt: "Show me all unread conversation threads"
Expected: Uses gmail_list_threads with query="is:unread"
Returns: List of threads with message_count for each
```

### Test 2.2: Get Thread (already existed)
```
Prompt: "Show me the full conversation for thread ID <thread_id>"
Expected: Uses gmail_get_thread with the thread_id
Returns: All messages in chronological order with cleaned text
```

### Test 2.3: Modify Thread Labels
```
Prompt: "Archive the entire conversation with thread ID <thread_id>"
Expected: Uses gmail_modify_thread_labels with remove_label_ids=["INBOX"]
```

### Test 2.4: Trash Thread
```
Prompt: "Move the entire thread <thread_id> to trash"
Expected: Uses gmail_trash_thread
```

### Test 2.5: Untrash Thread
```
Prompt: "Restore thread <thread_id> from trash"
Expected: Uses gmail_untrash_thread
```

## Feature 3: Filter Management

### Test 3.1: List Filters
```
Prompt: "Show me all my Gmail filters"
Expected: Uses gmail_list_filters
Returns: List of filters with their criteria and actions
```

### Test 3.2: Get Filter
```
Prompt: "Show me the details of filter <filter_id>"
Expected: Uses gmail_get_filter
```

### Test 3.3: Create Filter - Auto Archive
```
Prompt: "Create a filter to automatically archive all emails from newsletter@example.com"
Expected: Uses gmail_create_filter with:
  - from_email="newsletter@example.com"
  - archive=True
```

### Test 3.4: Create Filter - Label and Star
```
Prompt: "Create a filter to label all emails from github.com as 'GitHub' and star them"
Expected: Uses gmail_create_filter with:
  - from_email="github.com"
  - add_label_ids=["<GitHub_label_id>"] (may need to create label first)
  - star=True
```

### Test 3.5: Create Filter - Forward
```
Prompt: "Create a filter to forward all emails with subject containing 'URGENT' to mobile@example.com"
Expected: Uses gmail_create_filter with:
  - subject="URGENT"
  - forward_to="mobile@example.com"
```

### Test 3.6: Create Filter - Complex Query
```
Prompt: "Create a filter for emails larger than 5MB with attachments, and label them as 'Large Attachments'"
Expected: Uses gmail_create_filter with:
  - query="has:attachment larger:5M"
  - add_label_ids=["<LargeAttachments_label_id>"]
```

### Test 3.7: Delete Filter
```
Prompt: "Delete filter <filter_id>"
Expected: Uses gmail_delete_filter
```

## Multi-Account Testing

All tools should work with multi-account setups:

```
Prompt: "List all threads in my work account"
Expected: Uses gmail_list_threads with account="work"

Prompt: "Create a filter in my personal account to archive newsletters"
Expected: Uses gmail_create_filter with account="personal"
```

## Error Handling Tests

### Test: Batch operations with too many IDs
```
Prompt: "Try to batch delete 1500 messages"
Expected: Returns error: "Cannot delete more than 1000 messages at once"
```

### Test: Filter with no criteria
```
Prompt: "Create a filter with no matching criteria"
Expected: Returns error: "At least one criteria field must be provided"
```

### Test: Filter with no actions
```
Prompt: "Create a filter that matches 'test' but has no actions"
Expected: Returns error: "At least one action must be specified"
```

## Integration Tests

### Test: End-to-End Workflow
```
Scenario: Clean up inbox using new features
1. List all threads with label "Promotions"
2. Archive them using batch modify on all message IDs
3. Create a filter to auto-archive future promotional emails
4. Verify the filter was created by listing all filters
```

## Notes

- All tools accept optional `account` parameter for multi-account setups
- Batch operations have a hard limit of 1000 items per call (Gmail API limitation)
- Filter creation requires at least one criteria AND one action
- Thread operations work on entire conversations, not individual messages
