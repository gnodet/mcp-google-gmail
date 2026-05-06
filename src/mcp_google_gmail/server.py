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

_resolved_host = os.environ.get("HOST", os.environ.get("FASTMCP_HOST", "0.0.0.0"))
_resolved_port = int(os.environ.get("PORT", os.environ.get("FASTMCP_PORT", "8000")))

# ---------------------------------------------------------------------------
# Lifespan / Auth
# ---------------------------------------------------------------------------


@dataclass
class GmailContext:
    """Context holding the authenticated Gmail service."""

    service: Any


def _authenticate() -> Any:
    """Build an authenticated Gmail API service using the credential chain."""
    creds = None

    # 1. Base64-encoded service account from env var
    if CREDENTIALS_CONFIG:
        info = json.loads(base64.b64decode(CREDENTIALS_CONFIG))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )

    # 2. Service account JSON file
    if not creds and Path(SERVICE_ACCOUNT_PATH).exists():
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_PATH, scopes=SCOPES
        )

    # 3. Existing OAuth token
    if not creds and Path(TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

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
            Path(TOKEN_PATH).write_text(creds.to_json())
        elif Path(CREDENTIALS_PATH).exists():
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
            Path(TOKEN_PATH).write_text(creds.to_json())

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


@asynccontextmanager
async def gmail_lifespan(server: FastMCP) -> AsyncIterator[GmailContext]:
    """Create the Gmail service once at startup."""
    service = _authenticate()
    try:
        yield GmailContext(service=service)
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


def _get_service(ctx: Context) -> Any:
    """Extract the Gmail service from the lifespan context."""
    return ctx.request_context.lifespan_context.service


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
) -> dict:
    """List messages from the user's mailbox.

    Args:
        ctx: MCP context (injected automatically).
        query: Gmail search query (e.g. "is:unread", "from:alice@example.com").
        label_ids: Filter by label IDs (e.g. ["INBOX"], ["STARRED"]).
        max_results: Maximum messages to return (1-500, default 20).
        page_token: Token for the next page of results.
        include_spam_trash: Include SPAM and TRASH in results.
    """
    try:
        service = _get_service(ctx)
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
def gmail_get_message(ctx: Context, message_id: str) -> dict:
    """Get a single email message by ID with full body, headers, and attachments.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The Gmail message ID.
    """
    try:
        service = _get_service(ctx)
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return _parse_full_message(msg)
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
) -> dict:
    """Search messages using Gmail query syntax. Returns compact summaries.

    Use gmail_get_message to read the full body of a specific result.

    Args:
        ctx: MCP context (injected automatically).
        query: Gmail search query (e.g. "from:alice has:attachment after:2024/01/01").
        max_results: Maximum messages to return (1-100, default 10).
        page_token: Token for the next page.
    """
    try:
        service = _get_service(ctx)
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
) -> dict:
    """List drafts from the user's mailbox.

    Args:
        ctx: MCP context (injected automatically).
        max_results: Maximum drafts to return (1-500, default 20).
        page_token: Token for the next page.
        query: Gmail search query to filter drafts.
    """
    try:
        service = _get_service(ctx)
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
        service = _get_service(ctx)
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
    """
    try:
        service = _get_service(ctx)

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
        service = _get_service(ctx)
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
    """
    try:
        service = _get_service(ctx)
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
def gmail_delete_draft(ctx: Context, draft_id: str) -> dict:
    """Permanently delete a draft. This cannot be undone.

    Args:
        ctx: MCP context (injected automatically).
        draft_id: The ID of the draft to delete.
    """
    try:
        service = _get_service(ctx)
        service.users().drafts().delete(userId="me", id=draft_id).execute()
        return {"success": True}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_send_draft(ctx: Context, draft_id: str) -> dict:
    """Send an existing draft. The draft is deleted after sending.

    Args:
        ctx: MCP context (injected automatically).
        draft_id: The ID of the draft to send.
    """
    try:
        service = _get_service(ctx)
        result = (
            service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        )
        return {"message_id": result["id"], "thread_id": result.get("threadId", "")}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_list_labels(ctx: Context) -> dict:
    """List all labels in the user's mailbox (system and user-created).

    Args:
        ctx: MCP context (injected automatically).
    """
    try:
        service = _get_service(ctx)
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
def gmail_create_label(ctx: Context, name: str) -> dict:
    """Create a new user label. Use "/" for nesting (e.g. "Projects/Work").

    Args:
        ctx: MCP context (injected automatically).
        name: Display name for the new label.
    """
    try:
        service = _get_service(ctx)
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
def gmail_delete_label(ctx: Context, label_id: str) -> dict:
    """Delete a user label. System labels cannot be deleted.

    Args:
        ctx: MCP context (injected automatically).
        label_id: The ID of the label to delete.
    """
    try:
        service = _get_service(ctx)
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
) -> dict:
    """Add or remove labels from a message.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The message ID to modify.
        add_label_ids: Label IDs to add (e.g. ["STARRED"]).
        remove_label_ids: Label IDs to remove (e.g. ["UNREAD"]).
    """
    try:
        service = _get_service(ctx)
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
def gmail_trash_message(ctx: Context, message_id: str) -> dict:
    """Move a message to trash. Auto-deleted after 30 days.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The message ID to trash.
    """
    try:
        service = _get_service(ctx)
        result = service.users().messages().trash(userId="me", id=message_id).execute()
        return {"id": result["id"], "label_ids": result.get("labelIds", [])}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def gmail_untrash_message(ctx: Context, message_id: str) -> dict:
    """Restore a message from trash.

    Args:
        ctx: MCP context (injected automatically).
        message_id: The message ID to restore.
    """
    try:
        service = _get_service(ctx)
        result = (
            service.users().messages().untrash(userId="me", id=message_id).execute()
        )
        return {"id": result["id"], "label_ids": result.get("labelIds", [])}
    except HttpError as e:
        return {"error": str(e), "status_code": e.resp.status}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def auth():
    """Run OAuth flow and verify authentication."""
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
