"""凭证管理接口：维护认证凭证，覆盖提交生效、启用凭证、标记失效等动作。

凭据状态只能按待生效、生效、失效推进；启用时生成的认证窗口随凭据内容、
有效期与告警设置一起落库，过期窗口回写到端点监控待办区。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.auth import AuthService

router = APIRouter(prefix="/api/auth", tags=["凭证管理"])

service = AuthService()

LIST_FIELDS = ["凭证编号", "关联端点", "认证类型", "凭证密钥", "轮换策略", "有效期天数", "过期告警", "自动刷新"]
STATUSES = ["草稿", "待生效", "生效", "失效"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按凭证编号检索"),
    status: str | None = Query(default=None, description="草稿、待生效、生效、失效"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按凭证编号与状态过滤凭证管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/monitor-todos")
def monitor_todos() -> dict[str, Any]:
    """端点监控待办区：读取凭据过期窗口回写过来的待办记录。"""
    items = service.list_monitor_todos()
    return {"module": "auth", "total": len(items), "items": items}


@router.get("/batches/{batch_no}/window")
def batch_window(batch_no: str) -> dict[str, Any]:
    """按采集批次反查它实际使用的完整认证窗口，解释批次用了哪把密钥。"""
    entry = service.window_for_batch(batch_no)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集批次 {batch_no} 尚未绑定任何认证窗口")
    return entry


@router.post("/{entry_id}/batches", response_model=ActionResult)
def bind_batch(entry_id: int, payload: EntryPayload) -> ActionResult:
    """把采集批次绑定到凭据当前的完整认证窗口；一个批次只能使用一个窗口。"""
    batch_no = str(payload.values.get("批次编号") or payload.values.get("batch_no") or "").strip()
    entry, message = service.bind_batch(entry_id, batch_no)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/batch-finish", response_model=ActionResult)
def finish_batch(entry_id: int, payload: EntryPayload) -> ActionResult:
    """采集批次结束后解除在途标记，下一批次即可等待并启用新凭据。"""
    batch_no = str(payload.values.get("批次编号") or payload.values.get("batch_no") or "").strip()
    entry, message = service.finish_batch(entry_id, batch_no)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出凭证管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "auth", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条认证凭证明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"认证凭证 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条认证凭证，凭据内容、有效期、轮换策略与告警设置一起落库。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="认证凭证已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条认证凭证执行提交生效、启用凭证、标记失效；状态只能单向推进。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
