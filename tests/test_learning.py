import tempfile
import unittest
from pathlib import Path
import numpy as np
from helpers import candles, market
from truetrade.features.technical import Normalizer
from truetrade.rl.ppo import PPO, advantages
from truetrade.rl.environment import TradingEnv, ACTIONS
from truetrade.rl.evaluation import promotion_gate, walk_forward, metrics


class LearningTests(unittest.TestCase):
    def test_analytic_gradient_against_finite_difference(self):
        model = PPO(3, 4, hidden=4, seed=1)
        rng = np.random.default_rng(2)
        obs = rng.normal(size=(5,3)); actions = np.array([0,1,2,3,1])
        old = np.log(model.forward(obs)[0][np.arange(5), actions]) + .03
        adv = np.array([-1,.4,1.2,-.3,.7]); returns = rng.normal(size=5)
        _, grads, _ = model.loss_and_grad(obs, actions, old, adv, returns)
        eps = 1e-6
        for k in model.parameters:
            for idx in list(np.ndindex(model.parameters[k].shape))[:8]:
                original = model.parameters[k][idx]
                model.parameters[k][idx] = original+eps
                plus = model.loss_and_grad(obs, actions, old, adv, returns)[0]
                model.parameters[k][idx] = original-eps
                minus = model.loss_and_grad(obs, actions, old, adv, returns)[0]
                model.parameters[k][idx] = original
                self.assertAlmostEqual((plus-minus)/(2*eps), grads[k][idx], places=5)

    def test_gae_does_not_bootstrap_across_termination(self):
        adv, ret = advantages([1., 2.], [0.,0.], [True, True], 999)
        np.testing.assert_allclose(adv, [1,2])

    def test_actual_training_changes_weights(self):
        c = candles(250)
        env = TradingEnv(c, market(), np.zeros(250), episode_length=32)
        model = PPO(env.n_features, len(ACTIONS))
        before = model.parameters["wp"].copy()
        report = model.train(env, episodes=3, rollout_steps=32)
        self.assertEqual(report["episodes"], 3)
        self.assertFalse(np.array_equal(before, model.parameters["wp"]))
        self.assertTrue(np.isfinite(model.parameters["wp"]).all())

    def test_checkpoint_roundtrip_and_tamper_detection(self):
        model = PPO(4, 3)
        n = Normalizer(np.zeros(2), np.ones(2))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"model"
            model.save(path, n, {"source":"synthetic_test"})
            restored, _, _ = PPO.load(path)
            np.testing.assert_allclose(model.forward(np.ones(4))[0], restored.forward(np.ones(4))[0])
            with (path/"weights.npz").open("ab") as f: f.write(b"tamper")
            with self.assertRaises(ValueError): PPO.load(path)

    def test_hold_has_no_fabricated_trades_or_profit(self):
        c = candles(100)
        env = TradingEnv(c, market(), np.zeros(100), episode_length=20, random_start=False)
        done = False
        while not done:
            _, reward, term, trunc, _ = env.step(0)
            done = term or trunc
            self.assertEqual(reward, 0.)
        self.assertEqual(env.closed, [])
        self.assertEqual(env.balance, 10000.)

    def test_entry_at_next_bar_and_terminal_costs(self):
        c = candles(100)
        env = TradingEnv(c, market(), np.zeros(100), episode_length=2, random_start=False)
        before = env.i
        _, _, _, _, info = env.step(3)
        self.assertEqual(info["risk_result"], "accepted")
        self.assertEqual(env.i, before+1)
        if env.positions:
            self.assertAlmostEqual(float(env.positions[0].plan.entry), c.open[before+1]*1.0002)
        env.step(0)
        self.assertEqual(env.positions, [])
        self.assertTrue(len(env.closed) > 0)

    def test_walkforward_temporal_separation(self):
        for fold in walk_forward(1500):
            self.assertLess(fold.train_end, fold.validation_start)
            self.assertLess(fold.validation_start, fold.validation_end)

    def test_synthetic_or_untrained_never_promoted(self):
        r = {"trades":100,"net_return":.1,"expectancy":1.,"max_drawdown":.01}
        self.assertFalse(promotion_gate([r]*3, 2000, "synthetic_test")["eligible_for_demo_review"])
        self.assertFalse(promotion_gate([r]*3, 10, "thetruetrade")["eligible_for_demo_review"])
        self.assertIsNone(metrics([100,100], [])["win_rate"])
