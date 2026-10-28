"""Released frontend model variants, shared by both services and the launcher."""
MODEL_NAMES = {"omni": "Realtime-Venus-Omni", "audio": "Realtime-Venus-Audio"}

def model_name(model_type):
    if model_type not in MODEL_NAMES:
        raise ValueError("Model type must be audio or omni")
    return MODEL_NAMES[model_type]
