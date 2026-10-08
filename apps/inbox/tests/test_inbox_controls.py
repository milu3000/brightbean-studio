"""Fresh regression checks for search controls and retired sentiment behavior."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.admin.sites import AdminSite
from django.urls import reverse

from apps.api.schemas import InboxMessageResponse
from apps.inbox.admin import InboxMessageAdmin
from apps.inbox.models import InboxMessage
from apps.inbox.tasks import InboxSyncEngine
from apps.inbox.tests.test_conversation_presentation import owner_client as _owner_client
from apps.inbox.tests.test_sync import _msg
from apps.social_accounts.models import SocialAccount

owner_client = _owner_client
pytestmark = pytest.mark.django_db


def feed(workspace):
    return reverse("inbox:feed", kwargs={"workspace_id": workspace.pk})


def test_retired_sentiment_query_is_explicitly_rejected(owner_client, inbox_workspace):
    response = owner_client.get(feed(inbox_workspace), {"sentiment": "negative"})
    assert response.status_code == 400 and b"retired" in response.content
    assert owner_client.get(feed(inbox_workspace), {"sentiment": ""}).status_code == 200


def test_retired_manual_post_cannot_change_historical_values(owner_client, inbox_message):
    inbox_message.sentiment = "negative"
    inbox_message.sentiment_source = "manual"
    inbox_message.save(update_fields=["sentiment", "sentiment_source"])
    url = reverse(
        "inbox:change_sentiment", kwargs={"workspace_id": inbox_message.workspace_id, "message_id": inbox_message.pk}
    )
    assert owner_client.post(url, {"sentiment": "positive"}).status_code == 410
    assert owner_client.get(url).status_code == 405
    inbox_message.refresh_from_db()
    assert (inbox_message.sentiment, inbox_message.sentiment_source) == ("negative", "manual")
    unknown = reverse(
        "inbox:change_sentiment", kwargs={"workspace_id": inbox_message.workspace_id, "message_id": uuid4()}
    )
    assert owner_client.post(unknown, {"sentiment": "positive"}).status_code == 404


def test_retired_sentiment_is_absent_from_forms_but_legacy_schema_is_deprecated(owner_client, inbox_message):
    html = owner_client.get(feed(inbox_message.workspace)).content.decode()
    assert 'name="sentiment"' not in html and "badge-sentiment" not in html
    detail = owner_client.get(
        reverse(
            "inbox:message_detail", kwargs={"workspace_id": inbox_message.workspace_id, "message_id": inbox_message.pk}
        )
    )
    assert "badge-sentiment" not in detail.content.decode()
    fields = InboxMessageAdmin(InboxMessage, AdminSite()).get_form(None).base_fields
    assert "sentiment" not in fields and "sentiment_source" not in fields
    assert InboxMessageResponse.model_json_schema()["properties"]["sentiment"]["deprecated"] is True


def test_polling_keeps_new_values_neutral_without_classifying_keywords(inbox_account):
    with patch("apps.inbox.tasks.get_provider") as provider, patch.object(InboxSyncEngine, "_notify_new_message"):
        provider.return_value.get_messages.return_value = [_msg("retired-keyword", text="This is terrible and awful")]
        InboxSyncEngine().sync_all()
    assert InboxMessage.objects.get(platform_message_id="retired-keyword").sentiment == "neutral"


def test_platforms_deduplicate_and_accounts_keep_names_handles_and_selected_id(owner_client, inbox_account):
    second = SocialAccount.objects.create(
        workspace=inbox_account.workspace,
        platform="facebook",
        account_platform_id="other",
        account_name="Other page",
        account_handle="otherpage",
    )
    response = owner_client.get(
        feed(inbox_account.workspace), {"platform": "facebook", "account": str(second.pk), "q": "coffee"}
    )
    html = response.content.decode()
    platform = html.split('name="platform"', 1)[1].split("</select>", 1)[0]
    account = html.split('name="account"', 1)[1].split("</select>", 1)[0]
    assert platform.count('value="facebook"') == 1
    assert "Other page · @otherpage" in account
    assert f'value="{second.pk}" data-platform="facebook" selected' in account
    assert 'data-inbox-clear-search aria-label="Clear search"' in html
    assert "inbox-filters.js" in html


def test_unavailable_account_filter_is_retained_without_exposing_a_foreign_name(owner_client, inbox_workspace):
    identifier = str(uuid4())
    response = owner_client.get(feed(inbox_workspace), {"account": identifier, "q": "coffee"})
    assert f'value="{identifier}" selected>Unavailable account' in response.content.decode()
    assert not response.context["inbox_messages"]


def test_permanent_architecture_copy_is_absent_from_conversation(owner_client, inbox_message, settings):
    settings.INBOX_CONVERSATION_PRESENTATION_ENABLED = True
    inbox_message.message_type = "dm"
    inbox_message.save(update_fields=["message_type"])
    response = owner_client.get(
        reverse(
            "inbox:message_detail", kwargs={"workspace_id": inbox_message.workspace_id, "message_id": inbox_message.pk}
        )
    )
    for text in (
        "stored incoming message",
        "Saved BrightBean history stays here",
        "Reply target:",
        "This reply stays attached",
    ):
        assert text not in response.content.decode()


def test_shortcut_control_is_only_on_the_reply_form_and_defaults_off(owner_client, inbox_message):
    response = owner_client.get(
        reverse(
            "inbox:message_detail", kwargs={"workspace_id": inbox_message.workspace_id, "message_id": inbox_message.pk}
        )
    )
    html = response.content.decode()
    assert html.count("data-inbox-reply-form") == 1
    assert html.count("data-inbox-send-button") == 1
    assert '<input type="checkbox" data-inbox-enter-send>' in html
    assert "Ctrl+Enter to send" in html and "inbox-composer.js" in html
    assert "Reply will be posted on" not in html


@pytest.mark.parametrize("identifier_kind", ["malformed", "missing", "foreign"])
def test_legacy_unavailable_account_filter_has_only_escaped_empty_state(owner_client, inbox_account, identifier_kind):
    from django.utils.html import escape

    from apps.inbox.tests.test_conversation_presentation import incoming
    from apps.workspaces.models import Workspace

    local = incoming(inbox_account, "local-private", body="LOCAL DM MUST NOT APPEAR")
    foreign_workspace = Workspace.objects.create(
        organization=inbox_account.workspace.organization, name="Foreign workspace"
    )
    foreign = SocialAccount.objects.create(
        workspace=foreign_workspace,
        platform="facebook",
        account_platform_id="foreign-native",
        account_name="FOREIGN ACCOUNT NAME",
    )
    incoming(foreign, "foreign-private", body="FOREIGN DM MUST NOT APPEAR")
    identifier = {
        "malformed": '"><script>alert("unsafe")</script>',
        "missing": str(uuid4()),
        "foreign": str(foreign.pk),
    }[identifier_kind]
    response = owner_client.get(feed(inbox_account.workspace), {"account": identifier})
    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["inbox_page"].paginator.count == 0
    assert response.context["active_filters"]["account"] == [identifier]
    assert f'value="{escape(identifier)}" selected>Unavailable account' in html
    assert local.body not in html and "FOREIGN DM MUST NOT APPEAR" not in html
    assert "FOREIGN ACCOUNT NAME" not in html and "<script>alert" not in html


def test_legacy_unavailable_account_in_multi_filter_never_expands_to_visible_account(owner_client, inbox_account):
    from apps.inbox.tests.test_conversation_presentation import incoming

    original = incoming(inbox_account, "visible-but-not-selected", body="UNSELECTED DM")
    identifiers = [str(inbox_account.pk), str(uuid4())]
    response = owner_client.get(feed(inbox_account.workspace), {"account": identifiers}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.context["inbox_page"].paginator.count == 0
    assert response.context["active_filters"]["account"] == identifiers
    assert original.body not in response.content.decode()


def test_legacy_empty_account_option_still_means_all_accounts(owner_client, inbox_account):
    from apps.inbox.tests.test_conversation_presentation import incoming

    original = incoming(inbox_account, "visible-blank-account")
    response = owner_client.get(feed(inbox_account.workspace), {"account": ""})
    assert response.status_code == 200
    assert [item.pk for item in response.context["inbox_messages"]] == [original.pk]


def test_legacy_unavailable_filter_still_rechecks_grants_after_render(owner_client, inbox_account):
    from apps.inbox import views
    from apps.members.models import WorkspaceMembership

    actual = views.render

    def revoke_after_render(*args, **kwargs):
        response = actual(*args, **kwargs)
        WorkspaceMembership.objects.filter(workspace=inbox_account.workspace).delete()
        return response

    with patch("apps.inbox.views.render", side_effect=revoke_after_render):
        response = owner_client.get(feed(inbox_account.workspace), {"account": str(uuid4())})
    assert response.status_code == 404
    assert inbox_account.account_name not in response.content.decode()
