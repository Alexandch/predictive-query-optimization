"""Value object representing an instantiated benchmark query template."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryCase:
    template_id: str
    sql_text: str

