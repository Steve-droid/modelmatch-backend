"""Pydantic contract schemas (camelCase out). See app.schemas.base.CamelModel.

S3 lands the base + the foundational vertical (auth + catalog) that S4/S5 consume.
Richer shapes (recommendation request/response, savings aggregates, chat) arrive
with their own slices.
"""

from app.schemas.auth import LoginRequest, TokenOut, UserCreate, UserOut
from app.schemas.base import CamelModel
from app.schemas.catalog import (
    BenchmarkOut,
    BenchmarkResultCreate,
    BenchmarkResultOut,
    CatalogRowIn,
    CatalogRowOut,
    HarnessOut,
    ModelCreate,
    ModelOut,
)

__all__ = [
    "CamelModel",
    "UserCreate",
    "UserOut",
    "LoginRequest",
    "TokenOut",
    "ModelCreate",
    "ModelOut",
    "HarnessOut",
    "BenchmarkOut",
    "BenchmarkResultCreate",
    "BenchmarkResultOut",
    "CatalogRowIn",
    "CatalogRowOut",
]
