"""业务模块注册表。

每个模块 = 一个可插拔的生意。模块不直接执行业务，而是声明：
- key/name/description/status
- actions：[{action, label, params_hint, task_type, needs_approval}]
控制台 POST /modules/{key}/run 时按声明创建任务，Worker 执行。

v0.1 约束：任何对外发送/收款/账号操作都必须走审批；模块只能产出草稿与记录。
"""
from ..db import now_iso

REGISTRY = {}


def register(key, name, description, status="ready", actions=()):
    REGISTRY[key] = {
        "key": key,
        "name": name,
        "description": description,
        "status": status,
        "actions": list(actions),
        "created_at": now_iso(),
    }


STATUS_LABELS = {
    "ready": "可运行",
    "experimental": "实验中",
    "disabled": "已停用",
}


def list_modules():
    out = []
    for m in REGISTRY.values():
        d = dict(m)
        d["status_label"] = STATUS_LABELS.get(m["status"], m["status"])
        out.append(d)
    return out


def get_module(key):
    return REGISTRY.get(key)
