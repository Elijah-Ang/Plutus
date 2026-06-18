from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, precision_score, recall_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .features import build_features
from .utils import PROJECT_ROOT


@dataclass(frozen=True)
class ShadowResult:
    model: Any
    metrics: dict[str, Any]
    features: list[str]


class MLShadowStrategy:
    execution_enabled = False

    def train(self, bars: pd.DataFrame, horizon: int = 5, cost_rate: float = 0.001, model_name: str = "logistic_regression") -> ShadowResult:
        frame = build_features(bars)
        columns = ["return_1", "return_5", "return_20", "distance_ma20", "distance_ma50", "volatility_10", "volatility_20", "volume_change"]
        frame["future_return"] = frame["close"].shift(-horizon) / frame["close"] - 1
        frame["target"] = (frame["future_return"] > cost_rate).astype(int)
        frame = frame.dropna(subset=columns + ["future_return"])
        if len(frame) < 60:
            raise ValueError("At least 60 complete observations are required")
        split = int(len(frame) * 0.8)
        train, test = frame.iloc[:split], frame.iloc[split:]
        model = RandomForestClassifier(n_estimators=200, max_depth=5, random_state=42) if model_name == "random_forest" else make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42))
        model.fit(train[columns], train["target"])
        prediction = model.predict(test[columns])
        probability = model.predict_proba(test[columns])[:, 1]
        strategy_return = np.where(prediction == 1, test["future_return"] - cost_rate, 0.0)
        cumulative = pd.Series(1 + strategy_return).cumprod()
        drawdown = cumulative / cumulative.cummax() - 1
        gains = strategy_return[strategy_return > 0].sum()
        losses = -strategy_return[strategy_return < 0].sum()
        metrics = {
            "accuracy": float(accuracy_score(test["target"], prediction)),
            "precision": float(precision_score(test["target"], prediction, zero_division=0)),
            "recall": float(recall_score(test["target"], prediction, zero_division=0)),
            "roc_auc": float(roc_auc_score(test["target"], probability)) if test["target"].nunique() > 1 else None,
            "confusion_matrix": confusion_matrix(test["target"], prediction).tolist(),
            "average_return_predicted_positive": float(test.loc[prediction == 1, "future_return"].mean() or 0),
            "profit_factor": float(gains / losses) if losses else None,
            "max_drawdown": float(drawdown.min()),
        }
        return ShadowResult(model, metrics, columns)

    def predict(self, result: ShadowResult, bars: pd.DataFrame) -> dict[str, Any]:
        row = build_features(bars).dropna(subset=result.features).iloc[-1:]
        probability = float(result.model.predict_proba(row[result.features])[:, 1][0])
        return {"opinion": "supportive" if probability >= 0.6 else "negative" if probability <= 0.4 else "neutral", "probability": probability}

    def save(self, result: ShadowResult, name: str, directory: str | Path = PROJECT_ROOT / "data" / "models") -> Path:
        path = Path(directory) / f"{name}.joblib"
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": result.model, "metrics": result.metrics, "features": result.features, "shadow_only": True}, path)
        return path

    def submit_order(self, *_: Any, **__: Any) -> None:
        raise PermissionError("ML shadow strategy cannot place orders")
