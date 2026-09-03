"""Feature extraction from PostgreSQL EXPLAIN JSON output."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class PlanFeatures:
    estimated_startup_cost: float
    estimated_total_cost: float
    estimated_plan_rows: float
    estimated_plan_width: int
    estimated_rows_all_nodes: float
    actual_total_time_ms: float | None
    node_count: int
    max_plan_depth: int
    relation_count: int
    root_node_type: str
    seq_scan_count: int
    index_scan_count: int
    index_only_scan_count: int
    bitmap_heap_scan_count: int
    hash_join_count: int
    merge_join_count: int
    nested_loop_count: int
    sort_node_count: int
    aggregate_node_count: int
    shared_hit_blocks: int
    shared_read_blocks: int
    temp_read_blocks: int
    temp_written_blocks: int

    def as_dict(self) -> dict[str, float | int | str | None]:
        return asdict(self)


def _walk_nodes(node: Mapping[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_nodes(child)


def _walk_nodes_with_depth(node: Mapping[str, Any], depth: int = 1):
    yield node, depth
    for child in node.get("Plans", []):
        yield from _walk_nodes_with_depth(child, depth + 1)


def _unwrap_explain(document: Any) -> Mapping[str, Any]:
    if isinstance(document, str):
        import json

        document = json.loads(document)

    if isinstance(document, Sequence) and not isinstance(document, (str, bytes)):
        if not document:
            raise ValueError("EXPLAIN returned an empty document")
        document = document[0]

    if not isinstance(document, Mapping) or "Plan" not in document:
        raise ValueError("Expected PostgreSQL EXPLAIN JSON with a top-level 'Plan' field")

    plan = document["Plan"]
    if not isinstance(plan, Mapping):
        raise ValueError("The EXPLAIN 'Plan' field must be an object")
    return plan


def extract_plan_features(document: Any) -> PlanFeatures:
    root = _unwrap_explain(document)
    nodes = list(_walk_nodes(root))
    nodes_with_depth = list(_walk_nodes_with_depth(root))
    node_types = [str(node.get("Node Type", "Unknown")) for node in nodes]
    relations = {
        str(node["Relation Name"])
        for node in nodes
        if node.get("Relation Name") is not None
    }

    def sum_int(field: str) -> int:
        return sum(int(node.get(field, 0) or 0) for node in nodes)

    actual_time = root.get("Actual Total Time")
    return PlanFeatures(
        estimated_startup_cost=float(root.get("Startup Cost", 0.0)),
        estimated_total_cost=float(root.get("Total Cost", 0.0)),
        estimated_plan_rows=float(root.get("Plan Rows", 0.0)),
        estimated_plan_width=int(root.get("Plan Width", 0) or 0),
        estimated_rows_all_nodes=sum(
            float(node.get("Plan Rows", 0.0) or 0.0) for node in nodes
        ),
        actual_total_time_ms=float(actual_time) if actual_time is not None else None,
        node_count=len(nodes),
        max_plan_depth=max(depth for _, depth in nodes_with_depth),
        relation_count=len(relations),
        root_node_type=str(root.get("Node Type", "Unknown")),
        seq_scan_count=node_types.count("Seq Scan"),
        index_scan_count=node_types.count("Index Scan"),
        index_only_scan_count=node_types.count("Index Only Scan"),
        bitmap_heap_scan_count=node_types.count("Bitmap Heap Scan"),
        hash_join_count=node_types.count("Hash Join"),
        merge_join_count=node_types.count("Merge Join"),
        nested_loop_count=node_types.count("Nested Loop"),
        sort_node_count=node_types.count("Sort"),
        aggregate_node_count=sum(
            node_type in {"Aggregate", "GroupAggregate"}
            for node_type in node_types
        ),
        shared_hit_blocks=sum_int("Shared Hit Blocks"),
        shared_read_blocks=sum_int("Shared Read Blocks"),
        temp_read_blocks=sum_int("Temp Read Blocks"),
        temp_written_blocks=sum_int("Temp Written Blocks"),
    )
