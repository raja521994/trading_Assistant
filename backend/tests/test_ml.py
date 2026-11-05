"""Meta-labelling model: walk-forward validation must find a real signal and must reject noise."""

import numpy as np
import pandas as pd

from app.ml import meta


def _synthetic(signal: bool, n: int = 6000, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2018-01-01", "2025-12-31", periods=n).tz_localize("Asia/Kolkata")
    X = pd.DataFrame(rng.normal(size=(n, len(meta.FEATURES))), columns=meta.FEATURES)
    X["strategy_code"] = rng.integers(0, 5, n)
    edge = 1.2 * X["adx14"] if signal else 0.0
    r = np.where(rng.random(n) < 1 / (1 + np.exp(-(edge - 0.3))), rng.uniform(0.5, 3.0, n), -1.0)
    df = X.assign(strategy="s", symbol="X", signal_ts=ts, exit_ts=ts + pd.Timedelta(days=10), r=r,
                  win=(r > 0).astype(int))
    return df


def test_walk_forward_finds_real_signal():
    wf = meta.walk_forward(_synthetic(True))
    assert wf["oos_auc"] > 0.65 and wf["useful"]
    assert wf["selected"]["expectancy_r"] > wf["all"]["expectancy_r"]
    for f in wf["folds"]:  # training data always ends before the test year (purge + embargo)
        assert f["train_setups"] < 6000


def test_walk_forward_rejects_noise():
    wf = meta.walk_forward(_synthetic(False))
    assert not wf["useful"] and wf["verdict"].startswith("NOT USEFUL")
    assert 0.4 < wf["oos_auc"] < 0.6


def test_no_model_means_no_score(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "MODEL_FILE", tmp_path / "missing.joblib")
    assert meta.score_setup("ema_cross", None, pd.Timestamp("2025-01-01", tz="Asia/Kolkata"), 100, 95) is None
