"""E0 单元测试：Reducer / Validator / DSL 的确定性规则（设计文档 §4.6–4.8、§5.2）。"""
from pa.agenda import dsl
from pa.schemas import StatePatch, parse_ops
from pa.state import Reducer, StateStore, Validator, build_attrs

ATTRS = build_attrs()


def commit(store, ops, t, view=None, source="vlm", check=()):
    parsed, rej = parse_ops(ops)
    assert not rej, rej
    patch = StatePatch(patch_id=f"p{t}", source=source, t_event_ms=t, ops=parsed, view=view)
    ok, rejected = Validator(ATTRS).check(patch, store, set(check))
    delta, ctx = Reducer(ATTRS).apply(ok, patch, store, t)
    return delta, rejected, ctx


def base_store():
    s = StateStore()
    commit(s, [{"op": "new", "tmp": "n1", "type": "PERSON", "cls": "person"},
               {"op": "new", "tmp": "n2", "type": "PLACE", "cls": "desk"},
               {"op": "new", "tmp": "n3", "type": "PLACE", "cls": "door"},
               {"op": "new", "tmp": "n4", "type": "OBJECT", "cls": "phone"},
               {"op": "new", "tmp": "n5", "type": "OBJECT", "cls": "backpack"},
               {"op": "set", "key": "n4.location", "value": {"rel": "ON", "ref": "n2"}},
               {"op": "set", "key": "n5.location", "value": "ON n2"}], 1000)
    return s


def test_ids_and_string_values_are_normalized():
    s = base_store()
    assert set(s.entities) == {"person_1", "desk_1", "door_1", "phone_1", "backpack_1"}
    assert s.val("backpack_1.location") == {"rel": "ON", "ref": "desk_1"}


def test_pick_up_and_with_chain():
    s = base_store()
    commit(s, [{"op": "act", "type": "PUT_INTO", "subj": "person_1", "obj": "phone_1", "target": "backpack_1"}], 2000)
    assert not s.with_person("phone_1", "person_1")
    commit(s, [{"op": "act", "type": "PICK_UP", "subj": "person_1", "obj": "backpack_1"}], 3000)
    assert s.with_person("phone_1", "person_1")  # 手机在包里，包在人手上


def test_still_does_not_create_delta_and_gone_makes_unknown_keeping_last_seen():
    s = base_store()
    v = s.version
    d, _, _ = commit(s, [{"op": "still", "keys": ["phone_1.location"]}], 2000)
    assert d is None and s.version == v and s.beliefs["phone_1.location"].last_support_ms == 2000
    d, _, _ = commit(s, [{"op": "gone", "keys": ["phone_1.location"]}], 3000)
    b = s.beliefs["phone_1.location"]
    assert d is not None and b.status == "unknown" and b.value is None
    assert b.last_seen["value"] == {"rel": "ON", "ref": "desk_1"}


def test_check_omission_becomes_unsure_not_still():
    s = base_store()
    d, _, _ = commit(s, [], 2000, check=["phone_1.location"])
    b = s.beliefs["phone_1.location"]
    assert d is None and b.last_support_ms == 1000 and b.status == "holds"


def test_interaction_unobserved_makes_stale_then_still_restores():
    from pa.schemas import ViewInfo
    s = base_store()
    d, _, _ = commit(s, [{"op": "set", "key": "person_1.near", "value": "desk_1"}], 2000,
                     view=ViewInfo(visible_places=["door_1"], quality="partial"))
    assert s.beliefs["phone_1.location"].status == "stale"
    assert s.beliefs["phone_1.location"].stale_reason == "interaction_unobserved"
    commit(s, [{"op": "still", "keys": ["phone_1.location"]}], 3000)
    assert s.beliefs["phone_1.location"].status == "holds"


def test_time_guard_old_observation_does_not_override_newer():
    s = base_store()
    commit(s, [{"op": "act", "type": "PICK_UP", "subj": "person_1", "obj": "phone_1"}], 5000)
    d, _, ctx = commit(s, [{"op": "set", "key": "phone_1.location", "value": "ON desk_1"}], 4000)
    assert s.val("phone_1.location") == {"rel": "HELD_BY", "ref": "person_1"}
    assert any(i["reason"] == "stale_op" for i in ctx.ignored)


def test_heard_does_not_override_fresh_seen():
    s = base_store()
    d, _, ctx = commit(s, [{"op": "set", "key": "phone_1.location", "value": "ON door_1", "src": "heard"}], 1000)
    assert s.val("phone_1.location") == {"rel": "ON", "ref": "desk_1"}


def test_validator_rejects_unknown_entity_and_bad_rel_and_owner_from_vision():
    s = base_store()
    _, rej, _ = commit(s, [{"op": "set", "key": "ghost_1.location", "value": "ON desk_1"},
                           {"op": "set", "key": "phone_1.location", "value": {"rel": "FLOATING", "ref": "desk_1"}},
                           {"op": "set", "key": "phone_1.location", "value": "HELD_BY desk_1"},
                           {"op": "set", "key": "phone_1.owner", "value": "person_1", "src": "seen"}], 2000)
    reasons = [r["reason"] for r in rej]
    assert any(r.startswith("unknown_entity") for r in reasons)
    assert any(r.startswith("bad_rel") for r in reasons)
    assert "held_by_non_person" in reasons
    assert any(r.startswith("source_not_allowed") for r in reasons)
    assert s.val("phone_1.location") == {"rel": "ON", "ref": "desk_1"}


def test_transient_ttl_expires_to_unknown():
    s = base_store()
    commit(s, [{"op": "set", "key": "person_1.activity", "value": "packing"}], 2000)
    r = Reducer(ATTRS)
    assert r.tick(s, 9000) is None
    d = r.tick(s, 10500)
    assert d is not None and s.beliefs["person_1.activity"].status == "unknown"


def test_dsl_any_person():
    s = base_store()
    cond = {"fn": "any_person", "args": ["activity", ["leaving", "moving_to_exit"]]}
    assert not dsl.evaluate(cond, s, {}, 2000)
    commit(s, [{"op": "new", "tmp": "n1", "type": "PERSON", "desc": "另一个 ID 的同一人"},
               {"op": "set", "key": "n1.activity", "value": "moving_to_exit"}], 2000)
    assert dsl.evaluate(cond, s, {}, 2000)
    assert dsl.deps(cond, {}) == (set(), {"activity"})


def test_dsl_missing_slot_and_meta():
    s = base_store()
    assert not dsl.evaluate({"path": "person_1.activity", "ne": "leaving"}, s, {}, 2000)  # 缺失 → False
    assert dsl.evaluate({"path": "$i.location#status", "eq": "holds"}, s, {"i": "phone_1"}, 2000)
    assert dsl.evaluate({"not": {"fn": "with", "args": ["$i", "$p"]}}, s, {"i": "phone_1", "p": "person_1"}, 2000)
    keys, attrs = dsl.deps({"all": [{"path": "$p.activity", "eq": "x"}, {"fn": "with", "args": ["$i", "$p"]}]},
                           {"i": "phone_1", "p": "person_1"})
    assert keys == {"person_1.activity"} and attrs == {"location"}


def test_person_without_cls_and_concurrent_dedupe():
    s = StateStore()
    parsed, _ = parse_ops([{"op": "new", "tmp": "n1", "type": "PERSON", "desc": "左侧女性"},
                           {"op": "new", "tmp": "n2", "type": "PLACE", "cls": "desk"},
                           {"op": "set", "key": "n1.activity", "value": "sitting_working"}])
    pa_ = StatePatch(patch_id="a", source="vlm", t_event_ms=1000, slice_version=0, ops=parsed)
    ok, rej = Validator(ATTRS).check(pa_, s)
    Reducer(ATTRS).apply(ok, pa_, s, 1000)
    assert not rej and set(s.entities) == {"person_1", "desk_1"} and s.val("person_1.activity") == "sitting_working"
    # 补丁 B 也基于切片版本 0（与 A 并发在途）：再次“新建”桌子，另外还有第二张桌子和一个杯子
    parsed, _ = parse_ops([{"op": "new", "tmp": "n1", "type": "PLACE", "cls": "desk"},
                           {"op": "new", "tmp": "n2", "type": "PLACE", "cls": "desk"},
                           {"op": "new", "tmp": "n3", "type": "OBJECT", "cls": "cup", "at": "n1"}])
    pb = StatePatch(patch_id="b", source="vlm", t_event_ms=2000, slice_version=0, ops=parsed)
    ok, _ = Validator(ATTRS).check(pb, s)
    _, ctx = Reducer(ATTRS).apply(ok, pb, s, 2000)
    assert sorted(e for e in s.entities if e.startswith("desk")) == ["desk_1", "desk_2"]  # 一个绑定、一个新建
    assert s.val("cup_1.location") == {"rel": "ON", "ref": "desk_1"}
    assert any(i.get("reason") == "dedup_concurrent" for i in ctx.ignored)


def test_ttl_uses_perception_watermark():
    s = base_store()
    commit(s, [{"op": "set", "key": "person_1.activity", "value": "sitting_working"}], 2000)
    r = Reducer(ATTRS)
    # 墙钟已到 25 s，但感知只处理到 6 s：还没有“本该支持却没支持”的观察，不应过期
    assert r.tick(s, 25000, watermark_ms=6000) is None
    # 感知已处理到 10.5 s（超过 TTL 8 s），仍无支持 → 过期
    assert r.tick(s, 26000, watermark_ms=10500) is not None
    # 感知完全停滞时，靠 hard_grace 兜底
    s2 = base_store()
    commit(s2, [{"op": "set", "key": "person_1.activity", "value": "packing"}], 2000)
    assert r.tick(s2, 71000, watermark_ms=2000) is not None
    # 只统计“看得到这条信念”的观察：切片版本早于 support_version 的窗口不算
    s3 = base_store()
    commit(s3, [{"op": "set", "key": "person_1.activity", "value": "packing"}], 2000)
    sv = s3.beliefs["person_1.activity"].support_version
    commits = [(sv - 1, 30000), (sv, 6000)]  # 旧切片窗口已到 30 s，但能看到它的窗口只到 6 s
    wm = lambda v: max([t for k, t in commits if k >= v], default=None)  # noqa: E731
    assert r.tick(s3, 31000, watermark_ms=wm) is None
