"""Deterministic lifecycle state transitions."""

from __future__ import annotations

from .models import DecisionValidationError, LifecycleAction, TaskStatus


ACTIVE_STATUSES = {
    TaskStatus.OPEN,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
}
CLOSED_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.CANCELLED,
    TaskStatus.CLOSED,
}


def next_status(
    current_status: str | TaskStatus,
    action: str | LifecycleAction,
) -> TaskStatus:
    status = TaskStatus(current_status)
    lifecycle_action = LifecycleAction(action)

    if lifecycle_action in {
        LifecycleAction.PROGRESS_UPDATE,
        LifecycleAction.MODIFY,
        LifecycleAction.TRANSFER,
    }:
        if status not in ACTIVE_STATUSES:
            raise DecisionValidationError(
                f"{status.value} 不能直接执行 {lifecycle_action.value}"
            )
        return TaskStatus.IN_PROGRESS
    if lifecycle_action is LifecycleAction.COMPLETE:
        if status not in ACTIVE_STATUSES:
            raise DecisionValidationError(
                f"{status.value} 不能重复完成"
            )
        return TaskStatus.COMPLETED
    if lifecycle_action is LifecycleAction.CANCEL:
        if status not in ACTIVE_STATUSES:
            raise DecisionValidationError(
                f"{status.value} 不能直接取消"
            )
        return TaskStatus.CANCELLED
    if lifecycle_action is LifecycleAction.REOPEN:
        if status not in CLOSED_STATUSES:
            raise DecisionValidationError(
                f"只有关闭状态才能 REOPEN，当前为 {status.value}"
            )
        return TaskStatus.IN_PROGRESS
    raise DecisionValidationError(
        f"{lifecycle_action.value} 不适用于已有任务状态迁移"
    )
