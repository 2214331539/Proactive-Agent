"""配置加载：内置 default.yaml ← 用户 yaml ← 命令行覆盖；并读取项目根目录的 .env（不覆盖已有环境变量）。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT = Path(__file__).resolve().parents[1] / "config" / "default.yaml"


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | None = None, overrides: dict | None = None) -> dict:
    with open(DEFAULT, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if path:
        with open(path, encoding="utf-8") as f:
            cfg = deep_merge(cfg, yaml.safe_load(f) or {})
    return deep_merge(cfg, overrides or {})


def set_path(cfg: dict, dotted: str, value: Any) -> None:
    """--set perception.provider=openai 这类覆盖。"""
    cur = cfg
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    v: Any = value
    for cast in (int, float):
        try:
            v = cast(value)
            break
        except ValueError:
            continue
    if value in ("true", "false"):
        v = value == "true"
    if value in ("null", "none", "None"):
        v = None if parts[-1] not in ("provider",) else value
    cur[parts[-1]] = v
