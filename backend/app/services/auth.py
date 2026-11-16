"""凭证管理业务规则：状态流转、字段校验与筛选口径都收在这里。

凭据状态只能沿「待生效 -> 生效 -> 失效」单向推进，不允许把已失效的旧凭据
重新启用。每次启用都会生成一个完整的认证窗口（窗口编号、生效时间、过期时间），
凭据内容、有效期与告警设置随窗口一起落库，凭据生效记录凭窗口编号即可说明
某个采集批次究竟用了哪一把密钥。过期窗口会同步回写到端点监控待办区。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "auth"
MONITOR_MODULE = "monitor"

# 凭据编号、关联端点、认证类型是登记时的硬门槛；其余字段一起落库，不再静默丢弃。
REQUIRED_FIELDS = ["凭证编号", "关联端点", "认证类型"]
# 凭据内容、有效期、告警设置都属于认证窗口的组成部分，登记时必须整体保存。
WINDOW_FIELDS = ["凭证密钥", "轮换策略", "有效期天数", "过期告警", "自动刷新"]

# 状态只能按待生效、生效、失效推进；草稿仅用于尚未提交的登记记录。
STATUS_ORDER = ["草稿", "待生效", "生效", "失效"]
# 动作到目标状态的映射，状态名即凭据状态机里的规范名称。
ACTION_RULES = {"提交生效": "待生效", "启用凭证": "生效", "标记失效": "失效"}
# 允许的相邻/初始推进关系；目标状态只能是当前状态的后继，不能回退。
NEXT_STATUS: dict[str, str] = {
    "草稿": "待生效",
    "待生效": "生效",
    "生效": "失效",
}
NEGATIVE_ACTIONS: list[str] = []

# 采集批次在途时允许登记的批次绑定字段；每个批次只能绑定一个完整认证窗口。
BATCH_FIELD = "在途批次"
WINDOW_ID_FIELD = "认证窗口"
WINDOW_SEQ_FIELD = "窗口序号"
# 批次结束后仍保留的批次->窗口历史，保证凭据生效记录可追溯每批用了哪把密钥。
BATCH_HISTORY_FIELD = "批次窗口历史"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _parse_days(value: Any) -> int | None:
    """把有效期天数解析成正整数；解析不出来时返回 None，由调用方决定口径。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


class AuthService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("凭证编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 凭据内容、有效期天数、轮换策略、告警配置随登记整体落库，不再丢失。
        for field in WINDOW_FIELDS:
            entry[field] = values.get(field)
        entry[WINDOW_SEQ_FIELD] = 0
        entry[WINDOW_ID_FIELD] = None
        entry["生效时间"] = None
        entry["过期时间"] = None
        entry[BATCH_FIELD] = None
        entry[BATCH_HISTORY_FIELD] = []
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"认证凭证 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于凭证管理可执行范围"
        target = ACTION_RULES[action]
        current = str(entry.get("status") or "")
        expected = NEXT_STATUS.get(current)
        if expected is None:
            # 已失效凭据到达状态机终点，旧凭据不能被重新启用。
            return None, f"认证凭证已{current}，状态只能按待生效、生效、失效推进，不能重新启用"
        if target != expected:
            return None, (
                f"认证凭证当前为{current}，只能推进到{expected}，"
                f"不能直接变更为{target}"
            )

        if action == "启用凭证":
            message = self._activate(entry)
            if message:
                return None, message
        elif action == "标记失效":
            self._expire(entry)
        else:
            entry["status"] = target

        entry["pending"] = entry["status"] != "失效"
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 每次状态推进后，把过期窗口回写到端点监控待办区（含幂等去重）。
        self._sync_monitor_todo(entry)
        return entry, f"认证凭证已{action}"

    # ------------------------------------------------------------------
    # 认证窗口：启用时生成，凭据内容、有效期、告警设置随窗口一起落库
    # ------------------------------------------------------------------
    def _activate(self, entry: dict[str, Any]) -> str:
        """把待生效凭据推进为生效，并开启一个完整认证窗口。"""
        days = _parse_days(entry.get("有效期天数"))
        if days is None:
            return "有效期天数缺失或不是正整数，无法计算认证窗口的过期时间"

        # 在途批次仍绑定旧窗口时，新批次必须等待新凭据，冲突处理优先级待定，
        # 这里先拦下启用动作并给出可读说明，避免一把密钥被多个窗口共用。
        bound_batch = str(entry.get(BATCH_FIELD) or "").strip()
        if bound_batch and entry.get(WINDOW_ID_FIELD):
            return (
                f"在途批次 {bound_batch} 仍绑定原认证窗口 {entry[WINDOW_ID_FIELD]}，"
                "需等该批次结束后再启用新凭据"
            )

        seq = int(entry.get(WINDOW_SEQ_FIELD, 0)) + 1
        entry[WINDOW_SEQ_FIELD] = seq
        started_at = _now()
        expires_at = started_at + timedelta(days=days)
        # 窗口编号带上凭据编号与窗口序号，凭据生效记录可据此解释批次用了哪把密钥。
        entry[WINDOW_ID_FIELD] = f"{entry['凭证编号']}-W{seq:03d}"
        entry["生效时间"] = started_at.isoformat(timespec="minutes")
        entry["过期时间"] = expires_at.isoformat(timespec="minutes")
        entry["status"] = "生效"
        return ""

    def _expire(self, entry: dict[str, Any]) -> None:
        """把生效凭据推进为失效；认证窗口保留为历史记录，不允许再次启用。"""
        entry["status"] = "失效"

    def bind_batch(self, entry_id: int, batch_no: str) -> tuple[dict[str, Any] | None, str]:
        """把采集批次绑定到凭据当前的完整认证窗口，一个批次只能使用一个窗口。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"认证凭证 {entry_id} 不存在或已归档"
        batch_no = str(batch_no or "").strip()
        if not batch_no:
            return None, "采集批次编号不能为空"
        if entry.get("status") != "生效" or not entry.get(WINDOW_ID_FIELD):
            return None, "只有生效中的认证窗口才能绑定采集批次"
        # 已绑定同一批次视为重复提交，保持原窗口不变，保证一个批次只有一个窗口。
        previous = str(entry.get(BATCH_FIELD) or "").strip()
        if previous and previous != batch_no:
            return None, (
                f"认证窗口 {entry[WINDOW_ID_FIELD]} 已被批次 {previous} 占用，"
                "一个完整认证窗口不能同时服务多个批次"
            )
        entry[BATCH_FIELD] = batch_no
        # 同步写入批次->窗口历史，同一批次只保留一份，结束后仍可解释它用了哪把密钥。
        history = entry.setdefault(BATCH_HISTORY_FIELD, [])
        if not any(str(item.get("批次编号")) == batch_no for item in history):
            history.append({
                "批次编号": batch_no,
                "认证窗口": entry.get(WINDOW_ID_FIELD),
                "凭证密钥": entry.get("凭证密钥"),
                "生效时间": entry.get("生效时间"),
                "过期时间": entry.get("过期时间"),
            })
        return entry, f"批次 {batch_no} 已绑定认证窗口 {entry[WINDOW_ID_FIELD]}"

    def finish_batch(self, entry_id: int, batch_no: str) -> tuple[dict[str, Any] | None, str]:
        """批次结束后解除在途标记，下一批次即可等待并启用新凭据。

        批次与认证窗口的对应关系保留在批次窗口历史里，不会随在途标记一起清掉。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"认证凭证 {entry_id} 不存在或已归档"
        if str(entry.get(BATCH_FIELD) or "").strip() == str(batch_no).strip():
            entry[BATCH_FIELD] = None
        return entry, f"批次 {batch_no} 的在途绑定已解除"

    def window_for_batch(self, batch_no: str) -> dict[str, Any] | None:
        """按采集批次反查它实际使用的认证窗口，解释批次究竟用了哪把密钥。

        先查在途绑定，再查批次窗口历史，已结束批次同样能追溯到完整认证窗口。
        """
        batch_no = str(batch_no).strip()
        for row in store.rows(MODULE):
            if str(row.get(BATCH_FIELD) or "").strip() == batch_no:
                return row
        for row in store.rows(MODULE):
            for item in row.get(BATCH_HISTORY_FIELD, []) or []:
                if str(item.get("批次编号") or "").strip() == batch_no:
                    # 历史记录里带上窗口与密钥，并保留来源凭据编号便于回查。
                    result = dict(item)
                    result["凭证编号"] = row.get("凭证编号")
                    result["status"] = row.get("status")
                    return result
        return None

    # ------------------------------------------------------------------
    # 端点监控待办区：过期窗口回写，认证窗口同步回显旧记录，重复启用只留一份
    # ------------------------------------------------------------------
    def list_monitor_todos(self) -> list[dict[str, Any]]:
        """读取端点监控待办区里由凭据过期窗口生成的待办记录。"""
        prefix = "AUTH-EXPIRE-"
        return [
            row for row in store.rows(MONITOR_MODULE)
            if str(row.get("来源") or "") == MODULE and str(row.get("待办编号", "")).startswith(prefix)
        ]

    def _sync_monitor_todo(self, entry: dict[str, Any]) -> None:
        """凭据状态推进后同步待办区：失效凭据写入/更新一条，恢复生效则移除。

        同一张凭据（同一认证记录）无论重复启用/失效多少次，待办区只保留一份，
        认证窗口字段回显该凭据当前（最近一次）的窗口信息。
        """
        monitor_rows = store.rows(MONITOR_MODULE)
        todo_id = f"AUTH-EXPIRE-{entry['凭证编号']}"
        existing = next(
            (row for row in monitor_rows if str(row.get("待办编号") or "") == todo_id),
            None,
        )

        if entry.get("status") != "失效":
            if existing is not None:
                monitor_rows.remove(existing)
            return

        window_ref = entry.get(WINDOW_ID_FIELD) or "未生成认证窗口"
        if existing is None:
            todo = {
                "id": max((int(row.get("id", 0)) for row in monitor_rows), default=0) + 1,
                "待办编号": todo_id,
                "来源": MODULE,
                "关联端点": entry.get("关联端点"),
                "指标编号": f"MONI-AUTH-{entry['凭证编号']}",
                # 认证窗口同步回显旧记录：窗口编号、生效/过期时间与告警设置一并带出。
                "认证窗口": window_ref,
                "过期时间": entry.get("过期时间"),
                "过期告警": entry.get("过期告警"),
                "轮换策略": entry.get("轮换策略"),
                "待办事项": f"认证凭证 {entry['凭证编号']} 的窗口 {window_ref} 已过期，请轮换凭据",
                "status": "待处理",
                "pending": True,
                "abnormal": True,
            }
            monitor_rows.append(todo)
            return

        # 重复启用同一张历史凭据只更新同一份待办，不新增第二条。
        existing["关联端点"] = entry.get("关联端点")
        existing["认证窗口"] = window_ref
        existing["过期时间"] = entry.get("过期时间")
        existing["过期告警"] = entry.get("过期告警")
        existing["轮换策略"] = entry.get("轮换策略")
        existing["待办事项"] = (
            f"认证凭证 {entry['凭证编号']} 的窗口 {window_ref} 已过期，请轮换凭据"
        )
        existing["status"] = "待处理"
        existing["pending"] = True
        existing["abnormal"] = True
