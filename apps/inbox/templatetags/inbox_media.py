"""Presentation of supplied attachment links; no fetching or URL invention."""

from urllib.parse import urlsplit

from django import template

from providers.meta_inbox_content import normalize_attachments, shared_content_url

register = template.Library()


@register.simple_tag
def attachment_card(value):
    items = normalize_attachments({"inbox_attachments": [value]})
    if not items:
        return {}
    item = items[0]
    url, preview = item["url"], item["preview_url"]
    host = (urlsplit(url).hostname or "").lower()
    media = any(
        host == domain or host.endswith("." + domain) for domain in ("fbcdn.net", "cdninstagram.com", "fbsbx.com")
    )
    media = media or urlsplit(url).path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"))
    post = shared_content_url(url) if item["type"] == "share" else ""
    return {
        **item,
        "post_url": post,
        "preview_target": post or preview,
        "image_url": preview or (url if item["type"] == "image" else ""),
        "link_label": "Open original post"
        if post
        else "View image"
        if item["type"] == "image"
        else "Open media"
        if media
        else "Open shared content"
        if item["type"] == "share"
        else "Open attachment",
        "post_link_missing": item["type"] == "share" and bool(url or preview) and (media or not url) and not post,
    }
