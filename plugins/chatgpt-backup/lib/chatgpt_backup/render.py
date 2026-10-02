"""Render one ChatGPT conversation (raw API JSON) to Markdown.

Pure functions, no I/O, no network: easy to test and reusable by ``rebuild``.
"""
from __future__ import annotations

import datetime
import json
import re

FILE_ID_RE = re.compile(r"^file[-_][A-Za-z0-9]+$")

MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf",
    "text/markdown": ".md", "text/xml": ".xml", "text/plain": ".txt",
    "audio/x-m4a": ".m4a", "audio/mp4": ".m4a", "video/mp4": ".mp4",
}


def ts(t):
    """Epoch seconds -> 'YYYY-MM-DD HH:MM' (UTC); '' when missing."""
    if not t:
        return ""
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def safe(s, n):
    """Filesystem-safe fragment: unicode word chars, dot and dash only."""
    s = re.sub(r"[^\w.\-]+", "_", s or "").strip("_")
    return s[:n]


def slug(title):
    return safe(title, 60) or "untitled"


def clean_text(t):
    """Drop ChatGPT's private-use citation markers, keeping links and entity names.

    Markers look like ``\\ue200 type \\ue202 arg \\ue202 arg \\ue201``.
    """
    def rep(m):
        parts = m.group(1).split("")
        typ, args = parts[0], parts[1:]
        try:
            if typ == "url" and len(args) >= 2:
                return "[%s](%s)" % (args[0], args[1])
            if typ == "entity" and args:
                j = json.loads(args[0])
                return str(j[1]) if len(j) > 1 else ""
            if typ in ("video", "link_title") and args:
                return args[0]
        except (ValueError, IndexError, TypeError):
            pass
        return ""

    t = re.sub("(.*?)", rep, t, flags=re.S)
    t = re.sub("[-]", "", t)
    return re.sub(r"[ \t]+\n", "\n", t).strip()


def active_path(conv):
    """Nodes from the root to ``current_node`` (the branch the user sees)."""
    out, n = [], conv.get("current_node")
    mapping = conv.get("mapping") or {}
    while n and n in mapping:
        node = mapping[n]
        out.append(node)
        n = node.get("parent")
    return out[::-1]


def _parts(msg):
    return msg["content"].get("parts") or []


def file_refs(conv):
    """All files referenced by a conversation: ``{file_id: {id,name,mime,kind}}``.

    ``kind`` is ``image`` | ``audio`` | ``att`` (user attachment) | ``other``.
    """
    refs = {}
    for node in (conv.get("mapping") or {}).values():
        msg = node.get("message")
        if not msg:
            continue
        for at in (msg.get("metadata") or {}).get("attachments") or []:
            refs.setdefault(at["id"], {"id": at["id"], "name": at.get("name"),
                                       "mime": at.get("mime_type"), "kind": "att"})
        for p in _parts(msg):
            if isinstance(p, dict) and p.get("asset_pointer"):
                fid = p["asset_pointer"].split("://", 1)[-1]
                kind = {"image_asset_pointer": "image", "audio_asset_pointer": "audio"}.get(
                    p.get("content_type"), "other")
                d = refs.setdefault(fid, {"id": fid, "name": None, "mime": None, "kind": kind})
                if d["kind"] == "att" and kind != "other":
                    d["kind"] = kind
    return refs


def unfetchable_reason(fid, kind):
    """Reason a file can never be fetched through the files API, or None."""
    if kind == "audio":
        return "audio recordings are not downloadable"
    if "#" in fid:
        m = re.search(r"#(file_[0-9a-f]+)#p_(\d+)", fid)
        if m:
            return "page preview of document %s (page %d); not downloadable by itself" % (
                m.group(1), int(m.group(2)) + 1)
        return "derived preview; not downloadable"
    if not FILE_ID_RE.match(fid):
        return "not a downloadable file id"
    return None


def _visible(msg):
    if not msg:
        return False
    if msg["author"]["role"] == "system":
        return False
    return not (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation")


def expected_message_count(conv):
    """Independent count of user/assistant messages with something to show."""
    k = 0
    for node in active_path(conv):
        msg = node.get("message")
        if not _visible(msg) or msg["author"]["role"] not in ("user", "assistant"):
            continue
        if msg["content"]["content_type"] not in ("text", "multimodal_text"):
            continue
        has = bool((msg.get("metadata") or {}).get("attachments"))
        for p in _parts(msg):
            if isinstance(p, str) and p.strip():
                has = True
            elif isinstance(p, dict) and (
                    (p.get("content_type") == "audio_transcription" and (p.get("text") or "").strip())
                    or (p.get("content_type") == "image_asset_pointer" and p.get("asset_pointer"))):
                has = True
        k += has
    return k


def render_markdown(conv, meta, file_info):
    """Render ``conv`` to Markdown.

    meta: ``{id, project, archived, deleted}``
    file_info(fid) -> ``(status, reason, filename)`` where status is
    ``ok`` | ``unavailable`` | ``error``; ``filename`` is the artifact basename.
    Returns ``(markdown, rendered_message_count)``.
    """
    refs = file_refs(conv)
    lines, count = [], 0

    def link(fid, label):
        status, reason, fn = file_info(fid)
        if status != "ok":
            return "*[%s unavailable: %s]*" % (label, reason)
        if label == "image":
            return "![image](artifacts/%s)" % fn
        return "[%s](artifacts/%s)" % (label, fn)

    for node in active_path(conv):
        msg = node.get("message")
        if not _visible(msg):
            continue
        role = msg["author"]["role"]
        md = msg.get("metadata") or {}
        ctype = msg["content"]["content_type"]
        if ctype in ("text", "multimodal_text"):
            body = []
            for p in _parts(msg):
                if isinstance(p, str):
                    if p.strip():
                        body.append(clean_text(p))
                elif isinstance(p, dict):
                    t = p.get("content_type")
                    if t == "audio_transcription" and (p.get("text") or "").strip():
                        body.append(clean_text(p["text"]))
                    elif t == "image_asset_pointer" and p.get("asset_pointer"):
                        body.append(link(p["asset_pointer"].split("://", 1)[-1], "image"))
            for at in md.get("attachments") or []:
                if not any(at["id"] in b for b in body):
                    body.append("Attachment: " + link(at["id"], at.get("name") or at["id"]))
            if role in ("user", "assistant") and body:
                count += 1
                model = md.get("model_slug")
                stamp = ""
                if msg.get("create_time"):
                    stamp = " (%s UTC%s)" % (ts(msg["create_time"]),
                                              ", " + model if model and role == "assistant" else "")
                lines += ["<!-- msg role=%s -->" % role,
                          "### %s%s" % ("User" if role == "user" else "Assistant", stamp),
                          "", "\n\n".join(body), ""]
            elif role == "tool" and any(b.startswith("![image]") or "image unavailable" in b for b in body):
                lines += ["### Generated image", "", "\n\n".join(body), ""]
        elif ctype == "code" and (msg["content"].get("text") or "").strip():
            lang = (msg["content"].get("language") or "").replace("unknown", "")
            lines.append("<details><summary>tool call</summary>\n\n```%s\n%s\n```\n\n</details>\n"
                         % (lang, msg["content"]["text"]))
        elif ctype == "execution_output" and (msg["content"].get("text") or "").strip():
            lines.append("<details><summary>tool output</summary>\n\n```\n%s\n```\n\n</details>\n"
                         % msg["content"]["text"])

    mapping_msgs = sum(1 for n in (conv.get("mapping") or {}).values() if n.get("message"))
    path_msgs = sum(1 for n in active_path(conv) if n.get("message"))
    head = ["# " + (conv.get("title") or "(untitled)").strip(), "",
            "- ID: `%s`" % meta["id"],
            "- Project: %s" % (meta.get("project") or "(none)"),
            "- Created: %s UTC" % ts(conv.get("create_time")),
            "- Updated: %s UTC" % ts(conv.get("update_time")),
            "- Model: %s" % (conv.get("default_model_slug") or ""),
            "- Archived: %s" % ("yes" if meta.get("archived") else "no")]
    if meta.get("deleted"):
        head.append("- Deleted on server: yes (kept here as last backed up)")
    if mapping_msgs != path_msgs:
        head.append("- Branches: %d messages are on other branches (only the active branch is shown; "
                    "see conversation.json)" % (mapping_msgs - path_msgs))
    head += ["", "---", ""]
    del refs  # refs are computed by callers; kept local only to validate input shape
    return "\n".join(head + lines), count


def sniff_ext(path):
    """File extension from magic bytes (for attachments the server gave no name/mime for)."""
    with open(path, "rb") as f:
        h = f.read(16)
    if h.startswith(b"\x89PNG"):
        return ".png"
    if h[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if h.startswith(b"%PDF"):
        return ".pdf"
    if h[4:8] == b"ftyp":
        return ".mp4"
    if h.startswith(b"RIFF") and h[8:12] == b"WAVE":
        return ".wav"
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        return ".webp"
    if h.startswith(b"GIF8"):
        return ".gif"
    if h.startswith(b"PK"):
        return ".zip"
    return ".bin"


def download_basename(fid, name, mime):
    """Name the browser should save a file under: ``<id>__<sanitized name or mime ext>``."""
    nm = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name or "")[:80] or MIME_EXT.get(mime, "")
    return fid + "__" + nm
