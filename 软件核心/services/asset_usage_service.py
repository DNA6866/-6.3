import hashlib
import json
import os
import random
import threading
import time
from collections import Counter, deque

from paths import config_dir


ASSET_HISTORY_VERSION = 1
MAX_ASSET_RECORDS = 25000
MAX_SCOPE_RECORDS = 128
MAX_COMBINATIONS_PER_SCOPE = 200
SELECTION_SAMPLE_SIZE = 64
AUTO_FLUSH_JOBS = 8
AUTO_FLUSH_SECONDS = 30.0


def _normalized_path(path):
    value = os.path.abspath(str(path or ""))
    return os.path.normcase(os.path.normpath(value))


def _scope_key(scope_path, candidates):
    normalized_scope = _normalized_path(scope_path) if scope_path else ""
    if not normalized_scope:
        candidate_paths = [_normalized_path(path) for path in candidates if path]
        try:
            normalized_scope = os.path.commonpath(candidate_paths) if candidate_paths else ""
        except (OSError, ValueError):
            normalized_scope = os.path.dirname(candidate_paths[0]) if candidate_paths else ""
    digest = hashlib.sha1(
        normalized_scope.encode("utf-8", errors="ignore")
    ).hexdigest()
    return digest, normalized_scope


class AssetUsageScheduler:
    """在渲染线程内自动平衡素材新旧、使用次数和近期组合。"""

    def __init__(self, state_path=None, rng=None, now_func=None):
        self.state_path = state_path or os.path.join(config_dir(), "asset_usage_history.json")
        self.rng = rng or random.Random()
        self.now_func = now_func or time.time
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._jobs = {}
        self._session_counts = Counter()
        self._recent_paths = deque(maxlen=12)
        self._metadata_cache = {}
        self._dirty_jobs = 0
        self._last_flush = self.now_func()

    def _empty_state(self):
        return {
            "version": ASSET_HISTORY_VERSION,
            "assets": {},
            "scopes": {},
        }

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(state, dict):
            return self._empty_state()
        assets = state.get("assets") if isinstance(state.get("assets"), dict) else {}
        scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
        return {
            "version": ASSET_HISTORY_VERSION,
            "assets": assets,
            "scopes": scopes,
        }

    def begin_job(self, job_id, scope_path, candidates):
        key, normalized_scope = _scope_key(scope_path, candidates)
        with self._lock:
            self._jobs[str(job_id)] = {
                "scope_key": key,
                "scope_path": normalized_scope,
                "segments": [],
                "started_at": self.now_func(),
            }

    def _file_metadata(self, path):
        key = _normalized_path(path)
        cached = self._metadata_cache.get(key)
        if cached is not None:
            return cached
        try:
            stat = os.stat(path)
            metadata = (float(stat.st_mtime), int(stat.st_size))
        except OSError:
            metadata = (0.0, 0)
        self._metadata_cache[key] = metadata
        return metadata

    @staticmethod
    def _freshness_weight(age_days):
        if age_days <= 7.0:
            return 6.0
        if age_days <= 30.0:
            return 2.5
        return 1.0

    def _combination_prefix_penalty(self, job, candidate_key):
        if not job:
            return 1.0
        prefix = [segment.get("path", "") for segment in job.get("segments", [])]
        test_prefix = prefix + [candidate_key]
        scope = self._state.get("scopes", {}).get(job.get("scope_key", ""), {})
        combinations = scope.get("combinations", []) if isinstance(scope, dict) else []
        matches = 0
        for combo in combinations[-80:]:
            paths = combo.get("paths", []) if isinstance(combo, dict) else []
            if paths[:len(test_prefix)] == test_prefix:
                matches += 1
        if matches <= 0:
            return 1.0
        return max(0.03, 0.18 ** min(matches, 3))

    def choose(self, candidates, source_type="product", job_id=""):
        with self._lock:
            # 大素材池先固定抽样，再做权重计算；每次只评估少量候选，避免选一段素材扫描数千文件。
            candidate_pool = candidates or []
            if len(candidate_pool) > SELECTION_SAMPLE_SIZE:
                candidate_pool = self.rng.sample(
                    candidate_pool,
                    SELECTION_SAMPLE_SIZE,
                )
            unique = []
            seen = set()
            for path in candidate_pool:
                path = str(path or "")
                key = _normalized_path(path)
                if not path or not key or key in seen:
                    continue
                seen.add(key)
                unique.append((path, key))
            if not unique:
                return ""

            now = self.now_func()
            job = self._jobs.get(str(job_id))
            records = self._state.setdefault("assets", {})
            counts = [
                int((records.get(key) or {}).get("uses", 0) or 0)
                for _path, key in unique
            ]
            min_count = min(counts) if counts else 0
            weights = []
            for path, key in unique:
                record = records.get(key) if isinstance(records.get(key), dict) else {}
                mtime, size = self._file_metadata(path)
                stored_mtime = float(record.get("mtime", 0.0) or 0.0)
                stored_size = int(record.get("size", 0) or 0)
                if stored_mtime > 0 and (
                    abs(stored_mtime - mtime) > 1.0 or stored_size != size
                ):
                    record = {}
                age_days = max(0.0, (now - mtime) / 86400.0) if mtime > 0 else 365.0
                uses = int(record.get("uses", 0) or 0)
                last_used = float(record.get("last_used", 0.0) or 0.0)
                elapsed = max(0.0, now - last_used) if last_used > 0 else 10**9
                if elapsed < 15 * 60:
                    recent_penalty = 0.12
                elif elapsed < 6 * 3600:
                    recent_penalty = 0.35
                elif elapsed < 24 * 3600:
                    recent_penalty = 0.62
                else:
                    recent_penalty = 1.0
                session_penalty = 1.0 / ((1.0 + self._session_counts[key] * 0.25) ** 0.5)
                usage_penalty = 1.0 / ((1.0 + max(0, uses - min_count) * 0.25) ** 0.5)
                recent_paths = tuple(self._recent_paths)
                if recent_paths and key == recent_paths[-1]:
                    immediate_penalty = 0.01
                elif key in recent_paths[-4:]:
                    immediate_penalty = 0.18
                else:
                    immediate_penalty = 1.0
                prefix_penalty = self._combination_prefix_penalty(job, key)
                weight = (
                    self._freshness_weight(age_days)
                    * recent_penalty
                    * session_penalty
                    * usage_penalty
                    * immediate_penalty
                    * prefix_penalty
                )
                weights.append(max(0.0001, weight))

            selected_path, selected_key = self.rng.choices(unique, weights=weights, k=1)[0]
            self._session_counts[selected_key] += 1
            self._recent_paths.append(selected_key)
            return selected_path

    def record_segment(self, job_id, plan):
        if not isinstance(plan, dict):
            return
        source_path = str(plan.get("source_video") or plan.get("video") or "")
        if not source_path:
            return
        key = _normalized_path(source_path)
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job:
                return
            segment = {
                "path": key,
                "start_bucket": round(float(plan.get("start", 0.0) or 0.0) * 2.0) / 2.0,
                "duration_bucket": round(float(plan.get("duration", 0.0) or 0.0) * 2.0) / 2.0,
                "source_type": str(plan.get("source_type") or "product"),
            }
            job["segments"].append(segment)

    def finish_job(self, job_id, successful):
        should_flush = False
        with self._lock:
            job = self._jobs.pop(str(job_id), None)
            if not job or not successful:
                return
            segments = job.get("segments", [])
            if not segments:
                return
            now = self.now_func()
            assets = self._state.setdefault("assets", {})
            for segment in segments:
                key = segment.get("path", "")
                record = assets.get(key) if isinstance(assets.get(key), dict) else {}
                mtime, size = self._file_metadata(key)
                stored_mtime = float(record.get("mtime", 0.0) or 0.0)
                stored_size = int(record.get("size", 0) or 0)
                if stored_mtime > 0 and (
                    abs(stored_mtime - mtime) > 1.0 or stored_size != size
                ):
                    record = {}
                record["uses"] = int(record.get("uses", 0) or 0) + 1
                record["last_used"] = now
                record["last_source_type"] = segment.get("source_type", "product")
                record["mtime"] = mtime
                record["size"] = size
                assets[key] = record

            paths = [segment.get("path", "") for segment in segments]
            signature_payload = json.dumps(segments, ensure_ascii=False, sort_keys=True)
            signature = hashlib.sha1(signature_payload.encode("utf-8")).hexdigest()
            scopes = self._state.setdefault("scopes", {})
            scope_key = job.get("scope_key", "")
            scope = scopes.get(scope_key) if isinstance(scopes.get(scope_key), dict) else {}
            combinations = scope.get("combinations", []) if isinstance(scope.get("combinations"), list) else []
            combinations.append({
                "signature": signature,
                "paths": paths,
                "used_at": now,
            })
            scope["path"] = job.get("scope_path", "")
            scope["last_used"] = now
            scope["combinations"] = combinations[-MAX_COMBINATIONS_PER_SCOPE:]
            scopes[scope_key] = scope
            self._dirty_jobs += 1
            should_flush = (
                self._dirty_jobs >= AUTO_FLUSH_JOBS
                or now - self._last_flush >= AUTO_FLUSH_SECONDS
            )
        if should_flush:
            self.flush()

    def discard_pending(self):
        with self._lock:
            self._jobs.clear()

    def _prune_state(self):
        assets = self._state.get("assets", {})
        if len(assets) > MAX_ASSET_RECORDS:
            ordered = sorted(
                assets.items(),
                key=lambda item: float((item[1] or {}).get("last_used", 0.0) or 0.0),
                reverse=True,
            )
            self._state["assets"] = dict(ordered[:MAX_ASSET_RECORDS])
        scopes = self._state.get("scopes", {})
        if len(scopes) > MAX_SCOPE_RECORDS:
            ordered = sorted(
                scopes.items(),
                key=lambda item: float((item[1] or {}).get("last_used", 0.0) or 0.0),
                reverse=True,
            )
            self._state["scopes"] = dict(ordered[:MAX_SCOPE_RECORDS])

    def flush(self):
        with self._lock:
            if self._dirty_jobs <= 0:
                return
            self._prune_state()
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            temp_path = self.state_path + ".tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(temp_path, self.state_path)
            except OSError:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
                return
            self._dirty_jobs = 0
            self._last_flush = self.now_func()
