"""Parametric Deep Q-Network for variable index-action sets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
from functools import lru_cache
import json
from pathlib import Path
import random


@dataclass(frozen=True, slots=True)
class DQNTrainingMetrics:
    split_mode: str
    experience_count: int
    train_count: int
    validation_count: int
    test_count: int
    train_templates: list[str]
    validation_templates: list[str]
    test_templates: list[str]
    validation_reward_mae: float
    validation_recommendation_accuracy: float
    validation_mean_regret: float
    reward_mae: float
    recommendation_accuracy: float
    mean_regret: float
    noop_accuracy: float
    noop_mean_regret: float
    random_accuracy: float
    random_mean_regret: float
    epochs: int
    best_epoch: int
    seed: int


def train_dqn(
    experience_path: str | Path,
    output_dir: str | Path,
    *,
    epochs: int = 500,
    batch_size: int = 64,
    learning_rate: float = 1e-3,
    gamma: float = 0.95,
    tau: float = 0.02,
    seed: int = 42,
    split_mode: str = "parameter",
) -> DQNTrainingMetrics:
    import numpy as np
    import torch
    from torch import nn

    records = _read_experience(experience_path)
    templates = sorted({record["template_id"] for record in records})
    if len(records) < 40 or len(templates) < 4:
        raise ValueError("DQN training requires at least 40 experiences and 4 templates")

    randomizer = random.Random(seed)
    if split_mode == "parameter":
        train_records, validation_records, test_records = _parameter_split(
            records, randomizer
        )
    elif split_mode == "unseen-template":
        train_records, validation_records, test_records = _template_split(
            records, templates, randomizer
        )
    else:
        raise ValueError("split_mode must be 'parameter' or 'unseen-template'")
    train_templates = {r["template_id"] for r in train_records}
    validation_templates = {r["template_id"] for r in validation_records}
    test_templates = {r["template_id"] for r in test_records}

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    x_train, y_train = _arrays(train_records, np)
    x_validation, y_validation = _arrays(validation_records, np)
    x_test, y_test = _arrays(test_records, np)
    feature_mean = x_train.mean(axis=0)
    feature_std = x_train.std(axis=0)
    feature_std[feature_std < 1e-6] = 1.0
    x_train = (x_train - feature_mean) / feature_std
    x_validation = (x_validation - feature_mean) / feature_std
    x_test = (x_test - feature_mean) / feature_std

    online = _build_network(nn, x_train.shape[1])
    target = _build_network(nn, x_train.shape[1])
    target.load_state_dict(online.state_dict())
    target.eval()
    optimizer = torch.optim.AdamW(online.parameters(), lr=learning_rate)
    loss_function = nn.SmoothL1Loss()
    replay = list(range(len(train_records)))

    train_x = torch.tensor(x_train, dtype=torch.float32)
    train_y = torch.tensor(y_train, dtype=torch.float32).unsqueeze(1)
    validation_x = torch.tensor(x_validation, dtype=torch.float32)
    validation_y = torch.tensor(y_validation, dtype=torch.float32).unsqueeze(1)
    best_validation_loss = float("inf")
    best_state = copy.deepcopy(online.state_dict())
    best_epoch = 0
    patience = max(50, epochs // 8)
    stale_epochs = 0
    completed_epochs = 0
    for epoch in range(1, epochs + 1):
        indices = randomizer.sample(replay, min(batch_size, len(replay)))
        batch_x = train_x[indices]
        rewards = train_y[indices]

        # Current experience is terminal. The target-network branch remains in
        # the artifact contract for the later multi-index sequential extension.
        expected_q = rewards
        predicted_q = online(batch_x)
        loss = loss_function(predicted_q, expected_q)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_value_(online.parameters(), 10.0)
        optimizer.step()

        with torch.no_grad():
            for target_parameter, online_parameter in zip(
                target.parameters(), online.parameters(), strict=True
            ):
                target_parameter.mul_(1.0 - tau).add_(online_parameter, alpha=tau)

            validation_loss = float(
                loss_function(online(validation_x), validation_y).item()
            )
        completed_epochs = epoch
        if validation_loss < best_validation_loss - 1e-7:
            best_validation_loss = validation_loss
            best_state = copy.deepcopy(online.state_dict())
            best_epoch = epoch
            stale_epochs = 0
        else:
            stale_epochs += 1
        if stale_epochs >= patience:
            break

    online.load_state_dict(best_state)

    with torch.no_grad():
        validation_predictions = online(validation_x).squeeze(1).numpy()
        predictions = online(torch.tensor(x_test, dtype=torch.float32)).squeeze(1)
        predictions = predictions.numpy()

    validation_accuracy, validation_regret = _recommendation_accuracy(
        validation_records, validation_predictions, np
    )
    test_accuracy, test_regret = _recommendation_accuracy(
        test_records, predictions, np
    )
    noop_accuracy, noop_regret, random_accuracy, random_regret = _baselines(
        test_records, seed, np
    )

    metrics = DQNTrainingMetrics(
        split_mode=split_mode,
        experience_count=len(records),
        train_count=len(train_records),
        validation_count=len(validation_records),
        test_count=len(test_records),
        train_templates=sorted(train_templates),
        validation_templates=sorted(validation_templates),
        test_templates=sorted(test_templates),
        validation_reward_mae=float(
            np.mean(np.abs(y_validation - validation_predictions))
        ),
        validation_recommendation_accuracy=validation_accuracy,
        validation_mean_regret=validation_regret,
        reward_mae=float(np.mean(np.abs(y_test - predictions))),
        recommendation_accuracy=test_accuracy,
        mean_regret=test_regret,
        noop_accuracy=noop_accuracy,
        noop_mean_regret=noop_regret,
        random_accuracy=random_accuracy,
        random_mean_regret=random_regret,
        epochs=completed_epochs,
        best_epoch=best_epoch,
        seed=seed,
    )

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": online.state_dict(),
            "target_state_dict": target.state_dict(),
            "input_size": int(x_train.shape[1]),
            "hidden_sizes": (128, 64),
            "feature_mean": torch.tensor(feature_mean, dtype=torch.float32),
            "feature_std": torch.tensor(feature_std, dtype=torch.float32),
            "gamma": gamma,
            "tau": tau,
            "seed": seed,
        },
        destination / "dqn_index_advisor.pt",
    )
    (destination / "metrics.json").write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metrics


def predict_action_values(
    model_path: str | Path,
    state: list[float],
    action_features: list[list[float]],
) -> list[float]:
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
    return [float(value) for value in values]


@lru_cache(maxsize=4)
def _load_prediction_model(model_path: str):
    import torch
    from torch import nn

    artifact = torch.load(model_path, map_location="cpu", weights_only=True)
    model = _build_network(nn, artifact["input_size"])
    model.load_state_dict(artifact["model_state_dict"])
    model.eval()
    return (
        model,
        artifact["feature_mean"].numpy(),
        artifact["feature_std"].numpy(),
    )


def _build_network(nn, input_size: int):
    return nn.Sequential(
        nn.Linear(input_size, 128),
        nn.ReLU(),
        nn.Linear(128, 64),
        nn.ReLU(),
        nn.Linear(64, 1),
    )


def _read_experience(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    if not records:
        raise ValueError("Experience dataset is empty")
    return records


def _arrays(records, np):
    x = np.asarray(
        [record["state"] + record["action_features"] for record in records],
        dtype=np.float32,
    )
    y = np.asarray([record["reward"] for record in records], dtype=np.float32)
    return x, y


def _recommendation_accuracy(records, predictions, np):
    groups: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        groups.setdefault(record["query_id"], []).append(index)
    correct = 0
    regrets = []
    for indices in groups.values():
        predicted_index = indices[int(np.argmax(predictions[indices]))]
        actual_index = indices[int(np.argmax([records[i]["reward"] for i in indices]))]
        correct += predicted_index == actual_index
        regrets.append(records[actual_index]["reward"] - records[predicted_index]["reward"])
    return float(correct / len(groups)), float(np.mean(regrets))


def _parameter_split(records: list[dict], randomizer: random.Random):
    """Split query identities inside every template, never individual actions."""
    query_ids_by_template: dict[str, list[str]] = {}
    for record in records:
        query_ids = query_ids_by_template.setdefault(record["template_id"], [])
        if record["query_id"] not in query_ids:
            query_ids.append(record["query_id"])

    validation_ids: set[str] = set()
    test_ids: set[str] = set()
    for query_ids in query_ids_by_template.values():
        randomizer.shuffle(query_ids)
        holdout = max(1, round(len(query_ids) * 0.20))
        test_ids.update(query_ids[:holdout])
        validation_ids.update(query_ids[holdout : 2 * holdout])

    train_records = [
        record
        for record in records
        if record["query_id"] not in validation_ids | test_ids
    ]
    validation_records = [
        record for record in records if record["query_id"] in validation_ids
    ]
    test_records = [record for record in records if record["query_id"] in test_ids]
    return train_records, validation_records, test_records


def _template_split(records, templates, randomizer: random.Random):
    shuffled = list(templates)
    randomizer.shuffle(shuffled)
    holdout = max(1, round(len(shuffled) * 0.20))
    test_templates = set(shuffled[:holdout])
    validation_templates = set(shuffled[holdout : 2 * holdout])
    train_records = [
        r for r in records
        if r["template_id"] not in test_templates | validation_templates
    ]
    validation_records = [
        r for r in records if r["template_id"] in validation_templates
    ]
    test_records = [r for r in records if r["template_id"] in test_templates]
    return train_records, validation_records, test_records


def _baselines(records, seed, np):
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(record["query_id"], []).append(record)
    randomizer = random.Random(seed)
    noop_correct = random_correct = 0
    noop_regrets = []
    random_regrets = []
    for candidates in groups.values():
        best = max(candidates, key=lambda item: item["reward"])
        noop = next(
            item for item in candidates if item["action"]["kind"] == "noop"
        )
        random_action = randomizer.choice(candidates)
        noop_correct += noop is best
        random_correct += random_action is best
        noop_regrets.append(best["reward"] - noop["reward"])
        random_regrets.append(best["reward"] - random_action["reward"])
    count = len(groups)
    return (
        float(noop_correct / count),
        float(np.mean(noop_regrets)),
        float(random_correct / count),
        float(np.mean(random_regrets)),
    )
