"""Local SLM agents that use AIOpsLab's native orchestrator."""

from .slm_agent import SLMAgent, NemotronAgent
from .system_evidence_collector import SystemEvidenceCollector

__all__ = ["SLMAgent", "NemotronAgent", "SystemEvidenceCollector"]

