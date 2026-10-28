"""Safe error classification; reporting never requires a working model."""

from dataclasses import asdict, dataclass
from uuid import uuid4

from harness.core.errors import ApiNotConfiguredError, ConfigurationError

from .messages import local_text


@dataclass(frozen=True)
class WorkFault:
    code: str
    stage: str
    message: str
    retryable: bool = False
    certainty: str = "failed"
    diagnostic_ref: str = ""

    def as_dict(self):
        return asdict(self)


def classify_fault(exc, stage="execution", language="zh"):
    """Use status/type first. Raw exception text is never returned to the user."""
    if getattr(exc, "__cause__", None) is not None and not getattr(
        exc, "status_code", 0
    ):
        return classify_fault(exc.__cause__, stage, language)
    status = getattr(exc, "status_code", 0) or getattr(
        getattr(exc, "response", None), "status_code", 0
    )
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    code = str(getattr(exc, "code", "")).lower()
    text = str(exc).lower()
    retryable, certainty = False, "failed"
    if isinstance(exc, ApiNotConfiguredError):
        kind, message = (
            "NOT_CONFIGURED",
            "所需后台服务尚未配置，任务无法继续。请配置对应服务或登录。",
        )
    elif status == 401 or code in {"invalid_api_key", "authentication_error"}:
        kind, message = (
            "AUTH_FAILED",
            "后台服务认证失败，任务无法继续。请检查服务凭据或登录状态。",
        )
    elif status == 403:
        kind, message = "ACCESS_DENIED", "当前账号没有使用所需后台能力的权限。"
    elif code in {"insufficient_quota", "quota_exceeded"} or (
        status == 429 and "quota" in text
    ):
        kind, message = (
            "QUOTA_EXHAUSTED",
            "后台服务额度不足，暂时无法继续，请检查服务额度。",
        )
    elif status == 429:
        kind, message, retryable = "RATE_LIMITED", "后台服务暂时受限。", True
    elif status and 500 <= status < 600:
        kind, message, retryable = "SERVICE_UNAVAILABLE", "后台服务暂时不可用。", True
    elif isinstance(exc, TimeoutError):
        kind, message, certainty = (
            "TIMEOUT",
            "当前阶段等待超时，暂时无法确认执行结果。",
            "unknown",
        )
    elif isinstance(exc, (ConnectionError, BrokenPipeError)) or "disconnected" in text:
        kind, message, certainty = (
            "DISCONNECTED",
            "后台连接已中断，最后一步是否完成尚未确认；不会自动重复执行。",
            "unknown",
        )
    elif isinstance(exc, FileNotFoundError):
        kind, message = (
            "RESOURCE_MISSING",
            "所需文件或后台程序不存在，请检查任务输入和服务配置。",
        )
    elif isinstance(exc, PermissionError):
        kind, message = "FILESYSTEM_DENIED", "没有读取输入或保存结果所需的文件权限。"
    elif isinstance(exc, OSError) and exc.errno == 28:
        kind, message = "STORAGE_FULL", "存储空间不足，无法保存任务结果。"
    elif isinstance(exc, ConfigurationError):
        kind, message = (
            "CONFIGURATION_INVALID",
            "后台服务配置无效，需修正配置后才能处理。",
        )
    elif "continuation" in text or "resume" in text:
        kind, message = (
            "CONTINUATION_UNAVAILABLE",
            "原任务暂时无法续接，已保留可用结果，没有重新执行任务。",
        )
    elif stage == "routing":
        kind, message = (
            "ROUTING_FAILED",
            "暂时无法确定任务的处理方式，本次未开始后台执行。",
        )
    elif isinstance(exc, (ValueError, TypeError)):
        kind, message = (
            "INVALID_DATA",
            "任务输入或后台返回的数据格式不符合要求，暂时无法完成。",
        )
    else:
        kind, message = (
            "UNKNOWN_INTERNAL",
            "任务没有完成，你可以调整请求后再试一次。",
        )
    if kind == "TIMEOUT" and stage in {"queued", "routing", "waiting_agent"}:
        message, certainty = "等待后台处理超时，本次尚未进入任务执行。", "not_started"
    return WorkFault(
        kind,
        stage,
        local_text(message, language),
        retryable,
        certainty,
        uuid4().hex[:12],
    )
