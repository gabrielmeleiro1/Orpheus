"""Voice recommendation helper for long-form audiobook narration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from modules.openai_compat import (
    completion_token_field,
    parse_chat_response_text,
    post_chat_completions_via_urlopen,
    supports_temperature_override,
)
from modules.ingestion.models import Document as IngestionDocument

class VoiceRecommender:
    """Suggest a narration voice from a short source excerpt and voice catalog."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key_env: str,
        api_model: Optional[str],
        timeout_seconds: int = 45,
        api_key: Optional[str] = None,
    ) -> None:
        self.api_base_url = (api_base_url or "").strip().rstrip("/")
        self.api_key_env = (api_key_env or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
        self.api_model = (api_model or "gpt-5-nano").strip() or "gpt-5-nano"
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    def suggest_for_book(
        self,
        *,
        source_path: Path,
        voices: list[dict[str, Any]],
        max_units: int = 3,
        max_chars: int = 5000,
    ) -> dict[str, Any]:
        """Return a structured recommendation payload for web/API surfaces."""
        if not voices:
            raise ValueError("No voices available for recommendation")

        sample = self._extract_book_sample(source_path=source_path, max_units=max_units, max_chars=max_chars)
        model_payload = self._request_model_suggestion(book_sample=sample["excerpt"], voices=voices)

        available_voice_ids = {str(v["id"]) for v in voices if v.get("id")}
        recommended_voice_id = str(model_payload.get("recommended_voice_id", "")).strip()
        if recommended_voice_id not in available_voice_ids:
            raise RuntimeError("Voice suggestion did not identify an available voice")

        return {
            "book_sample": sample,
            "book_profile": {
                "book_type": str(model_payload.get("book_type", "general")).strip() or "general",
                "tone": str(model_payload.get("tone", "balanced")).strip() or "balanced",
            },
            "voice_suggestion": {
                "recommended_voice_id": recommended_voice_id,
                "recommended_voice_style": (
                    str(model_payload.get("recommended_voice_style", "neutral long-form narration")).strip()
                    or "neutral long-form narration"
                ),
                "reasoning": (
                    str(model_payload.get("reasoning", "Selected for long-form audiobook clarity.")).strip()
                    or "Selected for long-form audiobook clarity."
                ),
            },
            "api_used": True,
        }

    def _extract_book_sample(
        self,
        *,
        source_path: Path,
        max_units: int,
        max_chars: int,
    ) -> dict[str, Any]:
        suffix = source_path.suffix.lower()
        if suffix == ".pdf":
            return self._extract_pdf_sample(source_path=source_path, max_chapters=max_units, max_chars=max_chars)
        if suffix == ".epub":
            return self._extract_epub_sample(source_path=source_path, max_chapters=max_units, max_chars=max_chars)
        if suffix == ".txt":
            return self._extract_text_sample(source_path=source_path, max_chars=max_chars)
        if suffix == ".md":
            return self._extract_markdown_sample(source_path=source_path, max_sections=max_units, max_chars=max_chars)
        raise ValueError(f"Unsupported source type for recommendation: {suffix}")

    def _extract_pdf_sample(
        self,
        *,
        source_path: Path,
        max_chapters: int,
        max_chars: int,
    ) -> dict[str, Any]:
        from modules.ingestion.pdf_parser import PDFParser

        document = PDFParser(source_path).parse()
        return self._sample_document(
            document=document,
            unit="chapters",
            max_units=max_chapters,
            max_chars=max_chars,
        )

    def _extract_epub_sample(
        self,
        *,
        source_path: Path,
        max_chapters: int,
        max_chars: int,
    ) -> dict[str, Any]:
        from modules.ingestion.epub_parser import EPUBParser

        document = EPUBParser(source_path).parse()
        return self._sample_document(
            document=document,
            unit="chapters",
            max_units=max_chapters,
            max_chars=max_chars,
        )

    def _extract_text_sample(
        self,
        *,
        source_path: Path,
        max_chars: int,
    ) -> dict[str, Any]:
        from modules.ingestion.text_parser import TextParser

        document = TextParser(source_path).parse()
        return self._sample_document(
            document=document,
            unit="sections",
            max_units=len(document.chapters),
            max_chars=max_chars,
        )

    def _extract_markdown_sample(
        self,
        *,
        source_path: Path,
        max_sections: int,
        max_chars: int,
    ) -> dict[str, Any]:
        from modules.ingestion.markdown_parser import MarkdownParser

        document = MarkdownParser(source_path).parse()
        return self._sample_document(
            document=document,
            unit="sections",
            max_units=max_sections,
            max_chars=max_chars,
        )

    def _sample_document(
        self,
        *,
        document: IngestionDocument,
        unit: str,
        max_units: int,
        max_chars: int,
    ) -> dict[str, Any]:
        chapters = [chapter for chapter in document.chapters[:max_units] if str(chapter.content).strip()]
        excerpt = "\n\n".join(str(chapter.content).strip() for chapter in chapters).strip()
        if not excerpt:
            raise ValueError("No readable text found in source document")

        return {
            "unit": unit,
            "units_analyzed": len(chapters),
            "total_units": len(document.chapters),
            "excerpt": excerpt[:max_chars],
        }

    def _request_model_suggestion(
        self,
        *,
        book_sample: str,
        voices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.api_base_url:
            raise RuntimeError("Cleaner API base URL is empty")

        api_key = self.api_key if self.api_key is not None else os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key in environment variable: {self.api_key_env}")

        compact_voices = [
            {
                "id": str(voice.get("id", "")),
                "name": str(voice.get("name", "")),
                "locale": str(voice.get("locale", voice.get("language", ""))),
                "description": str(voice.get("description", "")),
                "style_tags": list(voice.get("style_tags", [])),
            }
            for voice in voices
        ]
        prompt = (
            "Analyze this audiobook excerpt and available voices. "
            "Return only valid JSON with keys: "
            "book_type, tone, recommended_voice_id, recommended_voice_style, reasoning. "
            "book_type should be a short class such as serious nonfiction, romance, trading, war, memoir, fantasy. "
            "tone should be 1-3 words. reasoning should be one concise sentence.\n\n"
            f"EXCERPT:\n{book_sample}\n\n"
            f"VOICES:\n{json.dumps(compact_voices, ensure_ascii=True)}"
        )
        payload = {
            "model": self.api_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an audiobook casting assistant. "
                        "Pick one best voice for full-book narration from the provided list."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            completion_token_field(self.api_model): 600,
        }
        if supports_temperature_override(self.api_model):
            payload["temperature"] = 0.2
        body = self._post_chat_completions(payload=payload, api_key=api_key)
        content = self._extract_choice_content(body)
        parsed = self._parse_suggestion_content(content=content, voices=voices)
        if not isinstance(parsed, dict):
            raise RuntimeError("Suggestion response JSON must be an object")
        return parsed

    def _post_chat_completions(self, *, payload: dict[str, Any], api_key: str) -> str:
        return post_chat_completions_via_urlopen(
            base_url=self.api_base_url,
            api_key=api_key,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            http_error_prefix="Voice suggestion API",
            connection_error_prefix="Voice suggestion API connection failed",
        )

    @staticmethod
    def _extract_choice_content(response_body: str) -> str:
        return parse_chat_response_text(
            response_body,
            empty_choices_message="Voice suggestion API returned no choices",
            empty_content_message="Voice suggestion API returned empty content",
            parse_error_prefix="Voice suggestion API response parse failed",
        )

    @staticmethod
    def _extract_json_blob(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            stripped = stripped.replace("json", "", 1).strip()

        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Voice suggestion API did not return JSON")
        return stripped[start : end + 1]

    @classmethod
    def _parse_suggestion_content(
        cls,
        *,
        content: str,
        voices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Parse model output as JSON first, then as key-value plain text."""
        try:
            json_blob = cls._extract_json_blob(content)
            parsed = json.loads(json_blob)
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError("Suggestion response JSON must be an object")
        except Exception:
            parsed = cls._parse_key_value_suggestion(content=content, voices=voices)
            if parsed:
                return parsed
            raise RuntimeError("Voice suggestion API did not return JSON")

    @classmethod
    def _parse_key_value_suggestion(
        cls,
        *,
        content: str,
        voices: list[dict[str, Any]],
    ) -> Optional[dict[str, str]]:
        """
        Best-effort parser for non-JSON LLM replies.

        Accepts lines like:
          book type: general
          tone: balanced
          recommended voice: af_alloy
          recommended style: neutral long-form narration
          reason: ...
        """
        key_map = {
            "booktype": "book_type",
            "genre": "book_type",
            "tone": "tone",
            "mood": "tone",
            "recommendedvoice": "recommended_voice_id",
            "recommendedvoiceid": "recommended_voice_id",
            "voice": "recommended_voice_id",
            "voiceid": "recommended_voice_id",
            "recommendedstyle": "recommended_voice_style",
            "voicestyle": "recommended_voice_style",
            "narrationstyle": "recommended_voice_style",
            "reason": "reasoning",
            "reasoning": "reasoning",
            "why": "reasoning",
        }

        parsed: dict[str, str] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue

            line = line.lstrip("-*• ").strip()
            key, value = line.split(":", 1)
            normalized_key = re.sub(r"[^a-z0-9]+", "", key.lower())
            field = key_map.get(normalized_key)
            if not field:
                continue

            cleaned_value = value.strip().strip("`").strip()
            if cleaned_value:
                parsed[field] = cleaned_value

        if not parsed:
            return None

        voice_value = parsed.get("recommended_voice_id")
        if voice_value:
            parsed["recommended_voice_id"] = cls._resolve_voice_id(voice_value, voices)
        return parsed

    @staticmethod
    def _resolve_voice_id(candidate: str, voices: list[dict[str, Any]]) -> str:
        """Resolve free-form voice label/name to a concrete voice id when possible."""
        requested = candidate.strip()
        if not requested:
            return candidate

        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for voice in voices:
            voice_id = str(voice.get("id", "")).strip()
            voice_name = str(voice.get("name", "")).strip()
            if voice_id:
                by_id[voice_id.lower()] = voice_id
            if voice_name and voice_id:
                by_name[voice_name.lower()] = voice_id

        lowered = requested.lower()
        if lowered in by_id:
            return by_id[lowered]
        if lowered in by_name:
            return by_name[lowered]

        for voice_id_lower, voice_id in by_id.items():
            if voice_id_lower and voice_id_lower in lowered:
                return voice_id
        return requested
