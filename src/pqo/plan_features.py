"""Feature extraction from PostgreSQL EXPLAIN JSON output."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


# Estimated data only: none of these columns needs EXPLAIN ANALYZE.
PLAN_FLOW_FEATURES = [
    "sort_input_rows_sum", "sort_input_rows_max", "sort_input_bytes_sum", "sort_input_bytes_max",
    "aggregate_input_rows_sum", "aggregate_input_rows_max", "aggregate_input_bytes_sum",
    "window_input_rows_sum", "window_input_rows_max", "window_input_bytes_sum",
    "window_agg_node_count", "incremental_sort_node_count", "distinct_sort_signature_count",
    "hashed_aggregate_node_count", "sorted_aggregate_node_count",
]


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
    sort_input_rows_sum: float = 0
    sort_input_rows_max: float = 0
    sort_input_bytes_sum: float = 0
    sort_input_bytes_max: float = 0
    aggregate_input_rows_sum: float = 0
    aggregate_input_rows_max: float = 0
    aggregate_input_bytes_sum: float = 0
    window_input_rows_sum: float = 0
    window_input_rows_max: float = 0
    window_input_bytes_sum: float = 0
    window_agg_node_count: int = 0
    incremental_sort_node_count: int = 0
    distinct_sort_signature_count: int = 0
    hashed_aggregate_node_count: int = 0
    sorted_aggregate_node_count: int = 0

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
    flows = {name: [] for name in ("sort", "aggregate", "window")}
    sort_signatures = set()
    for node in nodes:
        kind = str(node.get("Node Type", ""))
        category = (
            "sort" if kind in {"Sort", "Incremental Sort"} else
            "aggregate" if kind in {"Aggregate", "GroupAggregate", "HashAggregate"} else
            "window" if kind == "WindowAgg" else None
        )
        if category is None:
            continue
        # InitPlans/SubPlans are separately executed expressions, not the input
        # stream consumed by this operator. Do not add them to its row count.
        children = [child for child in node.get("Plans", [])
                    if child.get("Parent Relationship") not in {"InitPlan", "SubPlan"}]
        rows = sum(max(0.0, float(child.get("Plan Rows", 0) or 0)) for child in children)
        size = sum(max(0.0, float(child.get("Plan Rows", 0) or 0)) *
                   max(0.0, float(child.get("Plan Width", 0) or 0)) for child in children)
        flows[category].append((rows, size))
        if category == "sort" and node.get("Sort Key"):
            sort_signatures.add(tuple(map(str, node["Sort Key"])))

    def total(category, column):
        return sum(item[column] for item in flows[category])

    def maximum(category, column):
        return max((item[column] for item in flows[category]), default=0.0)

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
        sort_input_rows_sum=total("sort", 0),
        sort_input_rows_max=maximum("sort", 0),
        sort_input_bytes_sum=total("sort", 1),
        sort_input_bytes_max=maximum("sort", 1),
        aggregate_input_rows_sum=total("aggregate", 0),
        aggregate_input_rows_max=maximum("aggregate", 0),
        aggregate_input_bytes_sum=total("aggregate", 1),
        window_input_rows_sum=total("window", 0),
        window_input_rows_max=maximum("window", 0),
        window_input_bytes_sum=total("window", 1),
        window_agg_node_count=node_types.count("WindowAgg"),
        incremental_sort_node_count=node_types.count("Incremental Sort"),
        distinct_sort_signature_count=len(sort_signatures),
        hashed_aggregate_node_count=sum(
            node.get("Node Type") == "HashAggregate" or
            (node.get("Node Type") == "Aggregate" and node.get("Strategy") in {"Hashed", "Mixed"}) for node in nodes),
        sorted_aggregate_node_count=sum(
            node.get("Node Type") == "GroupAggregate" or
            (node.get("Node Type") == "Aggregate" and node.get("Strategy") == "Sorted") for node in nodes),
    )
