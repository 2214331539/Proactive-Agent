"""条件 DSL（设计文档 §5.2）。JSON 表达，便于 LLM 生成、程序校验。

Cond := {"all":[...]} | {"any":[...]} | {"not": Cond}
      | {"path": "<$var|实体ID>.<属性>[.<字段>][#元信息]", "<cmp>": 值}   cmp ∈ eq ne in nin gt lt exists
      | {"fn": "with"|"present"|"visible"|"age_gt", "args": [...]}
缺失槽位或 value 为 None 时，除 exists 外的比较一律为 False。
"""
from __future__ import annotations

from typing import Any

from ..state.store import StateStore

CMPS = ("eq", "ne", "in", "nin", "gt", "lt", "exists")
FNS = ("with", "present", "visible", "age_gt", "any_person", "held_before")


def _bind(token: str, binds: dict[str, str]) -> str:
    return binds.get(token[1:], token) if isinstance(token, str) and token.startswith("$") else token


def _resolve(path: str, store: StateStore, binds: dict[str, str], now_ms: int) -> tuple[bool, Any]:
    """返回 (槽位是否存在, 取值)。"""
    path, _, meta = path.partition("#")
    parts = path.split(".")
    if len(parts) < 2:
        return False, None
    ent, attr, field = _bind(parts[0], binds), parts[1], (parts[2] if len(parts) > 2 else None)
    b = store.beliefs.get(f"{ent}.{attr}")
    if b is None:
        return False, None
    if meta:
        return True, {"status": b.status, "source": b.source, "age_ms": now_ms - b.last_support_ms, "rev": b.rev}.get(meta)
    v = b.value
    if field is not None:
        v = v.get(field) if isinstance(v, dict) else None
    return True, v


def evaluate(cond: dict | None, store: StateStore, binds: dict[str, str], now_ms: int) -> bool:
    if not cond:
        return True
    if "all" in cond:
        return all(evaluate(c, store, binds, now_ms) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, store, binds, now_ms) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], store, binds, now_ms)
    if "fn" in cond:
        args = [_bind(a, binds) for a in cond.get("args", [])]
        fn = cond["fn"]
        if fn == "with":
            return store.with_person(args[0], args[1])
        if fn == "present":
            return store.present(args[0])
        if fn == "visible":
            e = store.entities.get(args[0])
            return bool(e and e.last_visible_ms >= now_ms - (args[1] if len(args) > 1 else 3000))
        if fn == "age_gt":
            ok, age = _resolve(args[0] + "#age_ms", store, binds, now_ms)
            return ok and age is not None and age > args[1]
        if fn == "held_before":  # 该人拿过 / 放下过这个物品（物品与人的相关性）
            e = store.entities.get(args[0])
            return bool(e and e.last_holder == args[1])
        if fn == "any_person":  # 任一人物的某属性取值在列表中（第一人称视角下身份不稳定时的兜底）
            attr, values = args[0], args[1]
            return any(e.type == "PERSON" and not e.merged_into
                       and (b := store.beliefs.get(f"{pid}.{attr}")) is not None and b.status == "holds"
                       and b.value in values for pid, e in store.entities.items())
        raise ValueError(f"unknown fn {fn}")
    if "path" in cond:
        ok, v = _resolve(cond["path"], store, binds, now_ms)
        if "exists" in cond:
            return ok and (v is not None) == bool(cond["exists"])
        if not ok or v is None:
            return False
        for cmp in CMPS:
            if cmp in cond:
                arg = cond[cmp]
                arg = [_bind(a, binds) for a in arg] if isinstance(arg, list) else _bind(arg, binds)
                if cmp == "eq":
                    return v == arg
                if cmp == "ne":
                    return v != arg
                if cmp == "in":
                    return v in arg
                if cmp == "nin":
                    return v not in arg
                if cmp == "gt":
                    return v > arg
                if cmp == "lt":
                    return v < arg
        raise ValueError(f"no comparator in {cond}")
    raise ValueError(f"bad cond {cond}")


def deps(cond: dict | None, binds: dict[str, str]) -> tuple[set[str], set[str]]:
    """静态分析：返回 (槽位键集合, 通配属性集合)。with() 依赖所有 *.location。"""
    keys: set[str] = set()
    attrs: set[str] = set()
    if not cond:
        return keys, attrs
    if "all" in cond or "any" in cond:
        for c in cond.get("all", []) + cond.get("any", []):
            k, a = deps(c, binds)
            keys |= k
            attrs |= a
    elif "not" in cond:
        return deps(cond["not"], binds)
    elif "fn" in cond:
        args = [_bind(a, binds) for a in cond.get("args", [])]
        if cond["fn"] == "with":
            attrs.add("location")
        elif cond["fn"] == "present":
            keys.add(f"{args[0]}.presence")
        elif cond["fn"] == "age_gt":
            ent, attr = args[0].split(".")[:2]
            keys.add(f"{ent}.{attr}")
        elif cond["fn"] == "any_person":
            attrs.add(args[0])
        elif cond["fn"] == "held_before":
            attrs.add("location")
    elif "path" in cond:
        parts = cond["path"].partition("#")[0].split(".")
        keys.add(f"{_bind(parts[0], binds)}.{parts[1]}")
    return keys, attrs


def validate_cond(cond: dict | None, depth: int = 0) -> list[str]:
    """供慢环编译出的条件做语法校验，返回错误列表。"""
    errs: list[str] = []
    if cond is None or cond == {}:
        return errs
    if depth > 12:
        return ["too_deep"]
    if not isinstance(cond, dict):
        return ["not_object"]
    if "all" in cond or "any" in cond:
        for c in cond.get("all", []) + cond.get("any", []):
            errs += validate_cond(c, depth + 1)
    elif "not" in cond:
        errs += validate_cond(cond["not"], depth + 1)
    elif "fn" in cond:
        if cond["fn"] not in FNS:
            errs.append(f"bad_fn:{cond['fn']}")
    elif "path" in cond:
        if not any(c in cond for c in CMPS):
            errs.append("no_cmp")
    else:
        errs.append("unknown_node")
    return errs
