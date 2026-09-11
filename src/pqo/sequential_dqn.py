"""Offline Bellman DQN for sequential multi-index recommendations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
from functools import lru_cache
import json
from pathlib import Path
import random

from .dqn_features import SEQUENTIAL_ACTION_ENCODING


@dataclass(frozen=True, slots=True)
class SequentialDQNMetrics:
    action_encoding_version: str
    experience_count: int
    episode_count: int
    state_count: int
    train_count: int
    validation_count: int
    test_count: int
    validation_return_mae: float
    validation_recommendation_accuracy: float
    validation_mean_regret: float
    return_mae: float
    recommendation_accuracy: float
    mean_regret: float
    stop_accuracy: float
    stop_mean_regret: float
    random_accuracy: float
    random_mean_regret: float
    decision_threshold: float
    epochs: int
    best_epoch: int
    gamma: float
    ranking_weight: float
    seed: int


def merge_sequential_experience(
    input_paths: list[str | Path], output_path: str | Path
) -> int:
    """Validate and combine complete domain datasets without re-encoding them."""
    if not input_paths:
        raise ValueError("At least one input dataset is required")
    combined: list[dict] = []
    episode_sources: dict[str, Path] = {}
    for raw_path in input_paths:
        path = Path(raw_path)
        records = _read_experience(path)
        _validate_records(records)
        for record in records:
            episode_id = record["episode_id"]
            previous = episode_sources.setdefault(episode_id, path)
            if previous != path:
                raise ValueError(
                    f"Episode {episode_id} occurs in both {previous} and {path}"
                )
        combined.extend(records)
    _validate_records(combined)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for record in combined:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(combined)


def train_sequential_dqn(
    experience_path: str | Path,
    output_dir: str | Path,
    *,
    epochs: int = 800,
    batch_size: int = 64,
    learning_rate: float = 1e-3,
    gamma: float = 0.95,
    tau: float = 0.02,
    ranking_weight: float = 0.20,
    seed: int = 42,
) -> SequentialDQNMetrics:
    """Train Q(s, a) with target-network Bellman backups over measured actions."""
    import numpy as np
    import torch
    from torch import nn

    if not 0 <= gamma <= 1:
        raise ValueError("gamma must be between zero and one")
    if not 0 < tau <= 1:
        raise ValueError("tau must be in (0, 1]")
    if ranking_weight < 0:
        raise ValueError("ranking_weight must be non-negative")
    records = _read_experience(experience_path)
    state_size, action_size = _validate_records(records)
    templates = {record["template_id"] for record in records}
    if len(records) < 40 or len(templates) < 4:
        raise ValueError(
            "Sequential DQN requires at least 40 transitions and 4 templates"
        )

    randomizer = random.Random(seed)
    train_records, validation_records, test_records = _episode_parameter_split(
        records, randomizer
    )
    if not train_records or not validation_records or not test_records:
        raise ValueError("Episode split produced an empty partition")

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    train_x = _input_array(train_records, np)
    validation_x = _input_array(validation_records, np)
    test_x = _input_array(test_records, np)
    feature_mean = train_x.mean(axis=0)
    feature_std = train_x.std(axis=0)
    feature_std[feature_std < 1e-6] = 1.0
    train_x = (train_x - feature_mean) / feature_std
    validation_x = (validation_x - feature_mean) / feature_std
    test_x = (test_x - feature_mean) / feature_std

    online = _build_network(nn, train_x.shape[1])
    target = _build_network(nn, train_x.shape[1])
    target.load_state_dict(online.state_dict())
    target.eval()
    optimizer = torch.optim.AdamW(online.parameters(), lr=learning_rate)
    loss_function = nn.SmoothL1Loss()
    train_tensor = torch.tensor(train_x, dtype=torch.float32)
    validation_tensor = torch.tensor(validation_x, dtype=torch.float32)
    train_groups = _state_groups(train_records)
    train_state_map = _state_index_map(train_records)
    train_returns = torch.tensor(
        _bellman_returns(train_records, gamma), dtype=torch.float32
    ).unsqueeze(1)
    validation_returns_array = np.asarray(
        _bellman_returns(validation_records, gamma), dtype=np.float32
    )

    best_loss = float("inf")
    best_state = copy.deepcopy(online.state_dict())
    best_epoch = 0
    stale_epochs = 0
    patience = max(60, epochs // 8)
    completed_epochs = 0
    for epoch in range(1, epochs + 1):
        group_count = min(max(1, batch_size // 2), len(train_groups))
        selected_groups = randomizer.sample(train_groups, group_count)
        indices = [index for group in selected_groups for index in group]
        predicted = online(train_tensor[indices])
        expected = _target_values(
            indices,
            train_records,
            train_tensor,
            train_state_map,
            target,
            gamma,
            torch,
        )
        local_groups = []
        offset = 0
        for group in selected_groups:
            local_groups.append(list(range(offset, offset + len(group))))
            offset += len(group)
        loss = loss_function(predicted, expected) + ranking_weight * _ranking_loss(
            predicted,
            train_returns[indices],
            local_groups,
            torch,
        )
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(online.parameters(), 10.0)
        optimizer.step()
        with torch.no_grad():
            for target_parameter, online_parameter in zip(
                target.parameters(), online.parameters(), strict=True
            ):
                target_parameter.mul_(1.0 - tau).add_(online_parameter, alpha=tau)
            validation_predictions = online(validation_tensor).squeeze(1)
            validation_loss = float(
                loss_function(
                    validation_predictions,
                    torch.tensor(validation_returns_array, dtype=torch.float32),
                ).item()
            )
        completed_epochs = epoch
        if validation_loss < best_loss - 1e-7:
            best_loss = validation_loss
            best_state = copy.deepcopy(online.state_dict())
            best_epoch = epoch
            stale_epochs = 0
        else:
            stale_epochs += 1
        if stale_epochs >= patience:
            break

    online.load_state_dict(best_state)
    with torch.no_grad():
        validation_predictions = online(validation_tensor).squeeze(1).numpy()
        test_predictions = online(
            torch.tensor(test_x, dtype=torch.float32)
        ).squeeze(1).numpy()
    decision_threshold = _calibrate_threshold(
        validation_records, validation_predictions, gamma, np
    )
    validation_accuracy, validation_regret = _policy_metrics(
        validation_records,
        validation_predictions,
        gamma,
        np,
        threshold=decision_threshold,
    )
    test_accuracy, test_regret = _policy_metrics(
        test_records,
        test_predictions,
        gamma,
        np,
        threshold=decision_threshold,
    )
    stop_accuracy, stop_regret, random_accuracy, random_regret = _baselines(
        test_records, gamma, seed, np
    )
    test_returns = np.asarray(_bellman_returns(test_records, gamma), dtype=np.float32)
    metrics = SequentialDQNMetrics(
        action_encoding_version=SEQUENTIAL_ACTION_ENCODING,
        experience_count=len(records),
        episode_count=len({record["episode_id"] for record in records}),
        state_count=len({record["state_id"] for record in records}),
        train_count=len(train_records),
        validation_count=len(validation_records),
        test_count=len(test_records),
        validation_return_mae=float(
            np.mean(np.abs(validation_returns_array - validation_predictions))
        ),
        validation_recommendation_accuracy=validation_accuracy,
        validation_mean_regret=validation_regret,
        return_mae=float(np.mean(np.abs(test_returns - test_predictions))),
        recommendation_accuracy=test_accuracy,
        mean_regret=test_regret,
        stop_accuracy=stop_accuracy,
        stop_mean_regret=stop_regret,
        random_accuracy=random_accuracy,
        random_mean_regret=random_regret,
        decision_threshold=decision_threshold,
        epochs=completed_epochs,
        best_epoch=best_epoch,
        gamma=gamma,
        ranking_weight=ranking_weight,
        seed=seed,
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": online.state_dict(),
            "input_size": int(train_x.shape[1]),
            "state_size": state_size,
            "action_size": action_size,
            "hidden_sizes": (128, 64),
            "feature_mean": torch.tensor(feature_mean, dtype=torch.float32),
            "feature_std": torch.tensor(feature_std, dtype=torch.float32),
            "gamma": gamma,
            "ranking_weight": ranking_weight,
            "decision_threshold": decision_threshold,
            "action_encoding_version": SEQUENTIAL_ACTION_ENCODING,
            "training_kind": "offline-sequential-bellman-dqn",
        },
        destination / "sequential_dqn_index_advisor.pt",
    )
    (destination / "metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def predict_sequential_action_values(
    model_path: str | Path,
    state: list[float],
    action_features: list[list[float]],
) -> list[float]:
    """Score available actions and enforce the exact terminal value of STOP."""
    import numpy as np
    import torch

    model, feature_mean, feature_std = _load_prediction_model(
        str(Path(model_path).resolve())
    )
    combined = np.asarray(
        [state + action for action in action_features], dtype=np.float32
    )
    normalized = (combined - feature_mean) / feature_std
    with torch.no_grad():
        values = model(torch.tensor(normalized, dtype=torch.float32)).squeeze(1)
    result = [float(value) for value in values]
    for index, action in enumerate(action_features):
        if action[-1] >= 0.5:
            result[index] = 0.0
    return result


@lru_cache(maxsize=4)
def _load_prediction_model(model_path: str):
    import torch
    from torch import nn

    artifact = torch.load(model_path, map_location="cpu", weights_only=True)
    if artifact.get("training_kind") != "offline-sequential-bellman-dqn":
        raise ValueError("Not a sequential Bellman DQN artifact")
    model = _build_network(nn, artifact["input_size"])
    model.load_state_dict(artifact["model_state_dict"])
    model.eval()
    return model, artifact["feature_mean"].numpy(), artifact["feature_std"].numpy()


def _target_values(indices, records, inputs, state_map, target, gamma, torch):
    values = []
    with torch.no_grad():
        for index in indices:
            record = records[index]
            value = float(record["reward"])
            if not record["done"]:
                next_indices = state_map[record["next_state_id"]]
                value += gamma * float(target(inputs[next_indices]).max().item())
            values.append(value)
    return torch.tensor(values, dtype=torch.float32).unsqueeze(1)


def _ranking_loss(predicted, expected, groups, torch):
    losses = []
    predicted = predicted.squeeze(1)
    expected = expected.squeeze(1)
    for indices in groups:
        if len(indices) < 2:
            continue
        local = torch.tensor(indices, dtype=torch.long)
        rewards = expected[local]
        values = predicted[local]
        best = int(torch.argmax(rewards).item())
        alternatives = torch.arange(len(indices)) != best
        required_gap = torch.clamp(
            rewards[best] - rewards[alternatives], min=0.01, max=0.50
        )
        losses.append(
            torch.relu(required_gap - (values[best] - values[alternatives])).mean()
        )
    if not losses:
        return predicted.sum() * 0.0
    return torch.stack(losses).mean()


def _bellman_returns(records: list[dict], gamma: float) -> list[float]:
    state_map = _state_index_map(records)
    cache: dict[int, float] = {}
    active: set[int] = set()

    def value(index: int) -> float:
        if index in cache:
            return cache[index]
        if index in active:
            raise ValueError("Sequential experience contains a transition cycle")
        active.add(index)
        record = records[index]
        result = float(record["reward"])
        if not record["done"]:
            result += gamma * max(
                value(next_index)
                for next_index in state_map[record["next_state_id"]]
            )
        active.remove(index)
        cache[index] = result
        return result

    return [value(index) for index in range(len(records))]


def _calibrate_threshold(records, predictions, gamma, np) -> float:
    create_values = sorted(
        float(predictions[index])
        for index, record in enumerate(records)
        if record["action"]["kind"] != "stop"
    )
    if not create_values:
        return 0.0
    candidates = [create_values[0] - 1e-6, create_values[-1] + 1e-6]
    candidates.extend(
        (left + right) / 2.0
        for left, right in zip(create_values, create_values[1:])
        if left != right
    )
    scored = [
        (*_policy_metrics(records, predictions, gamma, np, threshold=value), value)
        for value in candidates
    ]
    _, _, threshold = max(scored, key=lambda item: (item[0], -item[1]))
    return float(threshold)


def _policy_metrics(records, predictions, gamma, np, *, threshold=0.0):
    returns = _bellman_returns(records, gamma)
    predictions = predictions.copy()
    for index, record in enumerate(records):
        if record["action"]["kind"] == "stop":
            predictions[index] = 0.0
    groups = _state_groups(records)
    correct = 0
    regrets = []
    for indices in groups:
        stop_index = next(
            index for index in indices if records[index]["action"]["kind"] == "stop"
        )
        create_indices = [index for index in indices if index != stop_index]
        predicted_index = stop_index
        if create_indices:
            best_create = create_indices[int(np.argmax(predictions[create_indices]))]
            if predictions[best_create] > threshold:
                predicted_index = best_create
        actual_index = max(indices, key=lambda index: returns[index])
        correct += predicted_index == actual_index
        regrets.append(returns[actual_index] - returns[predicted_index])
    return float(correct / len(groups)), float(np.mean(regrets))


def _baselines(records, gamma, seed, np):
    returns = _bellman_returns(records, gamma)
    randomizer = random.Random(seed)
    stop_correct = random_correct = 0
    stop_regrets = []
    random_regrets = []
    for indices in _state_groups(records):
        best = max(indices, key=lambda index: returns[index])
        stop = next(
            index
            for index in indices
            if records[index]["action"]["kind"] == "stop"
        )
        random_action = randomizer.choice(indices)
        stop_correct += stop == best
        random_correct += random_action == best
        stop_regrets.append(returns[best] - returns[stop])
        random_regrets.append(returns[best] - returns[random_action])
    count = len(_state_groups(records))
    return (
        float(stop_correct / count),
        float(np.mean(stop_regrets)),
        float(random_correct / count),
        float(np.mean(random_regrets)),
    )


def _episode_parameter_split(records, randomizer):
    episodes_by_template: dict[str, list[str]] = {}
    for record in records:
        episodes = episodes_by_template.setdefault(record["template_id"], [])
        if record["episode_id"] not in episodes:
            episodes.append(record["episode_id"])
    validation: set[str] = set()
    test: set[str] = set()
    for episodes in episodes_by_template.values():
        randomizer.shuffle(episodes)
        if len(episodes) >= 3:
            test.add(episodes[0])
            validation.add(episodes[1])
        elif len(episodes) == 2:
            test.add(episodes[0])
    train = [r for r in records if r["episode_id"] not in validation | test]
    valid = [r for r in records if r["episode_id"] in validation]
    held_out = [r for r in records if r["episode_id"] in test]
    return train, valid, held_out


def _validate_records(records: list[dict]) -> tuple[int, int]:
    if not records:
        raise ValueError("Sequential experience dataset is empty")
    state_size = len(records[0]["state"])
    action_size = len(records[0]["action_features"])
    state_ids = {record["state_id"] for record in records}
    for record in records:
        if record.get("action_encoding_version") != SEQUENTIAL_ACTION_ENCODING:
            raise ValueError(
                "Sequential experience has an incompatible action encoding"
            )
        if (
            len(record["state"]) != state_size
            or len(record["action_features"]) != action_size
        ):
            raise ValueError("Sequential feature vectors have inconsistent sizes")
        if not record["done"] and record.get("next_state_id") not in state_ids:
            raise ValueError(f"Missing next state {record.get('next_state_id')}")
    return state_size, action_size


def _read_experience(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _input_array(records, np):
    return np.asarray(
        [record["state"] + record["action_features"] for record in records],
        dtype=np.float32,
    )


def _state_index_map(records: list[dict]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        result.setdefault(record["state_id"], []).append(index)
    return result


def _state_groups(records: list[dict]) -> list[list[int]]:
    return list(_state_index_map(records).values())


def _build_network(nn, input_size: int):
    return nn.Sequential(
        nn.Linear(input_size, 128),
        nn.ReLU(),
        nn.Linear(128, 64),
        nn.ReLU(),
        nn.Linear(64, 1),
    )
