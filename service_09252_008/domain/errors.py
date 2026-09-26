"""领域错误类型。

所有业务异常都继承 :class:`DomainError`，接口边界据此映射 HTTP 状态码。
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """领域错误基类。"""

    code = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    """引用的实体不存在。"""

    code = "not_found"


class ValidationError(DomainError):
    """输入载荷不合法（缺字段、类型错误、朴素时间等）。"""

    code = "validation_error"


class BusinessRuleError(DomainError):
    """违反业务规则（前置培训不足、安全等级超限、运输周期不够等）。"""

    code = "business_rule_violation"


class StateError(DomainError):
    """当前状态不允许执行该操作。"""

    code = "invalid_state"


class ConflictError(DomainError):
    """资源冲突（互斥资源被占用、容量已满、库存被并发锁定等）。"""

    code = "resource_conflict"


class IdempotencyConflict(DomainError):
    """幂等键已被不同载荷使用。"""

    code = "idempotency_conflict"


class BookingImmutableError(StateError):
    """预约已不可变更（材料已发运或流程已终结）。"""

    code = "booking_immutable"
