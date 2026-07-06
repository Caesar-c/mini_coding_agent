"""Context management — task graph and context compaction for the agent loop."""

from context_manager.context import ContextCompactor
from context_manager.macro_compressor import MacroCompressor
from context_manager.meso_compressor import MesoCompressor
from context_manager.micro_compressor import MicroCompressor, estimate_tokens
from context_manager.pipeline import ContextPipeline
from context_manager.task_graph import TaskGraphManager

__all__ = [
    "ContextCompactor",
    "ContextPipeline",
    "MacroCompressor",
    "MesoCompressor",
    "MicroCompressor",
    "TaskGraphManager",
    "estimate_tokens",
]
