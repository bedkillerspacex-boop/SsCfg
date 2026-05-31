import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("sscfg_publisher.py")
SPEC = importlib.util.spec_from_file_location("sscfg_publisher", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
sp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sp
SPEC.loader.exec_module(sp)


class SouthsidePublisherTests(unittest.TestCase):
    FIXED_TS_1 = 1717027200
    FIXED_TS_2 = 1717113600

    def make_repo(self) -> Path:
        temp_dir = Path(tempfile.mkdtemp(prefix="southside-publisher-"))
        (temp_dir / "packs").mkdir(parents=True)
        return temp_dir

    def write_pack(self, repo_dir: Path, name: str, content: str) -> Path:
        path = repo_dir / "packs" / name
        path.write_text(content, encoding="utf-8")
        return path

    def set_mtime(self, path: Path, timestamp: int) -> None:
        os.utime(path, (timestamp, timestamp))

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

    def test_write_source_text_updates_pack_json(self) -> None:
        repo_dir = self.make_repo()
        path = self.write_pack(repo_dir, "demo.json", '{"a":1}\n')

        sp.write_source_text(repo_dir, "packs/demo.json", '{\n  "a": 2\n}')

        self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "a": 2\n}\n')

    def test_write_source_text_rejects_invalid_json(self) -> None:
        repo_dir = self.make_repo()
        path = self.write_pack(repo_dir, "demo.json", '{"a":1}\n')
        before = path.read_text(encoding="utf-8")

        with self.assertRaises(sp.PublishError):
            sp.write_source_text(repo_dir, "packs/demo.json", '{"a": }')

        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_collect_source_packs_hashes_published_lf_bytes(self) -> None:
        repo_dir = self.make_repo()
        path = repo_dir / "packs" / "demo.json"
        path.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')

        source = sp.collect_source_packs(repo_dir)[0]
        expected = b'{\n  "a": 1\n}\n'

        self.assertEqual(source.size_bytes, len(expected))
        self.assertEqual(source.sha256, sp.sha256_bytes(expected))

    def test_source_pack_from_file_hashes_published_lf_bytes(self) -> None:
        repo_dir = self.make_repo()
        path = repo_dir / "packs" / "demo.json"
        path.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')

        source = sp.source_pack_from_file(repo_dir, "packs/demo.json")
        expected = b'{\n  "a": 1\n}\n'

        self.assertEqual(source.size_bytes, len(expected))
        self.assertEqual(source.sha256, sp.sha256_bytes(expected))

    def test_create_source_file_creates_json_under_packs(self) -> None:
        repo_dir = self.make_repo()

        source = sp.create_source_file(repo_dir, "new_pack", '{"ok":true}')

        self.assertEqual(source.rel_path, "packs/new_pack.json")
        self.assertTrue((repo_dir / "packs" / "new_pack.json").exists())
        self.assertEqual((repo_dir / "packs" / "new_pack.json").read_text(encoding="utf-8"), '{"ok":true}\n')

    def test_delete_source_file_removes_unpublished_json(self) -> None:
        repo_dir = self.make_repo()
        pack_path = self.write_pack(repo_dir, "demo.json", '{"a":1}\n')

        sp.delete_source_file(repo_dir, "packs/demo.json")

        self.assertFalse(pack_path.exists())

    def test_rebuild_index_preserves_pack_bytes_and_emits_manual_fields(self) -> None:
        repo_dir = self.make_repo()
        pack_path = self.write_pack(repo_dir, "demo.json", '{\n  "x": "a"\n}\n')
        self.set_mtime(pack_path, self.FIXED_TS_1)
        before = pack_path.read_bytes()
        published_before = sp.published_source_bytes(before)
        expected_date = sp.source_file_modified_at(repo_dir, "packs/demo.json")

        meta = sp.publish_source_file(repo_dir, "packs/demo.json")
        meta = sp.replace(
            meta,
            name="Demo Pack",
            author="Alice",
            summary="Notes",
            pack_type="暴力",
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
        self.assertEqual(pack["date"], expected_date)
        self.assertEqual(pack["southsideVersion"], "1.21.80")
        self.assertEqual(pack["author"], "Alice")
        self.assertEqual(pack["summary"], "Notes")
        self.assertEqual(pack["type"], "暴力")
        self.assertEqual(pack["sha256"], sp.sha256_bytes(published_before))
        self.assertIn("/packs/demo.json", pack["downloadUrl"])
        self.assertEqual(result.index_data["maxPackId"], 1)

    def test_source_edit_changes_index_hash_without_touching_sidecar_fields(self) -> None:
        repo_dir = self.make_repo()
        pack_path = self.write_pack(repo_dir, "demo.json", '{"x":"a"}\n')
        self.set_mtime(pack_path, self.FIXED_TS_1)
        meta = sp.publish_source_file(repo_dir, "packs/demo.json")
        meta = sp.replace(
            meta,
            name="Demo",
            author="Alice",
            summary="Summary",
            pack_type="安全",
            version=3,
            date="2026-05-30",
            southside_version="1.21.80",
        )
        sp.write_pack_meta(repo_dir, meta)
        first_result = sp.build_index_data("bedkillerspacex-boop/SouthsideConfigLoader", "master", sp.scan_repository_state(repo_dir))
        first_pack = first_result.index_data["packs"][0]
        first_expected_date = sp.source_file_modified_at(repo_dir, "packs/demo.json")

        sp.write_source_text(repo_dir, "packs/demo.json", '{"x":"b"}')
        self.set_mtime(pack_path, self.FIXED_TS_2)

        second_result = sp.build_index_data("bedkillerspacex-boop/SouthsideConfigLoader", "master", sp.scan_repository_state(repo_dir))
        second_pack = second_result.index_data["packs"][0]
        second_expected_date = sp.source_file_modified_at(repo_dir, "packs/demo.json")

        self.assertNotEqual(first_pack["sha256"], second_pack["sha256"])
        self.assertEqual(second_pack["name"], "Demo")
        self.assertEqual(second_pack["author"], "Alice")
        self.assertEqual(second_pack["summary"], "Summary")
        self.assertEqual(second_pack["type"], "安全")
        self.assertEqual(second_pack["version"], 3)
        self.assertEqual(first_pack["date"], first_expected_date)
        self.assertEqual(second_pack["date"], second_expected_date)
        self.assertNotEqual(first_pack["date"], second_pack["date"])
        self.assertEqual(second_pack["southsideVersion"], "1.21.80")

    def test_default_meta_uses_safe_type(self) -> None:
        meta = sp.default_meta_for_source(1, "packs/demo.json")
        self.assertEqual(meta.pack_type, "安全")

    def test_load_sidecar_type_defaults_to_safe(self) -> None:
        repo_dir = self.make_repo()
        meta = sp.default_meta_for_source(1, "packs/demo.json")
        sp.write_pack_meta(repo_dir, meta)

        loaded = sp.load_sidecars(repo_dir)[1]

        self.assertEqual(loaded.pack_type, "安全")

    def test_rebuild_index_from_unpublished_source_publishes_and_increments_max_id(self) -> None:
        repo_dir = self.make_repo()
        (repo_dir / ".git").mkdir()
        self.write_pack(repo_dir, "demo.json", '{"a":1}\n')

        original_preferred_repo_dir = sp.preferred_repo_dir
        try:
            sp.preferred_repo_dir = lambda _owner_repo: repo_dir
            with mock.patch.object(sp.messagebox, "showinfo", lambda *args, **kwargs: None), mock.patch.object(
                sp.messagebox, "showerror", lambda *args, **kwargs: None
            ):
                app = sp.PublisherApp()
                try:
                    app.withdraw()
                    app.owner_repo_var.set("temp/test")
                    app.branch_var.set("master")
                    app.load_state(repo_dir)
                    app.focus_source_file("packs/demo.json")
                    app.rebuild_index()
                finally:
                    app.destroy()
        finally:
            sp.preferred_repo_dir = original_preferred_repo_dir

        registry = sp.load_registry(repo_dir)
        index_data = sp.read_json(repo_dir / "index.json")
        self.assertEqual(registry.max_pack_id, 1)
        self.assertEqual(registry.bindings, {"packs/demo.json": 1})
        self.assertEqual(index_data["maxPackId"], 1)
        self.assertEqual(index_data["packs"][0]["id"], 1)
        self.assertEqual(
            index_data["packs"][0]["downloadUrl"],
            "https://raw.githubusercontent.com/temp/test/refs/heads/master/packs/demo.json",
        )

    def test_unpublish_pack_deletes_binding_but_keeps_max_pack_id_monotonic(self) -> None:
        repo_dir = self.make_repo()
        self.write_pack(repo_dir, "one.json", '{"a":1}\n')
        self.write_pack(repo_dir, "two.json", '{"b":2}\n')
        sp.publish_source_file(repo_dir, "packs/one.json")
        sp.publish_source_file(repo_dir, "packs/two.json")

        sp.unpublish_pack(repo_dir, 2, delete_source=True)

        registry = sp.load_registry(repo_dir)
        state = sp.scan_repository_state(repo_dir)
        self.assertEqual(registry.max_pack_id, 2)
        self.assertEqual(registry.bindings, {"packs/one.json": 1})
        self.assertFalse((repo_dir / "packs" / "two.json").exists())
        self.assertEqual([record.meta.pack_id for record in state.published], [1])

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
