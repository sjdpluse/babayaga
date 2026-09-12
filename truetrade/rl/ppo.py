"""Small, auditable CPU PPO with GAE and fresh on-policy rollouts.

Reference: Schulman et al. 2017, https://arxiv.org/abs/1707.06347.
This implementation deliberately has no dependency on exchange connectivity.
"""
import hashlib
import json
from pathlib import Path
import numpy as np


def softmax(logits):
    z = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(z)
    return exp / exp.sum(axis=-1, keepdims=True)


def advantages(rewards, values, dones, last_value, gamma=.99, lam=.95):
    adv = np.zeros(len(rewards))
    carry = 0.
    for t in reversed(range(len(rewards))):
        next_value = last_value if t == len(rewards)-1 else values[t+1]
        cont = 1. - float(dones[t])
        delta = rewards[t] + gamma * next_value * cont - values[t]
        carry = delta + gamma * lam * cont * carry
        adv[t] = carry
    return adv, adv + np.asarray(values)


class PPO:
    def __init__(self, observation_size, action_size, hidden=32, seed=0, learning_rate=3e-4):
        self.rng = np.random.default_rng(seed)
        self.lr = learning_rate
        self.parameters = {
            "w1": self.rng.normal(0, np.sqrt(2/observation_size), (observation_size, hidden)),
            "b1": np.zeros(hidden),
            "wp": self.rng.normal(0, .01, (hidden, action_size)),
            "bp": np.zeros(action_size),
            "wv": self.rng.normal(0, 1/np.sqrt(hidden), (hidden, 1)),
            "bv": np.zeros(1)}
        self.m = {k: np.zeros_like(v) for k, v in self.parameters.items()}
        self.v = {k: np.zeros_like(v) for k, v in self.parameters.items()}
        self.updates = 0

    def forward(self, obs):
        x = np.atleast_2d(obs).astype(np.float64)
        if not np.isfinite(x).all(): raise ValueError("Non-finite observation")
        p = self.parameters
        hidden = np.tanh(x @ p["w1"] + p["b1"])
        probs = softmax(hidden @ p["wp"] + p["bp"])
        values = (hidden @ p["wv"] + p["bv"]).ravel()
        return probs, values, hidden

    def choose(self, obs, deterministic=False):
        probs, values, _ = self.forward(obs)
        p = probs[0]
        action = int(p.argmax()) if deterministic else int(self.rng.choice(len(p), p=p))
        return action, float(np.log(max(p[action], 1e-12))), float(values[0]), p

    def loss_and_grad(self, obs, actions, old_logp, adv, returns, clip=.2, entropy_coef=.01, value_coef=.5):
        x = np.asarray(obs, dtype=float)
        probs, values, hidden = self.forward(x)
        n = len(x); rows = np.arange(n)
        logprobs = np.log(np.maximum(probs, 1e-12))
        selected = logprobs[rows, actions]
        ratio = np.exp(np.clip(selected-old_logp, -30, 30))
        unclipped = ratio * adv
        clipped = np.clip(ratio, 1-clip, 1+clip) * adv
        entropy = -(probs * logprobs).sum(axis=1)
        value_error = values - returns
        loss = -np.minimum(unclipped, clipped).mean() + value_coef * .5 * np.mean(value_error**2) - entropy_coef * entropy.mean()
        active = ((adv >= 0) & (ratio <= 1+clip)) | ((adv < 0) & (ratio >= 1-clip))
        dlogp = -adv * ratio * active / n
        onehot = np.zeros_like(probs); onehot[rows, actions] = 1
        dz = dlogp[:, None] * (onehot - probs)
        dz += entropy_coef * probs * (logprobs + entropy[:, None]) / n
        dv = (value_coef * value_error / n)[:, None]
        p = self.parameters
        dh = (dz @ p["wp"].T + dv @ p["wv"].T) * (1-hidden**2)
        grads = {"wp": hidden.T @ dz, "bp": dz.sum(axis=0), "wv": hidden.T @ dv,
                 "bv": dv.sum(axis=0), "w1": x.T @ dh, "b1": dh.sum(axis=0)}
        kl = float(np.mean((ratio - 1) - (selected-old_logp)))
        return float(loss), grads, kl

    def update(self, obs, actions, old_logp, adv, returns, epochs=4, batch_size=64):
        adv = (adv - adv.mean()) / max(adv.std(), 1e-8)
        losses = []
        for _ in range(epochs):
            order = self.rng.permutation(len(obs))
            for begin in range(0, len(obs), batch_size):
                ids = order[begin:begin+batch_size]
                loss, grads, kl = self.loss_and_grad(obs[ids], actions[ids], old_logp[ids], adv[ids], returns[ids])
                if not np.isfinite(loss): raise RuntimeError("Training diverged")
                if kl > .03: return {"loss": float(np.mean(losses)) if losses else loss, "kl_stop": True}
                norm = np.sqrt(sum(np.sum(g*g) for g in grads.values()))
                scale = min(1., .5 / max(norm, 1e-12))
                self.updates += 1
                for k, grad in grads.items():
                    g = grad * scale
                    self.m[k] = .9 * self.m[k] + .1 * g
                    self.v[k] = .999 * self.v[k] + .001 * g*g
                    m = self.m[k] / (1 - .9**self.updates)
                    v = self.v[k] / (1 - .999**self.updates)
                    self.parameters[k] -= self.lr * m / (np.sqrt(v) + 1e-8)
                losses.append(loss)
        return {"loss": float(np.mean(losses)), "kl_stop": False}

    def train(self, env, episodes, rollout_steps=256, progress=None):
        if episodes < 1 or rollout_steps < 2: raise ValueError("Invalid training budget")
        obs, _ = env.reset()
        completed = steps = 0
        while completed < episodes:
            xs, acts, lps, vals, rewards, dones = [], [], [], [], [], []
            for _ in range(rollout_steps):
                action, logp, value, probs = self.choose(obs)
                next_obs, reward, terminated, truncated, _ = env.step(action, confidence=float(probs[action]))
                done = terminated or truncated
                xs.append(obs); acts.append(action); lps.append(logp); vals.append(value); rewards.append(reward); dones.append(done)
                obs = next_obs; steps += 1
                if done:
                    completed += 1
                    obs, _ = env.reset()
                    if completed >= episodes: break
            last_value = float(self.forward(obs)[1][0])
            adv, returns = advantages(rewards, vals, dones, last_value)
            report = self.update(np.asarray(xs), np.asarray(acts), np.asarray(lps), adv, returns)
            if progress: progress({"episodes": completed, "steps": steps, **report})
        return {"episodes": completed, "steps": steps}

    def save(self, path, normalizer, metadata):
        path = Path(path); path.mkdir(parents=True, exist_ok=False)
        arrays = dict(self.parameters)
        arrays.update({"mean": normalizer.mean, "scale": normalizer.scale})
        arrays.update({"adam_m_"+k: v for k, v in self.m.items()})
        arrays.update({"adam_v_"+k: v for k, v in self.v.items()})
        np.savez_compressed(path / "weights.npz", **arrays)
        digest = hashlib.sha256((path / "weights.npz").read_bytes()).hexdigest()
        document = {**metadata, "sha256": digest, "optimizer_updates": self.updates,
                    "learning_rate": self.lr, "rng_state": self.rng.bit_generator.state}
        (path / "manifest.json").write_text(json.dumps(document, indent=2, allow_nan=False))
        return document

    @classmethod
    def load(cls, path):
        from truetrade.features.technical import Normalizer
        path = Path(path)
        metadata = json.loads((path / "manifest.json").read_text())
        raw = (path / "weights.npz").read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise ValueError("Checkpoint hash mismatch")
        with np.load(path / "weights.npz", allow_pickle=False) as data:
            model = cls(data["w1"].shape[0], data["bp"].shape[0], data["b1"].shape[0], learning_rate=metadata["learning_rate"])
            for k in model.parameters:
                model.parameters[k] = data[k].copy()
                model.m[k] = data["adam_m_"+k].copy()
                model.v[k] = data["adam_v_"+k].copy()
            norm = Normalizer(data["mean"].copy(), data["scale"].copy())
        if any(not np.isfinite(v).all() for v in model.parameters.values()) or (norm.scale <= 0).any():
            raise ValueError("Invalid checkpoint arrays")
        if not np.isfinite(norm.mean).all() or not np.isfinite(norm.scale).all():
            raise ValueError("Invalid checkpoint normalizer")
        model.updates = metadata["optimizer_updates"]
        model.rng.bit_generator.state = metadata["rng_state"]
        return model, norm, metadata
