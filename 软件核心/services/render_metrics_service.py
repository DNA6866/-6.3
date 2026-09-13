from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import threading
import time
from typing import Callable, Dict, Optional

from paths import logs_dir


_METRICS_WRITE_LOCK = threading.RLock()
_STAGE_ORDER = (
    "任务准备",
    "素材缓存",
    "字幕识别",
    "片段渲染",
    "快速合成",
    "转场合成",
    "水印烧录",
    "AI开场",
    "封面生成",
    "最终封装",
    "平台校验",
    "其他节点",
)


@dataclass
class StageMetric:
    seconds: float = 0.0
    calls: int = 0
    failures: int = 0


def classify_ffmpeg_stage(output_path: str) -> str:
    """根据中间文件名归类 FFmpeg 节点，避免业务代码重复打点。"""
    name = os.path.basename(str(output_path or "")).lower()
    if "_std" in name or "_sdr" in name or "标准化" in name:
        return "素材缓存"
    if "fastmix_" in name or "fast_mix_" in name or name.startswith("main_v_"):
        return "快速合成"
    if "_clip_" in name or name.startswith("temp_") and name.endswith(".ts"):
        return "片段渲染"
    if "trans_" in name or "xfade" in name:
        return "转场合成"
    if "wm_" in name or "watermark" in name:
        return "水印烧录"
    if "ai_intro" in name or "prefixed" in name:
        return "AI开场"
    if "cover_" in name:
        return "封面生成"
    if "temp_out_" in name or name.endswith(".mp4"):
        return "最终封装"
    return "其他节点"


class RenderStageProfiler:
    """记录单条成片各阶段耗时，并输出一行可机器分析的 JSONL。"""

    def __init__(
        self,
        job_id: str,
        emit: Optional[Callable[[str], None]] = None,
    ):
        self.job_id = str(job_id or "未命名任务")[:160]
        self.emit = emit
        self.started_at = time.monotonic()
        self.wall_started_at = time.time()
        self.metrics: Dict[str, StageMetric] = {}
        self._lock = threading.RLock()
        self._finished = False

    def record(self, stage: str, seconds: float, success: bool = True) -> None:
        name = str(stage or "其他节点")
        with self._lock:
            metric = self.metrics.setdefault(name, StageMetric())
            metric.seconds += max(0.0, float(seconds or 0.0))
            metric.calls += 1
            if not success:
                metric.failures += 1

    def record_ffmpeg(
        self,
        output_path: str,
        seconds: float,
        success: bool,
    ) -> None:
        self.record(classify_ffmpeg_stage(output_path), seconds, success)

    @contextmanager
    def stage(self, name: str):
        started = time.monotonic()
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            self.record(name, time.monotonic() - started, succeeded)

    def finish(self, success: bool) -> str:
        with self._lock:
            if self._finished:
                return ""
            self._finished = True
            total = max(0.0, time.monotonic() - self.started_at)
            accounted = sum(metric.seconds for metric in self.metrics.values())
            untracked = max(0.0, total - accounted)
            if untracked >= 0.001:
                metric = self.metrics.setdefault("任务准备", StageMetric())
                metric.seconds += untracked
                metric.calls += 1
            ordered_names = [name for name in _STAGE_ORDER if name in self.metrics]
            ordered_names.extend(
                name
                for name in self.metrics
                if name not in ordered_names
            )
            stage_text = "、".join(
                f"{name} {self.metrics[name].seconds:.1f}s/"
                f"{self.metrics[name].calls}次"
                + (
                    f"(失败{self.metrics[name].failures})"
                    if self.metrics[name].failures
                    else ""
                )
                for name in ordered_names
            ) or "无子节点记录"
            message = (
                f"[性能计时] {self.job_id}：总耗时 {total:.1f}s；"
                f"{stage_text}；结果：{'成功' if success else '未完成'}。"
            )
            payload = {
                "timestamp": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(self.wall_started_at),
                ),
                "job_id": self.job_id,
                "success": bool(success),
                "total_seconds": round(total, 4),
                "stages": {
                    name: {
                        "seconds": round(metric.seconds, 4),
                        "calls": metric.calls,
                        "failures": metric.failures,
                    }
                    for name, metric in self.metrics.items()
                },
            }
        if self.emit:
            try:
                self.emit(message)
            except Exception:
                pass
        self._append_jsonl(payload)
        return message

    @staticmethod
    def _append_jsonl(payload: Dict[str, object]) -> None:
        try:
            root = logs_dir()
            os.makedirs(root, exist_ok=True)
            path = os.path.join(
                root,
                f"render_performance_{time.strftime('%Y%m%d')}.jsonl",
            )
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with _METRICS_WRITE_LOCK, open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, TypeError, ValueError):
            pass
