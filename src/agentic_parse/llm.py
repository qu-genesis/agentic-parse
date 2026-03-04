from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class LLMClient:
    def __init__(self) -> None:
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self._client = None
        self.token_input_total = 0
        self.token_output_total = 0
        if self.api_key:
            try:
                from openai import OpenAI  # type: ignore

                self._client = OpenAI(api_key=self.api_key)
            except Exception:
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _cache_path(self, cache_dir: Path, key: str) -> Path:
        return cache_dir / f"{key}.json"

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    def _record_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.token_input_total += max(0, int(input_tokens))
        self.token_output_total += max(0, int(output_tokens))

    def usage_snapshot(self) -> tuple[int, int]:
        return self.token_input_total, self.token_output_total

    def _cache_key(self, task: str, system_prompt: str, user_prompt: str) -> str:
        material = f"{self.model}|{task}|{system_prompt}|{user_prompt}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def text(
        self,
        task: str,
        cache_dir: Path,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int = 800,
        cache_key_override: str | None = None,
    ) -> str | None:
        key = cache_key_override or self._cache_key(task=task, system_prompt=system_prompt, user_prompt=user_prompt)
        cache_path = self._cache_path(cache_dir, key)
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return str(payload.get("text", ""))

        if not self.enabled:
            return None

        try:
            response = self._client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
                ],
                max_output_tokens=max_output_tokens,
            )
            text = getattr(response, "output_text", "") or ""
            self._record_usage(self._estimate_tokens(system_prompt + user_prompt), self._estimate_tokens(text))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"model": self.model, "task": task, "text": text}),
                encoding="utf-8",
            )
            return text
        except Exception:
            return None

    def json(
        self,
        task: str,
        cache_dir: Path,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int = 1400,
        cache_key_override: str | None = None,
    ) -> dict | None:
        key = cache_key_override or self._cache_key(task=task, system_prompt=system_prompt, user_prompt=user_prompt)
        cache_path = self._cache_path(cache_dir, key)
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            data = payload.get("json")
            return data if isinstance(data, dict) else None

        if not self.enabled:
            return None

        try:
            response = self._client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
                ],
                max_output_tokens=max_output_tokens,
            )
            text = (getattr(response, "output_text", "") or "").strip()
            if not text:
                return None
            self._record_usage(self._estimate_tokens(system_prompt + user_prompt), self._estimate_tokens(text))
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"model": self.model, "task": task, "json": data}),
                encoding="utf-8",
            )
            return data
        except Exception:
            return None

    def transcribe_audio(self, path: Path, model: str = "gpt-4o-mini-transcribe") -> list[dict] | None:
        if not self.enabled:
            return None
        try:
            with path.open("rb") as handle:
                result = self._client.audio.transcriptions.create(
                    model=model,
                    file=handle,
                    response_format="verbose_json",
                )
            segments = []
            raw_segments = getattr(result, "segments", None) or []
            for segment in raw_segments:
                start = float(getattr(segment, "start", 0.0))
                end = float(getattr(segment, "end", start))
                text = str(getattr(segment, "text", "")).strip()
                segments.append(
                    {
                        "start_ms": int(start * 1000),
                        "end_ms": int(end * 1000),
                        "text": text,
                    }
                )
            if not segments:
                text = str(getattr(result, "text", "")).strip()
                if text:
                    segments = [{"start_ms": 0, "end_ms": 0, "text": text}]
            approx_in = self._estimate_tokens(path.name)
            approx_out = self._estimate_tokens(" ".join([s["text"] for s in segments]))
            self._record_usage(approx_in, approx_out)
            return segments
        except Exception:
            return None


_CLIENT: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient()
    return _CLIENT
