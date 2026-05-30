import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("sscfg_publisher.py")
SPEC = importlib.util.spec_from_file_location("sscfg_publisher", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
sp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sp
SPEC.loader.exec_module(sp)


class SouthsidePublisherTests(unittest.TestCase):
    def make_repo(self) -> Path:
        temp_dir = Path(tempfile.mkdtemp(prefix="southside-publisher-"))
        (temp_dir / "packs").mkdir(parents=True)
        return temp_dir

    def write_pack(self, repo_dir: Path, name: str, content: str) -> Path:
        path = repo_dir / "packs" / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_publish_allocates_next_id_and_updates_registry(self) -> None:
        repo_dir = self.make_repo()
        self.write_pack(repo_dir, "demo.json", '{"a":1}\n')

        meta = sp.publish_source_file(repo_dir, "packs/demo.json")
        registry = sp.load_registry(repo_dir)
        sidecars = sp.load_sidecars(repo_dir)

        self.assertEqual(meta.pack_id, 1)
        self.assertEqual(registry.max_pack_id, 1)
        self.assertEqual(registry.bindings["packs/demo.json"], 1)
        self.assertIn(1, sidecars)
        self.assertEqual(sidecars[1].source_file, "packs/demo.json")

    def test_source_file_from_path_accepts_repo_pack_file(self) -> None:
        repo_dir = self.make_repo()
        path = self.write_pack(repo_dir, "demo.json", '{"a":1}\n')
        self.assertEqual(sp.source_file_from_path(repo_dir, path), "packs/demo.json")

    def test_source_file_from_path_rejects_file_outside_repo(self) -> None:
        repo_dir = self.make_repo()
        external_dir = Path(tempfile.mkdtemp(prefix="southside-publisher-external-"))
        external_path = external_dir / "demo.json"
        external_path.write_text('{"a":1}\n', encoding="utf-8")

        with self.assertRaises(sp.PublishError):
            sp.source_file_from_path(repo_dir, external_path)

    def test_rebuild_index_preserves_pack_bytes_and_emits_manual_fields(self) -> None:
        repo_dir = self.make_repo()
        pack_path = self.write_pack(repo_dir, "demo.json", '{\n  "x": "a"\n}\n')
        before = pack_path.read_bytes()

        meta = sp.publish_source_file(repo_dir, "packs/demo.json")
        meta = sp.replace(
            meta,
            name="Demo Pack",
            author="Alice",
            summary="Notes",
            version=7,
            date="2026-05-30",
            southside_version="1.21.80",
        )
        sp.write_pack_meta(repo_dir, meta)

        state = sp.scan_repository_state(repo_dir)
        result = sp.build_index_data("bedkillerspacex-boop/SouthsideConfigLoader", "master", state)
        sp.write_index_file(repo_dir, result)

        after = pack_path.read_bytes()
        pack = result.index_data["packs"][0]
        self.assertEqual(before, after)
        self.assertEqual(pack["version"], 7)
        self.assertEqual(pack["date"], "2026-05-30")
        self.assertEqual(pack["southsideVersion"], "1.21.80")
        self.assertEqual(pack["author"], "Alice")
        self.assertEqual(pack["summary"], "Notes")
        self.assertEqual(pack["sha256"], sp.sha256_bytes(before))
        self.assertIn("/packs/demo.json", pack["downloadUrl"])
        self.assertEqual(result.index_data["maxPackId"], 1)

    def test_scan_does_not_silently_rebind_renamed_files(self) -> None:
        repo_dir = self.make_repo()
        old_path = self.write_pack(repo_dir, "old.json", '{"a":1}\n')
        sp.publish_source_file(repo_dir, "packs/old.json")
        old_path.unlink()
        self.write_pack(repo_dir, "new.json", '{"a":1}\n')

        state = sp.scan_repository_state(repo_dir)

        self.assertEqual(len(state.published), 1)
        self.assertEqual(state.published[0].status, "源文件缺失")
        self.assertEqual(state.published[0].meta.source_file, "packs/old.json")
        self.assertEqual([source.rel_path for source in state.unpublished], ["packs/new.json"])

    def test_rebind_requires_explicit_action_and_updates_binding(self) -> None:
        repo_dir = self.make_repo()
        self.write_pack(repo_dir, "one.json", '{"a":1}\n')
        self.write_pack(repo_dir, "two.json", '{"b":2}\n')
        sp.publish_source_file(repo_dir, "packs/one.json")

        meta = sp.rebind_pack_source(repo_dir, 1, "packs/two.json")
        registry = sp.load_registry(repo_dir)

        self.assertEqual(meta.source_file, "packs/two.json")
        self.assertEqual(registry.bindings, {"packs/two.json": 1})

    def test_missing_source_warns_and_is_skipped_from_index(self) -> None:
        repo_dir = self.make_repo()
        path = self.write_pack(repo_dir, "demo.json", '{"a":1}\n')
        sp.publish_source_file(repo_dir, "packs/demo.json")
        path.unlink()

        state = sp.scan_repository_state(repo_dir)
        result = sp.build_index_data("bedkillerspacex-boop/SouthsideConfigLoader", "master", state)

        self.assertEqual(result.published_count, 0)
        self.assertEqual(result.missing_count, 1)
        self.assertTrue(any("已跳过 id 1" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
