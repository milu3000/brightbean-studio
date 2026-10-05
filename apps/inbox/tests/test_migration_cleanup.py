"""Migration-test cleanup must survive both partial reversals and assertions."""

from unittest.mock import Mock, call

import pytest

from apps.inbox.tests import conftest as inbox_fixtures


@pytest.mark.parametrize(
    "failure", [None, AssertionError("synthetic failed assertion"), RuntimeError("Cannot reverse")]
)
def test_migration_cleanup_restores_all_graph_leaves_with_a_fresh_executor(monkeypatch, failure):
    heads = [("inbox", "synthetic_latest"), ("mcp", "synthetic_dependent_latest")]
    before, after = Mock(), Mock()
    before.loader.graph.leaf_nodes.return_value = heads
    executor = Mock(side_effect=[before, after])
    monkeypatch.setattr(inbox_fixtures, "MigrationExecutor", executor)
    # Drive the fixture's setup/teardown directly without connecting a database.
    cleanup = inbox_fixtures.restore_migrations.__wrapped__(transactional_db=None)
    next(cleanup)
    if failure is None:
        with pytest.raises(StopIteration):
            next(cleanup)
    else:
        with pytest.raises(type(failure), match=str(failure)):
            cleanup.throw(failure)
    assert executor.call_args_list == [call(inbox_fixtures.connection), call(inbox_fixtures.connection)]
    before.loader.graph.leaf_nodes.assert_called_once_with()
    before.migrate.assert_not_called()
    after.migrate.assert_called_once_with(heads)
