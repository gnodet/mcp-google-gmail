"""MCP server for Gmail API.

Provides tools for listing, reading, sending, drafting, labeling,
and trashing emails via the Gmail API.

Authentication priority:
    1. GMAIL_CREDENTIALS_CONFIG env var (base64-encoded service account JSON)
    2. GMAIL_SERVICE_ACCOUNT_PATH env var (path to service account JSON file)
    3. GMAIL_TOKEN_PATH env var (path to existing OAuth token.json)
    4. GMAIL_CREDENTIALS_PATH env var (path to OAuth credentials.json, interactive flow)
    5. Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS / gcloud)
"""

import base64
import email
import email.utils
import json
import mimetypes
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email import encoders
from email.mime.audio import MIMEAudio
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import google.auth
import google.auth.transport.requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

CREDENTIALS_CONFIG = os.environ.get("GMAIL_CREDENTIALS_CONFIG")
SERVICE_ACCOUNT_PATH = os.environ.get(
    "GMAIL_SERVICE_ACCOUNT_PATH", "service_account.json"
)
TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "token.json")
CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
ACCOUNTS_CONFIG_PATH = os.environ.get("GMAIL_ACCOUNTS_CONFIG")

_resolved_host = os.environ.get("HOST", os.environ.get("FASTMCP_HOST", "0.0.0.0"))
_resolved_port = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8000")))

# ---------------------------------------------------------------------------
# Lifespan / Auth
# ---------------------------------------------------------------------------


@dataclass
class GmailContext:
    """Context holding authenticated Gmail service(s)."""

    services: dict[str, Any]
    default_account: str
    account_emails: dict[str, str]


def _authenticate_with_paths(
    credentials_config: str | None = None,
    service_account_path: str = "service_account.json",
    token_path: str = "token.json",
    credentials_path: str = "credentials.json",
) -> Any:
    """Build an authenticated Gmail API service using the credential chain."""
    creds = None
    sa_path = Path(service_account_path).expanduser()
    tk_path = Path(token_path).expanduser()
    cr_path = Path(credentials_path).expanduser()

    # 1. Base64-encoded service account from env var
    if credentials_config:
        info = json.loads(base64.b64decode(credentials_config))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )

    # 2. Service account JSON file
    if not creds and sa_path.exists():
        creds = service_account.Credentials.from_service_account_file(
            str(sa_path), scopes=SCOPES
        )

    # 3. Existing OAuth token
    if not creds and tk_path.exists():
        creds = Credentials.from_authorized_user_file(str(tk_path), SCOPES)

    # 4. Refresh or interactive OAuth flow
    if not creds or not creds.valid:
        if (
            creds
            and hasattr(creds, "expired")
            and creds.expired
            and hasattr(creds, "refresh_token")
            and creds.refresh_token
        ):
            creds.refresh(Request())
            tk_path.write_text(creds.to_json())
        elif cr_path.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(cr_path), SCOPES)
            creds = flow.run_local_server(port=0)
            tk_path.write_text(creds.to_json())

    # 5. Application Default Credentials
    if not creds:
        creds, _ = google.auth.default(scopes=SCOPES)

    if not creds:
        raise RuntimeError(
            "All authentication methods failed. Configure credentials via "
            "GMAIL_CREDENTIALS_CONFIG, GMAIL_SERVICE_ACCOUNT_PATH, "
            "GMAIL_TOKEN_PATH, or GMAIL_CREDENTIALS_PATH."
        )

    return build("gmail", "v1", credentials=creds)


def _authenticate() -> Any:
    """Legacy single-account authentication using env vars."""
    return _authenticate_with_paths(
        credentials_config=CREDENTIALS_CONFIG,
        service_account_path=SERVICE_ACCOUNT_PATH,
        token_path=TOKEN_PATH,
        credentials_path=CREDENTIALS_PATH,
    )


def _load_accounts_config() -> tuple[dict[str, Any], str, dict[str, str]]:
    """Load account config and authenticate all accounts.

    Returns (services, default_account, account_emails).
    """
    if not ACCOUNTS_CONFIG_PATH:
        service = _authenticate()
        profile = service.users().getProfile(userId="me").execute()
        email_addr = profile.get("emailAddress", "unknown")
        return {"default": service}, "default", {"default": email_addr}

    config_path = Path(ACCOUNTS_CONFIG_PATH).expanduser()
    if not config_path.exists():
        raise RuntimeError(f"Accounts config not found: {config_path}")

    with open(config_path) as f:
        config = json.load(f)

    accounts = config.get("accounts", {})
    default = config.get("default")
    if not accounts:
        raise RuntimeError(f"No accounts defined in {config_path}")
    if default and default not in accounts:
        raise RuntimeError(
            f"Default account '{default}' not found in accounts: "
            f"{list(accounts.keys())}"
        )
    if not default:
        default = next(iter(accounts))

    services = {}
    emails = {}
    for name, acct in accounts.items():
        svc = _authenticate_with_paths(
            credentials_config=acct.get("credentials_config"),
            service_account_path=acct.get("service_account_path", "service_account.json"),
            token_path=acct.get("token_path", "token.json"),
            credentials_path=acct.get("credentials_path", "credentials.json"),
        )
        services[name] = svc
        profile = svc.users().getProfile(userId="me").execute()
        emails[name] = profile.get("emailAddress", "unknown")

    return services, default, emails


@asynccontextmanager
async def gmail_lifespan(server: FastMCP) -> AsyncIterator[GmailContext]:
    """Authenticate all configured Gmail accounts at startup."""
    services, default_account, account_emails = _load_accounts_config()
    try:
        yield GmailContext(
            services=services,
            default_account=default_account,
            account_emails=account_emails,
        )
    finally:
        pass


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Gmail",
    dependencies=[
        "google-auth",
        "google-auth-oauthlib",
        "google-api-python-client",
    ],
    lifespan=gmail_lifespan,
    host=_resolved_host,
    port=_resolved_port,
)


def _get_service(ctx: Context, account: str | None = None) -> Any:
    """Extract the Gmail service for the given account (or default)."""
    gmail_ctx: GmailContext = ctx.request_context.lifespan_context
    name = account or gmail_ctx.default_account
    if name not in gmail_ctx.services:
        available = list(gmail_ctx.services.keys())
        raise ValueError(
            f"Unknown account '{name}'. Available accounts: {available}"
        )
    return gmail_ctx.services[name]


@mcp.tool()
def gmail_list_accounts(ctx: Context) -> dict:
    """List all configured Gmail accounts.

    Returns account names, email addresses, and which is the default.
    Use the account name as the 'account' parameter in other tools.

    Args:
        ctx: MCP context (injected automatically).
    """
    gmail_ctx: GmailContext = ctx.request_context.lifespan_context
    accounts = []
    for name in gmail_ctx.services:
        accounts.append({
            "name": name,
            "email": gmail_ctx.account_emails.get(name, "unknown"),
            "is_default": name == gmail_ctx.default_account,
        })
    return {"accounts": accounts, "default": gmail_ctx.default_account}


# ---------------------------------------------------------------------------
# Helper: MIME message building
# ---------------------------------------------------------------------------


def _attach_file(message: MIMEMultipart, file_path: str) -> None:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Attachment not found: {file_path}")
    content_type, _ = mimetypes.guess_type(str(path))
    if content_type is None:
        content_type = "application/octet-stream"
    main_type, sub_type = content_type.split("/", 1)
    file_data = path.read_bytes()
    if main_type == "text":
        att = MIMEText(file_data.decode(), _subtype=sub_type)
    elif main_type == "image":
        att = MIMEImage(file_data, _subtype=sub_type)
    elif main_type == "audio":
        att = MIMEAudio(file_data, _subtype=sub_type)
    else:
        att = MIMEBase(main_type, sub_type)
        att.set_payload(file_data)
        encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment", filename=path.name)
    message.attach(att)


def _build_message(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
    attachment_paths: list[str] | None = None,
    reply_to_message_id: str | None = None,
    thread_id: str | None = None,
) -> dict:
    has_attachments = attachment_paths and len(attachment_paths) > 0
    has_html = html_body is not None

    if has_attachments:
        message = MIMEMultipart("mixed")
        if has_html:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain"))
            alt.attach(MIMEText(html_body, "html"))
            message.attach(alt)
        else:
            message.attach(MIMEText(body, "plain"))
        for fp in attachment_paths:
            _attach_file(message, fp)
    elif has_html:
        message = MIMEMultipart("alternative")
        message.attach(MIMEText(body, "plain"))
        message.attach(MIMEText(html_body, "html"))
    else:
        message = MIMEText(body, "plain")

    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    if reply_to_message_id:
        message["In-Reply-To"] = reply_to_message_id
        message["References"] = reply_to_message_id

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = {"raw": encoded}
    if thread_id:
        result["threadId"] = thread_id
    return result


# ---------------------------------------------------------------------------
# Helper: MIME payload parsing
# ---------------------------------------------------------------------------


def _extract_body(payload: dict) -> tuple[str, str]:
    body_text = ""
    body_html = ""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain" and "body" in payload:
        data = payload["body"].get("data", "")
        if data:
            body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime_type == "text/html" and "body" in payload:
        data = payload["body"].get("data", "")
        if data:
            body_html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif "parts" in payload:
        for part in payload["parts"]:
            t, h = _extract_body(part)
            if t and not body_text:
                body_text = t
            if h and not body_html:
                body_html = h
    return body_text, body_html


def _extract_attachments(payload: dict) -> list[dict]:
    attachments = []
    if payload.get("filename"):
        body = payload.get("body", {})
        attachments.append(
            {
                "filename": payload["filename"],
                "mime_type": payload.get("mimeType", ""),
                "size": body.get("size", 0),
                "attachment_id": body.get("attachmentId", ""),
            }
        )
    for part in payload.get("parts", []):
        attachments.extend(_extract_attachments(part))
    return attachments


# ---------------------------------------------------------------------------
# Helper: text cleaning for LLM-friendly output
# ---------------------------------------------------------------------------

# Pattern matching "On <Day>, <Mon> <D>, <YYYY> at <time> <Name> <email> wrote:"
# Gmail often wraps this across 2-3 lines, so we use DOTALL to match across newlines.
_QUOTED_REPLY_PATTERNS = [
    # "On <date> <name> <email> wrote:" (Gmail style, may span multiple lines)
    re.compile(r"\nOn .+?wrote:\s*\n", re.DOTALL),
    # "---------- Forwarded message ----------"
    re.compile(r"^-{5,}\s*Forwarded message\s*-{5,}", re.MULTILINE),
    # "> " quoted lines after a blank line (catches remaining quoted blocks)
    re.compile(r"\n\n(?:>.*\n?){3,}"),
]

# Common email signature markers
_SIGNATURE_PATTERNS = [
    re.compile(r"^--\s*$", re.MULTILINE),  # "-- " standard sig separator
    re.compile(r"^Sent from my (?:iPhone|iPad|Android)", re.MULTILINE),
    re.compile(r"^Get Outlook for", re.MULTILINE),
    # "CONFIDENTIALITY NOTICE" boilerplate
    re.compile(r"^CONFIDENTIALITY NOTICE", re.MULTILINE),
]


def _strip_quoted_reply(text: str) -> str:
    """Remove quoted reply content from an email body, keeping only the new content."""
    if not text:
        return text

    # Find the earliest match of any quoted reply pattern
    earliest_pos = len(text)
    for pattern in _QUOTED_REPLY_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()

    return text[:earliest_pos].rstrip()


def _strip_signature(text: str) -> str:
    """Remove email signatures from the body text."""
    if not text:
        return text

    earliest_pos = len(text)
    for pattern in _SIGNATURE_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()

    return text[:earliest_pos].rstrip()


def _clean_body_text(text: str) -> str:
    """Strip quoted replies, signatures, and excessive whitespace from body text."""
    text = _strip_quoted_reply(text)
    text = _strip_signature(text)
    # Collapse runs of 3+ newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_full_message(msg: dict) -> dict:
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body_text, body_html = _extract_body(msg.get("payload", {}))
    attachment_list = _extract_attachments(msg.get("payload", {}))
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "subject": headers.get("Subject", ""),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "date": headers.get("Date", ""),
        "body_text": body_text,
        "body_html": body_html,
        "labels": msg.get("labelIds", []),
        "attachments": attachment_list,
    }


def _parse_raw_draft(raw_data: str) -> dict:
    if not raw_data:
        return {}
    msg_bytes = base64.urlsafe_b64decode(raw_data)
    msg = email.message_from_bytes(msg_bytes)
    body = ""
    html_body = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not body:
                p = part.get_payload(decode=True)
                if p:
                    body = p.decode("utf-8", errors="replace")
            elif ct == "text/html" and not html_body:
                p = part.get_payload(decode=True)
                if p:
                    html_body = p.decode("utf-8", errors="replace")
    else:
        p = msg.get_payload(decode=True)
        if p:
            if msg.get_content_type() == "text/html":
                html_body = p.decode("utf-8", errors="replace")
            else:
                body = p.decode("utf-8", errors="replace")
    return {
        "to": msg.get("To", ""),
        "subject": msg.get("Subject", ""),
        "cc": msg.get("Cc"),
        "bcc": msg.get("Bcc"),
        "body": body,
        "html_body": html_body,
    }


# ===========================================================================
# Tools
# ===========================================================================


@mcp.tool()
def gmail_list_messages(
    ctx: Context,
    query: str | None = None,
    label_ids: list[str] | None = None,
    max_results: int = 20,
    page_token: str | None = None,
    include_spam_trash: bool = False,
    account: str | None = None,
) -> dict:
    """List messages from the user's mailbox.

    Args:
        ctx: MCP context (injected automatically).
        query: Gmail search query (e.g. "is:unread", "from:alice@example.com").
        label_ids: Filter by label IDs (e.g. ["INBOX"], ["STARRED"]).
        max_results: Maximum messages to return (1-500, default 20).
        page_token: Token for the next page of results.
        include_spam_trash: Include SPAM and TRASH in results.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        kwargs = {
            "userId": "me",
            "maxResults": min(max(1, max_results), 500),
            "includeSpamTrash": include_spam_trash,
        }
        if query:
            kwargs["q"] = query
        if label_ids:
            kwargs["labelIds"] = label_ids
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        messages = []
        for stub in response.get("messages", []):
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            messages.append(
                {
                    "id": msg["id"],
                    "thread_id": msg["threadId"],
                    "snippet": msg.get("snippet", ""),
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                }
            )
        return {
            "messages": messages,
            "next_page_token": response.get("nextPageToken"),
            "result_size_estimate": response.get("resultSizeEstimate", 0),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_get_message(
    ctx: Context,
    message_id: str,
    clean: bool = True,
    account: str | None = None,
) -> dict:
    """Get a single email message by ID with full body, headers, and attachments.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The Gmail message ID.
        clean: If True (default), return only the new content of the message
               (strips quoted replies, signatures, and HTML body). Set to False
               for the raw unprocessed body text and HTML.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        result = _parse_full_message(msg)
        if clean:
            result["body_text"] = _clean_body_text(result["body_text"])
            del result["body_html"]
        return result
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_get_thread(
    ctx: Context,
    thread_id: str,
    offset: int = 0,
    limit: int | None = None,
    account: str | None = None,
) -> dict:
    """Get an entire email thread as a deduplicated chronological conversation.

    Returns each message with only its NEW content (quoted replies and signatures
    stripped), plus metadata. This is far more token-efficient than fetching each
    message individually, especially for long threads.

    For very long threads (50+ messages), use offset and limit to paginate:
        - First call: gmail_get_thread(thread_id="...") to see message_count
        - Then: gmail_get_thread(thread_id="...", offset=0, limit=25)
        - Then: gmail_get_thread(thread_id="...", offset=25, limit=25)

    Args:
        ctx: MCP context (injected automatically).
        thread_id: The Gmail thread ID (available from search results as thread_id).
        offset: Start from this message index (0-based, default 0).
        limit: Maximum number of messages to return. None returns all messages.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        all_messages = thread.get("messages", [])
        total_count = len(all_messages)

        # Get subject from the first message
        first_msg = all_messages[0] if all_messages else {}
        first_headers = {
            h["name"]: h["value"]
            for h in first_msg.get("payload", {}).get("headers", [])
        }

        # Apply pagination
        end = offset + limit if limit is not None else total_count
        page = all_messages[offset:end]

        messages = []
        for msg in page:
            parsed = _parse_full_message(msg)
            cleaned_text = _clean_body_text(parsed["body_text"])
            messages.append(
                {
                    "id": parsed["id"],
                    "from": parsed["from"],
                    "to": parsed["to"],
                    "cc": parsed["cc"],
                    "date": parsed["date"],
                    "body_text": cleaned_text,
                    "attachments": [
                        {"filename": a["filename"], "size": a["size"]}
                        for a in parsed["attachments"]
                        if a.get("filename")
                    ],
                }
            )

        result = {
            "thread_id": thread_id,
            "subject": first_headers.get("Subject", ""),
            "message_count": total_count,
            "messages": messages,
        }
        # Include pagination hints when not returning all messages
        if limit is not None:
            result["offset"] = offset
            result["limit"] = limit
            if end < total_count:
                result["next_offset"] = end
        return result
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_download_attachment(
    ctx: Context, message_id: str, attachment_id: str, account: str | None = None
) -> dict:
    """Download an attachment from a message.

    Returns the raw base64url-encoded data from the Gmail API.
    Use gmail_save_attachment to decode and save to disk.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The Gmail message ID containing the attachment.
        attachment_id: The attachment ID (from gmail_get_message attachments list).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        return {
            "data": attachment.get("data", ""),
            "size": attachment.get("size", 0),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_save_attachment(
    ctx: Context,
    message_id: str,
    attachment_id: str,
    filename: str,
    save_path: str,
    account: str | None = None,
) -> dict:
    """Download and save an attachment to disk.

    Creates the save directory if it doesn't exist.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The Gmail message ID containing the attachment.
        attachment_id: The attachment ID (from gmail_get_message attachments list).
        filename: Name to save the file as.
        save_path: Directory path to save the file in.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )

        # Decode base64url data
        data = attachment.get("data", "")
        file_data = base64.urlsafe_b64decode(data)

        # Create directory if needed
        save_dir = Path(save_path).expanduser()
        os.makedirs(save_dir, exist_ok=True)

        # Write file
        file_path = save_dir / filename
        file_path.write_bytes(file_data)

        return {
            "path": str(file_path),
            "size": len(file_data),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_search_messages(
    ctx: Context,
    query: str,
    max_results: int = 10,
    page_token: str | None = None,
    account: str | None = None,
) -> dict:
    """Search messages using Gmail query syntax. Returns compact summaries.

    Use gmail_get_message to read the full body of a specific result.

    Args:
        ctx: MCP context (injected automatically).
        query: Gmail search query (e.g. "from:alice has:attachment after:2024/01/01").
        max_results: Maximum messages to return (1-100, default 10).
        page_token: Token for the next page.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        capped = min(max(1, max_results), 100)
        kwargs = {"userId": "me", "q": query, "maxResults": capped}
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        messages = []
        for stub in response.get("messages", []):
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            messages.append(
                {
                    "id": msg["id"],
                    "thread_id": msg["threadId"],
                    "snippet": msg.get("snippet", ""),
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "date": headers.get("Date", ""),
                    "labels": msg.get("labelIds", []),
                }
            )
        return {
            "messages": messages,
            "next_page_token": response.get("nextPageToken"),
            "result_size_estimate": response.get("resultSizeEstimate", 0),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_list_drafts(
    ctx: Context,
    max_results: int = 20,
    page_token: str | None = None,
    query: str | None = None,
    account: str | None = None,
) -> dict:
    """List drafts from the user's mailbox.

    Args:
        ctx: MCP context (injected automatically).
        max_results: Maximum drafts to return (1-500, default 20).
        page_token: Token for the next page.
        query: Gmail search query to filter drafts.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        kwargs = {"userId": "me", "maxResults": min(max(1, max_results), 500)}
        if page_token:
            kwargs["pageToken"] = page_token
        if query:
            kwargs["q"] = query

        response = service.users().drafts().list(**kwargs).execute()
        drafts = []
        for stub in response.get("drafts", []):
            draft = (
                service.users()
                .drafts()
                .get(userId="me", id=stub["id"], format="metadata")
                .execute()
            )
            msg = draft.get("message", {})
            headers = {
                h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
            }
            drafts.append(
                {
                    "draft_id": draft["id"],
                    "message_id": msg.get("id", ""),
                    "subject": headers.get("Subject", ""),
                    "to": headers.get("To", ""),
                    "snippet": msg.get("snippet", ""),
                }
            )
        return {
            "drafts": drafts,
            "next_page_token": response.get("nextPageToken"),
            "result_size_estimate": response.get("resultSizeEstimate", 0),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_send_message(
    ctx: Context,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
    attachment_paths: list[str] | None = None,
    reply_to_message_id: str | None = None,
    thread_id: str | None = None,
    account: str | None = None,
) -> dict:
    """Send an email message.

    Args:
        ctx: MCP context (injected automatically).
        to: Recipient email address(es), comma-separated for multiple.
        subject: Email subject line.
        body: Plain text body content.
        cc: CC recipients, comma-separated.
        bcc: BCC recipients, comma-separated.
        html_body: HTML version of the body.
        attachment_paths: List of absolute file paths to attach.
        reply_to_message_id: Message-ID header value to reply to (for threading).
        thread_id: Gmail thread ID to place this message in.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        message_body = _build_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            attachment_paths=attachment_paths,
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
        )
        service = _get_service(ctx, account)
        sent = service.users().messages().send(userId="me", body=message_body).execute()
        return {
            "id": sent["id"],
            "thread_id": sent.get("threadId", ""),
            "label_ids": sent.get("labelIds", []),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_reply_on_message(
    ctx: Context,
    message_id: str,
    body: str,
    reply_all: bool = False,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
    attachment_paths: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """Reply to an existing email message.

    Automatically fetches the original message to set the correct recipient,
    subject (with "Re:" prefix), thread ID, and In-Reply-To / References
    headers for proper threading.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The Gmail message ID to reply to.
        body: Plain text reply body.
        reply_all: If True, reply to all original recipients (To + CC).
        cc: Additional CC recipients (comma-separated). Merged with original CC when reply_all is True.
        bcc: BCC recipients, comma-separated.
        html_body: HTML version of the reply body.
        attachment_paths: List of absolute file paths to attach.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)

        # Fetch the original message headers
        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=[
                    "Subject",
                    "From",
                    "To",
                    "Cc",
                    "Message-ID",
                    "Reply-To",
                ],
            )
            .execute()
        )
        headers = {
            h["name"]: h["value"]
            for h in original.get("payload", {}).get("headers", [])
        }
        thread_id = original.get("threadId", "")
        original_message_id_header = headers.get("Message-ID", "")
        original_subject = headers.get("Subject", "")
        original_from = headers.get("From", "")
        original_to = headers.get("To", "")
        original_cc = headers.get("Cc", "")
        original_reply_to = headers.get("Reply-To", "")

        # Build the reply subject
        subject = original_subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Extract clean email addresses using email.utils
        # "John Doe <john@example.com>" -> "john@example.com"
        def _parse_addr(header_value: str) -> str:
            _, addr = email.utils.parseaddr(header_value)
            return addr

        def _parse_addr_list(header_value: str) -> list[str]:
            return [
                addr for _, addr in email.utils.getaddresses([header_value]) if addr
            ]

        # Get our own email to exclude from recipients
        profile = service.users().getProfile(userId="me").execute()
        my_email = profile.get("emailAddress", "").lower()

        # Reply-To header takes priority over From
        if original_reply_to:
            to = _parse_addr(original_reply_to)
        else:
            to = _parse_addr(original_from)

        # For reply_all, add original To and CC (excluding ourselves and the sender)
        merged_cc = ""
        if reply_all:
            all_recipients: list[str] = []
            for addr_list in [original_to, original_cc]:
                if addr_list:
                    all_recipients.extend(_parse_addr_list(addr_list))
            # Filter out our own address and the primary reply recipient
            filtered = [
                a
                for a in all_recipients
                if a.lower() != my_email and a.lower() != to.lower()
            ]
            if filtered:
                merged_cc = ", ".join(filtered)

        # Merge user-provided CC with reply-all CC
        if cc and merged_cc:
            merged_cc = f"{merged_cc}, {cc}"
        elif cc:
            merged_cc = cc

        message_body = _build_message(
            to=to,
            subject=subject,
            body=body,
            cc=merged_cc or None,
            bcc=bcc,
            html_body=html_body,
            attachment_paths=attachment_paths,
            reply_to_message_id=original_message_id_header,
            thread_id=thread_id,
        )
        sent = service.users().messages().send(userId="me", body=message_body).execute()
        return {
            "id": sent["id"],
            "thread_id": sent.get("threadId", ""),
            "label_ids": sent.get("labelIds", []),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_create_draft(
    ctx: Context,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
    attachment_paths: list[str] | None = None,
    reply_to_message_id: str | None = None,
    thread_id: str | None = None,
    account: str | None = None,
) -> dict:
    """Create a draft email message without sending it.

    Args:
        ctx: MCP context (injected automatically).
        to: Recipient email address(es), comma-separated for multiple.
        subject: Email subject line.
        body: Plain text body content.
        cc: CC recipients, comma-separated.
        bcc: BCC recipients, comma-separated.
        html_body: HTML version of the body.
        attachment_paths: List of absolute file paths to attach.
        reply_to_message_id: Message-ID header value to reply to.
        thread_id: Gmail thread ID to place this draft in.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        message_body = _build_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html_body=html_body,
            attachment_paths=attachment_paths,
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
        )
        service = _get_service(ctx, account)
        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": message_body})
            .execute()
        )
        return {"draft_id": draft["id"], "message_id": draft["message"]["id"]}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_update_draft(
    ctx: Context,
    draft_id: str,
    to: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
    attachment_paths: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """Update an existing draft. Only provided fields are changed.

    Note: Gmail replaces the entire draft — the underlying message ID will change.

    Args:
        ctx: MCP context (injected automatically).
        draft_id: The ID of the draft to update.
        to: New recipient(s). None keeps existing.
        subject: New subject. None keeps existing.
        body: New plain text body. None keeps existing.
        cc: New CC recipients. None keeps existing.
        bcc: New BCC recipients. None keeps existing.
        html_body: New HTML body. None keeps existing.
        attachment_paths: New file attachments (replaces all).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        existing = (
            service.users()
            .drafts()
            .get(userId="me", id=draft_id, format="raw")
            .execute()
        )
        raw_data = existing.get("message", {}).get("raw", "")
        current = _parse_raw_draft(raw_data)

        message_body = _build_message(
            to=to if to is not None else current.get("to", ""),
            subject=subject if subject is not None else current.get("subject", ""),
            body=body if body is not None else current.get("body", ""),
            cc=cc if cc is not None else current.get("cc"),
            bcc=bcc if bcc is not None else current.get("bcc"),
            html_body=html_body if html_body is not None else current.get("html_body"),
            attachment_paths=attachment_paths,
        )
        updated = (
            service.users()
            .drafts()
            .update(userId="me", id=draft_id, body={"message": message_body})
            .execute()
        )
        return {"draft_id": updated["id"], "message_id": updated["message"]["id"]}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_delete_draft(ctx: Context, draft_id: str, account: str | None = None) -> dict:
    """Permanently delete a draft. This cannot be undone.

    Args:
        ctx: MCP context (injected automatically).
        draft_id: The ID of the draft to delete.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        service.users().drafts().delete(userId="me", id=draft_id).execute()
        return {"success": True}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_send_draft(ctx: Context, draft_id: str, account: str | None = None) -> dict:
    """Send an existing draft. The draft is deleted after sending.

    Args:
        ctx: MCP context (injected automatically).
        draft_id: The ID of the draft to send.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        result = (
            service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        )
        return {"message_id": result["id"], "thread_id": result.get("threadId", "")}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_list_labels(ctx: Context, account: str | None = None) -> dict:
    """List all labels in the user's mailbox (system and user-created).

    Args:
        ctx: MCP context (injected automatically).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        response = service.users().labels().list(userId="me").execute()
        labels = [
            {"id": l["id"], "name": l["name"], "type": l.get("type", "")}
            for l in response.get("labels", [])
        ]
        return {"labels": labels}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_create_label(ctx: Context, name: str, account: str | None = None) -> dict:
    """Create a new user label. Use "/" for nesting (e.g. "Projects/Work").

    Args:
        ctx: MCP context (injected automatically).
        name: Display name for the new label.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        label = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        return {"id": label["id"], "name": label["name"]}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_delete_label(ctx: Context, label_id: str, account: str | None = None) -> dict:
    """Delete a user label. System labels cannot be deleted.

    Args:
        ctx: MCP context (injected automatically).
        label_id: The ID of the label to delete.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        service.users().labels().delete(userId="me", id=label_id).execute()
        return {"success": True}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_modify_message_labels(
    ctx: Context,
    message_id: str,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """Add or remove labels from a message.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The message ID to modify.
        add_label_ids: Label IDs to add (e.g. ["STARRED"]).
        remove_label_ids: Label IDs to remove (e.g. ["UNREAD"]).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        body = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        if not body:
            return {
                "error": "Provide at least one of add_label_ids or remove_label_ids"
            }
        result = (
            service.users()
            .messages()
            .modify(userId="me", id=message_id, body=body)
            .execute()
        )
        return {"id": result["id"], "label_ids": result.get("labelIds", [])}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_trash_message(ctx: Context, message_id: str, account: str | None = None) -> dict:
    """Move a message to trash. Auto-deleted after 30 days.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The message ID to trash.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        result = service.users().messages().trash(userId="me", id=message_id).execute()
        return {"id": result["id"], "label_ids": result.get("labelIds", [])}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_untrash_message(ctx: Context, message_id: str, account: str | None = None) -> dict:
    """Restore a message from trash.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The message ID to restore.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        result = (
            service.users().messages().untrash(userId="me", id=message_id).execute()
        )
        return {"id": result["id"], "label_ids": result.get("labelIds", [])}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------


@mcp.tool()
def gmail_batch_delete_messages(
    ctx: Context, message_ids: list[str], account: str | None = None
) -> dict:
    """Permanently delete up to 1000 messages by ID list.

    This is a destructive operation that cannot be undone. Messages are
    immediately and permanently deleted (not moved to trash).

    Args:
        ctx: MCP context (injected automatically).
        message_ids: List of message IDs to delete (max 1000).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        if not message_ids:
            return {"error": "message_ids cannot be empty"}
        if len(message_ids) > 1000:
            return {"error": "Cannot delete more than 1000 messages at once"}
        service = _get_service(ctx, account)
        service.users().messages().batchDelete(
            userId="me", body={"ids": message_ids}
        ).execute()
        return {"success": True, "deleted_count": len(message_ids)}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_batch_modify_messages(
    ctx: Context,
    message_ids: list[str],
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """Add or remove labels on up to 1000 messages at once.

    More efficient than modifying messages individually when working with
    multiple messages. Example: archive multiple messages by removing "INBOX"
    label, or mark multiple messages as read by removing "UNREAD" label.

    Args:
        ctx: MCP context (injected automatically).
        message_ids: List of message IDs to modify (max 1000).
        add_label_ids: Label IDs to add to all messages (e.g. ["STARRED"]).
        remove_label_ids: Label IDs to remove from all messages (e.g. ["UNREAD", "INBOX"]).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        if not message_ids:
            return {"error": "message_ids cannot be empty"}
        if len(message_ids) > 1000:
            return {"error": "Cannot modify more than 1000 messages at once"}
        body = {"ids": message_ids}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        if not add_label_ids and not remove_label_ids:
            return {
                "error": "Provide at least one of add_label_ids or remove_label_ids"
            }
        service = _get_service(ctx, account)
        service.users().messages().batchModify(userId="me", body=body).execute()
        return {"success": True, "modified_count": len(message_ids)}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Thread Operations
# ---------------------------------------------------------------------------


@mcp.tool()
def gmail_list_threads(
    ctx: Context,
    query: str | None = None,
    label_ids: list[str] | None = None,
    max_results: int = 20,
    page_token: str | None = None,
    include_spam_trash: bool = False,
    account: str | None = None,
) -> dict:
    """List email threads (conversations) from the user's mailbox.

    Threads group related messages together. Use gmail_get_thread to read
    the full conversation.

    Args:
        ctx: MCP context (injected automatically).
        query: Gmail search query (e.g. "is:unread", "from:alice@example.com").
        label_ids: Filter by label IDs (e.g. ["INBOX"], ["STARRED"]).
        max_results: Maximum threads to return (1-500, default 20).
        page_token: Token for the next page of results.
        include_spam_trash: Include SPAM and TRASH in results.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        kwargs = {
            "userId": "me",
            "maxResults": min(max(1, max_results), 500),
            "includeSpamTrash": include_spam_trash,
        }
        if query:
            kwargs["q"] = query
        if label_ids:
            kwargs["labelIds"] = label_ids
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().threads().list(**kwargs).execute()
        threads = []
        for stub in response.get("threads", []):
            # Get thread metadata without full message content
            thread = (
                service.users()
                .threads()
                .get(userId="me", id=stub["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"])
                .execute()
            )
            # Get subject from first message
            first_msg = thread.get("messages", [{}])[0]
            headers = {
                h["name"]: h["value"]
                for h in first_msg.get("payload", {}).get("headers", [])
            }
            threads.append(
                {
                    "thread_id": thread["id"],
                    "snippet": thread.get("snippet", ""),
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "message_count": len(thread.get("messages", [])),
                }
            )
        return {
            "threads": threads,
            "next_page_token": response.get("nextPageToken"),
            "result_size_estimate": response.get("resultSizeEstimate", 0),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_modify_thread_labels(
    ctx: Context,
    thread_id: str,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
    account: str | None = None,
) -> dict:
    """Add or remove labels from all messages in a thread.

    Modifies all messages in the thread at once. Useful for archiving or
    starring entire conversations.

    Args:
        ctx: MCP context (injected automatically).
        thread_id: The thread ID to modify.
        add_label_ids: Label IDs to add to all messages in thread (e.g. ["STARRED"]).
        remove_label_ids: Label IDs to remove from all messages in thread (e.g. ["UNREAD", "INBOX"]).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        body = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        if not body:
            return {
                "error": "Provide at least one of add_label_ids or remove_label_ids"
            }
        service = _get_service(ctx, account)
        result = (
            service.users()
            .threads()
            .modify(userId="me", id=thread_id, body=body)
            .execute()
        )
        return {"thread_id": result["id"], "messages": len(result.get("messages", []))}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_trash_thread(ctx: Context, thread_id: str, account: str | None = None) -> dict:
    """Move an entire thread (conversation) to trash.

    All messages in the thread are moved to trash and will be auto-deleted
    after 30 days. Can be restored with gmail_untrash_thread.

    Args:
        ctx: MCP context (injected automatically).
        thread_id: The thread ID to trash.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        result = service.users().threads().trash(userId="me", id=thread_id).execute()
        return {"thread_id": result["id"], "messages": len(result.get("messages", []))}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_untrash_thread(ctx: Context, thread_id: str, account: str | None = None) -> dict:
    """Restore an entire thread (conversation) from trash.

    All messages in the thread are restored from trash.

    Args:
        ctx: MCP context (injected automatically).
        thread_id: The thread ID to restore.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        result = service.users().threads().untrash(userId="me", id=thread_id).execute()
        return {"thread_id": result["id"], "messages": len(result.get("messages", []))}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Filter Management
# ---------------------------------------------------------------------------


@mcp.tool()
def gmail_list_filters(ctx: Context, account: str | None = None) -> dict:
    """List all mail filters for the user's account.

    Filters automatically organize incoming mail by applying labels, archiving,
    marking as read, starring, forwarding, or deleting messages that match
    specific criteria.

    Args:
        ctx: MCP context (injected automatically).
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        response = service.users().settings().filters().list(userId="me").execute()
        filters = []
        for f in response.get("filter", []):
            criteria = f.get("criteria", {})
            action = f.get("action", {})
            filters.append(
                {
                    "id": f.get("id", ""),
                    "criteria": criteria,
                    "action": action,
                }
            )
        return {"filters": filters, "count": len(filters)}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_get_filter(ctx: Context, filter_id: str, account: str | None = None) -> dict:
    """Get a specific mail filter by ID.

    Args:
        ctx: MCP context (injected automatically).
        filter_id: The filter ID to retrieve.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        filter_obj = (
            service.users().settings().filters().get(userId="me", id=filter_id).execute()
        )
        return {
            "id": filter_obj.get("id", ""),
            "criteria": filter_obj.get("criteria", {}),
            "action": filter_obj.get("action", {}),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_create_filter(
    ctx: Context,
    from_email: str | None = None,
    to_email: str | None = None,
    subject: str | None = None,
    query: str | None = None,
    negated_query: str | None = None,
    has_attachment: bool | None = None,
    exclude_chats: bool | None = None,
    size: int | None = None,
    size_comparison: str | None = None,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
    forward_to: str | None = None,
    mark_as_read: bool | None = None,
    mark_as_important: bool | None = None,
    mark_as_spam: bool | None = None,
    star: bool | None = None,
    archive: bool | None = None,
    trash: bool | None = None,
    account: str | None = None,
) -> dict:
    """Create a new mail filter with criteria and actions.

    Filters automatically process incoming mail. At least one criteria field
    must be provided. At least one action must be specified.

    Criteria fields:
        from_email: Match sender email/domain (e.g. "alice@example.com" or "example.com").
        to_email: Match recipient email/domain.
        subject: Match subject line text.
        query: Gmail search query (e.g. "has:attachment larger:5M").
        negated_query: Messages NOT matching this query.
        has_attachment: If True, match only messages with attachments.
        exclude_chats: If True, exclude chat messages.
        size: Size threshold in bytes.
        size_comparison: "larger" or "smaller" (required if size is specified).

    Action fields:
        add_label_ids: Label IDs to apply to matching messages.
        remove_label_ids: Label IDs to remove from matching messages.
        forward_to: Email address to forward matching messages to.
        mark_as_read: If True, mark as read.
        mark_as_important: If True, mark as important. If False, mark as not important.
        mark_as_spam: If True, move to spam.
        star: If True, star the message.
        archive: If True, remove from inbox (archive).
        trash: If True, move to trash.

    Args:
        ctx: MCP context (injected automatically).
        (criteria and action fields as described above)
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        criteria = {}
        if from_email:
            criteria["from"] = from_email
        if to_email:
            criteria["to"] = to_email
        if subject:
            criteria["subject"] = subject
        if query:
            criteria["query"] = query
        if negated_query:
            criteria["negatedQuery"] = negated_query
        if has_attachment is not None:
            criteria["hasAttachment"] = has_attachment
        if exclude_chats is not None:
            criteria["excludeChats"] = exclude_chats
        if size is not None:
            if size_comparison not in ["larger", "smaller"]:
                return {"error": "size_comparison must be 'larger' or 'smaller' when size is specified"}
            criteria["size"] = size
            criteria["sizeComparison"] = size_comparison

        if not criteria:
            return {"error": "At least one criteria field must be provided"}

        action = {}
        if add_label_ids:
            action["addLabelIds"] = add_label_ids
        if remove_label_ids:
            action["removeLabelIds"] = remove_label_ids
        if forward_to:
            action["forward"] = forward_to
        if mark_as_read is not None:
            if mark_as_read:
                action["removeLabelIds"] = action.get("removeLabelIds", []) + ["UNREAD"]
        if mark_as_important is not None:
            if mark_as_important:
                action["addLabelIds"] = action.get("addLabelIds", []) + ["IMPORTANT"]
            else:
                action["removeLabelIds"] = action.get("removeLabelIds", []) + ["IMPORTANT"]
        if mark_as_spam is not None and mark_as_spam:
            action["addLabelIds"] = action.get("addLabelIds", []) + ["SPAM"]
        if star is not None and star:
            action["addLabelIds"] = action.get("addLabelIds", []) + ["STARRED"]
        if archive is not None and archive:
            action["removeLabelIds"] = action.get("removeLabelIds", []) + ["INBOX"]
        if trash is not None and trash:
            action["addLabelIds"] = action.get("addLabelIds", []) + ["TRASH"]

        if not action:
            return {"error": "At least one action must be specified"}

        service = _get_service(ctx, account)
        filter_obj = (
            service.users()
            .settings()
            .filters()
            .create(userId="me", body={"criteria": criteria, "action": action})
            .execute()
        )
        return {
            "id": filter_obj.get("id", ""),
            "criteria": filter_obj.get("criteria", {}),
            "action": filter_obj.get("action", {}),
        }
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_delete_filter(ctx: Context, filter_id: str, account: str | None = None) -> dict:
    """Delete a mail filter by ID. This cannot be undone.

    Args:
        ctx: MCP context (injected automatically).
        filter_id: The filter ID to delete.
        account: (Optional) Account name. Defaults to the default account.
    """
    try:
        service = _get_service(ctx, account)
        service.users().settings().filters().delete(userId="me", id=filter_id).execute()
        return {"success": True}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def auth():
    """Run OAuth flow and verify authentication."""
    if ACCOUNTS_CONFIG_PATH:
        config_path = Path(ACCOUNTS_CONFIG_PATH).expanduser()
        print(f"Accounts config: {config_path}")
        print()
        try:
            services, default_account, emails = _load_accounts_config()
            for name in services:
                marker = " (default)" if name == default_account else ""
                print(f"  [{name}]{marker}: {emails.get(name, 'unknown')}")
            print(f"\nAll {len(services)} account(s) authenticated successfully.")
        except Exception as e:
            print(f"Authentication failed: {e}")
            sys.exit(1)
    else:
        print(f"Credentials path: {CREDENTIALS_PATH}")
        print(f"Token path:       {TOKEN_PATH}")
        print()
        try:
            service = _authenticate()
            profile = service.users().getProfile(userId="me").execute()
            print(f"Authenticated as: {profile['emailAddress']}")
            print(f"Total messages:   {profile['messagesTotal']}")
            print(f"Token saved to:   {TOKEN_PATH}")
        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Authentication failed: {e}")
            sys.exit(1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        auth()
        return

    transport = "stdio"
    for i, arg in enumerate(sys.argv):
        if arg == "--transport" and i + 1 < len(sys.argv):
            transport = sys.argv[i + 1]
            break
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
