"""Offline tests for checkpoint selection, pinned revisions, and local safety.

Run from the repository root with ``python -m unittest discover -s tests -v``.
All Hub calls are mocked; no model weights or network access are required.
"""

from __future__ import annotations

import __future__
import ast
from contextlib import redirect_stdout
from fnmatch import fnmatch
import importlib.util
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "realtime_venus_downloader_under_test", REPO_ROOT / "download_models.py"
)
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)

COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
MANIFEST = {
    "name": "Realtime-Venus",
    "models": {
        "omni": {"path": "Realtime-Venus-Omni"},
        "audio": {"path": "Realtime-Venus-Audio"},
    },
}
REMOTE_FILES = (
    "config.yaml",
    "README.md",
    "Realtime-Venus-Omni/config.json",
    "Realtime-Venus-Omni/model-00001-of-00002.safetensors",
    "Realtime-Venus-Omni/assets/video/sample.mp4",
    "Realtime-Venus-Audio/config.json",
    "Realtime-Venus-Audio/model-00001-of-00002.safetensors",
    "Realtime-Venus-Audio/model-00002-of-00002.safetensors",
    "Realtime-Venus-Audio/model.safetensors.index.json",
    "Realtime-Venus-Audio/tokenizer.json",
    "Realtime-Venus-Audio/modeling_venus.py",
    "Realtime-Venus-Audio/assets/audio/sample.wav",
)


def write_download(root, filename, revision=COMMIT, content="fixture"):
    """Write a tiny fixture and the public library's local-download metadata."""
    destination = Path(root) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    metadata = Path(root) / ".cache/huggingface/download" / (filename + ".metadata")
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(f"{revision}\nfixture-etag\n{time.time()}\n", encoding="utf-8")
    return destination


class DownloadModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "checkpoints"
        self.manifest = yaml.safe_dump(MANIFEST, sort_keys=False)
        self.remote_files = list(REMOTE_FILES)
        self.info = SimpleNamespace(
            sha=COMMIT,
            siblings=[SimpleNamespace(rfilename=name, size=None) for name in self.remote_files],
        )
        self.api = Mock()
        self.api.model_info.return_value = self.info
        api_patch = patch.object(downloader, "HfApi", return_value=self.api)
        self.api_factory = api_patch.start()
        self.addCleanup(api_patch.stop)
        manifest_patch = patch.object(
            downloader, "hf_hub_download", side_effect=self.download_manifest
        )
        self.manifest_download = manifest_patch.start()
        self.addCleanup(manifest_patch.stop)
        snapshot_patch = patch.object(
            downloader, "snapshot_download", side_effect=self.download_snapshot
        )
        self.snapshot = snapshot_patch.start()
        self.addCleanup(snapshot_patch.stop)
        self.output = redirect_stdout(StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def download_manifest(self, **kwargs):
        return str(write_download(kwargs["local_dir"], kwargs["filename"],
                                  kwargs["revision"], self.manifest))

    def download_snapshot(self, **kwargs):
        for name in self.remote_files:
            if any(fnmatch(name, pattern) for pattern in kwargs["allow_patterns"]):
                write_download(kwargs["local_dir"], name, kwargs["revision"])
        return str(kwargs["local_dir"])

    def test_audio_download_is_complete_and_excludes_omni(self):
        result = downloader.download_models("audio", self.root, "release", 3, "test-token")
        self.assertEqual(result, {"audio": self.root / "Realtime-Venus-Audio"})
        self.api.model_info.assert_called_once_with(
            downloader.REPO_ID, revision="release", token="test-token", files_metadata=True
        )
        manifest_args = self.manifest_download.call_args.kwargs
        snapshot_args = self.snapshot.call_args.kwargs
        self.assertEqual(manifest_args["revision"], COMMIT)
        self.assertEqual(snapshot_args["revision"], COMMIT)
        self.assertEqual(manifest_args["token"], "test-token")
        self.assertEqual(snapshot_args["token"], "test-token")
        self.assertEqual(snapshot_args["max_workers"], 3)
        self.assertEqual(snapshot_args["allow_patterns"], ["Realtime-Venus-Audio/*"])
        self.assertEqual((self.root / "config.yaml").read_text(), self.manifest)
        for name in self.remote_files:
            if name.startswith("Realtime-Venus-Audio/"):
                self.assertTrue((self.root / name).is_file(), name)
        self.assertFalse((self.root / "Realtime-Venus-Omni").exists())
        self.assertFalse((self.root / "README.md").exists())
        self.manifest_download.assert_called_once()

    def test_all_downloads_both_models_and_keeps_unrelated_files(self):
        unrelated = self.root / "user-notes.txt"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep")
        result = downloader.download_models(local_dir=self.root)
        self.assertEqual(set(result), {"omni", "audio"})
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertTrue((result["omni"] / "config.json").is_file())
        self.assertTrue((result["audio"] / "config.json").is_file())

    def test_audio_download_preserves_existing_omni(self):
        existing = self.root / "Realtime-Venus-Omni/config.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("local Omni configuration")
        downloader.download_models("audio", self.root)
        self.assertEqual(existing.read_text(), "local Omni configuration")

    def test_paths_are_read_from_manifest(self):
        self.manifest = self.manifest.replace("Realtime-Venus-Audio", "versions/audio-v2")
        self.remote_files = [name.replace("Realtime-Venus-Audio", "versions/audio-v2")
                             for name in self.remote_files]
        self.info.siblings = [SimpleNamespace(rfilename=name, size=None) for name in self.remote_files]
        result = downloader.download_models("audio", self.root)
        self.assertEqual(result["audio"], self.root / "versions/audio-v2")
        self.assertEqual(self.snapshot.call_args.kwargs["allow_patterns"], ["versions/audio-v2/*"])

    def test_invalid_arguments_fail_before_any_hub_call(self):
        for kwargs in ({"model": "vision"}, {"max_workers": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                downloader.download_models(local_dir=self.root, **kwargs)
        self.api_factory.assert_not_called()
        self.manifest_download.assert_not_called()
        self.snapshot.assert_not_called()

    def test_invalid_manifests_never_download_model_files(self):
        invalid = [None, [], {}, {"name": "Other", "models": MANIFEST["models"]}]
        for path in ("../outside", "/absolute", "a/../outside", "a//b", "a/./b",
                     "a/*", "a?", "C:\\outside", "a/", ""):
            invalid.append({"name": "Realtime-Venus", "models": {
                "omni": {"path": "Realtime-Venus-Omni"}, "audio": {"path": path}}})
        for path in ("Realtime-Venus-Omni", "Realtime-Venus-Omni/audio"):
            invalid.append({"name": "Realtime-Venus", "models": {
                "omni": {"path": "Realtime-Venus-Omni"}, "audio": {"path": path}}})
        for manifest in invalid:
            with self.subTest(manifest=manifest):
                self.manifest = yaml.safe_dump(manifest)
                with self.assertRaises(ValueError):
                    downloader.download_models("audio", self.root)
        self.snapshot.assert_not_called()

    def test_selected_directory_must_have_config(self):
        self.info.siblings = [SimpleNamespace(rfilename=name, size=None) for name in self.remote_files
                              if name != "Realtime-Venus-Audio/config.json"]
        with self.assertRaises(ValueError):
            downloader.download_models("audio", self.root)
        self.snapshot.assert_not_called()

    def test_stale_manifest_is_rejected_before_model_download(self):
        def stale_manifest(**kwargs):
            return str(write_download(self.root, "config.yaml", OLD_COMMIT, self.manifest))
        self.manifest_download.side_effect = stale_manifest
        with self.assertRaises(RuntimeError):
            downloader.download_models("audio", self.root)
        self.snapshot.assert_not_called()

    def test_incomplete_or_stale_snapshot_does_not_report_success(self):
        def stale_snapshot(**kwargs):
            for name in self.remote_files:
                if name.startswith("Realtime-Venus-Audio/"):
                    write_download(self.root, name, OLD_COMMIT)
            return str(self.root)
        for fallback in (lambda **kwargs: str(self.root), stale_snapshot):
            with self.subTest(fallback=fallback):
                self.snapshot.side_effect = fallback
                with self.assertRaises(RuntimeError):
                    downloader.download_models("audio", self.root)

    def test_locally_modified_file_with_old_metadata_is_rejected(self):
        def modified_snapshot(**kwargs):
            self.download_snapshot(**kwargs)
            changed = self.root / "Realtime-Venus-Audio/config.json"
            changed.write_text("truncated")
            # Library metadata is no longer valid once the local file is newer.
            future = time.time() + 10
            os.utime(changed, (future, future))
            return str(self.root)
        self.snapshot.side_effect = modified_snapshot
        with self.assertRaises(RuntimeError):
            downloader.download_models("audio", self.root)

    def test_incorrect_file_size_is_rejected_even_with_current_metadata(self):
        for entry in self.info.siblings:
            if entry.rfilename == "Realtime-Venus-Audio/config.json":
                entry.size = len("fixture") + 1
        with self.assertRaises(RuntimeError):
            downloader.download_models("audio", self.root)

    def test_snapshot_error_propagates_and_keeps_existing_other_model(self):
        existing = self.root / "Realtime-Venus-Omni/config.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("keep")
        self.snapshot.side_effect = OSError("download interrupted")
        with self.assertRaisesRegex(OSError, "download interrupted"):
            downloader.download_models("audio", self.root)
        self.assertEqual(existing.read_text(), "keep")

    def test_model_symlink_cannot_escape_destination(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.root.mkdir()
        (self.root / "Realtime-Venus-Audio").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            downloader.download_models("audio", self.root)
        self.snapshot.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_manifest_cache_symlink_is_rejected_before_manifest_download(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        cache = self.root / ".cache/huggingface"
        cache.mkdir(parents=True)
        (cache / "download").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            downloader.download_models("audio", self.root)
        self.manifest_download.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])


class AudioResolverTests(unittest.TestCase):
    def test_resolution_precedence_and_shared_downloader(self):
        source = REPO_ROOT / "frontend/Realtime-Venus-Audio/audio_offline_chat.py"
        function = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == "resolve_model_dir")
        # Execute only the resolver: inference dependencies and GPU stay unloaded.
        code = compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec",
                       flags=__future__.annotations.compiler_flag)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            namespace = {"Path": Path, "os": os, "sys": sys, "PROJECT_DIR": root}
            exec(code, namespace)
            resolve = namespace["resolve_model_dir"]
            download = Mock(return_value={"audio": root / "downloaded-audio"})
            fake_module = SimpleNamespace(download_models=download)
            with patch.dict(sys.modules, {"download_models": fake_module}), \
                    patch.object(sys, "path", list(sys.path)), \
                    patch.dict(os.environ, {"REALTIME_VENUS_MODEL_PATH": str(root / "env")}):
                self.assertEqual(resolve(str(root / "explicit")), root / "explicit")
                self.assertEqual(resolve(), root / "env")
                download.assert_not_called()
                del os.environ["REALTIME_VENUS_MODEL_PATH"]
                local = root / "Realtime-Venus-Audio"
                local.mkdir()
                (local / "config.json").write_text("{}")
                self.assertEqual(resolve(), local)
                download.assert_not_called()
                (local / "config.json").unlink()
                self.assertEqual(resolve(), root / "downloaded-audio")
                download.assert_called_once_with(model="audio", local_dir=root)


if __name__ == "__main__":
    unittest.main()
