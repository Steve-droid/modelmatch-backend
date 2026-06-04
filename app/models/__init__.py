"""ORM models package.

Importing this package registers every model on ``Base.metadata`` (Alembic's
``target_metadata``). Re-exports the Base + all entities for convenient imports.
"""

from app.models.base import Base
from app.models.orm import (
    Benchmark,
    BenchmarkResult,
    ChatMessage,
    CiFinding,
    CiRun,
    FindingFeedback,
    Harness,
    JenkinsConnection,
    LlmCall,
    LlmUsage,
    Model,
    ProactiveAlert,
    Project,
    RecommendationEvidence,
    RecommendationOption,
    RequirementsProfile,
    RetrievalTrace,
    SourceDocument,
    User,
)

__all__ = [
    "Base",
    "Benchmark",
    "BenchmarkResult",
    "ChatMessage",
    "CiFinding",
    "CiRun",
    "FindingFeedback",
    "Harness",
    "JenkinsConnection",
    "LlmCall",
    "LlmUsage",
    "Model",
    "ProactiveAlert",
    "Project",
    "RecommendationEvidence",
    "RecommendationOption",
    "RequirementsProfile",
    "RetrievalTrace",
    "SourceDocument",
    "User",
]
