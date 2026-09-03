"""UI-independent DQN index recommendation service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DatabaseSettings
from .dqn import predict_action_values
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
    state = build_query_state(sql_text, settings=settings)
    actions = generate_index_actions(sql_text)
    values = predict_action_values(
        model_path,
        state,
        [encode_action(action) for action in actions],
    )
    best_index = max(range(len(actions)), key=values.__getitem__)
    return IndexRecommendation(
        action=actions[best_index],
        predicted_reward=values[best_index],
        candidate_count=len(actions),
    )
