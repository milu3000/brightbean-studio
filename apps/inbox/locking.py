"""Common DM send/ingestion serialization; callers must hold an atomic block."""

from apps.social_accounts.models import SocialAccount


def lock_dm_account(account_id, workspace_id):
    """Lock and refresh the account without following nullable/joined relations.

    A send keeps this lock until the returned outbound ID is committed. Inbound
    ingestion waits on the same lock before checking that ID, closing the gap
    where an unmarked provider echo can arrive before the send response.
    Workspace reassignment while waiting fails closed for the old scope.
    """
    return SocialAccount.objects.select_for_update().filter(pk=account_id, workspace_id=workspace_id).first()
