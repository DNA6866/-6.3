from dataclasses import dataclass
import json
import time
from typing import Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GLM_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_MODELS = (
    "glm-4.7-flash",
    "glm-4-flash-250414",
)


MODE_LABELS: Dict[str, str] = {
    "spoken_script": "口播文案生成",
    "subtitle_polish": "字幕润色",
    "cover_title": "封面标题",
    "selling_points": "随机卖点",
}


SYSTEM_PROMPTS = {
    "spoken_script": (
        "你是中国电商短视频口播编导。根据用户资料写自然、具体、可直接朗读的中文口播稿。"
        "不要编造产品没有提供的功效、认证、价格或赠品，不要输出分析过程。"
    ),
    "subtitle_polish": (
        "你是短视频字幕编辑。只修正错别字、口语断句、标点和冗余语气词，保持原意和事实。"
        "输出润色后的正文，不要解释，不要新增原文没有的承诺。"
    ),
    "cover_title": (
        "你是电商短视频封面标题编辑。生成短、醒目、真实的中文标题，避免绝对化和虚假承诺。"
        "每行一个标题，不要编号，不要解释。"
    ),
    "selling_points": (
        "你是电商视频卖点编辑。根据资料生成可用于9:16文字水印的短卖点。"
        "每组卖点用空行分隔，组内每行一句；不得编造资料中没有的功能、价格或资质。"
    ),
}


@dataclass(frozen=True)
class GLMRequestConfig:
    api_key: str
    model: str = GLM_MODELS[0]
    timeout_sec: int = 90
    temperature: float = 0.75
    max_tokens: int = 1800


def build_user_prompt(
    mode: str,
    source_text: str,
    product_name: str = "",
    audience: str = "",
    style: str = "自然口语",
    count: int = 6,
) -> str:
    label = MODE_LABELS.get(mode)
    if not label:
        raise ValueError(f"未知文案模式：{mode}")
    source_text = str(source_text or "").strip()
    if not source_text:
        raise ValueError("请先填写产品资料或待处理文本")
    count = max(1, min(30, int(count or 1)))
    common = (
        f"任务：{label}\n"
        f"产品名称：{str(product_name or '未填写').strip()}\n"
        f"目标人群：{str(audience or '通用人群').strip()}\n"
        f"表达风格：{str(style or '自然口语').strip()}\n"
        f"原始资料：\n{source_text}\n"
    )
    if mode == "spoken_script":
        return common + "要求：输出一版约150至260字的完整口播稿，开头直接进入用户痛点，结尾自然收束。"
    if mode == "subtitle_polish":
        return common + "要求：保留原文信息顺序，按短视频阅读节奏断句，只输出润色后的正文。"
    if mode == "cover_title":
        return common + f"要求：输出{count}个标题，每个不超过16个汉字，每行一个。"
    return common + (
        f"要求：输出{count}组卖点，每组2至3行，每行不超过12个汉字；组与组之间留一个空行。"
    )


class GLMClient:
    def __init__(self, config: GLMRequestConfig):
        api_key = str(config.api_key or "").strip()
        if not api_key:
            raise ValueError("请填写智谱 API Key")
        self.config = config

    def generate(self, mode: str, user_prompt: str) -> str:
        payload = {
            "model": self.config.model if self.config.model in GLM_MODELS else GLM_MODELS[0],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPTS[mode]},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "temperature": max(0.1, min(1.0, float(self.config.temperature))),
            "max_tokens": max(128, min(4096, int(self.config.max_tokens))),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = None
        for attempt in range(2):
            request = Request(
                GLM_ENDPOINT,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.config.api_key.strip()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Niuyeye-Video-Toolbox/6.3",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=max(15, int(self.config.timeout_sec))) as response:
                    response_text = response.read().decode("utf-8", errors="replace")
                data = json.loads(response_text)
                choices = data.get("choices") if isinstance(data, dict) else None
                message = choices[0].get("message", {}) if choices else {}
                content = message.get("content", "") if isinstance(message, dict) else ""
                if isinstance(content, list):
                    content = "\n".join(
                        str(item.get("text", "")) if isinstance(item, dict) else str(item)
                        for item in content
                    )
                content = str(content or "").strip()
                if not content:
                    raise RuntimeError("智谱接口返回成功，但没有生成文案")
                return content
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[-1200:]
                last_error = RuntimeError(f"智谱接口 HTTP {exc.code}：{detail or exc.reason}")
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    break
            except URLError as exc:
                last_error = RuntimeError(f"无法连接智谱接口：{exc.reason}")
            except (json.JSONDecodeError, TimeoutError, RuntimeError) as exc:
                last_error = exc
            if attempt == 0:
                time.sleep(1.2)
        raise RuntimeError(str(last_error or "智谱接口调用失败"))
