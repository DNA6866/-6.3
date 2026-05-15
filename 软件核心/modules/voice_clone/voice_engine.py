import os


class VoiceCloneError(Exception):
    pass


def humanize_error(error):
    text = str(error)
    lower = text.lower()
    if 'cuda' in lower:
        return "当前电脑未检测到可用的 NVIDIA CUDA 环境。请确认已安装 NVIDIA 官方显卡驱动，并且当前 PyTorch 是 GPU 版本。"
    if 'out of memory' in lower or '显存' in lower:
        return "当前显卡显存可能不足，AI音频克隆需要较高显存，建议使用 8GB 以上显存的 NVIDIA 显卡。"
    if 'model' in lower or 'pretrained' in lower:
        return "未找到 VoxCPM2 模型文件，请确认模型文件夹选择正确。"
    if 'audio' in lower or 'wav' in lower:
        return "参考音频无法读取，请更换为清晰的人声音频，建议 wav 或 mp3 格式。"
    return f"音频生成失败，请先点击“检测运行环境”，确认环境通过后再重试。底层信息：{text}"


class VoxCPM2Engine:
    """VoxCPM2 推理封装，UI 不直接调用模型。"""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None

    def load_model(self):
        if self.model is not None:
            return self.model
        if not self.model_path or not os.path.isdir(self.model_path):
            raise VoiceCloneError("模型路径错误")
        try:
            from voxcpm import VoxCPM
            self.model = VoxCPM.from_pretrained(self.model_path, load_denoiser=False)
            return self.model
        except Exception as e:
            raise VoiceCloneError(humanize_error(e))

    def _write_wav(self, wav, output_path):
        try:
            import soundfile as sf
            sample_rate = getattr(getattr(self.model, 'tts_model', None), 'sample_rate', 48000)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            sf.write(output_path, wav, sample_rate)
            return output_path
        except Exception as e:
            raise VoiceCloneError(humanize_error(e))

    def generate_tts(self, text: str, voice_description: str, output_path: str, cfg_value: float = 2.0, inference_timesteps: int = 10):
        model = self.load_model()
        prompt_text = text.strip()
        if voice_description.strip():
            prompt_text = f"({voice_description.strip()}){prompt_text}"
        try:
            wav = model.generate(
                text=prompt_text,
                cfg_value=float(cfg_value),
                inference_timesteps=int(inference_timesteps),
            )
            return self._write_wav(wav, output_path)
        except Exception as e:
            raise VoiceCloneError(humanize_error(e))

    def generate_clone(self, text: str, reference_audio: str, control_prompt: str, output_path: str, cfg_value: float = 2.0, inference_timesteps: int = 10):
        model = self.load_model()
        if not os.path.exists(reference_audio):
            raise VoiceCloneError("参考音频无法读取，请更换为清晰的人声音频，建议 wav 或 mp3 格式。")
        prompt_text = text.strip()
        if control_prompt.strip():
            prompt_text = f"({control_prompt.strip()}){prompt_text}"
        try:
            wav = model.generate(
                text=prompt_text,
                reference_wav_path=reference_audio,
                cfg_value=float(cfg_value),
                inference_timesteps=int(inference_timesteps),
            )
            return self._write_wav(wav, output_path)
        except Exception as e:
            raise VoiceCloneError(humanize_error(e))

    def generate_ultimate_clone(self, text: str, reference_audio: str, prompt_text: str, output_path: str, cfg_value: float = 2.0, inference_timesteps: int = 10):
        model = self.load_model()
        try:
            wav = model.generate(
                text=text.strip(),
                prompt_wav_path=reference_audio,
                prompt_text=prompt_text.strip(),
                reference_wav_path=reference_audio,
                cfg_value=float(cfg_value),
                inference_timesteps=int(inference_timesteps),
            )
            return self._write_wav(wav, output_path)
        except Exception as e:
            raise VoiceCloneError(humanize_error(e))

    def unload_model(self):
        self.model = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
