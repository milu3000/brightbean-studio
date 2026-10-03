"""Navigation regressions; JS state tests use Node without browser dependencies."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings

from apps.members.models import WorkspaceMembership


class _PanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.back_inside_panel = False
        self.back_found = False
        self.panel_attrs = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id") == "inbox-detail-panel":
            self.panel_attrs = attributes
        if tag == "button" and attributes.get("@click") == "closeDetail()":
            self.back_found = True
            self.back_inside_panel = any(a.get("id") == "inbox-detail-panel" for _, a in self.stack)
        if tag not in {"input", "img", "br", "hr", "link", "meta", "source", "area", "wbr"}:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_mobile_back_is_outside_swap_target_and_old_composer_is_inert():
    # The target's innerHTML is replaced for every selection and Manage/history
    # request. Back must be its sibling so all those responses preserve it.
    template = (settings.BASE_DIR / "templates/inbox/feed.html").read_text()
    parser = _PanelParser()
    parser.feed(template)
    assert parser.back_found
    assert not parser.back_inside_panel
    assert parser.panel_attrs[":inert"] == "!detailReady"
    assert parser.panel_attrs["x-show"] == "detailReady"
    assert parser.panel_attrs[":aria-busy"] == "detailLoading"


@pytest.mark.parametrize("template", ["inbox/feed.html", "inbox/message_detail.html"])
def test_both_detail_surfaces_register_navigation_guards(template):
    source = (settings.BASE_DIR / "templates" / template).read_text()
    assert "js/inbox-navigation.js" in source
    assert "...inboxNavigation()" in source
    assert '@htmx:before-request.window="beforeDetailRequest($event)"' in source
    assert '@htmx:before-swap.window="beforeDetailSwap($event)"' in source
    assert '@htmx:after-request.window="afterDetailRequest($event)"' in source
    assert '@htmx:after-swap.window="afterDetailSwap($event)"' in source
    assert '@click="retryDetail()"' in source
    assert '@popstate.window="restoreDetailHistory($event)"' in source
    assert 'hx-history="false"' in source  # Never snapshot a hidden stale composer.


def test_navigation_javascript_state_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for inbox JavaScript state regression tests")
    script = Path(settings.BASE_DIR) / "tests/js/inbox_navigation.test.cjs"
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


_HISTORY_HEADERS = [
    {},
    {"HTTP_HX_REQUEST": "true"},
    {"HTTP_HX_HISTORY_RESTORE_REQUEST": "true"},
    {"HTTP_HX_REQUEST": "true", "HTTP_HX_HISTORY_RESTORE_REQUEST": "true"},
]


@pytest.mark.django_db
@pytest.mark.parametrize("headers", _HISTORY_HEADERS)
@pytest.mark.parametrize("page", ["feed", "message_detail"])
def test_history_cache_miss_returns_full_page(client, inbox_message, org_owner, headers, page):
    workspace = inbox_message.workspace
    WorkspaceMembership.objects.create(workspace=workspace, user=org_owner, workspace_role="owner")
    client.force_login(org_owner)
    url = f"/workspace/{workspace.id}/inbox/"
    if page == "message_detail":
        url += f"{inbox_message.id}/"
    response = client.get(url, **headers)
    assert response.status_code == 200
    full = not headers.get("HTTP_HX_REQUEST") or bool(headers.get("HTTP_HX_HISTORY_RESTORE_REQUEST"))
    partial = "_message_list" if page == "feed" else "_message_panel"
    expected = f"inbox/{page}.html" if full else f"inbox/partials/{partial}.html"
    assert response.templates[0].name == expected
    assert (b'hx-history="false"' in response.content) is full
    vary = {header.strip().lower() for header in response.headers["Vary"].split(",")}
    assert {"hx-request", "hx-history-restore-request"}.issubset(vary)


@pytest.mark.django_db
@pytest.mark.parametrize("headers", _HISTORY_HEADERS)
def test_history_requests_keep_auth_and_workspace_permissions(client, inbox_message, user, headers):
    base = f"/workspace/{inbox_message.workspace_id}/inbox/"
    urls = [base, f"{base}{inbox_message.id}/"]
    for url in urls:
        assert client.get(url, **headers).status_code == 302
    client.force_login(user)  # No workspace membership.
    for url in urls:
        assert client.get(url, **headers).status_code == 403
