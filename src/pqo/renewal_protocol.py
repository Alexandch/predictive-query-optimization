"""Select representative training SQL and separate parameter controls, before timing."""

import hashlib


def stable_key(text):
    return hashlib.sha256(("renewal-20261027" + text).encode()).hexdigest()


def select_cases(frame, protected):
    """Use only SQL identity, family and estimated cost, never historical targets.

    Up to four cost-spread representatives per family; one is reserved as a
    parameter control if possible. Small families are not silently discarded.
    """
    result = []
    for family, group in frame.groupby("template_id", sort=True):
        rows = [row for row in group.drop_duplicates("sql_text").to_dict("records")
                if row["sql_text"] not in protected]
        rows.sort(key=lambda row: (float(row["estimated_total_cost"]), stable_key(row["sql_text"])))
        if not rows:
            continue
        count = min(4, len(rows))
        indices = sorted({round(index * (len(rows) - 1) / max(1, count - 1)) for index in range(count)})
        chosen = [rows[index] for index in indices]
        reserved = int(stable_key(str(family))[:8], 16) % len(chosen) if len(chosen) > 1 else None
        for position, row in enumerate(chosen):
            result.append({"family": str(family), "sql": row["sql_text"],
                           "split": "parameter_control" if position == reserved else "train"})
    return result
