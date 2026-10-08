"""A supplied original post wins over media; previews remain separate."""

from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string

from providers.meta_inbox_content import normalize_attachments, shared_content_url

POST = "https://www.instagram.com/p/synthetic-post/"
IMAGE = "https://scontent.cdninstagram.com/synthetic.jpg"


@pytest.mark.parametrize("field", ["permalink", "permalink_url", "link"])
def test_supplied_post_url_wins_over_cdn_media(field):
    items = normalize_attachments({"attachments": [{"type": "share", "payload": {"url": IMAGE}, field: POST}]})
    assert items[0]["url"] == POST


def test_cta_target_and_template_preview_remain_separate():
    item = {"type": "share", "url": IMAGE, "generic_template": {"cta": {"url": POST}, "image_url": IMAGE}}
    value = normalize_attachments({"attachments": [item]})[0]
    assert value["url"] == POST and value["preview_url"] == IMAGE


def test_unsafe_first_candidate_does_not_mask_valid_supplied_post():
    value = normalize_attachments(
        {"attachments": [{"type": "share", "link": "javascript:bad()", "payload": {"url": POST}}]}
    )[0]
    assert value["url"] == POST


def test_later_cdn_observation_cannot_downgrade_known_original_link():
    value = normalize_attachments(
        {
            "attachments": [{"id": "same", "type": "share", "url": POST}],
            "inbox_attachments": [{"id": "same", "type": "share", "url": IMAGE}],
        }
    )[0]
    assert value["url"] == POST


@pytest.mark.parametrize(
    "url",
    [
        IMAGE,
        "https://instagram.com.attacker.example/p/test/",
        "https://www.instagram.com/p/",
        "https://www.instagram.com/p/x/?access_token=secret",
        "https://user@www.instagram.com/p/x/",
    ],
)
def test_original_post_recognition_does_not_guess_or_accept_unsafe_urls(url):
    assert shared_content_url(url) == ""


def test_share_card_links_preview_to_post_and_image_to_separate_action():
    message = SimpleNamespace(
        body="",
        content_status="link_provided",
        attachments=[
            {
                "type": "share",
                "url": POST,
                "preview_url": IMAGE,
                "title": "A shared post",
                "availability": "available",
            }
        ],
    )
    html = render_to_string("inbox/partials/_attachment_cards.html", {"message": message})
    assert f'href="{POST}"' in html and 'aria-label="Open original post"' in html
    assert f'href="{IMAGE}"' in html and "View image" in html
    assert 'referrerpolicy="no-referrer"' in html
    assert "may expire" not in html


def test_media_only_share_never_invents_a_permalink():
    message = SimpleNamespace(
        body="",
        content_status="link_provided",
        attachments=[
            {
                "type": "share",
                "id": "opaque-platform-id",
                "url": IMAGE,
                "preview_url": IMAGE,
            }
        ],
    )
    html = render_to_string("inbox/partials/_attachment_cards.html", {"message": message})
    assert "Open original post" not in html and "Post link unavailable." in html
    assert "opaque-platform-id" not in html
