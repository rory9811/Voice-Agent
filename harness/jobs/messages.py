"""Fixed user-facing messages; generated text follows the configured request language."""

POLISH_ERROR = "暂时查不到进度，请稍后再试。"

ENGLISH = {
    "暂时查不到进度，请稍后再试。": "I couldn't check the progress just now; please try again shortly.",
    "还在排队，轮到后就开始处理。": "It is queued and will start when a slot is free.",
    "我先确认一下怎么处理。": "I'm checking how to handle this.",
    "等上一项做完，就接着处理这项。": "I'll start this once the related task finishes.",
    "结果有了，我整理一下就告诉你。": "The result is ready; I'm putting the reply together.",
    "我还在处理，有结果就告诉你。": "I'm still working on it.",
    "我正在看你提供的内容。": "I'm reviewing what you shared.",
    "任务已取消。": "The task has been cancelled.",
    "任务执行失败。": "The task failed.",
    "任务已完成。": "The task is complete.",
    "任务已部分完成。": "The task is partially complete.",
    "已安排有限重试。": "A limited number of retries has been scheduled.",
    "已请求停止任务，但无法确认所有后台工具均已停止。": "The task was asked to stop, but not all background tools are confirmed stopped.",
    "结果未能确认送达，请查看已保存的任务结果。": "Delivery could not be confirmed. Please check the saved task result.",
    "Work 状态存储不可用；本次状态仅保存在内存中。": "Work state storage is unavailable; state is kept in memory only.",
    "Work 状态保存失败；已保留内存中的任务结果。": "Work state could not be saved; the task result is retained in memory.",
    "所需后台服务尚未配置，任务无法继续。请配置对应服务或登录。": "The required backend service is not configured. Configure it or sign in to continue.",
    "后台服务认证失败，任务无法继续。请检查服务凭据或登录状态。": "Backend authentication failed. Check the service credentials or sign-in status.",
    "当前账号没有使用所需后台能力的权限。": "This account does not have permission to use the required backend capability.",
    "后台服务额度不足，暂时无法继续，请检查服务额度。": "The backend service quota is exhausted. Please check the available quota.",
    "后台服务暂时受限。": "The backend service is temporarily rate limited.",
    "后台服务暂时不可用。": "The backend service is temporarily unavailable.",
    "当前阶段等待超时，暂时无法确认执行结果。": "This stage timed out; the execution result cannot currently be confirmed.",
    "后台连接已中断，最后一步是否完成尚未确认；不会自动重复执行。": "The backend connection was interrupted. The last step is unconfirmed and will not be repeated automatically.",
    "所需文件或后台程序不存在，请检查任务输入和服务配置。": "A required file or backend program is missing. Check the task inputs and service configuration.",
    "没有读取输入或保存结果所需的文件权限。": "File permissions do not allow reading the inputs or saving the results.",
    "存储空间不足，无法保存任务结果。": "There is not enough storage space to save the task result.",
    "后台服务配置无效，需修正配置后才能处理。": "The backend configuration is invalid and must be corrected before processing.",
    "原任务暂时无法续接，已保留可用结果，没有重新执行任务。": "The original task cannot currently be continued. Available results were retained; the task was not rerun.",
    "暂时无法确定任务的处理方式，本次未开始后台执行。": "The task handling method could not be determined. Backend execution has not started.",
    "任务输入或后台返回的数据格式不符合要求，暂时无法完成。": "The task input or backend response has an invalid format; the task could not be completed.",
    "任务没有完成，你可以调整请求后再试一次。": "The task did not finish. You can adjust your request and try again.",
    "等待后台处理超时，本次尚未进入任务执行。": "The wait for backend processing timed out; task execution has not started.",
}


def local_text(text, language="zh"):
    return ENGLISH.get(text, text) if str(language) == "en" else text


def polish_error(language="zh"):
    return local_text(POLISH_ERROR, language)
