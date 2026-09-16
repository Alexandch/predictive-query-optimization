"""Evidence-based recommendations beyond ordinary B-tree index actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Any, Sequence

from .explain import _assert_read_only_query
from .plan_features import _unwrap_explain, _walk_nodes


class RecommendationCategory(StrEnum):
    AGGREGATION = "aggregation"
    JOIN = "join"
    SORT = "sort"
    MATERIALIZED_VIEW = "materialized_view"


class RecommendationPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class StructuralRecommendation:
    category: RecommendationCategory
    rule_id: str
    priority: RecommendationPriority
    title: str
    evidence: str
    action: str
    verification: str
    suggested_sql: str | None = None


def analyze_query_structure(
    sql_text: str,
    *,
    plan_json: Any | None = None,
    predicted_time_ms: float | None = None,
) -> tuple[StructuralRecommendation, ...]:
    """Inspect PostgreSQL SQL and its plan without changing or executing the query."""
    from sqlglot import exp, parse_one

    normalized = _assert_read_only_query(sql_text)
    tree = parse_one(normalized, read="postgres")
    recommendations: list[StructuralRecommendation] = []
    root = _unwrap_explain(plan_json) if plan_json is not None else None
    nodes = list(_walk_nodes(root)) if root is not None else []
    estimated_cost = float(root.get("Total Cost", 0.0)) if root else 0.0
    estimated_rows_all = sum(float(node.get("Plan Rows", 0) or 0) for node in nodes)

    joins = list(tree.find_all(exp.Join))
    physical_tables = _physical_tables(tree, exp)
    where = tree.find(exp.Where)

    _recommend_join_fixes(recommendations, joins, where, exp)
    _recommend_join_plan_fixes(recommendations, nodes)
    _recommend_aggregation_fixes(
        recommendations, tree, joins, nodes, estimated_rows_all, exp
    )
    _recommend_sort_fixes(recommendations, tree, nodes, exp)
    _recommend_materialized_view(
        recommendations,
        tree,
        normalized,
        physical_tables,
        joins,
        estimated_cost,
        predicted_time_ms,
        exp,
    )
    return tuple(_deduplicate(recommendations))


def _recommend_join_fixes(destination, joins, where, exp) -> None:
    for join in joins:
        side = str(join.args.get("side") or "").upper()
        kind = str(join.args.get("kind") or "").upper()
        on = join.args.get("on")
        using = join.args.get("using")
        table = join.this
        alias = table.alias_or_name if isinstance(table, exp.Table) else "joined_source"

        if on is None and not using:
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.JOIN,
                    "cartesian-join",
                    RecommendationPriority.HIGH,
                    "Проверить декартово соединение",
                    f"JOIN с источником «{alias}» не содержит ON/USING ({kind or 'implicit'} JOIN).",
                    "Добавьте условие связи либо подтвердите, что произведение строк действительно требуется.",
                    "Сравните количество строк и Total Cost в EXPLAIN до и после добавления условия.",
                )
            )

        if on is not None and _contains_wrapped_join_column(on, exp):
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.JOIN,
                    "expression-on-join-key",
                    RecommendationPriority.MEDIUM,
                    "Убрать функцию или CAST с ключа JOIN",
                    f"Условие соединения с «{alias}» вычисляет выражение над ключом.",
                    "Согласуйте типы столбцов в схеме или вынесите нормализованное значение в отдельный столбец.",
                    "Проверьте, исчезли ли вычисления из Join Filter/Hash Cond и уменьшилось ли фактическое время.",
                )
            )

        if side == "LEFT" and where is not None and alias:
            affected = _null_rejecting_columns(where.this, alias, exp)
            if affected:
                columns = ", ".join(sorted(affected))
                destination.append(
                    StructuralRecommendation(
                        RecommendationCategory.JOIN,
                        "left-join-null-rejected",
                        RecommendationPriority.MEDIUM,
                        "Проверить LEFT JOIN, превращённый фильтром в INNER JOIN",
                        f"WHERE отбрасывает NULL правой стороны «{alias}» по столбцам: {columns}.",
                        "Если строки без соответствия не нужны, явно используйте INNER JOIN; иначе перенесите допустимый фильтр в ON.",
                        "Сначала проверьте равенство результатов через EXCEPT ALL в обе стороны.",
                    )
                )


def _recommend_aggregation_fixes(
    destination, tree, joins, nodes, estimated_rows_all, exp
):
    having = tree.find(exp.Having)
    if having is not None:
        movable = [
            term
            for term in _flatten_and(having.this, exp)
            if not any(isinstance(node, exp.AggFunc) for node in term.walk())
        ]
        if movable:
            predicates = " AND ".join(
                term.sql(dialect="postgres") for term in movable
            )
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.AGGREGATION,
                    "non-aggregate-having-filter",
                    RecommendationPriority.MEDIUM,
                    "Перенести неагрегатный фильтр из HAVING в WHERE",
                    f"До агрегации можно применить условие: {predicates}.",
                    "Отфильтруйте исходные строки до GROUP BY, сохранив агрегатные условия в HAVING.",
                    "Докажите равенство результатов и сравните число строк на входе Aggregate.",
                )
            )

    distinct_aggregates = []
    for aggregate in tree.find_all(exp.AggFunc):
        if aggregate.find(exp.Distinct) is not None:
            distinct_aggregates.append(aggregate.sql(dialect="postgres"))
    if distinct_aggregates and joins and estimated_rows_all >= 10_000:
        destination.append(
            StructuralRecommendation(
                RecommendationCategory.AGGREGATION,
                "distinct-after-large-join",
                RecommendationPriority.MEDIUM,
                "Сократить поток перед COUNT/SUM DISTINCT",
                "DISTINCT-агрегат выполняется после соединений; оценочный поток плана "
                f"содержит {estimated_rows_all:,.0f} строк.",
                "Рассмотрите предварительное выделение уникальных ключей в CTE/подзапросе до широкого JOIN.",
                "Сравните HashAggregate/Sort memory, временные блоки и Execution Time через EXPLAIN ANALYZE.",
            )
        )

    if tree.args.get("distinct") and joins:
        destination.append(
            StructuralRecommendation(
                RecommendationCategory.AGGREGATION,
                "distinct-masks-join-multiplication",
                RecommendationPriority.LOW,
                "Проверить DISTINCT после JOIN",
                "SELECT DISTINCT может маскировать размножение строк соединением.",
                "Если нужны только факты существования связанных строк, рассмотрите EXISTS; иначе проверьте кардинальность JOIN.",
                "Сравните результаты через EXCEPT ALL и число строк до Unique/HashAggregate.",
            )
        )

    for node in nodes:
        if node.get("Node Type") not in {"Aggregate", "GroupAggregate"}:
            continue
        batches = int(node.get("HashAgg Batches", 1) or 1)
        disk_usage = int(node.get("Disk Usage", 0) or 0)
        if batches > 1 or disk_usage > 0:
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.AGGREGATION,
                    "aggregate-spill-to-disk",
                    RecommendationPriority.HIGH,
                    "Устранить сброс агрегации на диск",
                    f"Aggregate использовал {batches} партий и {disk_usage:,} КиБ диска.",
                    "Сократите вход до GROUP BY, проверьте лишние DISTINCT и только затем рассмотрите локальный work_mem.",
                    "Повторите EXPLAIN (ANALYZE, BUFFERS) и проверьте Disk Usage=0 и отсутствие временных блоков.",
                )
            )
            break


def _recommend_join_plan_fixes(destination, nodes) -> None:
    for node in nodes:
        node_type = str(node.get("Node Type") or "")
        rows = float(node.get("Actual Rows", node.get("Plan Rows", 0)) or 0)
        loops = float(node.get("Actual Loops", 1) or 1)
        if node_type == "Nested Loop" and rows * loops >= 100_000:
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.JOIN,
                    "large-nested-loop",
                    RecommendationPriority.MEDIUM,
                    "Проверить большой Nested Loop",
                    f"Узел Nested Loop обрабатывает примерно {rows * loops:,.0f} строк с учётом циклов.",
                    "Проверьте оценки кардинальности, статистику ANALYZE и доступность индекса на внутреннем ключе; не отключайте алгоритм планировщика глобально.",
                    "Сравните Actual Rows с Plan Rows и Execution Time после обновления статистики или изменения запроса.",
                )
            )
            break
        if node_type == "Hash" and int(node.get("Hash Batches", 1) or 1) > 1:
            batches = int(node.get("Hash Batches", 1))
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.JOIN,
                    "hash-join-multiple-batches",
                    RecommendationPriority.HIGH,
                    "Сократить разбиение Hash Join на партии",
                    f"Хеш-таблица соединения использовала {batches} партий, что указывает на нехватку памяти или большой вход.",
                    "Сократите вход фильтрами/предагрегацией; затем оцените локальное увеличение work_mem для этого отчёта.",
                    "Повторите EXPLAIN (ANALYZE, BUFFERS) и добейтесь Hash Batches=1 без ухудшения других запросов.",
                )
            )
            break


def _recommend_sort_fixes(destination, tree, nodes, exp) -> None:
    offset = tree.find(exp.Offset)
    if offset is not None:
        value = _integer_literal(offset.expression, exp)
        if value is not None and value >= 1_000:
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.SORT,
                    "large-offset-pagination",
                    RecommendationPriority.HIGH,
                    "Заменить большой OFFSET на keyset-пагинацию",
                    f"Запрос пропускает {value:,} уже найденных и отсортированных строк.",
                    "Передавайте последнее значение стабильного ORDER BY и фильтруйте следующую страницу сравнением по ключу.",
                    "Сравните Execution Time для первой и глубокой страницы; добавьте уникальный tie-breaker в ORDER BY.",
                )
            )

    for select in tree.find_all(exp.Select):
        if select is tree:
            continue
        if select.args.get("order") is not None and select.args.get("limit") is None:
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.SORT,
                    "subquery-order-without-limit",
                    RecommendationPriority.MEDIUM,
                    "Убрать ORDER BY из подзапроса без LIMIT",
                    "Внутренняя сортировка не гарантирует порядок внешнего результата и может быть лишней.",
                    "Оставьте окончательный ORDER BY на внешнем уровне, если порядок действительно нужен.",
                    "Проверьте отсутствие внутреннего Sort в EXPLAIN и равенство мультимножества строк.",
                )
            )
            break

    for node in nodes:
        if node.get("Node Type") != "Sort":
            continue
        method = str(node.get("Sort Method") or "")
        space_type = str(node.get("Sort Space Type") or "")
        if "external" in method.lower() or space_type.lower() == "disk":
            destination.append(
                StructuralRecommendation(
                    RecommendationCategory.SORT,
                    "sort-spill-to-disk",
                    RecommendationPriority.HIGH,
                    "Устранить сброс сортировки на диск",
                    f"PostgreSQL сообщил Sort Method={method or 'unknown'}, Sort Space Type={space_type or 'unknown'}.",
                    "Сначала сократите строки до сортировки; затем рассмотрите покрывающий порядок индекса или локальную настройку work_mem.",
                    "Повторите EXPLAIN (ANALYZE, BUFFERS) и убедитесь, что Sort Space Type стал Memory.",
                )
            )
            break


def _recommend_materialized_view(
    destination,
    tree,
    normalized_sql,
    physical_tables,
    joins,
    estimated_cost,
    predicted_time_ms,
    exp,
) -> None:
    has_aggregation = any(True for _ in tree.find_all(exp.AggFunc)) or any(
        select.args.get("group") is not None for select in tree.find_all(exp.Select)
    )
    expensive = estimated_cost >= 10_000 or (predicted_time_ms or 0) >= 100
    if not (has_aggregation and joins and len(physical_tables) >= 2 and expensive):
        return
    if _contains_volatile_function(tree, exp):
        return

    digest = hashlib.sha256(normalized_sql.encode()).hexdigest()[:10]
    view_name = f"pqo_summary_{digest}"
    where = tree.find(exp.Where)
    suggested_sql = None
    if where is None:
        suggested_sql = (
            f'CREATE MATERIALIZED VIEW "{view_name}" AS\n'
            f"{normalized_sql}\nWITH NO DATA;"
        )
    filter_note = (
        " Запрос содержит WHERE: представление следует обобщить и оставить "
        "изменяемый фильтр во внешнем запросе."
        if where is not None
        else ""
    )
    destination.append(
        StructuralRecommendation(
            RecommendationCategory.MATERIALIZED_VIEW,
            "expensive-summary-materialization",
            RecommendationPriority.MEDIUM,
            "Рассмотреть материализованное представление для сводного запроса",
            f"Агрегация соединяет {len(physical_tables)} таблицы; Total Cost={estimated_cost:,.2f}, "
            f"прогноз={float(predicted_time_ms or 0):,.2f} мс.{filter_note}",
            "Используйте materialized view только для часто повторяемого и допустимо устаревающего отчёта; задайте расписание REFRESH.",
            "Измерьте чтение из представления, стоимость REFRESH, размер и допустимую задержку данных на отдельной копии БД.",
            suggested_sql,
        )
    )


def _physical_tables(tree, exp) -> set[tuple[str, str]]:
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return {
        (table.db or "public", table.name)
        for table in tree.find_all(exp.Table)
        if table.name not in cte_names
    }


def _contains_wrapped_join_column(condition, exp) -> bool:
    wrappers = (exp.Cast, exp.Func)
    for column in condition.find_all(exp.Column):
        parent = column.parent
        while parent is not None and parent is not condition:
            if isinstance(parent, wrappers) and not isinstance(parent, exp.AggFunc):
                return True
            if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                break
            parent = parent.parent
    return False


def _null_rejecting_columns(condition, alias: str, exp) -> set[str]:
    if any(isinstance(node, exp.Or) for node in condition.walk()):
        return set()
    result = set()
    comparisons = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)
    for column in condition.find_all(exp.Column):
        if column.table != alias:
            continue
        parent = column.parent
        while parent is not None:
            if isinstance(parent, exp.Is):
                break
            if isinstance(parent, comparisons):
                result.add(column.name)
                break
            if parent is condition:
                break
            parent = parent.parent
    return result


def _flatten_and(node, exp):
    if isinstance(node, exp.And):
        return [*_flatten_and(node.this, exp), *_flatten_and(node.expression, exp)]
    return [node]


def _integer_literal(node, exp) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.this)
        except ValueError:
            return None
    return None


def _contains_volatile_function(tree, exp) -> bool:
    volatile = {"RANDOM", "NOW", "CLOCK_TIMESTAMP", "STATEMENT_TIMESTAMP"}
    for function in tree.find_all(exp.Func):
        name = str(getattr(function, "name", "") or function.key).upper()
        if name in volatile:
            return True
    return False


def _deduplicate(items: Sequence[StructuralRecommendation]):
    seen = set()
    for item in items:
        if item.rule_id not in seen:
            seen.add(item.rule_id)
            yield item
