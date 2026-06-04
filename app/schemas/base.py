"""Shared Pydantic base — the API contract speaks camelCase.

Python/DB stay snake_case; the wire stays camelCase. `CamelModel` does both:
- `alias_generator=to_camel` → fields serialize as camelCase (dump with
  `by_alias=True`); responses set `response_model` so FastAPI does this for us.
- `populate_by_name=True` → still constructable with snake_case names in code.
- `from_attributes=True` → `model_validate(orm_obj)` reads straight off an ORM row.
- `protected_namespaces=()` → silence Pydantic's `model_*` field warning (we have
  legitimate fields like `model_id`).
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        protected_namespaces=(),
    )
