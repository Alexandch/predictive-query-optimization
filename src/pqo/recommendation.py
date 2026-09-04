"""UI-independent DQN index recommendation service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DatabaseSettings
from .dqn import dqn_action_encoding_version, predict_action_values
from .dqn_features import build_query_state, encode_action
from .index_actions import IndexAction, generate_index_actions


@dataclass(frozen=True, slots=True)
class IndexRecommendation:
    action: IndexAction
    predicted_reward: float
    candidate_count: int


def recommend_index(
    sql_text: str,
    model_path: str | Path,
    settings: DatabaseSettings | None = None,
) -> IndexRecommendation:
    settings = settings or DatabaseSettings.from_env()
    state = build_query_state(sql_text, settings=settings)
    actions = generate_index_actions(
        sql_text,
        allowed_schemas=settings.allowed_schemas,
    )
    values = predict_action_values(
        model_path,
        state,
        [
            encode_action(
                action,
                sql_text,
                encoding_version=dqn_action_encoding_version(model_path),
            )
            for action in actions
        ],
    )
    best_index = max(range(len(actions)), key=values.__getitem__)
    if best_index != 0 and values[best_index] <= 0:
        best_index = 0
    return IndexRecommendation(
        action=actions[best_index],
        predicted_reward=values[best_index],
        candidate_count=len(actions),
    )
