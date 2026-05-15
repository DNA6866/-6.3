import json
import os
import time
import urllib.parse
import urllib.request
import uuid


class ComfyAPIError(Exception):
    pass


class ComfyUIApi:
    def __init__(self, host="127.0.0.1", port=8188):
        self.host = host
        self.port = int(port)
        self.base_url = f"http://{host}:{int(port)}"
        self.client_id = str(uuid.uuid4())

    def _request_json(self, path, payload=None, timeout=30):
        url = self.base_url + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise ComfyAPIError(f"ComfyUI API 调用失败：{exc}")

    def system_stats(self):
        return self._request_json("/system_stats", timeout=5)

    def queue_prompt(self, workflow):
        payload = {"prompt": workflow, "client_id": self.client_id}
        data = self._request_json("/prompt", payload=payload, timeout=30)
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyAPIError(f"ComfyUI 没有返回任务编号：{data}")
        return prompt_id

    def history(self, prompt_id):
        return self._request_json(f"/history/{prompt_id}", timeout=10)

    def wait_for_images(self, prompt_id, output_dir, timeout=900, poll_interval=1.2):
        os.makedirs(output_dir, exist_ok=True)
        start = time.time()
        while time.time() - start < timeout:
            hist = self.history(prompt_id)
            item = hist.get(prompt_id)
            if item:
                status = item.get("status", {})
                if status.get("completed") is False and status.get("status_str") == "error":
                    raise ComfyAPIError("图片生成失败，请先点击“检测环境”，确认通过后再重试。")
                outputs = item.get("outputs", {})
                files = []
                for node_output in outputs.values():
                    for image in node_output.get("images", []):
                        files.append(self.download_image(image, output_dir))
                if files:
                    return files
            time.sleep(poll_interval)
        raise ComfyAPIError("图片生成等待超时，请检查 ComfyUI 是否仍在运行。")

    def download_image(self, image_info, output_dir):
        params = urllib.parse.urlencode({
            "filename": image_info.get("filename", ""),
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        })
        url = f"{self.base_url}/view?{params}"
        filename = image_info.get("filename") or f"image_{int(time.time())}.png"
        local_path = os.path.join(output_dir, filename)
        if os.path.exists(local_path):
            name, ext = os.path.splitext(filename)
            local_path = os.path.join(output_dir, f"{name}_{int(time.time())}{ext}")
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                with open(local_path, "wb") as f:
                    f.write(res.read())
            return local_path
        except Exception as exc:
            raise ComfyAPIError(f"图片下载失败：{exc}")

