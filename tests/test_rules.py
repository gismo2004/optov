"""Display rules: which datapoints a controller's equipment hides."""

from catalog_db import _hidden_by_rule_trees


def group(gid, kind, parent=None, target=None):
    return {
        "id": gid,
        "parent_id": parent,
        "type": kind,
        "target_event_type_id": target,
        "target_group_id": None,
    }


def cond(cid, gid, holds):
    return {"id": cid, "group_id": gid, "holds": holds}


def hidden(groups, conditions):
    return _hidden_by_rule_trees(groups, conditions, lambda c: c["holds"])[0]


def test_an_all_group_hides_only_when_every_condition_holds():
    # "No internal solar and no Vitosolic 100 and no Vitosolic 200" on a unit with internal
    # solar: two of three hold, so the pump's counter stays.
    groups = [group(1, 1, target=6826)]
    assert (
        hidden(groups, [cond(1, 1, False), cond(2, 1, True), cond(3, 1, True)]) == set()
    )
    assert hidden(groups, [cond(1, 1, True), cond(2, 1, True), cond(3, 1, True)]) == {
        6826
    }


def test_an_any_group_hides_when_one_condition_holds():
    groups = [group(1, 2, target=7)]
    assert hidden(groups, [cond(1, 1, False), cond(2, 1, True)]) == {7}
    assert hidden(groups, [cond(1, 1, False), cond(2, 1, False)]) == set()


def test_a_child_group_counts_as_one_member_of_its_parent():
    # all(A, any(B, C)): hidden when A holds and at least one of B, C does.
    groups = [group(1, 1, target=9), group(2, 2, parent=1)]
    assert hidden(groups, [cond(1, 1, True), cond(2, 2, False), cond(3, 2, True)]) == {
        9
    }
    assert (
        hidden(groups, [cond(1, 1, True), cond(2, 2, False), cond(3, 2, False)])
        == set()
    )


def test_several_rules_on_one_target_hide_it_when_any_holds():
    groups = [group(1, 1, target=5), group(2, 1, target=5)]
    assert hidden(groups, [cond(1, 1, False), cond(2, 2, True)]) == {5}


def test_greater_or_equal_and_less_or_equal_compare_as_they_say():
    from catalog_db import _condition_holds

    assert _condition_holds(5, "ge", 5) and not _condition_holds(4, "ge", 5)
    assert _condition_holds(5, "le", 5) and not _condition_holds(6, "le", 5)
