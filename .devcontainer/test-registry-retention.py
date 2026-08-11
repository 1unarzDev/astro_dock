#!/usr/bin/env python3
import datetime as dt
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


path = pathlib.Path(__file__).with_name("registry-retention.py")
spec = importlib.util.spec_from_file_location("registry_retention", path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RetentionPolicyTest(unittest.TestCase):
    def tag(self, name: str):
        return module.Tag(name, f"sha256:{len(name):064x}", dt.datetime.now(dt.timezone.utc), 1)

    def test_only_exact_legacy_names_are_deleted(self):
        tags = [
            self.tag("core"),
            self.tag("core-old"),
            self.tag("core-amd64-" + "a" * 40),
            self.tag("cuda-" + "b" * 40),
            self.tag("jetson-" + "c" * 40),
            self.tag("deps-" + "d" * 64),
            self.tag("cache-deps"),
            self.tag("do-not-delete"),
        ]
        self.assertEqual(
            module.migration_deletions(tags),
            sorted([tags[2].name, tags[3].name, tags[4].name, tags[5].name]),
        )

    def test_every_fixed_tag_is_protected(self):
        tags = [self.tag(name) for name in module.FIXED_TAGS]
        self.assertEqual(module.migration_deletions(tags), [])

    def test_repository_must_have_namespace(self):
        with self.assertRaises(ValueError):
            module.split_repository("astro")

    @mock.patch.object(module, "request")
    def test_stale_count_stops_on_short_page(self, request):
        request.return_value = {
            "count": 111,
            "next": "stale-page-two",
            "results": [
                {
                    "name": "core",
                    "digest": "sha256:core",
                    "last_updated": "2026-08-10T00:00:00Z",
                    "full_size": 1,
                }
            ],
        }
        tags = module.inventory("example/astro")
        self.assertEqual([tag.name for tag in tags], ["core"])
        request.assert_called_once()

    @mock.patch.object(module, "request")
    def test_migration_rejects_incomplete_inventory(self, request):
        request.return_value = {
            "count": 111,
            "next": "stale-page-two",
            "results": [],
        }
        with self.assertRaisesRegex(RuntimeError, "reported 111 tags but returned 0"):
            module.inventory("example/astro", require_complete=True)

    @mock.patch.object(module, "delete_unreferenced")
    @mock.patch.object(module, "retag")
    @mock.patch.object(module, "resolve_digest", return_value="sha256:new")
    @mock.patch.object(module, "inventory")
    def test_two_slot_rotation_deletes_evicted_previous(
        self, inventory, _resolve, retag, delete_unreferenced
    ):
        inventory.return_value = [
            module.Tag("deps", "sha256:current", dt.datetime.now(dt.timezone.utc), 1),
            module.Tag("deps-prev", "sha256:evicted", dt.datetime.now(dt.timezone.utc), 1),
        ]
        module.rotate("example/astro", "deps", "deps-next", 2)
        self.assertEqual(
            retag.call_args_list,
            [
                mock.call("example/astro", "deps-prev", "sha256:current"),
                mock.call("example/astro", "deps", "sha256:new"),
            ],
        )
        delete_unreferenced.assert_called_once_with("example/astro", "sha256:evicted")

    @mock.patch.object(module, "delete_unreferenced")
    @mock.patch.object(module, "retag")
    @mock.patch.object(module, "resolve_digest", return_value="sha256:new")
    @mock.patch.object(module, "inventory")
    def test_candidate_reuses_oldest_of_five_slots(
        self, inventory, _resolve, retag, delete_unreferenced
    ):
        now = dt.datetime.now(dt.timezone.utc)
        inventory.return_value = [
            module.Tag(f"jetson-c{i}", f"sha256:{i}", now + dt.timedelta(minutes=i), 1)
            for i in range(1, 6)
        ]
        slot, digest = module.candidate_slot("example/astro", "jetson-next", 5)
        self.assertEqual((slot, digest), ("jetson-c1", "sha256:new"))
        retag.assert_called_once_with("example/astro", "jetson-c1", "sha256:new")
        delete_unreferenced.assert_called_once_with("example/astro", "sha256:1")


if __name__ == "__main__":
    unittest.main()
