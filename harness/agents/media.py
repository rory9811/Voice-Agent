"""Bounded recovery for media creation blocked by unavailable generation tools."""

import re

MEDIA_INSTRUCTIONS = """
For image/video creation, a specialized generative service is one implementation,
not a prerequisite for every task. Choose available tools that meet the objective.
For illustrations, posters, diagrams and simple animations, use local code when
appropriate (for example Pillow/SVG rendering, or rendered frames plus ffmpeg).
If a generation tool is absent, credentials are missing, or its network/service
fails, try feasible local rendering before declaring the task failed. Do not keep
retrying the same unavailable service. Execute the code and deliver actual files;
source code, prompts, and a still image alone do not fulfill a requested video.
Verify image decoding and video duration/frame count before reporting success.
Respect explicit style, fidelity and tool requirements. Never silently replace a
photorealistic result with a schematic/cartoon. If an approximation is allowed,
describe it honestly; unmet requirements mean partial (with useful artifacts) or
failed, not completed. Missing permissions or required input are not permission
to bypass restrictions. Do not install dependencies or invoke paid/external
services just to recover; use what is already available in the authorized workspace.
"""

MEDIA_RECOVERY = """The previous attempt did not deliver media and reported an
unavailable tool, service or credential. Make one recovery attempt for the ORIGINAL
objective using only available local coding/rendering tools. This is not a request
to repeat external API calls, change configuration, install software, obtain new
credentials, or bypass approvals. Network access is disabled for this attempt.
Inspect existing task files first and reuse useful work. If feasible, render and
verify the actual requested image/video and return its artifact paths. Preserve
all original requirements: do not substitute cartoons for required photorealism,
ignore required inputs, or claim a plan/source file is a completed media artifact.
If local tools cannot meet the objective, return the truthful limitation without
further retries. Return the same structured result format.
"""

_CREATE = re.compile(
    r"生成|制作|绘制|画一|画个|做一|做个|做成|渲染|设计|转成|转换|导出|"
    r"\b(?:generate|create|make|draw|render|design|animate|convert|export)\b", re.I
)
_MEDIA = re.compile(
    r"图片|图像|插画|海报|图表|动画|视频|动图|配图|示意图|"
    r"\b(?:images?|pictures?|illustrations?|posters?|diagrams?|charts?|animations?|videos?|gif|png|mp4|svg)\b", re.I
)
_DRAW = re.compile(r"画(?:一|个|只|张)|绘制|\b(?:draw|animate)\b", re.I)
_TOOL = re.compile(
    r"工具|服务|凭证|密钥|网络|接口|依赖|环境|"
    r"\b(?:tool|service|credential|api|key|network|dependency|dependencies|ffmpeg|image_gen|sora)\b", re.I
)
_UNAVAILABLE = re.compile(
    r"缺少|缺失|未配置|未提供|未安装|不可用|不支持|无法|不能|失败|没有|不具备|"
    r"\b(?:unavailable|missing|unsupported|unreachable|failed|failure|cannot)\b|"
    r"not (?:available|configured|installed)|no (?:access|tool|credential|api)", re.I
)


def should_recover_media(request, result):
    """Conservative eligibility; never retry successful work or existing artifacts.

    Only a completed Codex turn with a media-creation objective and an explicit
    capability failure qualifies. Transport failures/unknown execution are never
    replayed. The recovery turn itself decides whether local rendering can satisfy
    the original requirements.
    """
    if result.outcome not in {"failed", "partial"} or result.artifacts:
        return False
    if not (_DRAW.search(request.objective) or (
        _CREATE.search(request.objective) and _MEDIA.search(request.objective)
    )):
        return False
    reason = "\n".join((result.full_result, *result.unresolved))
    return bool(_TOOL.search(reason) and _UNAVAILABLE.search(reason))
