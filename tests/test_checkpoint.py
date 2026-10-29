import json
import tempfile
import unittest
from pathlib import Path

from demos.model.checkpoint import checkpoint_directory
from demos.model.server import _ServerConfig, resolve_assets
from test_model_modes import checkpoint


class CheckpointTests(unittest.TestCase):
    def test_repository_root_selects_variant_and_wrong_type_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            omni = checkpoint(root/'Realtime-Venus-Omni', 'realtime_venus_omni')
            audio = checkpoint(root/'Realtime-Venus-Audio', 'minicpmo')
            self.assertEqual(checkpoint_directory(root, 'audio'), audio.resolve())
            self.assertEqual(checkpoint_directory(root, 'omni'), omni.resolve())
            with self.assertRaisesRegex(ValueError, 'contains omni weights'):
                checkpoint_directory(omni, 'audio')

    def test_referenced_config_yaml_must_sit_in_download_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            omni = checkpoint(root/'Realtime-Venus-Omni', 'realtime_venus_omni')
            (omni/'config.json').write_text(json.dumps({'model_type':'realtime_venus_omni','venus_config_ref':{}}))
            with self.assertRaisesRegex(ValueError, 'config.yaml'):
                checkpoint_directory(omni, 'omni')
            (root/'config.yaml').write_text('name: Realtime-Venus')
            self.assertEqual(checkpoint_directory(root, 'omni'), omni.resolve())

    def test_reference_audio_defaults_to_checkpoint_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            weights = checkpoint(Path(tmp)/'weights', 'realtime_venus_omni')
            config = resolve_assets(_ServerConfig(model_path=str(weights), model_type='omni'))
            self.assertEqual(config.model_path, str(weights.resolve()))
            self.assertEqual(config.ref_audio_path, str(weights.resolve()/'assets/HT_ref_audio.wav'))

    def test_relative_reference_audio_is_inside_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            weights = checkpoint(Path(tmp)/'weights', 'realtime_venus_omni')
            (weights/'voices').mkdir()
            (weights/'voices/mine.wav').write_bytes(b'voice')
            config = resolve_assets(_ServerConfig(model_path=str(weights), model_type='omni', ref_audio_path='voices/mine.wav'))
            self.assertEqual(config.ref_audio_path, str(weights.resolve()/'voices/mine.wav'))
            with self.assertRaisesRegex(ValueError, 'Missing or empty model asset'):
                resolve_assets(_ServerConfig(model_path=str(weights), model_type='omni', ref_audio_path='voices/none.wav'))

    def test_missing_model_path_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, 'model path'):
            resolve_assets(_ServerConfig(model_type='omni'))


if __name__ == "__main__":
    unittest.main()
