"""Orchestration pipeline: composes Layer 1, 2, 3 compression.

Drop-in replacement for ``ContextCompactor``. Provides the same
``should_compact`` + ``compact`` interface, plus a new
``compress_tool_result`` entry point for Layer 1.
"""

from context_manager.macro_compressor import MacroCompressor
from context_manager.meso_compressor import MesoCompressor
from context_manager.micro_compressor import MicroCompressor, estimate_tokens
from context_manager.tracker import ProgressTracker
from logger import get_logger

logger = get_logger(__name__)


class ContextPipeline:
    """Three-layer context compression pipeline.

    Drop-in replacement for ``ContextCompactor``. Composes:

    - Layer 1 (Micro): per-message smart truncation
    - Layer 2 (Meso): section-level tool exchange summarization
    - Layer 3 (Macro): full context rebuild via LLM

    Layers run in cascade order (cheapest first). Each layer's output
    feeds into the next layer's input.
    """

    def __init__(
        self,
        micro_max_chars: int = 4000,
        micro_keep_head_lines: int = 10,
        micro_keep_tail_lines: int = 15,
        meso_message_threshold: int = 20,
        meso_token_threshold: int = 8000,
        meso_use_llm: bool = False,
        macro_token_threshold: int = 32000,
        keep_recent: int = 12,
        llm_provider=None,
        progress_tracker: ProgressTracker | None = None,
    ):
        self.micro = MicroCompressor(
            max_chars=micro_max_chars,
            keep_head_lines=micro_keep_head_lines,
            keep_tail_lines=micro_keep_tail_lines,
        )
        self.meso = MesoCompressor(
            meso_message_threshold=meso_message_threshold,
            meso_token_threshold=meso_token_threshold,
            keep_recent=keep_recent,
            use_llm=meso_use_llm,
            llm_provider=llm_provider,
        )
        self.macro = MacroCompressor(
            token_threshold=macro_token_threshold,
            keep_recent=keep_recent,
            llm_provider=llm_provider,
            progress_tracker=progress_tracker,
        )
        self._stats = {
            "micro_compressions": 0,
            "meso_compressions": 0,
            "macro_compressions": 0,
        }

    @property
    def stats(self) -> dict:
        """Return compression statistics."""
        return dict(self._stats)

    def compress_tool_result(self, tool_name: str, content: str) -> str:
        """Layer 1 entry point: compress a tool result at creation time.

        Called from ``_handle_tool_call()`` instead of the old dumb truncation.
        """
        result = self.micro.compress(tool_name, content)
        if len(result) < len(content):
            self._stats["micro_compressions"] += 1
        return result

    def should_compact(self, messages: list[dict]) -> bool:
        """Check if any layer needs to run.

        Returns True if Layer 2 OR Layer 3 would fire.
        (Layer 1 runs eagerly via ``compress_tool_result``, not checked here.)
        """
        return self.meso.should_compress(messages) or self.macro.should_compress(messages)

    def compact(self, messages: list[dict]) -> list[dict]:
        """Run the full compression pipeline.

        Cascade: Layer 1 (defensive re-pass) → Layer 2 → Layer 3.
        Each layer only runs if its threshold is met.
        """
        original_count = len(messages)
        original_tokens = sum(estimate_tokens(m.get("content") or "") for m in messages)

        # Layer 1: defensive re-pass on any uncompressed tool results
        micro_before = sum(
            1
            for m in messages
            if m.get("role") == "tool" and len(m.get("content") or "") > self.micro.max_chars
        )
        result = self._apply_micro_pass(messages)
        if micro_before > 0:
            logger.info(
                "Layer 1 (Micro defensive pass): compressed %d uncompressed tool messages",
                micro_before,
            )

        # Layer 2: section-level compression
        if self.meso.should_compress(result):
            before = len(result)
            result = self.meso.compress(result)
            if len(result) < before:
                self._stats["meso_compressions"] += 1
                logger.info("Layer 2 (Meso): %d -> %d messages", before, len(result))

        # Layer 3: full context rebuild (only if still too large)
        if self.macro.should_compress(result):
            before = len(result)
            result = self.macro.compress(result)
            if len(result) < before:
                self._stats["macro_compressions"] += 1
                logger.info("Layer 3 (Macro): %d -> %d messages", before, len(result))

        final_tokens = sum(estimate_tokens(m.get("content") or "") for m in result)
        logger.info(
            "Pipeline: %d msgs (~%d tokens) -> %d msgs (~%d tokens)",
            original_count,
            original_tokens,
            len(result),
            final_tokens,
        )
        return result

    def _apply_micro_pass(self, messages: list[dict]) -> list[dict]:
        """Re-apply Layer 1 to any uncompressed tool result messages."""
        result: list[dict] = []
        for msg in messages:
            if msg.get("role") == "tool":
                compressed = self.micro.compress_message(msg)
                result.append(compressed)
            else:
                result.append(msg)
        return result
