"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class ConflictError(DomainError):
    """请求编号或业务状态与既有内容冲突。"""

    code = "conflict"
    status = 409


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class CapacityExceededError(DomainError):
    """目标时段剩余容量不足以容纳本次占位。"""

    code = "capacity_exceeded"
    status = 409


class OrderStateError(DomainError):
    """订单当前状态不允许执行该动作（如确认已取消的订单）。"""

    code = "order_state_invalid"
    status = 409
