from datetime import datetime, timezone
from uuid import uuid4
import numpy as np
from truetrade.features.technical import FEATURE_NAMES
from truetrade.rl.environment import ACTIONS, ACCOUNT_FEATURES


def explain(model, observation, raw_features, model_version, final_result, risk=None, language="fa"):
    action, _, value, probs = model.choose(observation, deterministic=True)
    baseline = probs[action]
    names = FEATURE_NAMES + ACCOUNT_FEATURES
    effects = []
    for i, name in enumerate(names):
        perturbed = np.asarray(observation).copy()
        perturbed[i] = 0.  # Training-mean ablation for normalized market features.
        alt = model.forward(perturbed)[0][0, action]
        effects.append({"feature": name, "delta_probability": float(baseline-alt)})
    denominator = sum(abs(x["delta_probability"]) for x in effects)
    for x in effects:
        x["relative_absolute_sensitivity"] = abs(x["delta_probability"])/denominator if denominator else 0.
    effects.sort(key=lambda x: -abs(x["delta_probability"]))
    action_name, tier, leverage = ACTIONS[action]
    if language == "fa":
        summary = f"مدل اقدام «{action_name}» را انتخاب کرد. نتیجهٔ کنترل اجرا: {final_result}. "
        summary += "اندازهٔ معامله با فاصلهٔ استاپ، هزینه‌ها، ریسک مجموع پوزیشن‌ها و مارجین محاسبه می‌شود. "
        summary += "احتمال خروجی مدل، احتمال تضمین‌شدهٔ سود نیست؛ اثر فیچرها سنجش حساسیت است، نه دلیل قطعی."
    else:
        summary = f"Policy selected {action_name}; execution result: {final_result}. Size is constrained by stop risk, costs, aggregate exposure and margin. Feature ablations are local sensitivity, not causal explanations or calibrated win probabilities."
    return {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
            "model_version": model_version, "action": action_name, "action_id": action,
            "raw_probabilities": probs.tolist(), "value_estimate": value,
            "policy_confidence": float(baseline), "requested_risk_tier": tier, "requested_leverage": leverage,
            "features": dict(zip(FEATURE_NAMES, map(float, raw_features))), "sensitivities": effects,
            "risk": risk, "final_result": final_result, "explanation": summary,
            "attribution_method": "single_feature_zero_ablation_not_causal"}
