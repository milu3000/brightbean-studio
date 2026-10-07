"""URL patterns for the Unified Social Inbox."""

from django.urls import path

from . import basic_views, canonical_views, message_details, unassigned_views, views

app_name = "inbox"

urlpatterns = [
    path("basic/unassigned/", basic_views.basic_unassigned_feed, name="basic_unassigned_feed"),
    path("basic/unassigned/<uuid:message_id>/", basic_views.basic_unassigned_detail, name="basic_unassigned_detail"),
    path(
        "basic/unassigned/<uuid:message_id>/content/",
        basic_views.basic_unassigned_content,
        name="basic_unassigned_content",
    ),
    path(
        "basic/<uuid:conversation_id>/messages/<uuid:message_id>/",
        basic_views.basic_message_content,
        name="basic_message_content",
    ),
    path(
        "basic/preserved/<uuid:message_id>/content/",
        basic_views.basic_preserved_content,
        name="basic_preserved_content",
    ),
    path(
        "basic/preserved/<uuid:message_id>/related/",
        basic_views.basic_preserved_related,
        name="basic_preserved_related",
    ),
    path("basic/", basic_views.basic_feed, name="basic_feed"),
    path("basic/preserved/", basic_views.basic_preserved_feed, name="basic_preserved_feed"),
    path("basic/preserved/<uuid:message_id>/", basic_views.basic_preserved_detail, name="basic_preserved_detail"),
    path("basic/<uuid:conversation_id>/", basic_views.basic_detail, name="basic_detail"),
    path("accounts/<uuid:account_id>/dm-send-status/", views.dm_send_gate_status, name="dm_send_status"),
    path("unassigned/", unassigned_views.feed, name="unassigned_feed"),
    path("unassigned/<uuid:message_id>/", unassigned_views.detail, name="unassigned_detail"),
    path("unassigned/<uuid:message_id>/content/", message_details.detail, name="unassigned_message_content"),
    # Main inbox feed
    path("", views.inbox_feed, name="feed"),
    path("conversations/<uuid:conversation_id>/", canonical_views.detail, name="conversation_detail"),
    path("conversations/<uuid:conversation_id>/read/", canonical_views.acknowledge_read, name="conversation_read_ack"),
    path("conversations/<uuid:conversation_id>/draft/", canonical_views.save_draft, name="conversation_save_draft"),
    path("conversations/<uuid:conversation_id>/reply/", canonical_views.send_reply, name="conversation_send_reply"),
    path("conversations/<uuid:conversation_id>/done/", canonical_views.mark_done, name="conversation_mark_done"),
    path(
        "conversations/<uuid:conversation_id>/retire-failed/",
        canonical_views.retire_failed,
        name="conversation_retire_failed",
    ),
    path(
        "conversations/<uuid:conversation_id>/retire-draft/",
        canonical_views.retire_draft,
        name="conversation_retire_draft",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages/<uuid:message_id>/content/",
        message_details.detail,
        name="conversation_message_content",
    ),
    path(
        "conversations/<uuid:conversation_id>/drafts/",
        canonical_views.pending_history,
        name="conversation_pending_history",
    ),
    # Message detail + thread
    path("<uuid:message_id>/", views.message_detail, name="message_detail"),
    path("<uuid:message_id>/native-thread/", views.native_thread_refresh, name="native_thread_refresh"),
    # Reply to message
    path("<uuid:message_id>/reply/", views.send_reply, name="send_reply"),
    # Draft replies
    path("<uuid:message_id>/reply/draft/", views.save_reply_draft, name="save_reply_draft"),
    path("replies/<uuid:reply_id>/edit/", views.update_reply_draft, name="update_reply_draft"),
    path("replies/<uuid:reply_id>/send/", views.send_reply_draft, name="send_reply_draft"),
    path("replies/<uuid:reply_id>/discard/", views.discard_reply_draft, name="discard_reply_draft"),
    path("replies/<uuid:reply_id>/review-delivery/", views.review_reply_outcome, name="review_reply_outcome"),
    # Internal notes
    path("<uuid:message_id>/note/", views.add_note, name="add_note"),
    # Assignment
    path("<uuid:message_id>/assign/", views.assign_message, name="assign"),
    # Status changes
    path("<uuid:message_id>/status/", views.change_status, name="change_status"),
    # Sentiment override
    path("<uuid:message_id>/sentiment/", views.change_sentiment, name="change_sentiment"),
    # Bulk actions
    path("bulk-action/", views.bulk_action, name="bulk_action"),
    # Saved replies
    path("saved-replies/", views.saved_replies_list, name="saved_replies"),
    path("saved-replies/create/", views.saved_reply_create, name="saved_reply_create"),
    path("saved-replies/<uuid:reply_id>/edit/", views.saved_reply_edit, name="saved_reply_edit"),
    path("saved-replies/<uuid:reply_id>/delete/", views.saved_reply_delete, name="saved_reply_delete"),
    # SLA config
    path("sla-config/", views.sla_config, name="sla_config"),
]
